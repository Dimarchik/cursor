"""Run several dry-run configs side by side and compare their books.

Every `bots/dryrun-ab/*.yaml` runs in its own process against the same live
coins; each keeps its book in `trades_dryrun/<name>/`. When the run ends (after
`--minutes`, or Ctrl+C) the report values every held coin at its current price
(GeckoTerminal) and prints one row per config.

Usage:
    uv run tools/run_dryrun_ab.py --minutes 45
    uv run tools/run_dryrun_ab.py --report-only
"""

import argparse
import json
import multiprocessing
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_runner import run_bot_process  # noqa: E402

CONFIG_DIR = PROJECT_ROOT / "bots" / "dryrun-ab"
BOOKS = PROJECT_ROOT / "trades_dryrun"
WSOL = "So11111111111111111111111111111111111111112"
GECKO_PRICES = (
    "https://api.geckoterminal.com/api/v2/simple/networks/solana/token_price/"
)


def prices_in_sol(mints: list[str]) -> dict[str, float]:
    """Current SOL price per token; coins GeckoTerminal cannot price are left out."""
    usd: dict[str, float] = {}
    batch = [WSOL, *mints]
    for i in range(0, len(batch), 30):
        url = GECKO_PRICES + ",".join(batch[i : i + 30])
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310
                data = json.load(resp)["data"]["attributes"]["token_prices"]
        except (OSError, ValueError, KeyError):
            continue
        usd.update({m: float(p) for m, p in data.items() if p is not None})
        time.sleep(2.1)
    sol = usd.get(WSOL)
    return {m: p / sol for m, p in usd.items() if sol and m != WSOL}


def report() -> None:
    rows = []
    for book in sorted(p for p in BOOKS.iterdir() if p.is_dir()):
        summary_path = book / "dryrun_summary.json"
        if not summary_path.exists():
            continue
        summary = json.loads(summary_path.read_text())
        bags: dict[str, dict] = {}
        bags_path = book / "moonbags.jsonl"
        if bags_path.exists():
            for line in bags_path.read_text().splitlines():
                record = json.loads(line)
                bags[record["mint"]] = record
        rows.append((book.name, summary, bags))

    prices = prices_in_sol(sorted({m for _, _, bags in rows for m in bags}))
    print(
        f"\n{'config':16}{'buys':>6}{'2x+ won back':>14}{'reverted':>10}"
        f"{'dropped':>9}{'cash SOL':>11}{'rent SOL':>10}{'bags now':>10}{'P&L SOL':>10}"
    )
    for name, s, bags in rows:
        recovered = sum(b["reason"] == "recovered" for b in bags.values())
        bag_value = sum(
            b["quantity_held"] * prices.get(m, 0.0) for m, b in bags.items()
        )
        pnl = (
            s["balance_sol"] + s["rent_locked_sol"] + bag_value - s["start_balance_sol"]
        )
        print(
            f"{name:16}{s['buys']:>6}{recovered:>14}{s['reverted']:>10}{s['dropped']:>9}"
            f"{s['balance_sol']:>11.5f}{s['rent_locked_sol']:>10.5f}{bag_value:>10.5f}"
            f"{pnl:>+10.5f}"
        )
    print(
        "\nP&L counts the rent as recoverable and values held coins at today's "
        "price, which a dead coin's thin pool may not pay out."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=45)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.report_only:
        configs = sorted(CONFIG_DIR.glob("*.yaml"))
        procs = [
            multiprocessing.Process(target=run_bot_process, args=(str(c),), name=c.stem)
            for c in configs
        ]
        for p in procs:
            p.start()
        print(f"Running {len(procs)} dry-run configs for {args.minutes} min")
        try:
            time.sleep(args.minutes * 60)
        except KeyboardInterrupt:
            pass
        # Stop the bots once; they record open positions as held on the way
        # out. A second Ctrl+C must not cut the report off.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for p in procs:
            if p.is_alive() and p.pid:
                os.kill(p.pid, signal.SIGINT)
        for p in procs:
            p.join(timeout=90)
    report()


if __name__ == "__main__":
    main()
