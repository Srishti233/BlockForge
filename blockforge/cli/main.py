"""BlockForge command line: `python run.py <cmd>` or the `blockforge` console script."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from blockforge import __version__
from blockforge.blockchain.crypto import CryptoError, is_valid_address
from blockforge.blockchain.wallet import WalletStore
from blockforge.config import ConfigError, load_config

DEFAULT_NODE = "http://127.0.0.1:5001"


class CliError(Exception):
    """A user-facing error: printed as `error: ...`, exit status 1."""


# ----------------------------------------------------------------------- helpers
def _passphrase(args: argparse.Namespace, confirm: bool = False) -> str:
    if getattr(args, "passphrase", None):
        return args.passphrase
    env = os.environ.get("BLOCKFORGE_PASSPHRASE")
    if env:
        return env
    pw = getpass.getpass("Passphrase: ")
    if confirm and getpass.getpass("Repeat passphrase: ") != pw:
        raise CliError("passphrases do not match")
    if not pw:
        raise CliError("empty passphrase")
    return pw


def _store(args: argparse.Namespace) -> WalletStore:
    return WalletStore(args.wallet_dir or Path(args.data_dir or "data") / "wallets")


def _http(args: argparse.Namespace):
    import httpx
    return httpx.Client(base_url=args.node.rstrip("/"), timeout=60.0)


def _call(args: argparse.Namespace, method: str, path: str, **kw: Any) -> Any:
    import httpx
    try:
        with _http(args) as c:
            r = c.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise CliError(f"cannot reach node at {args.node}: {exc}") from exc
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise CliError(f"{err.get('code', r.status_code)}: {err.get('message', r.text[:200])}")
    return body


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _resolve_address(args: argparse.Namespace, value: str) -> str:
    """Accept a raw address or a wallet name."""
    if is_valid_address(value):
        return value
    for w in _store(args).list():
        if w["name"] == value:
            return w["address"]
    raise CliError(f"{value!r} is neither a valid address nor a known wallet name")


# ---------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> None:
    data = Path(args.data_dir or "data")
    (data / "wallets").mkdir(parents=True, exist_ok=True)
    cfg_path = Path("config/blockforge.toml")
    example = Path(__file__).resolve().parents[2] / "config" / "blockforge.example.toml"
    if not cfg_path.exists() and example.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(example, cfg_path)
        print(f"wrote {cfg_path}")
    print(f"initialized data directory {data}/")
    print("next: python run.py wallet create alice && python run.py node --port 5001")


def cmd_wallet(args: argparse.Namespace) -> None:
    store = _store(args)
    try:
        if args.wallet_cmd == "create":
            w = store.create(args.name, _passphrase(args, confirm=True))
            print(f"created wallet {args.name!r}\naddress: {w.address}")
        elif args.wallet_cmd == "list":
            rows = store.list()
            if not rows:
                print("(no wallets)")
            for r in rows:
                print(f"{r['name']:<20} {r['address']}")
        elif args.wallet_cmd == "export":
            print("WARNING: this prints the unencrypted private key. Keep it secret.", file=sys.stderr)
            print(store.export_private_key(args.name, _passphrase(args)))
        elif args.wallet_cmd == "import":
            key = args.private_key or getpass.getpass("Private key (hex): ")
            w = store.import_private_key(args.name, key.strip(), _passphrase(args, confirm=True))
            print(f"imported wallet {args.name!r}\naddress: {w.address}")
    except (CryptoError, FileExistsError, FileNotFoundError, ValueError) as exc:
        raise CliError(str(exc)) from exc


def cmd_balance(args: argparse.Namespace) -> None:
    _out(_call(args, "GET", f"/api/v1/accounts/{_resolve_address(args, args.address)}"))


def cmd_send(args: argparse.Namespace) -> None:
    """Sign locally with the wallet's key and POST only the signed transaction."""
    try:
        wallet = _store(args).load(args.sender, _passphrase(args))
    except (CryptoError, FileNotFoundError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    to = _resolve_address(args, args.to)
    acct = _call(args, "GET", f"/api/v1/accounts/{wallet.address}")
    chain_id = _call(args, "GET", "/api/v1/chain/info")["chain_id"]
    tx = wallet.create_transaction(to, args.amount, args.fee, acct["next_nonce"], chain_id)
    res = _call(args, "POST", "/api/v1/transactions", json=tx.to_dict())
    print(f"submitted {res['tx_id']} ({wallet.address[:10]}... -> {to[:10]}..., amount {args.amount}, fee {args.fee})")


def cmd_mempool(args: argparse.Namespace) -> None:
    _out(_call(args, "GET", "/api/v1/mempool"))


def cmd_mine(args: argparse.Namespace) -> None:
    res = _call(args, "POST", "/api/v1/mine", json={"address": _resolve_address(args, args.address)})
    print(f"mined block {res['height']} {res['hash']} ({res['tx_count']} txs)")


def cmd_block(args: argparse.Namespace) -> None:
    _out(_call(args, "GET", f"/api/v1/blocks/{args.id}"))


def cmd_chain(args: argparse.Namespace) -> None:
    _out(_call(args, "GET", "/api/v1/chain/info"))


def cmd_validate(args: argparse.Namespace) -> None:
    rep = _call(args, "GET", "/api/v1/chain/validate")
    _out(rep)
    if not rep["valid"]:
        raise CliError("chain validation FAILED")


def cmd_peers(args: argparse.Namespace) -> None:
    if args.peers_cmd == "add":
        _out(_call(args, "POST", "/api/v1/peers", json={"address": args.address}))
    else:
        _out(_call(args, "GET", "/api/v1/peers"))


def cmd_node(args: argparse.Namespace) -> None:
    from blockforge.logging_setup import setup_logging
    from blockforge.network.node import Node

    alloc: dict[str, int] = {}
    for item in args.genesis_alloc or []:
        addr, _, amt = item.partition("=")
        if not amt.isdigit():
            raise CliError(f"--genesis-alloc expects ADDRESS=AMOUNT, got {item!r}")
        alloc[addr] = int(amt)
    peers = [p.strip() for chunk in (args.peers or []) for p in chunk.split(",") if p.strip()]
    overrides = {
        "host": args.host, "port": args.port, "peers": peers or None, "difficulty_bits": args.difficulty,
        "data_dir": args.data_dir, "db_path": args.db_path, "chain_id": args.chain_id,
        "log_level": args.log_level, "block_reward": args.block_reward,
        "miner_address": args.miner, "test_hooks": args.test_hooks, "genesis_allocations": alloc or None,
    }
    try:
        cfg = load_config(args.config, overrides=overrides)
    except ConfigError as exc:
        raise CliError(f"configuration error: {exc}") from exc
    import uvicorn

    from blockforge.api.routes import create_app

    setup_logging(cfg.log_level)
    node = Node(cfg)
    print(f"BlockForge node {node.node_id} on http://{cfg.host}:{cfg.port}  "
          f"(explorer /, API docs /docs, db {cfg.db_path})", flush=True)
    uvicorn.run(create_app(node), host=cfg.host, port=cfg.port, log_level="warning")


# ------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    S = argparse.SUPPRESS   # so options may appear before OR after the subcommand
    common.add_argument("--node", default=S, help=f"node URL (default {DEFAULT_NODE})")
    common.add_argument("--data-dir", default=S, help="databases and wallets directory (default: data)")
    common.add_argument("--wallet-dir", default=S, help="wallet directory (default: <data-dir>/wallets)")
    common.add_argument("--passphrase", default=S,
                        help="wallet passphrase (INSECURE: visible in process list; prefer the prompt or BLOCKFORGE_PASSPHRASE)")

    p = argparse.ArgumentParser(prog="blockforge", parents=[common],
                                description="BlockForge: educational blockchain (no real value).")
    p.add_argument("--version", action="version", version=f"blockforge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(parent, name, **kw):
        return parent.add_parser(name, parents=[common], **kw)

    add(sub, "init", help="create data dir and default config").set_defaults(fn=cmd_init)

    w = add(sub, "wallet", help="manage encrypted wallets")
    ws = w.add_subparsers(dest="wallet_cmd", required=True)
    for name in ("create", "export"):
        add(ws, name).add_argument("name")
    add(ws, "list")
    imp = add(ws, "import")
    imp.add_argument("name")
    imp.add_argument("--private-key", default=None, help="hex (INSECURE on the command line; omit to be prompted)")
    w.set_defaults(fn=cmd_wallet)

    b = add(sub, "balance", help="account balance and nonce")
    b.add_argument("address", help="address or wallet name")
    b.set_defaults(fn=cmd_balance)

    t = add(sub, "transaction", help="transactions")
    ts = t.add_subparsers(dest="tx_cmd", required=True)
    s = add(ts, "send", help="sign locally and submit")
    s.add_argument("--from", dest="sender", required=True, help="wallet name")
    s.add_argument("--to", required=True, help="address or wallet name")
    s.add_argument("--amount", type=int, required=True)
    s.add_argument("--fee", type=int, default=1)
    s.set_defaults(fn=cmd_send)

    add(sub, "mempool", help="pending transactions").set_defaults(fn=cmd_mempool)
    m = add(sub, "mine", help="mine one block")
    m.add_argument("--address", required=True, help="reward address or wallet name")
    m.set_defaults(fn=cmd_mine)
    bl = add(sub, "block", help="show a block")
    bl.add_argument("id", help="height or hash")
    bl.set_defaults(fn=cmd_block)
    add(sub, "chain", help="chain summary").set_defaults(fn=cmd_chain)
    add(sub, "validate", help="full chain replay validation").set_defaults(fn=cmd_validate)

    pe = add(sub, "peers", help="list or add peers")
    pes = pe.add_subparsers(dest="peers_cmd")
    add(pes, "list")
    pa = add(pes, "add")
    pa.add_argument("address", help="host:port")
    pe.set_defaults(fn=cmd_peers)

    n = add(sub, "node", help="run a node (`node` and `node start` are equivalent)")
    n.add_argument("action", nargs="?", default="start", choices=["start"])
    n.add_argument("--host", default=None)
    n.add_argument("--port", type=int, default=None)
    n.add_argument("--peers", action="append", default=None, help="host:port[,host:port] (repeatable)")
    n.add_argument("--miner", default=None, metavar="ADDRESS", help="mine continuously, rewards to ADDRESS")
    n.add_argument("--difficulty", type=int, default=None, help="difficulty_bits")
    n.add_argument("--config", default=None, help="TOML config file")
    n.add_argument("--db-path", default=None)
    n.add_argument("--chain-id", default=None)
    n.add_argument("--block-reward", type=int, default=None)
    n.add_argument("--log-level", default=None)
    n.add_argument("--test-hooks", action="store_true", default=None,
                   help="DEMO ONLY: enable /p2p/partition and /p2p/heal")
    n.add_argument("--genesis-alloc", action="append", default=None, metavar="ADDR=AMOUNT")
    n.set_defaults(fn=cmd_node)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    for name, default in (("node", os.environ.get("BLOCKFORGE_NODE", DEFAULT_NODE)), ("data_dir", None),
                          ("wallet_dir", None), ("passphrase", None)):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        args.fn(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
