"""CLI via subprocess. Wallet commands are offline; node-backed ones live in test_network.py."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env_extra=None, cwd=None):
    env = {**os.environ, "BLOCKFORGE_PASSPHRASE": "pw", **(env_extra or {})}
    return subprocess.run([sys.executable, str(ROOT / "run.py"), *args], capture_output=True, text=True,
                          env=env, cwd=cwd or ROOT, timeout=120)


def test_wallet_create_list_export_import(tmp_path):
    d = str(tmp_path)
    r = run("wallet", "create", "alice", "--data-dir", d)
    assert r.returncode == 0, r.stderr
    addr = [l for l in r.stdout.splitlines() if l.startswith("address:")][0].split()[1]
    assert addr.startswith("bf") and len(addr) == 42
    assert addr in run("wallet", "list", "--data-dir", d).stdout
    key = run("wallet", "export", "alice", "--data-dir", d).stdout.strip()
    assert len(key) == 64
    r = run("wallet", "import", "bob", "--private-key", key, "--data-dir", d)
    assert addr in r.stdout
    assert run("wallet", "create", "alice", "--data-dir", d).returncode == 1       # exists
    bad = run("wallet", "export", "alice", "--data-dir", d, env_extra={"BLOCKFORGE_PASSPHRASE": "wrong"})
    assert bad.returncode == 1 and "passphrase" in bad.stderr
    ks = json.loads((tmp_path / "wallets" / "alice.json").read_text())
    assert key not in json.dumps(ks)                       # private key is never stored in clear


def test_init_and_help(tmp_path):
    r = run("init", "--data-dir", str(tmp_path / "d"), cwd=tmp_path)
    assert r.returncode == 0 and (tmp_path / "d" / "wallets").is_dir()
    h = run("--help")
    assert h.returncode == 0 and "node" in h.stdout and "transaction" in h.stdout
    assert run("nonsense").returncode == 2


def test_unreachable_node_reports_cleanly(tmp_path):
    r = run("chain", "--node", "http://127.0.0.1:9", "--data-dir", str(tmp_path))
    assert r.returncode == 1 and "cannot reach node" in r.stderr and "Traceback" not in r.stderr


def test_invalid_node_config_fails_fast(tmp_path):
    r = run("node", "--port", "5999", "--difficulty", "99", "--data-dir", str(tmp_path))
    assert r.returncode == 1 and "difficulty_bits" in r.stderr
