"""How long pump.fun coins take to double, how fast the rest die, and which exit timing pays.

Reuses the one-minute candles cached by `tools/backtest_24h.py`. Entry is the
close of the first candle (about a minute after creation); a coin with no
candles is one nobody traded. Reports:

  1. minutes from entry until the price first reaches 2x, for coins that do;
  2. minutes until the price first falls under 0.7x and 0.5x of entry;
  3. the median and quartiles of price / entry at fixed minutes;
  4. P&L of recover_and_hold for each timeout and stop loss, selling whole at
     the last close when the timeout fires and at the stop price on a stop.

Minute candles see neither the first seconds nor sub-minute spikes, and a sell
is assumed to fill at its trigger, so the absolute P&L is optimistic; compare
the rows with each other.

Usage:
    uv run tools/backtest_timing.py
"""

import json
import statistics
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "analysis_data"
WSOL = "So11111111111111111111111111111111111111112"
FEE = 0.0125
BUY = 0.005
MULTIPLIER = 2.0
BUFFER = 0.05


def load() -> tuple[list[dict], dict[str, float], float]:
    coins = [json.loads(p.read_text()) for p in (DATA / "coins").glob("*.json")]
    coins = [
        c for c in coins
        if c.get("mint") and c.get("status") == 200 and c.get("quote") in (WSOL, None)
    ]
    prices = json.loads((DATA / "prices_now.json").read_text())
    return coins, prices, prices[WSOL]


def path(coin: dict) -> tuple[float, list[tuple[float, float, float, float]]] | None:
    """Entry price and (minutes after entry, high, low, close) per later candle."""
    rows = [c for c in coin["ohlcv"] if c[0] >= coin["t"] - 60]
    if not rows:
        return None
    entry_t, entry = rows[0][0] + 60, rows[0][4]
    later = [((c[0] + 60 - entry_t) / 60, c[2], c[3], c[4]) for c in rows[1:]]
    return entry, later


def first_minute(later: list, entry: float, test) -> float | None:  # noqa: ANN001
    for minute, high, low, _ in later:
        if test(high / entry, low / entry):
            return minute
    return None


def quartiles(values: list[float]) -> str:
    if not values:
        return "none"
    if len(values) < 4:
        return f"n={len(values)} values {sorted(values)}"
    q = statistics.quantiles(values, n=4)
    return f"n={len(values)}  25%={q[0]:.0f}  median={q[1]:.0f}  75%={q[2]:.0f} min"


def simulate(entry: float, later: list, timeout: float, stop: float | None, now_sol: float) -> float:
    """P&L in SOL of one coin; held tokens valued at the current price."""
    tokens = BUY * (1 - FEE) / entry
    for minute, high, low, close in later:
        if minute > timeout:
            break
        if stop is not None and low / entry <= 1 - stop:
            return tokens * entry * (1 - stop) * (1 - FEE) - BUY
        if high / entry >= MULTIPLIER:
            sold = min(1.0, (1 / MULTIPLIER) * (1 + BUFFER))
            cash = tokens * sold * entry * MULTIPLIER * (1 - FEE)
            return cash + tokens * (1 - sold) * now_sol - BUY
    window = [c for c in later if c[0] <= timeout]
    exit_price = window[-1][3] if window else entry
    return tokens * exit_price * (1 - FEE) - BUY


def main() -> None:
    coins, prices, sol_usd = load()
    paths = [(c, path(c)) for c in coins]
    traded = [(c, p) for c, p in paths if p]
    print(f"{len(coins)} SOL-paired coins, {len(coins) - len(traded)} never traded after creation")

    to_2x, to_07, to_05 = [], [], []
    for _, (entry, later) in traded:
        m = first_minute(later, entry, lambda h, _l: h >= MULTIPLIER)
        if m is not None:
            to_2x.append(m)
        m = first_minute(later, entry, lambda _h, low: low <= 0.7)
        if m is not None:
            to_07.append(m)
        m = first_minute(later, entry, lambda _h, low: low <= 0.5)
        if m is not None:
            to_05.append(m)
    n = len(traded)
    print("\n1. Reaching 2x within 30 min of entry")
    print(f"   {len(to_2x)} of {n} ({100 * len(to_2x) / n:.0f}%); {quartiles(to_2x)}")
    for cut in (1, 2, 3, 5, 10, 15):
        share = sum(m <= cut for m in to_2x) / len(to_2x) if to_2x else 0
        print(f"   by minute {cut:2}: {100 * share:5.1f}% of the coins that ever got there")
    print("\n2. Dying")
    print(f"   under 0.7x: {len(to_07)} ({100 * len(to_07) / n:.0f}%); {quartiles(to_07)}")
    print(f"   under 0.5x: {len(to_05)} ({100 * len(to_05) / n:.0f}%); {quartiles(to_05)}")

    print("\n3. Price / entry at minute (coins with a trade by then carry their last close)")
    for cut in (1, 2, 3, 5, 10, 15, 30):
        ratios = []
        for _, (entry, later) in traded:
            closes = [c for c in later if c[0] <= cut]
            ratios.append((closes[-1][3] if closes else entry) / entry)
        q = statistics.quantiles(ratios, n=4)
        print(f"   minute {cut:2}: 25%={q[0]:.2f}x  median={q[1]:.2f}x  75%={q[2]:.2f}x")

    print("\n4. P&L of recover_and_hold over all traded coins (SOL, per 100 coins)")
    header = "   timeout  " + "".join(f"{label:>12}" for label in ("no stop", "stop 30%", "stop 50%"))
    print(header)
    for timeout in (1, 2, 3, 5, 10, 15, 30):
        cells = []
        for stop in (None, 0.3, 0.5):
            total = 0.0
            for c, (entry, later) in traded:
                usd = prices.get(c["mint"])
                total += simulate(entry, later, timeout, stop, usd / sol_usd if usd else 0.0)
            cells.append(100 * total / n)
        print(f"   {timeout:5} min" + "".join(f"{v:>+12.4f}" for v in cells))
    print(f"\n   (100 coins x {BUY} SOL = {100 * BUY} SOL invested)")


if __name__ == "__main__":
    main()
