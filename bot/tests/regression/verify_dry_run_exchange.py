"""Verify the dry-run exchange settles trades the way the chain would.

A dry run is only worth trusting if it is no kinder than mainnet. The simulated
exchange prices each trade on the curve as it stands when the transaction
lands, applies the instruction's slippage guard, and charges fees and rent.

Offline machine checks against a stub curve, no network and no funds moved:

  1. An exact-out pump.fun buy costs the constant-product amount for its token
     count plus the fee, and the balance also pays the network fee and rent.
  2. If the price runs past the slippage ceiling before the buy lands, the buy
     reverts: the network fee is paid and nothing else changes.
  3. A partial sell gets the curve's proceeds less the fee and keeps the
     account open; selling the rest closes it and refunds the rent.
  4. A sell whose proceeds fall under its floor reverts, and is reported with
     a reason the trader retries.
  5. A dropped transaction costs nothing and is reported as never submitted.
  6. The fill is priced off the state read after the landing delay, not before.

Usage:
    uv run tests/regression/verify_dry_run_exchange.py
"""

import asyncio
import math
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE  # noqa: E402
from interfaces.core import TradeFailureReason  # noqa: E402
from trading.dry_run import (  # noqa: E402
    DRY_RUN_FEE,
    TX_COST_SOL,
    DryRunExchange,
    failure_reason,
)

LAMPORTS = 1_000_000_000
RENT = TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE / LAMPORTS
# pump.fun's opening virtual reserves.
VTR = 1_073_000_000 * 10**6
VSR = 30 * LAMPORTS
MINT = Pubkey.new_unique()
TOLERANCE = 1e-9
PRICE_TOLERANCE = 1e-15  # SOL per token
ROUNDING = 1e-4  # relative; lamport rounding on a small trade


class StubCurve:
    """Serves reserves; `after_reads` swaps them once read that many times."""

    def __init__(self, vtr: int, vsr: int) -> None:
        self.vtr, self.vsr = vtr, vsr
        self.reads = 0

    async def get_pool_state(self, _pool: Pubkey) -> dict:
        self.reads += 1
        return {
            "virtual_token_reserves": self.vtr,
            "virtual_sol_reserves": self.vsr,
            "real_token_reserves": self.vtr,
            "graduated": False,
        }


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


async def _buy(
    ex: DryRunExchange, curve: "StubCurve", tokens: int, ceiling: int
) -> tuple:
    """Exact-out pump.fun buy of `tokens` raw units with a lamport `ceiling`."""
    return await ex.buy(
        MINT,
        pool_address=MINT,
        curve_manager=curve,
        token_amount_raw=tokens,
        max_cost_raw=ceiling,
        exact_in=False,
        quote_amount=0.0,
    )


def _exchange(scratch: str, drop_rate: float = 0.0) -> DryRunExchange:
    return DryRunExchange(
        1.0, Path(scratch), latency=(0.0, 0.0), drop_rate=drop_rate, seed=1
    )


async def check_buy(scratch: str) -> bool:
    print("\n1. An exact-out buy pays the curve cost, fee, network fee and rent")
    ex = _exchange(scratch)
    tokens = 100_000 * 10**6
    curve_cost = math.ceil(tokens * VSR / (VTR - tokens))
    cost = math.ceil(curve_cost * (1 + DRY_RUN_FEE))
    ok, got, price, _ = await _buy(ex, StubCurve(VTR, VSR), tokens, cost + 1)
    expected_balance = 1.0 - cost / LAMPORTS - TX_COST_SOL - RENT
    return (
        _check("filled", ok and abs(got - 100_000) < TOLERANCE, f"{ok}, {got} tokens")
        & _check(
            "balance",
            abs(ex.balance - expected_balance) < TOLERANCE,
            f"{ex.balance:.9f} (expected {expected_balance:.9f})",
        )
        & _check(
            "price excludes fee",
            abs(price - curve_cost / LAMPORTS / 100_000) < PRICE_TOLERANCE,
            f"{price:.3e}",
        )
    )


async def check_buy_reverts(scratch: str) -> bool:
    print("\n2. A buy whose cost runs past the slippage ceiling reverts")
    ex = _exchange(scratch)
    tokens = 100_000 * 10**6
    ceiling = math.ceil(
        math.ceil(tokens * VSR / (VTR - tokens)) * (1 + DRY_RUN_FEE) * 1.3
    )
    # By landing time buyers have pushed the price up ~2x.
    curve = StubCurve(int(VTR / 1.42), int(VSR * 1.42))
    ok, _, _, reason = await _buy(ex, curve, tokens, ceiling)
    return (
        _check("reverted", not ok and reason.startswith("reverted"), f"{reason}")
        & _check(
            "only the network fee paid",
            abs(ex.balance - (1.0 - TX_COST_SOL)) < TOLERANCE,
            f"{ex.balance:.9f}",
        )
        & _check(
            "trader retries a revert",
            failure_reason(reason) is TradeFailureReason.REVERTED,
            f"{failure_reason(reason)}",
        )
    )


async def check_sells(scratch: str) -> bool:
    print("\n3-4. Sells: partial keeps the account, full refunds rent, floor reverts")
    ex = _exchange(scratch)
    curve = StubCurve(VTR, VSR)
    tokens = 100_000 * 10**6
    await _buy(ex, curve, tokens, 10 * LAMPORTS)
    after_buy = ex.balance

    async def state() -> tuple:
        return ("curve", curve.vtr, curve.vsr)

    half = tokens // 2
    proceeds = half * VSR / (VTR + half) * (1 - DRY_RUN_FEE) / LAMPORTS
    ok, _, _ = await ex.sell(
        MINT, read_state=state, token_amount_raw=half, min_out_raw=1, fee_fraction=0.0
    )
    partial_ok = _check(
        "partial sell: proceeds, no rent back",
        ok and abs(ex.balance - (after_buy + proceeds - TX_COST_SOL)) < TOLERANCE,
        f"{ex.balance - after_buy:+.9f} SOL",
    )
    before = ex.balance
    ok, _, reason = await ex.sell(
        MINT,
        read_state=state,
        token_amount_raw=tokens - half,
        min_out_raw=10 * LAMPORTS,
        fee_fraction=0.0,
    )
    floor_ok = _check(
        "sell under its floor reverts",
        not ok
        and reason.startswith("reverted")
        and abs(ex.balance - (before - TX_COST_SOL)) < TOLERANCE,
        f"{reason}",
    )
    before = ex.balance
    rest = tokens - half
    proceeds = rest * VSR / (VTR + rest) * (1 - DRY_RUN_FEE) / LAMPORTS
    ok, _, _ = await ex.sell(
        MINT, read_state=state, token_amount_raw=rest, min_out_raw=1, fee_fraction=0.0
    )
    full_ok = _check(
        "full sell refunds rent",
        ok and abs(ex.balance - (before + proceeds - TX_COST_SOL + RENT)) < TOLERANCE,
        f"{ex.balance - before:+.9f} SOL",
    )
    return partial_ok & floor_ok & full_ok


async def check_dropped(scratch: str) -> bool:
    print("\n5. A dropped transaction costs nothing and may be resent")
    ex = _exchange(scratch, drop_rate=1.0)
    ok, _, _, reason = await _buy(ex, StubCurve(VTR, VSR), 10**6, LAMPORTS)
    return _check(
        "dropped, free, resendable",
        not ok
        and ex.balance == 1.0
        and failure_reason(reason) is TradeFailureReason.SUBMIT_FAILED,
        f"{reason}, balance {ex.balance}",
    )


async def check_priced_after_delay(scratch: str) -> bool:
    print("\n6. The fill uses the state read after the landing delay")
    ex = DryRunExchange(1.0, Path(scratch), latency=(0.2, 0.2), drop_rate=0.0)
    curve = StubCurve(VTR, VSR)

    async def move_price() -> None:
        await asyncio.sleep(0.1)
        curve.vtr, curve.vsr = int(VTR / 1.2), int(VSR * 1.2)

    tokens = 10**6 * 1000
    mover = asyncio.create_task(move_price())
    ok, _, price, _ = await _buy(ex, curve, tokens, 10 * LAMPORTS)
    await mover
    moved_price = math.ceil(tokens * curve.vsr / (curve.vtr - tokens)) / LAMPORTS / 1000
    sent_price = VSR / VTR * 10**6 / LAMPORTS
    # Lamport rounding of the cost is ~1e-5 of a trade this small.
    return _check(
        "priced at landing, not at sending",
        ok
        and abs(price - moved_price) / moved_price < ROUNDING
        and price > 1.3 * sent_price,
        f"{price:.4e} (landing {moved_price:.4e}, sending {sent_price:.4e})",
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        results = [
            await check_buy(scratch),
            await check_buy_reverts(scratch),
            await check_sells(scratch),
            await check_dropped(scratch),
            await check_priced_after_delay(scratch),
        ]
    print(f"\n{'ALL PASSED' if all(results) else 'FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
