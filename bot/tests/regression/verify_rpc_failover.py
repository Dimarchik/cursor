"""Verify the RPC client fails over between endpoints quickly.

Free endpoints refuse with 429 under load. Before this fix the reads that go
through solana-py's `AsyncClient` — the curve reads that price every open
position — never reported a failure to the endpoint rotator, so a rate-limited
endpoint stayed active through a whole dry run; and `post_rpc` sat out up to
ten 429 back-offs on one endpoint while others were idle.

Offline machine checks, no network and no funds moved:

  1. The rotator moves on at once for an `immediate` failure and after two
     ordinary ones.
  2. `post_rpc` answered 429 by one endpoint retries on the next without
     waiting.
  3. A solana-py read that fails with a wrapped 429 rotates the client, and
     the next `AsyncClient` is built for the new endpoint.
  4. A failed read whose error is not a rate limit counts toward the streak
     instead of rotating at once.

Usage:
    uv run tests/regression/verify_rpc_failover.py
"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.client import SolanaClient, _is_rate_limited  # noqa: E402
from core.endpoint_rotator import EndpointRotator  # noqa: E402

A, B, C = "https://a.example", "https://b.example", "https://c.example"
FAST = 0.5  # seconds; a 429 back-off waits at least 1s


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict:
        return {"result": "ok"}


class _Session:
    """Answers 429 from the endpoints in `limited`, 200 from the rest."""

    def __init__(self, limited: set[str]) -> None:
        self.limited = limited
        self.calls: list[str] = []

    def post(self, endpoint: str, json: dict) -> _Response:  # noqa: ARG002
        self.calls.append(endpoint)
        return _Response(429 if endpoint in self.limited else 200)


class _HTTPError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("HTTP error")
        self.response = type("R", (), {"status_code": status})()


def _client(endpoints: list[str]) -> SolanaClient:
    client = object.__new__(SolanaClient)
    client._rotator = EndpointRotator(endpoints)  # noqa: SLF001
    client.rpc_endpoint = endpoints[0]
    client._client = None  # noqa: SLF001

    class _Limiter:
        async def acquire(self) -> None:
            return None

    client._rate_limiter = _Limiter()  # noqa: SLF001
    return client


def check_rotator() -> bool:
    print("\n1. Rotation thresholds")
    r = EndpointRotator([A, B, C])
    immediate = r.mark_failure(immediate=True) and r.current == B
    r.mark_failure()
    after_one = r.current == B
    after_two = r.mark_failure() and r.current == C
    return (
        _check("immediate failure rotates", immediate, r.endpoints[1])
        & _check("one ordinary failure stays", after_one, "B")
        & _check("two ordinary failures rotate", after_two, r.current)
    )


async def check_post_rpc_429() -> bool:
    print("\n2. post_rpc retries a 429 on the next endpoint without waiting")
    client = _client([A, B])
    session = _Session(limited={A})

    async def get_session() -> _Session:
        return session

    client._get_session = get_session  # noqa: SLF001
    t = time.monotonic()
    out = await client.post_rpc({"method": "getSlot"})
    elapsed = time.monotonic() - t
    return _check(
        "answered by B, fast",
        out == {"result": "ok"} and session.calls == [A, B] and elapsed < FAST,
        f"{session.calls} in {elapsed:.2f}s",
    )


async def check_reported_reads() -> bool:
    print("\n3-4. solana-py reads report to the rotator")
    client = _client([A, B, C])

    async def rate_limited() -> None:
        try:
            raise _HTTPError(429)
        except _HTTPError as inner:
            raise RuntimeError(
                "<class 'HTTPStatusError'> raised in GetAccountInfo"
            ) from inner

    try:
        await client._reported(rate_limited())  # noqa: SLF001
    except RuntimeError:
        pass
    built = await client.get_client()
    ok = _check(
        "wrapped 429 rotates at once",
        client._rotator.current == B and client.rpc_endpoint == B,  # noqa: SLF001
        client._rotator.current,  # noqa: SLF001
    )
    ok &= _check(
        "AsyncClient rebuilt for B", built is not None and client.rpc_endpoint == B, B
    )

    async def timeout() -> None:
        raise TimeoutError

    try:
        await client._reported(timeout())  # noqa: SLF001
    except TimeoutError:
        pass
    ok &= _check(
        "one timeout does not rotate",
        client._rotator.current == B,  # noqa: SLF001
        client._rotator.current,  # noqa: SLF001
    )
    ok &= _check(
        "a plain error is not a rate limit",
        _is_rate_limited(RuntimeError("connection reset")) is False,
        "not rate limited",
    )
    await built.close()
    return ok


async def main() -> None:
    results = [
        check_rotator(),
        await check_post_rpc_429(),
        await check_reported_reads(),
    ]
    print(f"\n{'ALL PASSED' if all(results) else 'FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
