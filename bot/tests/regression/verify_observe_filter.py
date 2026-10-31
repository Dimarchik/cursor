"""Verify the watch-before-buying filter buys only coins whose demand qualifies.

With `filters.observe_seconds` set, a new coin is watched on its own task:
its curve is read, read again after the watch, and the coin is bought only if
enough SOL came in and the price moved within the configured ratios. The
queue does not wait for the watch.

Offline machine checks, no network and no funds moved:

  1. A coin with too little SOL coming in is not bought.
  2. A coin that already ran past the maximum price ratio is not bought.
  3. A coin that qualifies is bought.
  4. `_handle_token` returns before the watch ends, so other coins are not
     held up.

Usage:
    uv run tests/regression/verify_observe_filter.py
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

LAMPORTS = 1_000_000_000
WATCH = 0.05


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


class Curve:
    """First read returns `start`, every later one `end`: (SOL in curve, price)."""

    def __init__(self, start: tuple[float, float], end: tuple[float, float]) -> None:
        self.states = [start, end]
        self.reads = 0

    async def get_pool_state(self, _pool: Pubkey) -> dict:
        sol, price = self.states[min(self.reads, 1)]
        self.reads += 1
        return {
            "real_sol_reserves": int(sol * LAMPORTS),
            "price_per_token": price,
            "graduated": False,
        }


class Buyer:
    def __init__(self) -> None:
        self.bought: list[str] = []

    async def execute(self, token: TokenInfo) -> TradeResult:
        self.bought.append(token.symbol)
        return TradeResult(success=False, platform=token.platform, error_message="stub")


def _trader(curve: Curve, buyer: Buyer) -> UniversalTrader:
    t = object.__new__(UniversalTrader)
    t.platform = Platform.PUMP_FUN
    t.platform_implementations = SimpleNamespace(
        curve_manager=curve, address_provider=None
    )
    t.buyer = buyer
    t.mayhem_only = False
    t.allowed_quote_mints = None
    t.quote_amounts = {WSOL_MINT: 0.005}
    t.max_transfer_fee_bps = None
    t.exit_strategy = "recover_and_hold"
    t.max_open_positions = 50
    t._open_positions = {}  # noqa: SLF001
    t._watching = {}  # noqa: SLF001
    t._observed = {}  # noqa: SLF001
    t.observe_seconds = WATCH
    t.observe_min_inflow_sol = 0.5
    t.observe_min_price_ratio = 1.0
    t.observe_max_price_ratio = 3.0
    t.yolo_mode = False

    async def failed(*_a: object) -> None:
        return None

    t._handle_failed_buy = failed  # noqa: SLF001
    return t


def _coin(symbol: str) -> TokenInfo:
    return TokenInfo(
        name=symbol,
        symbol=symbol,
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
        quote_mint=WSOL_MINT,
    )


async def _run(
    start: tuple[float, float], end: tuple[float, float]
) -> tuple[list[str], float]:
    buyer = Buyer()
    trader = _trader(Curve(start, end), buyer)
    t0 = time.monotonic()
    await trader._handle_token(_coin("C"))  # noqa: SLF001
    returned_after = time.monotonic() - t0
    await asyncio.gather(*list(trader._watching))  # noqa: SLF001
    return buyer.bought, returned_after


async def main() -> None:
    weak, _ = await _run((0.1, 1.0), (0.3, 1.2))
    ran, _ = await _run((0.1, 1.0), (5.0, 4.0))
    good, returned_after = await _run((0.1, 1.0), (1.5, 1.6))
    results = [
        _check("too little SOL in: not bought", weak == [], f"{weak}"),
        _check("already ran past max ratio: not bought", ran == [], f"{ran}"),
        _check("qualifying coin bought", good == ["C"], f"{good}"),
        _check(
            "queue not held up by the watch",
            returned_after < WATCH,
            f"{returned_after:.3f}s",
        ),
    ]
    print(f"\n{'ALL PASSED' if all(results) else 'FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
