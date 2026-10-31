"""Replay the recover_and_hold strategy over pump.fun coins created in the last day.

Data, all public and read-only:
  - Solana RPC: every pump.fun creation in the window, found through the
    mint-authority PDA, which signs creations and nothing else; then a random
    sample of those creation transactions for their mints.
  - GeckoTerminal: one-minute candles of each sampled coin's bonding curve for
    the first half hour, and current prices.

For each SOL-paired coin the strategy buys `--buy` SOL, sells enough to repay
the cost once the price reaches `--multiplier` x entry, and holds the rest; a
coin that misses the target within `--hold-minutes` is sold whole at the last
price in the window. Minute candles cannot place a buy seconds after creation,
so three entries bracket it:
  first_trade  open of the first candle (as fast as the first buyer)
  after_1min   close of the first candle
  worst_1min   high of the first candle

Raw responses are cached under `analysis_data/`, so a rerun only fetches what is
missing. Moves no funds.

Usage:
    uv run tools/backtest_24h.py --sample 450
"""

import argparse
import json
import random
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

from solders.pubkey import Pubkey

RPC = "https://api.mainnet-beta.solana.com"
GECKO = "https://api.geckoterminal.com/api/v2"
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
WSOL = "So11111111111111111111111111111111111111112"

FEE = 0.0125  # pump.fun fee per trade, each side
TX_COST = 0.000025  # SOL per transaction: base fee + 200k microlamports priority
RENT = 0.00203928  # SOL locked in each held token account
GECKO_INTERVAL = 2.1  # seconds between GeckoTerminal calls (30/min limit)

DATA = Path(__file__).resolve().parent.parent / "analysis_data"


def http_json(url: str, payload: dict | None = None, retries: int = 6) -> dict:
    """GET (or POST `payload`) and decode JSON, backing off on 429 and 5xx.

    Raises:
        urllib.error.HTTPError: For a 4xx other than 429, e.g. 404 for a pool
            GeckoTerminal does not know
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {url}")


def rpc(method: str, params: list, retries: int = 6) -> dict:
    """Call the RPC, retrying the public node's transient storage errors."""
    for attempt in range(retries):
        out = http_json(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" not in out:
            return out["result"]
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{method}: {out['error']}")


def cached(name: str, fetch):  # noqa: ANN001, ANN201
    path = DATA / name
    if path.exists():
        return json.loads(path.read_text())
    value = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


def list_creations(start: int, end: int) -> list[dict]:
    """Successful pump.fun creation signatures with blockTime in [start, end)."""
    found, before = [], None
    while True:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [MINT_AUTHORITY, opts])
        if not page:
            break
        for sig in page:
            t = sig.get("blockTime") or 0
            if start <= t < end and sig["err"] is None:
                found.append({"sig": sig["signature"], "t": t})
        before = page[-1]["signature"]
        oldest = page[-1].get("blockTime") or 0
        print(f"  creations listed back to {time.strftime('%H:%M', time.gmtime(oldest))} UTC: {len(found)}")
        if oldest < start:
            break
        time.sleep(0.3)
    return found


def mint_of(tx: dict) -> str | None:
    """Account 0 of the pump.fun create instruction, outer or inner."""
    ixs = list(tx["transaction"]["message"]["instructions"])
    for group in tx["meta"].get("innerInstructions") or []:
        ixs += group["instructions"]
    for ix in ixs:
        accounts = ix.get("accounts") or []
        if (
            ix.get("programId") == str(PUMP_PROGRAM)
            and len(accounts) > 1
            and accounts[1] == MINT_AUTHORITY
        ):
            return accounts[0]
    return None


def bonding_curve(mint: str) -> str:
    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))], PUMP_PROGRAM
    )
    return str(pda)


def candles(curve: str, t0: int, minutes: int) -> dict:
    """First `minutes` + 2 one-minute candles of a curve, priced in its quote asset."""
    url = (
        f"{GECKO}/networks/solana/pools/{curve}/ohlcv/minute?aggregate=1"
        f"&before_timestamp={t0 + (minutes + 2) * 60}&limit={minutes + 4}"
        f"&currency=token&token=base"
    )
    try:
        out = http_json(url)
    except urllib.error.HTTPError as e:
        return {"status": e.code}
    finally:
        time.sleep(GECKO_INTERVAL)
    return {
        "status": 200,
        "quote": out.get("meta", {}).get("quote", {}).get("address"),
        "ohlcv": sorted(out["data"]["attributes"]["ohlcv_list"]),
    }


def current_prices_usd(mints: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i : i + 30]
        out = http_json(f"{GECKO}/simple/networks/solana/token_price/{','.join(chunk)}")
        for mint, price in out["data"]["attributes"]["token_prices"].items():
            if price is not None:
                prices[mint] = float(price)
        time.sleep(GECKO_INTERVAL)
    return prices


def simulate(
    coin: dict, entry_mode: str, args: argparse.Namespace, now_price_sol: float | None
) -> dict:
    """One coin under one entry assumption. Amounts in SOL."""
    rows = [c for c in coin["ohlcv"] if c[0] >= coin["t"] - 60]
    buy = args.buy
    if not rows:
        # Nobody traded after creation: the price never moved, the timeout
        # sells back at entry and the account is closed.
        cash = buy * (1 - FEE) ** 2
        return {"outcome": "no_trades", "cash": cash, "bag_sol": 0.0, "max_x": 1.0}

    first = rows[0]
    if entry_mode == "first_trade":
        entry, entry_t, later = first[1], first[0], rows
    elif entry_mode == "after_1min":
        entry, entry_t, later = first[4], first[0] + 60, rows[1:]
    else:  # worst_1min
        entry, entry_t, later = first[2], first[0] + 60, rows[1:]
    window = [c for c in later if c[0] < entry_t + args.hold_minutes * 60]
    tokens = buy * (1 - FEE) / entry
    max_x = max((c[2] for c in window), default=entry) / entry

    if max_x >= args.multiplier:
        target = entry * args.multiplier
        sold = min(1.0, (1 / args.multiplier) * (1 + args.buffer))
        cash = tokens * sold * target * (1 - FEE)
        held = tokens * (1 - sold)
        bag = held * now_price_sol if now_price_sol is not None else 0.0
        return {"outcome": "recovered", "cash": cash, "bag_sol": bag, "max_x": max_x}

    exit_price = window[-1][4] if window else entry
    cash = tokens * exit_price * (1 - FEE)
    return {"outcome": "timeout_sold", "cash": cash, "bag_sol": 0.0, "max_x": max_x}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=450)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--buy", type=float, default=0.005)
    parser.add_argument("--multiplier", type=float, default=2.0)
    parser.add_argument("--buffer", type=float, default=0.05)
    parser.add_argument("--hold-minutes", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    meta = cached("window.json", lambda: {"end": int(time.time()) - (args.hold_minutes + 5) * 60})
    end = meta["end"]
    start = end - int(args.hours * 3600)
    print(f"Window: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(start))} - "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(end))} UTC")

    print("1. Listing creations")
    creations = cached("creations.json", lambda: list_creations(start, end))
    print(f"   {len(creations)} successful creations")

    rng = random.Random(args.seed)
    sample = rng.sample(creations, min(args.sample, len(creations)))

    print(f"2. Reading {len(sample)} creation transactions and their candles")
    coins = []
    for i, c in enumerate(sample, 1):
        def fetch_coin(c=c) -> dict:  # noqa: ANN001
            tx = rpc("getTransaction", [c["sig"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
            time.sleep(0.25)
            mint = mint_of(tx) if tx else None
            if not mint:
                return {"sig": c["sig"], "t": c["t"], "mint": None}
            curve = bonding_curve(mint)
            return {"sig": c["sig"], "t": c["t"], "mint": mint, "curve": curve,
                    **candles(curve, c["t"], args.hold_minutes + 1)}
        coins.append(cached(f"coins/{c['sig']}.json", fetch_coin))
        if i % 25 == 0:
            print(f"   {i}/{len(sample)}")

    sol_paired = [c for c in coins if c.get("mint") and c.get("status") == 200 and c.get("quote") in (WSOL, None)]
    other_quote = [c for c in coins if c.get("status") == 200 and c.get("quote") not in (WSOL, None)]
    unknown = [c for c in coins if c.get("mint") and c.get("status") != 200]
    print(f"   SOL-paired: {len(sol_paired)}, other quote: {len(other_quote)}, "
          f"not on GeckoTerminal: {len(unknown)}")

    print("3. Current prices")
    prices = cached("prices_now.json", lambda: current_prices_usd([WSOL] + [c["mint"] for c in sol_paired]))
    sol_usd = prices.get(WSOL)

    # A coin GeckoTerminal never listed had no trades it could index: treat it
    # like a coin nobody traded.
    population = sol_paired + [{**c, "ohlcv": []} for c in unknown]
    report = {"window_utc": [start, end], "creations": len(creations), "sample": len(coins),
              "sol_paired_or_untraded": len(population), "other_quote": len(other_quote),
              "sol_usd": sol_usd, "params": vars(args), "scenarios": {}}
    for mode in ("first_trade", "after_1min", "worst_1min"):
        results = []
        for c in population:
            p = prices.get(c["mint"])
            now_sol = p / sol_usd if p is not None and sol_usd else None
            results.append(simulate(c, mode, args, now_sol))
        n = len(results)
        rec = [r for r in results if r["outcome"] == "recovered"]
        invested = n * args.buy
        cash = sum(r["cash"] for r in results)
        bags = sum(r["bag_sol"] for r in results)
        tx_fees = sum(2 * TX_COST for _ in results)
        rent_locked = len(rec) * RENT
        report["scenarios"][mode] = {
            "coins": n,
            "recovered": len(rec),
            "recovered_pct": 100 * len(rec) / n if n else 0,
            "no_trades": sum(r["outcome"] == "no_trades" for r in results),
            "median_max_x_30min": statistics.median(r["max_x"] for r in results) if n else None,
            "share_max_x_ge_10": sum(r["max_x"] >= 10 for r in results) / n if n else 0,
            "invested_sol": invested,
            "cash_back_sol": cash,
            "tx_fees_sol": tx_fees,
            "rent_locked_sol": rent_locked,
            "bags_value_now_sol": bags,
            "net_sol_excl_rent": cash + bags - invested - tx_fees,
            "best_bag_now_sol": max((r["bag_sol"] for r in rec), default=0.0),
        }
    (DATA / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
