"""Find which launch signals separate coins worth buying from the rest.

Runs after `tools/backtest_24h.py` and reuses its cache. For every sampled
SOL-paired coin it collects what is knowable at or near the moment of entry:

  from the creation transaction
    dev_share      share of supply the creator bought in the create transaction
    bundled_buyers other wallets that bought inside the create transaction
    via_router     created by another program (launch tools, bundlers)
  from the creator's wallet
    creator_txs    transactions in the last day (up to 1000)
    creator_age_h  hours since the wallet's oldest transaction, when visible
  from the coin's metadata
    has_twitter / has_telegram / has_website
  from the first minute of trading (only usable with a delayed entry)
    m1_volume      first-minute volume
    m1_change      first-minute close / open

and then shows, per signal bucket, how often the strategy recovered its cost
under the `after_1min` entry, and what it earned per coin. A rule found on the
first half of the day is checked on the second half, so a filter that merely
fits noise shows up as one that stops working.

Usage:
    uv run tools/backtest_features.py
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import base58

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_24h import (  # noqa: E402
    DATA,
    MINT_AUTHORITY,
    PUMP_PROGRAM,
    WSOL,
    http_json,
    rpc,
    simulate,
)

TOKEN_SUPPLY = 1_000_000_000


def cached(name: str, fetch):  # noqa: ANN001, ANN201
    path = DATA / name
    if path.exists():
        return json.loads(path.read_text())
    value = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return value


def create_features(sig: str, mint: str, curve: str) -> dict:
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    time.sleep(0.25)
    msg = tx["transaction"]["message"]
    creator = msg["accountKeys"][0]["pubkey"]
    top_level = any(
        ix.get("programId") == str(PUMP_PROGRAM)
        and (ix.get("accounts") or [None, None])[1] == MINT_AUTHORITY
        for ix in msg["instructions"]
    )
    holders = {}
    for bal in tx["meta"]["postTokenBalances"]:
        if bal["mint"] == mint and bal.get("owner") != curve:
            holders[bal["owner"]] = float(bal["uiTokenAmount"]["uiAmount"] or 0)
    args = create_args(tx)
    return {
        "creator": creator,
        "uri": args[2] if args else None,
        "via_router": not top_level,
        "dev_share": holders.get(creator, 0.0) / TOKEN_SUPPLY,
        "bundled_buyers": len([o for o in holders if o != creator]),
    }


def creator_features(creator: str, t0: int) -> dict:
    sigs = rpc("getSignaturesForAddress", [creator, {"limit": 1000}])
    time.sleep(0.25)
    times = [s["blockTime"] for s in sigs if s.get("blockTime")]
    recent = [t for t in times if t0 - 86400 <= t <= t0]
    oldest = min(times) if times else t0
    return {
        "creator_txs": len(recent),
        "creator_age_h": (t0 - oldest) / 3600 if len(sigs) < 1000 else None,
    }


def create_args(tx: dict) -> tuple[str, str, str] | None:
    """name, symbol, uri from the create instruction's borsh-encoded args."""
    msg = tx["transaction"]["message"]
    ixs = list(msg["instructions"])
    for group in tx["meta"].get("innerInstructions") or []:
        ixs += group["instructions"]
    for ix in ixs:
        accounts = ix.get("accounts") or []
        if ix.get("programId") == str(PUMP_PROGRAM) and len(accounts) > 1 and accounts[1] == MINT_AUTHORITY:
            raw = base58.b58decode(ix["data"])[8:]
            out = []
            for _ in range(3):
                n = int.from_bytes(raw[:4], "little")
                out.append(raw[4 : 4 + n].decode("utf-8", "replace"))
                raw = raw[4 + n :]
            return out[0], out[1], out[2]
    return None


def metadata_features(uri: str | None) -> dict:
    """Socials listed in the coin's metadata JSON; None when it cannot be read."""
    empty = {"has_twitter": None, "has_telegram": None, "has_website": None}
    if not uri or not uri.startswith("http"):
        return empty
    try:
        req = urllib.request.Request(uri, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            meta = json.loads(resp.read(200_000))
    except Exception:  # noqa: BLE001 - dead links and non-JSON are common
        return empty
    return {
        "has_twitter": bool(meta.get("twitter")),
        "has_telegram": bool(meta.get("telegram")),
        "has_website": bool(meta.get("website")),
    }


def market_features(coin: dict) -> dict:
    rows = [c for c in coin["ohlcv"] if c[0] >= coin["t"] - 60]
    if not rows:
        return {"m1_volume": 0.0, "m1_change": None}
    _, o, _, _, c, v = rows[0][:6]
    return {"m1_volume": v, "m1_change": c / o if o else None}


def bucket_table(rows: list[dict], key: str, edges: list) -> list[str]:
    """Recovery rate and mean P&L per coin for each value bucket of `key`."""
    lines = []
    def label(v):  # noqa: ANN001, ANN202
        if v is None:
            return "n/a"
        if isinstance(v, bool):
            return str(v)
        for lo, hi in zip(edges, edges[1:]):
            if lo <= v < hi:
                return f"[{lo:g}, {hi:g})"
        return f">= {edges[-1]:g}"
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(label(r[key]), []).append(r)
    for name, g in sorted(groups.items()):
        rec = sum(x["sim"]["outcome"] == "recovered" for x in g)
        pnl = sum(x["pnl"] for x in g) / len(g)
        lines.append(f"  {key:15} {name:18} n={len(g):4}  recovered {100 * rec / len(g):5.1f}%  P&L/coin {pnl:+.5f} SOL")
    return lines


def main() -> None:
    report = json.loads((DATA / "report.json").read_text())
    params = report["params"]
    args = type("A", (), params)
    prices = json.loads((DATA / "prices_now.json").read_text())
    sol_usd = prices.get(WSOL)

    coins = [json.loads(p.read_text()) for p in sorted((DATA / "coins").glob("*.json"))]
    coins = [c for c in coins if c.get("mint") and c.get("status") == 200 and c.get("quote") in (WSOL, None)]
    print(f"{len(coins)} SOL-paired coins")

    rows = []
    for i, c in enumerate(coins, 1):
        feats = {}
        feats.update(cached(f"features/create_{c['sig']}.json", lambda c=c: create_features(c["sig"], c["mint"], c["curve"])))
        feats.update(cached(f"features/creator_{c['sig']}.json", lambda c=c, f=feats: creator_features(f["creator"], c["t"])))
        feats.update(cached(f"features/meta_{c['mint']}.json", lambda f=feats: metadata_features(f.get("uri"))))
        feats.update(market_features(c))
        price = prices.get(c["mint"])
        sim = simulate(c, "after_1min", args, price / sol_usd if price and sol_usd else None)
        pnl = sim["cash"] + sim["bag_sol"] - params["buy"]
        rows.append({**feats, "t": c["t"], "sim": sim, "pnl": pnl})
        if i % 50 == 0:
            print(f"  features {i}/{len(coins)}")

    (DATA / "features.json").write_text(json.dumps(rows))
    print("\nPer-signal results, entry after the first minute:")
    tables = {
        "dev_share": [0, 0.01, 0.03, 0.06, 0.1, 1],
        "bundled_buyers": [0, 1, 2, 5, 100],
        "via_router": [],
        "creator_txs": [0, 3, 10, 50, 200, 1001],
        "creator_age_h": [0, 1, 24, 24 * 7, 24 * 365],
        "has_twitter": [],
        "has_telegram": [],
        "has_website": [],
        "m1_volume": [0, 1, 5, 20, 100, 1e12],
        "m1_change": [0, 0.5, 0.9, 1.1, 1.5, 3, 1e9],
    }
    for key, edges in tables.items():
        print("\n".join(bucket_table(rows, key, edges)))


if __name__ == "__main__":
    main()
