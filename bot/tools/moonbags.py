"""Show the positions recover_and_hold kept and what they are worth now.

Reads `trades/moonbags.jsonl`, then for each coin reads the wallet's token
balance and the coin's current price: from its bonding curve while it is on
one, from DexScreener once it has graduated. Moves no funds.

Usage:
    uv run tools/moonbags.py
    uv run tools/moonbags.py --owner <WALLET_PUBKEY>   # no private key needed
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from solders.pubkey import Pubkey

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.client import SolanaClient  # noqa: E402
from core.pubkeys import (  # noqa: E402
    TOKEN_DECIMALS,
    quote_symbol,
    resolve_quote_token_program,
)
from core.wallet import Wallet  # noqa: E402
from interfaces.core import CurveGraduatedError, Platform  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402
from utils.logger import install_secret_redaction  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
install_secret_redaction()

BAGS_FILE = PROJECT_ROOT / "trades" / "moonbags.jsonl"
DEXSCREENER_URL = "https://api.dexscreener.com/tokens/v1/solana/{mint}"
LAMPORTS_PER_SOL = 1_000_000_000


def load_bags(path: Path) -> dict[str, dict]:
    """Latest record per mint; a mint recorded twice keeps its last entry."""
    bags: dict[str, dict] = {}
    if not path.exists():
        return bags
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            bags[record["mint"]] = record
    return bags


async def dexscreener_price(session: aiohttp.ClientSession, mint: str) -> float | None:
    """Price in the pair's quote asset from the most liquid DexScreener pair."""
    try:
        async with session.get(
            DEXSCREENER_URL.format(mint=mint), timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            pairs = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    if not pairs:
        return None
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    price = best.get("priceNative")
    return float(price) if price else None


async def read_bag(
    client: SolanaClient,
    session: aiohttp.ClientSession,
    owner: Pubkey,
    bag: dict,
) -> dict:
    """Balance, price and value of one held coin.

    Returns:
        The record extended with `balance`, `price` (None if unreadable),
        `multiple` and `value`, plus `status`: "curve", "graduated" or "error".
    """
    platform = Platform(bag["platform"])
    impl = get_platform_implementations(platform, client)
    mint = Pubkey.from_string(bag["mint"])
    quote_mint = Pubkey.from_string(bag["quote_mint"])
    token_program = (
        Pubkey.from_string(bag["token_program"]) if bag.get("token_program") else None
    )

    ata = impl.address_provider.derive_user_token_account(owner, mint, token_program)
    try:
        balance = await client.get_token_account_balance(ata) / 10**TOKEN_DECIMALS
    except Exception:  # noqa: BLE001 - a closed account has no balance to read
        balance = 0.0

    price, status = None, "curve"
    try:
        await resolve_quote_token_program(quote_mint, client.get_account_info)
        price = await impl.curve_manager.calculate_price(
            impl.address_provider.derive_pool_address(mint, quote_mint)
        )
    except CurveGraduatedError:
        status = "graduated"
        price = await dexscreener_price(session, bag["mint"])
    except Exception:  # noqa: BLE001
        status = "error"

    entry = bag["entry_price"]
    return {
        **bag,
        "balance": balance,
        "price": price,
        "status": status,
        "multiple": price / entry if price and entry else None,
        "value": balance * price if price else None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", help="Wallet public key (default: from .env)")
    args = parser.parse_args()

    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    if not rpc:
        sys.exit("SOLANA_NODE_RPC_ENDPOINT is not set")
    if args.owner:
        owner = Pubkey.from_string(args.owner)
    else:
        owner = Wallet(os.environ["SOLANA_PRIVATE_KEY"]).pubkey

    bags = load_bags(BAGS_FILE)
    client = SolanaClient(rpc)
    try:
        sol = (await (await client.get_client()).get_balance(owner)).value
        print(f"Wallet {owner}: {sol / LAMPORTS_PER_SOL:.6f} SOL\n")
        if not bags:
            print(f"No held positions in {BAGS_FILE}")
            return

        async with aiohttp.ClientSession() as session:
            rows = [await read_bag(client, session, owner, b) for b in bags.values()]

        header = f"{'SYMBOL':<12}{'HELD':>16}{'ENTRY':>16}{'NOW':>16}{'X':>9}{'VALUE':>14}  STATUS / REASON"
        print(header)
        print("-" * len(header))
        totals: dict[str, float] = {}
        for r in rows:
            label = quote_symbol(Pubkey.from_string(r["quote_mint"]))
            now = f"{r['price']:.10f}" if r["price"] else "n/a"
            mult = f"{r['multiple']:.2f}x" if r["multiple"] else "n/a"
            value = f"{r['value']:.6f} {label}" if r["value"] is not None else "n/a"
            if r["value"]:
                totals[label] = totals.get(label, 0.0) + r["value"]
            print(
                f"{r['symbol'][:11]:<12}{r['balance']:>16.2f}{r['entry_price']:>16.10f}"
                f"{now:>16}{mult:>9}{value:>14}  {r['status']} / {r['reason']}"
            )
            print(f"{'':<12}{r['mint']}")
        print()
        for label, total in totals.items():
            print(f"Held positions are worth {total:.6f} {label}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
