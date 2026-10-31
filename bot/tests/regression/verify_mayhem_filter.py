"""Verify `filters.mayhem_only` keeps the bot to Mayhem mode coins.

In the last day's sample, coins launched in pump.fun's Mayhem mode doubled
after a one-minute-late entry 26% of the time and the rest 3%, so the shipped
config buys only Mayhem coins. The flag comes from the create event or
instruction, so the filter costs no RPC call.

Offline machine checks, no network and no funds moved:

  1. With the filter on, a non-Mayhem coin is skipped with a reason.
  2. With the filter on, a Mayhem coin passes.
  3. With the filter off, both pass.

Usage:
    uv run tests/regression/verify_mayhem_filter.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _trader(*, mayhem_only: bool) -> UniversalTrader:
    trader = object.__new__(UniversalTrader)
    trader.mayhem_only = mayhem_only
    trader.allowed_quote_mints = None
    trader.quote_amounts = {WSOL_MINT: 0.005}
    trader.max_transfer_fee_bps = None
    return trader


def _coin(*, mayhem: bool) -> TokenInfo:
    return TokenInfo(
        name="M",
        symbol="M",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        quote_mint=WSOL_MINT,
        is_mayhem_mode=mayhem,
    )


def main() -> None:
    on, off = _trader(mayhem_only=True), _trader(mayhem_only=False)
    plain, mayhem = _coin(mayhem=False), _coin(mayhem=True)
    reason = on._skip_reason(plain)  # noqa: SLF001
    results = [
        _check("non-Mayhem skipped", bool(reason) and "Mayhem" in reason, f"{reason}"),
        _check("Mayhem passes", on._skip_reason(mayhem) is None, "traded"),  # noqa: SLF001
        _check(
            "filter off: both pass",
            off._skip_reason(plain) is None and off._skip_reason(mayhem) is None,  # noqa: SLF001
            "traded",
        ),
    ]
    print(f"\n{'ALL PASSED' if all(results) else 'FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
