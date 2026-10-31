"""Verify the recover_and_hold exit: sell back the cost at the target, keep the rest.

At `recover_multiplier` x the entry price the strategy sells only enough to repay
the entry cost plus `recover_buffer`, records the remainder in
`trades/moonbags.jsonl` and stops monitoring. A held remainder must never reach
cleanup, which closes the account — and with `force_close_with_burn` burns what
is left in it.

Offline machine checks, no network and no funds moved. The real monitor loop
runs against a scripted price series and a stub seller:

  1. `Position.recover_quantity` sells `entry / price * (1 + buffer)` of the
     position, and all of it when that is more than the position.
  2. At the target the seller is asked for the recovery amount only, floored at
     the trigger price; the position closes as RECOVERED with the rest held.
  3. The held remainder is written to moonbags.jsonl and dropped from
     `traded_mints`, and post-sell cleanup is not run.
  4. A stop loss before the target sells the whole position and runs cleanup.
  5. `max_hold_time` with `recover_timeout_action: hold` sells nothing and
     records the position as held; with `sell_all` it sells everything.
  6. The shipped bots/bot-recover-and-hold.yaml passes config validation.
  7. In yolo mode the position is monitored in the background, so the caller
     returns at once; shutdown cancels the monitor and records the position as
     held instead of leaving it to cleanup.

Usage:
    uv run tests/regression/verify_recover_and_hold.py
"""

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from config_loader import validate_config  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position import ExitReason, Position  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

ENTRY_PRICE = 1.0e-6  # SOL per token
QUANTITY = 1_000_000.0  # tokens, so the entry cost is 1.0 SOL
MULTIPLIER = 2.0
BUFFER = 0.05
TOLERANCE = 1e-6  # tokens; float rounding in the fraction math


@dataclass
class StubCurveManager:
    """Serves a scripted price series; the last value repeats forever."""

    prices: list[float]
    calls: int = 0

    async def calculate_price(self, _pool_address: Pubkey) -> float:
        price = self.prices[min(self.calls, len(self.prices) - 1)]
        self.calls += 1
        return price


@dataclass
class StubSeller:
    """Records each sell and always succeeds."""

    sells: list[tuple[float, float]] = field(default_factory=list)

    async def execute(
        self, token_info: TokenInfo, token_amount: float, token_price: float
    ) -> TradeResult:
        self.sells.append((token_amount, token_price))
        return TradeResult(
            success=True,
            platform=token_info.platform,
            tx_signature="stub-signature",
            amount=token_amount,
            price=token_price,
        )


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _make_token_info() -> TokenInfo:
    return TokenInfo(
        name="VerifyRecover",
        symbol="VREC",
        uri="",
        mint=Pubkey.default(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.default(),
    )


def _make_position(
    stop_loss: float | None = None, max_hold_time: int | None = None
) -> Position:
    return Position.create_from_buy_result(
        mint=Pubkey.default(),
        symbol="VREC",
        entry_price=ENTRY_PRICE,
        quantity=QUANTITY,
        take_profit_percentage=MULTIPLIER - 1,
        stop_loss_percentage=stop_loss,
        max_hold_time=max_hold_time,
    )


async def _run(
    prices: list[float],
    position: Position,
    timeout_action: str = "sell_all",
) -> tuple[StubSeller, UniversalTrader, list[str], list[dict]]:
    """Drive the real monitor loop in a scratch directory.

    Returns:
        Seller stub, trader, mints cleanup was run for, moonbags.jsonl records
    """
    seller = StubSeller()
    trader = object.__new__(UniversalTrader)
    trader.price_check_interval = 0
    trader.max_exit_sell_attempts = 3
    trader.platform_implementations = SimpleNamespace(
        curve_manager=StubCurveManager(prices=list(prices)), address_provider=None
    )
    trader.seller = seller
    trader.exit_strategy = "recover_and_hold"
    trader.recover_buffer = BUFFER
    trader.recover_timeout_action = timeout_action
    token_info = _make_token_info()
    trader.traded_mints = {token_info.mint}
    trader._log_trade = lambda *_args, **_kwargs: None  # noqa: SLF001
    cleaned: list[str] = []

    async def cleanup(info: TokenInfo) -> None:
        cleaned.append(str(info.mint))

    trader._cleanup_after_exit = cleanup  # noqa: SLF001

    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as scratch:
        os.chdir(scratch)
        try:
            await asyncio.wait_for(
                trader._monitor_position_until_exit(token_info, position),  # noqa: SLF001
                timeout=10,
            )
            bags_file = Path("trades") / "moonbags.jsonl"
            bags = (
                [json.loads(line) for line in bags_file.read_text().splitlines()]
                if bags_file.exists()
                else []
            )
        finally:
            os.chdir(cwd)
    return seller, trader, cleaned, bags


def check_recover_quantity() -> bool:
    print("\n1. recover_quantity sells entry/price * (1 + buffer) of the position")
    position = _make_position()
    at_2x = position.recover_quantity(ENTRY_PRICE * 2, BUFFER)
    at_1x = position.recover_quantity(ENTRY_PRICE, BUFFER)
    expected = QUANTITY * 0.5 * (1 + BUFFER)
    return _check(
        "2x sells 52.5%", abs(at_2x - expected) < TOLERANCE, f"{at_2x / QUANTITY:.4f}"
    ) & _check("1x sells everything", at_1x == QUANTITY, f"{at_1x / QUANTITY:.4f}")


async def check_partial_sell_at_target() -> bool:
    print("\n2-3. At the target only the cost is sold back and the rest is held")
    trigger = ENTRY_PRICE * 2.2
    position = _make_position()
    seller, trader, cleaned, bags = await _run(
        [ENTRY_PRICE, ENTRY_PRICE * 1.5, trigger], position
    )
    sold, floor = seller.sells[0] if seller.sells else (None, None)
    expected = QUANTITY * (ENTRY_PRICE / trigger) * (1 + BUFFER)
    ok = _check("one sell", len(seller.sells) == 1, f"{len(seller.sells)} sell(s)")
    ok &= _check(
        "sold the recovery amount",
        sold is not None and abs(sold - expected) < TOLERANCE,
        f"{sold} (expected {expected:.6f})",
    )
    ok &= _check("floored at the trigger price", floor == trigger, f"{floor}")
    ok &= _check(
        "proceeds repay the cost",
        sold is not None and sold * trigger >= ENTRY_PRICE * QUANTITY,
        f"{(sold or 0) * trigger:.6f} >= {ENTRY_PRICE * QUANTITY:.6f} SOL",
    )
    ok &= _check(
        "position closed as recovered",
        not position.is_active and position.exit_reason is ExitReason.RECOVERED,
        f"{position.exit_reason}",
    )
    ok &= _check(
        "remainder held",
        abs(position.quantity - (QUANTITY - expected)) < TOLERANCE,
        f"{position.quantity:.2f} tokens",
    )
    ok &= _check(
        "recorded in moonbags.jsonl",
        len(bags) == 1
        and bags[0]["reason"] == "recovered"
        and abs(bags[0]["quantity_held"] - position.quantity) < TOLERANCE,
        f"{bags}",
    )
    ok &= _check(
        "dropped from traded_mints",
        not trader.traded_mints,
        f"{trader.traded_mints}",
    )
    ok &= _check("cleanup not run", not cleaned, f"{cleaned}")
    return ok


async def check_stop_loss_sells_all() -> bool:
    print("\n4. A stop loss before the target sells everything and cleans up")
    position = _make_position(stop_loss=0.5)
    seller, _, cleaned, bags = await _run([ENTRY_PRICE * 0.4], position)
    ok = _check(
        "sold whole position",
        [s[0] for s in seller.sells] == [QUANTITY],
        f"{seller.sells}",
    )
    ok &= _check("cleanup run", len(cleaned) == 1, f"{cleaned}")
    ok &= _check("nothing held", not bags, f"{bags}")
    return ok


async def check_timeout_actions() -> bool:
    print("\n5. max_hold_time: 'hold' keeps everything, 'sell_all' sells it")
    position = _make_position(max_hold_time=60)
    position.entry_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=120
    )
    seller, _, cleaned, bags = await _run([ENTRY_PRICE], position, "hold")
    ok = _check("hold: no sell", not seller.sells, f"{seller.sells}")
    ok &= _check(
        "hold: recorded",
        len(bags) == 1 and bags[0]["quantity_held"] == QUANTITY,
        f"{bags}",
    )
    ok &= _check("hold: cleanup not run", not cleaned, f"{cleaned}")

    position = _make_position(max_hold_time=60)
    position.entry_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=120
    )
    seller, _, cleaned, bags = await _run([ENTRY_PRICE], position, "sell_all")
    ok &= _check(
        "sell_all: whole position sold",
        [s[0] for s in seller.sells] == [QUANTITY],
        f"{seller.sells}",
    )
    ok &= _check("sell_all: nothing held", not bags, f"{bags}")
    return ok


def check_shipped_config() -> bool:
    print("\n6. bots/bot-recover-and-hold.yaml passes validation")
    config = yaml.safe_load(
        (PROJECT_ROOT / "bots" / "bot-recover-and-hold.yaml").read_text()
    )
    try:
        validate_config(config)
    except ValueError as e:
        return _check("validate_config", False, str(e))  # noqa: FBT003
    bad = {**config, "trade": {**config["trade"], "recover_timeout_action": "x"}}
    try:
        validate_config(bad)
        rejected = False
    except ValueError:
        rejected = True
    return _check("validate_config", True, "accepted") & _check(  # noqa: FBT003
        "unknown recover_timeout_action rejected", rejected, f"{rejected}"
    )


async def check_background_monitor_and_shutdown() -> bool:
    print("\n7. yolo: monitoring runs in the background; shutdown keeps the coin")
    seller = StubSeller()
    trader = object.__new__(UniversalTrader)
    trader.price_check_interval = 0.01
    trader.max_exit_sell_attempts = 3
    trader.platform_implementations = SimpleNamespace(
        curve_manager=StubCurveManager(prices=[ENTRY_PRICE]), address_provider=None
    )
    trader.seller = seller
    trader.exit_strategy = "recover_and_hold"
    trader.recover_multiplier = MULTIPLIER
    trader.recover_buffer = BUFFER
    trader.recover_timeout_action = "sell_all"
    trader.stop_loss_percentage = None
    trader.max_hold_time = None
    trader.yolo_mode = True
    trader._open_positions = {}  # noqa: SLF001
    trader._position_tasks = set()  # noqa: SLF001
    token_info = _make_token_info()
    trader.traded_mints = {token_info.mint}

    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as scratch:
        os.chdir(scratch)
        try:
            buy = TradeResult(
                success=True,
                platform=token_info.platform,
                amount=QUANTITY,
                price=ENTRY_PRICE,
            )
            await asyncio.wait_for(
                trader._handle_recover_and_hold_exit(token_info, buy),  # noqa: SLF001
                timeout=1,
            )
            await asyncio.sleep(0.05)
            running = len(trader._position_tasks)  # noqa: SLF001
            await trader._stop_position_monitors()  # noqa: SLF001
            bags_file = Path("trades") / "moonbags.jsonl"
            bags = [json.loads(x) for x in bags_file.read_text().splitlines()]
        finally:
            os.chdir(cwd)

    ok = _check("caller returned, monitor running", running == 1, f"{running} task(s)")
    ok &= _check("no sell below target", not seller.sells, f"{seller.sells}")
    ok &= _check(
        "shutdown recorded the position",
        len(bags) == 1 and bags[0]["reason"] == "shutdown",
        f"{[b['reason'] for b in bags]}",
    )
    ok &= _check(
        "dropped from traded_mints", not trader.traded_mints, f"{trader.traded_mints}"
    )
    return ok


async def main() -> None:
    results = [
        check_recover_quantity(),
        await check_partial_sell_at_target(),
        await check_stop_loss_sells_all(),
        await check_timeout_actions(),
        check_shipped_config(),
        await check_background_monitor_and_shutdown(),
    ]
    print(f"\n{'ALL PASSED' if all(results) else 'FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
