"""Simulated exchange for dry runs: live prices, nothing sent.

A dry run that fills at the price it decided on flatters the strategy. Each
simulated trade here is instead settled the way the chain would settle it:

  - The transaction lands `latency` seconds after it is sent, drawn uniformly
    from the configured range, and is priced against the curve as it is *then*.
  - A pump.fun fill walks the constant-product curve over the virtual reserves,
    so the trade's own size moves the price it gets.
  - The slippage guard the real instruction carries is applied: a buy whose
    cost exceeds its ceiling, or a sell whose proceeds fall under its floor,
    reverts. A revert still pays the network fee and changes nothing else, and
    the trader handles it exactly as it handles a live revert.
  - `drop_rate` of transactions never land at all; those cost nothing.
  - Every landed transaction pays the network fee, the curve fee on each side,
    and the first buy of a coin pays the token account's rent, refunded when a
    sell empties the account.

The resulting balance is saved to `<trades_dir>/dryrun_summary.json`.
"""

import asyncio
import json
import math
import random
from datetime import datetime
from pathlib import Path

from solders.pubkey import Pubkey

from core.pubkeys import (
    TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
    TOKEN_DECIMALS,
    WSOL_MINT,
    quote_units_per_token,
)
from interfaces.core import TradeFailureReason
from utils.logger import get_logger

logger = get_logger(__name__)

# pump.fun's protocol plus creator fee on each side, which the curve price does
# not include.
DRY_RUN_FEE = 0.0125
# Base fee plus the default 200k microlamport priority fee on ~100k CU.
TX_COST_SOL = 0.000025
# Below this many raw token units an account counts as emptied.
DUST = 10


class DryRunExchange:
    """Settles simulated trades against live curve state and keeps the balance.

    Amounts are SOL; only SOL-paired coins are accounted for.
    """

    def __init__(
        self,
        balance: float,
        trades_dir: Path,
        latency: tuple[float, float] = (1.0, 2.5),
        drop_rate: float = 0.05,
        seed: int | None = None,
    ) -> None:
        """Start a simulated wallet.

        Args:
            balance: Starting SOL
            latency: Seconds between sending a transaction and it landing,
                (min, max)
            drop_rate: Share of transactions that never land, 0..1
            seed: For a reproducible latency and drop sequence
        """
        self.start_balance = balance
        self.balance = balance
        self.latency = latency
        self.drop_rate = drop_rate
        self.rng = random.Random(seed)  # noqa: S311 - simulation, not crypto
        self.holdings: dict[str, int] = {}  # mint -> raw token units
        self.stats = {
            "buys": 0,
            "sells": 0,
            "reverted": 0,
            "dropped": 0,
            "fees_sol": 0.0,
        }
        self.path = trades_dir / "dryrun_summary.json"
        # Raw units per SOL; only SOL-paired coins are simulated.
        self.unit = quote_units_per_token(WSOL_MINT)
        self.rent = TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE / self.unit

    async def _land(self) -> bool:
        """Wait out the landing delay; False when the transaction is dropped."""
        await asyncio.sleep(self.rng.uniform(*self.latency))
        if self.rng.random() < self.drop_rate:
            self.stats["dropped"] += 1
            return False
        return True

    def _pay_network_fee(self) -> None:
        self.balance -= TX_COST_SOL
        self.stats["fees_sol"] += TX_COST_SOL

    async def buy(  # noqa: PLR0913 - one argument per instruction field
        self,
        mint: Pubkey,
        *,
        pool_address: Pubkey,
        curve_manager,  # noqa: ANN001 - platform CurveManager
        token_amount_raw: int,
        max_cost_raw: int,
        exact_in: bool,
        quote_amount: float,
    ) -> tuple[bool, float, float, str | None]:
        """Simulate a buy landing.

        Args:
            token_amount_raw: Tokens asked for (exact-out) or the floor (exact-in)
            max_cost_raw: Lamport ceiling the instruction allows (exact-out)
            exact_in: The program spends exactly `quote_amount` (LaunchLab)
                rather than buying an exact token count (pump.fun)
            quote_amount: SOL to spend when `exact_in`

        Returns:
            (success, tokens, price per token excluding fees, failure reason)
        """
        if not await self._land():
            return False, 0.0, 0.0, "dropped: transaction never landed"
        self._pay_network_fee()
        state = await curve_manager.get_pool_state(pool_address)
        if state.get("graduated"):
            self.stats["reverted"] += 1
            return False, 0.0, 0.0, "reverted: curve graduated before the buy landed"
        vtr = state["virtual_token_reserves"]
        vsr = state["virtual_sol_reserves"]

        if exact_in:
            spend = int(quote_amount * self.unit)
            net = spend * (1 - DRY_RUN_FEE)
            tokens = int(net * vtr / (vsr + net))
            if tokens < token_amount_raw:
                self.stats["reverted"] += 1
                return False, 0.0, 0.0, "reverted: fewer tokens than the slippage floor"
            cost = spend
        else:
            tokens = token_amount_raw
            if tokens >= vtr or tokens > state.get("real_token_reserves", vtr):
                self.stats["reverted"] += 1
                return False, 0.0, 0.0, "reverted: not enough tokens left on the curve"
            curve_cost = math.ceil(tokens * vsr / (vtr - tokens))
            cost = math.ceil(curve_cost * (1 + DRY_RUN_FEE))
            if cost > max_cost_raw:
                self.stats["reverted"] += 1
                return (
                    False,
                    0.0,
                    0.0,
                    (
                        f"reverted: cost {cost / self.unit:.6f} SOL above the "
                        f"slippage ceiling {max_cost_raw / self.unit:.6f} SOL"
                    ),
                )

        key = str(mint)
        first_buy = self.holdings.get(key, 0) <= DUST
        self.balance -= cost / self.unit + (self.rent if first_buy else 0.0)
        self.holdings[key] = self.holdings.get(key, 0) + tokens
        self.stats["buys"] += 1
        whole_tokens = tokens / 10**TOKEN_DECIMALS
        price = cost / (1 + DRY_RUN_FEE) / self.unit / whole_tokens
        self._save()
        return True, whole_tokens, price, None

    async def sell(
        self,
        mint: Pubkey,
        *,
        read_state,  # noqa: ANN001 - async () -> (vtr, vsr) or price
        token_amount_raw: int,
        min_out_raw: int,
        fee_fraction: float,
    ) -> tuple[bool, float, str | None]:
        """Simulate a sell landing.

        Args:
            read_state: Coroutine function returning ("curve", vtr, vsr) for a
                coin on its bonding curve or ("price", price_per_token) once it
                trades on an AMM pool, read after the landing delay
            min_out_raw: Lamport floor the instruction carries
            fee_fraction: Fee the venue takes from the proceeds; DRY_RUN_FEE
                is used when this is 0 on the bonding curve

        Returns:
            (success, effective price per token after fees, failure reason)
        """
        if not await self._land():
            return False, 0.0, "dropped: transaction never landed"
        self._pay_network_fee()
        state = await read_state()
        if state[0] == "curve":
            _, vtr, vsr = state
            gross = token_amount_raw * vsr / (vtr + token_amount_raw)
            out = gross * (1 - (fee_fraction or DRY_RUN_FEE))
        else:
            gross = token_amount_raw / 10**TOKEN_DECIMALS * state[1] * self.unit
            out = gross * (1 - (fee_fraction or DRY_RUN_FEE))
        if out < min_out_raw:
            self.stats["reverted"] += 1
            return (
                False,
                0.0,
                (
                    f"reverted: proceeds {out / self.unit:.6f} SOL under the "
                    f"slippage floor {min_out_raw / self.unit:.6f} SOL"
                ),
            )
        key = str(mint)
        self.balance += out / self.unit
        left = self.holdings.get(key, 0) - token_amount_raw
        if left <= DUST:
            # The account is emptied and closed; its rent comes back.
            self.balance += self.rent
            self.holdings.pop(key, None)
        else:
            self.holdings[key] = left
        self.stats["sells"] += 1
        self._save()
        return True, out / self.unit / (token_amount_raw / 10**TOKEN_DECIMALS), None

    def _save(self) -> None:
        open_accounts = sum(1 for v in self.holdings.values() if v > DUST)
        summary = {
            "updated": datetime.utcnow().isoformat(),
            "start_balance_sol": self.start_balance,
            "balance_sol": self.balance,
            "open_accounts": open_accounts,
            "rent_locked_sol": open_accounts * self.rent,
            **self.stats,
            "latency_s": list(self.latency),
            "drop_rate": self.drop_rate,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(summary, indent=2))
        except OSError:
            logger.exception("Failed to save dry-run summary")
        logger.info(
            f"[DRY RUN] balance {self.balance:.6f} SOL, {open_accounts} open "
            f"account(s), {self.stats['reverted']} reverted, "
            f"{self.stats['dropped']} dropped"
        )
        if self.balance < 0:
            logger.warning(
                f"[DRY RUN] balance is negative: a real wallet of "
                f"{self.start_balance} SOL would have run out"
            )


def failure_reason(message: str) -> TradeFailureReason:
    """Map a simulated failure to what the trader's retry logic expects."""
    if message.startswith("dropped"):
        # Never reached the chain, so resending is safe.
        return TradeFailureReason.SUBMIT_FAILED
    return TradeFailureReason.REVERTED
