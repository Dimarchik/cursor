"""Search the last day's pump.fun launches for signals that pick the coins that double.

Runs on the cache `tools/backtest_24h.py` builds (run it with a large
`--sample` first). For every SOL-paired coin it reads, once and cached:

  from the creation transaction  the launching app (outer programs), the
                                 creator's SOL outlay and share of supply, the
                                 priority fee paid, name/symbol/metadata host,
                                 hour of day
  from the bonding curve         Mayhem and cashback flags

and labels the coin with the shipped strategy: entry a minute after creation,
target 2x within 3 minutes, the remainder sold 2 minutes after the target, a
miss sold at 3 minutes. Then it tables each signal, and searches rules of one
or two conditions on the first half of the day, reporting how each does on the
second half; a rule that only fits the first half is noise.

Usage:
    uv run tools/backtest_patterns.py
"""

import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backtest_24h import DATA, MINT_AUTHORITY, PUMP_PROGRAM, WSOL, rpc  # noqa: E402

FEE = 0.0125
BUY = 0.005
TARGET = 2.0
WAIT_MIN = 3
REST_MIN = 2
MIN_RULE_N = 25
INFRA = {
    "ComputeBudget111111111111111111111111111111",
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    str(PUMP_PROGRAM),
}


def cached(path: Path, fetch):  # noqa: ANN001, ANN201
    if path.exists():
        return json.loads(path.read_text())
    value = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


def create_facts(sig: str, mint: str) -> dict:
    """Signals readable from the creation transaction."""
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    time.sleep(0.2)
    msg, meta = tx["transaction"]["message"], tx["meta"]
    keys = [k["pubkey"] for k in msg["accountKeys"]]
    payer = keys[0]
    outer = [ix.get("programId") for ix in msg["instructions"]]
    apps = sorted({p for p in outer if p and p not in INFRA})
    direct = any(
        ix.get("programId") == str(PUMP_PROGRAM)
        and (ix.get("accounts") or [None, None])[1] == MINT_AUTHORITY
        for ix in msg["instructions"]
    )
    cu_price = 0
    for ix in msg["instructions"]:
        if ix.get("programId") == "ComputeBudget111111111111111111111111111111":
            import base58

            raw = base58.b58decode(ix.get("data", ""))
            if raw[:1] == b"\x03":
                cu_price = int.from_bytes(raw[1:9], "little")
    tokens = {
        b["owner"]: float(b["uiTokenAmount"]["uiAmount"] or 0)
        for b in meta["postTokenBalances"]
        if b["mint"] == mint
    }
    name = symbol = uri = ""
    from backtest_features import create_args

    args = create_args(tx)
    if args:
        name, symbol, uri = args
    return {
        "payer": payer,
        "apps": apps,
        "direct": direct,
        "payer_sol_out": (meta["preBalances"][0] - meta["postBalances"][0]) / 1e9,
        "creator_share": tokens.get(payer, 0.0) / 1e9,
        "cu_price": cu_price,
        "n_instructions": len(outer),
        "name": name,
        "symbol": symbol,
        "uri_host": urlparse(uri).netloc if uri.startswith("http") else uri[:12],
    }


def curve_flags(coins: list[dict]) -> dict[str, dict]:
    """Mayhem and cashback flags for every coin, from batched curve reads."""
    import asyncio

    from solders.pubkey import Pubkey

    from core.client import SolanaClient
    from core.pubkeys import WSOL_MINT, resolve_quote_token_program
    from interfaces.core import Platform
    from platforms import get_platform_implementations

    async def read() -> dict[str, dict]:
        client = SolanaClient("https://api.mainnet-beta.solana.com,https://api.mainnet.solana.com")
        await resolve_quote_token_program(WSOL_MINT, client.get_account_info)
        manager = get_platform_implementations(Platform.PUMP_FUN, client).curve_manager
        out = {}
        for i in range(0, len(coins), 100):
            chunk = coins[i : i + 100]
            accounts = await client.get_multiple_accounts([Pubkey.from_string(c["curve"]) for c in chunk])
            for c, a in zip(chunk, accounts, strict=False):
                try:
                    s = manager.decode_pool_state(a.data) if a and a.data else {}
                except Exception:  # noqa: BLE001
                    s = {}
                out[c["sig"]] = {
                    "mayhem": bool(s.get("is_mayhem_mode")),
                    "cashback": bool(s.get("is_cashback_coin")),
                }
            await asyncio.sleep(0.5)
        await client.close()
        return out

    return asyncio.run(read())


def outcome(coin: dict, now_sol: float) -> tuple[bool, float]:
    """(doubled within WAIT_MIN, P&L in SOL) for the shipped strategy."""
    rows = [c for c in coin["ohlcv"] if c[0] >= coin["t"] - 60]
    if not rows:
        return False, BUY * (1 - FEE) ** 2 - BUY
    entry, t0 = rows[0][4], rows[0][0] + 60
    later = [((c[0] + 60 - t0) / 60, c[2], c[4]) for c in rows[1:]]
    tokens = BUY * (1 - FEE) / entry
    for minute, high, _ in later:
        if minute > WAIT_MIN:
            break
        if high / entry >= TARGET:
            sold = min(1.0, 1.1 / TARGET)
            cash = tokens * sold * entry * TARGET * (1 - FEE)
            rest = [c for c in later if c[0] <= minute + REST_MIN]
            rest_px = rest[-1][2] if rest else entry * TARGET
            return True, cash + tokens * (1 - sold) * rest_px * (1 - FEE) - BUY
    window = [c for c in later if c[0] <= WAIT_MIN]
    exit_px = window[-1][2] if window else entry
    return False, tokens * exit_px * (1 - FEE) - BUY


def signals(f: dict, flags: dict, t: int) -> dict[str, str]:
    """Bucketed signal values for one coin."""
    def band(v: float, edges: list[float]) -> str:
        for lo, hi in zip(edges, edges[1:], strict=False):
            if lo <= v < hi:
                return f"{lo:g}-{hi:g}"
        return f">={edges[-1]:g}"

    name, symbol = f["name"], f["symbol"]
    return {
        "mayhem": str(flags.get("mayhem")),
        "cashback": str(flags.get("cashback")),
        "app": ",".join(a[:6] for a in f["apps"]) or ("pump.fun" if f["direct"] else "other"),
        "creator_share": band(f["creator_share"], [0, 0.001, 0.01, 0.03, 0.1]),
        "payer_sol_out": band(f["payer_sol_out"], [0, 0.03, 0.1, 0.3, 1, 3]),
        "cu_price": band(f["cu_price"], [0, 1, 100_000, 1_000_000, 10_000_000]),
        "hour_utc": band(time.gmtime(t).tm_hour, [0, 4, 8, 12, 16, 20]),
        "uri_host": f["uri_host"] or "none",
        "symbol_len": band(len(symbol), [0, 3, 5, 8]),
        "symbol_upper": str(symbol.isupper()),
        "name_ascii": str(name.isascii()),
        "name_eq_symbol": str(name.strip().lower() == symbol.strip().lower()),
    }


def score(rows: list[dict]) -> tuple[int, float, float]:
    n = len(rows)
    if not n:
        return 0, 0.0, 0.0
    return n, 100 * sum(r["hit"] for r in rows) / n, 100 * sum(r["pnl"] for r in rows) / n / BUY


def main() -> None:
    coins = [json.loads(p.read_text()) for p in sorted((DATA / "coins").glob("*.json"))]
    coins = [c for c in coins if c.get("mint") and c.get("status") == 200 and c.get("quote") in (WSOL, None)]
    print(f"{len(coins)} SOL-paired coins")
    prices = json.loads((DATA / "prices_now.json").read_text())
    sol = prices[WSOL]
    flags = cached(DATA / f"flags_{len(coins)}.json", lambda: curve_flags(coins))

    rows = []
    for i, c in enumerate(coins, 1):
        try:
            f = cached(DATA / "facts" / f"{c['sig']}.json", lambda c=c: create_facts(c["sig"], c["mint"]))
        except (RuntimeError, TypeError, KeyError) as e:
            # Every endpoint refused the transaction, or it came back empty.
            print(f"  skipped {c['sig'][:12]}: {e}")
            continue
        hit, pnl = outcome(c, (prices.get(c["mint"]) or 0) / sol)
        rows.append({"t": c["t"], "hit": hit, "pnl": pnl, "s": signals(f, flags.get(c["sig"], {}), c["t"])})
        if i % 200 == 0:
            print(f"  facts {i}/{len(coins)}")

    mid = statistics.median(r["t"] for r in rows)
    first = [r for r in rows if r["t"] < mid]
    second = [r for r in rows if r["t"] >= mid]
    n, hit, pnl = score(rows)
    print(f"\nAll coins: n={n} doubled {hit:.1f}% P&L {pnl:+.1f}% of stake")

    print("\nSignal tables (all / first half / second half: n, doubled %, P&L %)")
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        for k, v in r["s"].items():
            by[k][v].append(r)
    for key in rows[0]["s"]:
        common = Counter({v: len(rs) for v, rs in by[key].items()}).most_common(8)
        for value, _ in common:
            rs = by[key][value]
            a = score(rs)
            h1 = score([r for r in rs if r["t"] < mid])
            h2 = score([r for r in rs if r["t"] >= mid])
            print(
                f"  {key:15} {value[:22]:22} n={a[0]:4} {a[1]:5.1f}% {a[2]:+6.1f}% | "
                f"{h1[1]:5.1f}% {h1[2]:+6.1f}% | {h2[1]:5.1f}% {h2[2]:+6.1f}%"
            )

    print(f"\nRules found on the first half (n>={MIN_RULE_N}), checked on the second")
    conditions = sorted({(k, v) for r in first for k, v in r["s"].items()})
    candidates = []
    for size in (1, 2):
        for combo in combinations(conditions, size):
            if size == 2 and combo[0][0] == combo[1][0]:
                continue
            match = [r for r in first if all(r["s"][k] == v for k, v in combo)]
            if len(match) < MIN_RULE_N:
                continue
            candidates.append((score(match)[2], combo))
    candidates.sort(reverse=True)
    for _, combo in candidates[:15]:
        h1 = score([r for r in first if all(r["s"][k] == v for k, v in combo)])
        h2 = score([r for r in second if all(r["s"][k] == v for k, v in combo)])
        label = " & ".join(f"{k}={v}" for k, v in combo)
        print(
            f"  {label[:60]:60} first n={h1[0]:3} {h1[1]:5.1f}% {h1[2]:+6.1f}% | "
            f"second n={h2[0]:3} {h2[1]:5.1f}% {h2[2]:+6.1f}%"
        )


if __name__ == "__main__":
    main()
