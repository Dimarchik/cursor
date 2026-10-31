"""Verify open positions are priced by one batched read per interval.

With dozens of positions waiting for their target, one account read per
position per interval exhausts a free RPC endpoint. The trader now reads every
open curve in a single getMultipleAccounts call and monitors read that cache.

Offline machine checks, no network and no funds moved:

  1. Three open positions cost one RPC call per polling round.
  2. A monitor reading a cached price makes no RPC call of its own.
  3. A graduated curve is left out of the cache, so its monitor reads the
     price itself (and goes on to the AMM pool).
  4. A stale cache entry is not used.

Usage:
    uv run tests/regression/verify_batched_prices.py
"""

import asyncio
import sys
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.position import Position  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

GRADUATED = b"graduated"
DIRECT_PRICE = 9.0


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


class StubClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_multiple_accounts(self, pubkeys: list[Pubkey]) -> list:
        self.calls += 1
        return [
            SimpleNamespace(data=bytes(pk)[:1] if i < 2 else GRADUATED)
            for i, pk in enumerate(pubkeys)
        ]


class StubCurveManager:
    def __init__(self) -> None:
        self.direct_reads = 0

    def decode_pool_state(self, data: bytes) -> dict:
        if data == GRADUATED:
            return {"graduated": True, "price_per_token": None}
        return {"graduated": False, "price_per_token": 1.0 + data[0] / 1000}

    async def calculate_price(self, _pool: Pubkey) -> float:
        self.direct_reads += 1
        return DIRECT_PRICE


async def main() -> None:
    trader = object.__new__(UniversalTrader)
    client, curve = StubClient(), StubCurveManager()
    trader.solana_client = client
    trader.platform_implementations = SimpleNamespace(
        curve_manager=curve, address_provider=None
    )
    trader.price_check_interval = 0.05
    trader._graduated_mints = set()  # noqa: SLF001
    trader._price_cache = {}  # noqa: SLF001
    trader._open_positions = {}  # noqa: SLF001
    infos = []
    for _ in range(3):
        mint = Pubkey.new_unique()
        info = TokenInfo(
            name="B",
            symbol="B",
            uri="",
            mint=mint,
            platform=Platform.PUMP_FUN,
            bonding_curve=Pubkey.new_unique(),
        )
        trader._open_positions[str(mint)] = (
            info,
            Position.create_from_buy_result(mint, "B", 1.0, 1.0),
        )  # noqa: SLF001
        infos.append(info)

    poller = asyncio.create_task(trader._poll_prices())  # noqa: SLF001
    await asyncio.sleep(0.12)  # three rounds at most
    poller.cancel()
    await asyncio.gather(poller, return_exceptions=True)
    rounds = client.calls

    print("\n1-3. Batched reads")
    cached = await trader._read_price(infos[0])  # noqa: SLF001
    graduated = await trader._read_price(infos[2])  # noqa: SLF001
    results = [
        _check(
            "one call per round for three positions",
            1 <= rounds <= 3,
            f"{rounds} call(s) in up to 3 rounds",
        ),
        _check(
            "cached price served without RPC",
            cached != DIRECT_PRICE and curve.direct_reads == 1,
            f"{cached}",
        ),
        _check(
            "graduated curve read directly", graduated == DIRECT_PRICE, f"{graduated}"
        ),
    ]

    print("\n4. Stale entries")
    pool = infos[1].bonding_curve
    trader._price_cache[pool] = (5.0, monotonic() - 10)  # noqa: SLF001
    stale = await trader._read_price(infos[1])  # noqa: SLF001
    results.append(_check("stale entry ignored", stale == DIRECT_PRICE, f"{stale}"))

    print(f"\n{'ALL PASSED' if all(results) else 'FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
