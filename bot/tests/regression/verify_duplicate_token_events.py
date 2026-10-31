"""Verify a coin reported twice by the listener is bought once.

The logs listener can deliver the same creation twice, and both copies can sit
in the token queue before either is processed. Found in a dry run: each copy was
queued, and only the second copy having aged past `max_token_age` kept it from
being bought again.

Offline machine checks, no network and no funds moved:

  1. Two queued copies of one coin reach `_handle_token` once.
  2. The repeated event does not reset the coin's discovery time, so its age
     is measured from the first report.

Usage:
    uv run tests/regression/verify_duplicate_token_events.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def main() -> None:
    trader = object.__new__(UniversalTrader)
    trader.token_queue = asyncio.Queue()
    trader.processed_tokens = set()
    trader.token_timestamps = {}
    trader.max_token_age = 60
    handled: list[str] = []

    async def handle(token: TokenInfo) -> None:
        handled.append(str(token.mint))

    trader._handle_token = handle  # noqa: SLF001
    token = TokenInfo(
        name="Dup",
        symbol="DUP",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
    )

    await trader._queue_token(token)  # noqa: SLF001
    first_seen = trader.token_timestamps[str(token.mint)]
    await asyncio.sleep(0.01)
    await trader._queue_token(token)  # noqa: SLF001
    processor = asyncio.create_task(trader._process_token_queue())  # noqa: SLF001
    await asyncio.wait_for(trader.token_queue.join(), timeout=5)
    processor.cancel()
    await asyncio.gather(processor, return_exceptions=True)

    print("\n1-2. A creation reported twice")
    results = [
        _check("handled once", handled == [str(token.mint)], f"{len(handled)} time(s)"),
        _check(
            "discovery time kept",
            trader.token_timestamps[str(token.mint)] == first_seen,
            "first report",
        ),
    ]
    print(f"\n{'ALL PASSED' if all(results) else 'FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
