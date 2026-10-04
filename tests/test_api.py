import json

import pytest
from fastapi.testclient import TestClient

from blockforge.api.routes import create_app
from blockforge.blockchain.wallet import Wallet
from blockforge.network.node import Node
from tests.helpers import make_cfg

CID = "test-chain"


@pytest.fixture
def env(tmp_path):
    alice, bob = Wallet.generate(), Wallet.generate()
    cfg = make_cfg(tmp_path, port=5555, genesis_allocations={alice.address: 1000},
                   max_request_bytes=20_000, rate_limit_per_sec=1000)
    node = Node(cfg)
    with TestClient(create_app(node, manage_node=False)) as client:
        yield client, node, alice, bob
    node.stop()


def test_node_chain_and_empty_listing(env):
    c, node, alice, bob = env
    assert c.get("/api/v1/node").json()["chain_id"] == CID
    info = c.get("/api/v1/chain/info").json()
    assert info["height"] == 0 and info["genesis_hash"] == node.chain.genesis.hash
    assert c.get("/api/v1/chain/validate").json()["valid"] is True
    assert c.get("/api/v1/chain/forks").json() == {"forks": []}
    assert c.get("/api/v1/mempool").json()["count"] == 0
    assert c.get("/api/v1/peers").json()["peers"] == []


def test_full_flow_send_mine_query_merkle(env):
    c, node, alice, bob = env
    tx = alice.create_transaction(bob.address, 100, 2, 0, CID)
    r = c.post("/api/v1/transactions", json=tx.to_dict())
    assert r.status_code == 202 and r.json()["tx_id"] == tx.tx_id
    assert c.get(f"/api/v1/transactions/{tx.tx_id}").json()["status"] == "pending"
    assert c.get("/api/v1/mempool").json()["count"] == 1
    assert c.get(f"/api/v1/accounts/{alice.address}").json()["next_nonce"] == 1
    r = c.post("/api/v1/mine", json={"address": bob.address})
    assert r.status_code == 200 and r.json()["height"] == 1 and r.json()["tx_count"] == 2
    assert c.get(f"/api/v1/accounts/{bob.address}").json()["balance"] == 100 + 2 + 50
    got = c.get(f"/api/v1/transactions/{tx.tx_id}").json()
    assert got["status"] == "confirmed" and got["confirmations"] == 1 and got["merkle_proof"]
    p = c.get(f"/api/v1/merkle/proof/{tx.tx_id}").json()
    ok = c.post("/api/v1/merkle/verify", json={"tx_id": tx.tx_id, "proof": p["proof"], "root": p["merkle_root"]})
    assert ok.json() == {"valid": True}
    if p["proof"]:
        p["proof"][0]["hash"] = "00" * 32
    bad = c.post("/api/v1/merkle/verify", json={"tx_id": tx.tx_id, "proof": p["proof"], "root": p["merkle_root"]})
    assert bad.json() == {"valid": False}
    blk = c.get("/api/v1/blocks/1").json()
    assert blk["is_main"] and blk["confirmations"] == 1 and c.get(f"/api/v1/blocks/{blk['hash']}").json()["height"] == 1
    lst = c.get("/api/v1/blocks?limit=1&offset=0").json()
    assert lst["total"] == 2 and lst["blocks"][0]["height"] == 1
    assert c.post("/api/v1/miner/start", json={"address": bob.address}).json()["mining"] is True
    assert c.post("/api/v1/miner/stop").json() == {"mining": False}


def test_transaction_rejections_have_structured_errors(env):
    c, node, alice, bob = env
    good = alice.create_transaction(bob.address, 10, 1, 0, CID)
    assert c.post("/api/v1/transactions", json=good.to_dict()).status_code == 202
    dup = c.post("/api/v1/transactions", json=good.to_dict())
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE"
    cases = [
        (alice.create_transaction(bob.address, 10, 1, 0, "other"), "WRONG_CHAIN"),
        (alice.create_transaction(bob.address, 10, 1, 7, CID), "BAD_NONCE"),
        (alice.create_transaction(bob.address, 5000, 1, 1, CID), "INSUFFICIENT_BALANCE"),
        (alice.create_transaction(bob.address, 0, 1, 1, CID), "BAD_AMOUNT"),
        (alice.create_transaction(bob.address, -4, 1, 1, CID), "BAD_AMOUNT"),
    ]
    for tx, code in cases:
        r = c.post("/api/v1/transactions", json=tx.to_dict())
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (code, r.json())
    forged = good.to_dict() | {"signature": "11" * 64, "nonce": 1}
    forged["tx_id"] = __import__("dataclasses").replace(good, nonce=1).compute_id()
    r = c.post("/api/v1/transactions", json=forged)
    assert r.json()["error"]["code"] == "INVALID_SIGNATURE"


def test_validation_and_not_found_errors(env):
    c, *_ = env
    for r in [c.post("/api/v1/transactions", json={"amount": "x"}), c.get("/api/v1/blocks?limit=0"),
              c.get("/api/v1/blocks?limit=1000"), c.post("/api/v1/mine", json={"address": "nope"})]:
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert c.get("/api/v1/blocks/99").json()["error"]["code"] == "NOT_FOUND"
    assert c.get("/api/v1/blocks/zzz").json()["error"]["code"] == "BAD_REQUEST"
    assert c.get("/api/v1/transactions/" + "0" * 64).status_code == 404
    assert c.get("/api/v1/transactions/short").status_code == 400
    assert c.get("/api/v1/accounts/bad").status_code == 400
    assert c.get("/api/v1/nope").json()["error"]["code"] == "NOT_FOUND"
    assert c.delete("/api/v1/node").json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert c.post("/api/v1/peers", json={"address": "bad"}).status_code == 400
    assert c.post("/api/v1/peers", json={"address": "127.0.0.1:9"}).status_code == 502


def test_no_endpoint_accepts_or_returns_private_keys(env):
    c, node, alice, bob = env
    secret = alice.private_hex()
    bodies = [{"private_key": secret}, {"address": bob.address, "private_key": secret},
              {**alice.create_transaction(bob.address, 1, 1, 0, CID).to_dict(), "private_key": secret}]
    posts = ["/api/v1/transactions", "/api/v1/mine", "/api/v1/miner/start", "/api/v1/peers",
             "/api/v1/merkle/verify", "/p2p/handshake", "/p2p/tx", "/p2p/block"]
    for path in posts:
        for body in bodies:
            r = c.post(path, json=body)
            assert r.status_code in (400, 422), (path, r.status_code)
            assert secret not in r.text                        # never echoed back
    spec = json.dumps(c.get("/openapi.json").json()).lower()
    assert "private_key" not in spec and "privatekey" not in spec and "mnemonic" not in spec
    assert c.get("/api/v1/mempool").status_code == 200 and secret not in c.get("/api/v1/mempool").text


def test_body_size_limit(env):
    c, *_ = env
    r = c.post("/api/v1/transactions", content=b"x" * 50_000, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_rate_limit(tmp_path):
    cfg = make_cfg(tmp_path, port=5556, rate_limit_per_sec=1)
    node = Node(cfg)
    try:
        c = TestClient(create_app(node, manage_node=False))
        codes = [c.post("/api/v1/miner/stop").status_code for _ in range(6)]
        assert 429 in codes and codes[0] == 200
    finally:
        node.stop()


def test_docs_explorer_and_offline_assets(env):
    c, *_ = env
    for path in ("/docs", "/", "/static/app.js", "/static/style.css", "/static/docs.js", "/openapi.json"):
        assert c.get(path).status_code == 200, path
    html = c.get("/").text + c.get("/docs").text
    assert "http://" not in html.replace("http://127.0.0.1", "") and "https://" not in html
    for js in ("/static/app.js", "/static/docs.js"):
        assert "https://" not in c.get(js).text and "cdn" not in c.get(js).text.lower()
    assert "default-src 'self'" in c.get("/").headers["content-security-policy"]


def test_explorer_escapes_everything_it_renders():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "blockforge" / "explorer" / "app.js").read_text()
    assert "function esc(" in js and "&lt;" in js
    assert "eval(" not in js and "document.write" not in js
    # every ${...} interpolation inside an HTML template must be wrapped in esc()/a helper
    import re
    for m in re.finditer(r"\$\{([^}]*)\}", js):
        expr = m.group(1).strip()
        assert not re.fullmatch(r"[a-z]\.[a-z_]+|t\.[a-z_]+|x\.[a-z_]+|b\.[a-z_]+", expr), expr


def test_p2p_endpoints(env):
    c, node, alice, bob = env
    info = node.handshake_info() | {"addr": "127.0.0.1:5999", "node_id": "other"}
    r = c.post("/p2p/handshake", json=info)
    assert r.status_code == 200 and r.json()["chain_id"] == CID
    assert c.post("/p2p/handshake", json=info | {"genesis_hash": "0" * 64}).json()["error"]["code"] == "GENESIS_MISMATCH"
    assert "127.0.0.1:5999" in c.get("/p2p/peers").json()["peers"]
    assert c.get("/p2p/status").json()["tip_height"] == 0
    node.mine_once(bob.address)
    blocks = c.get("/p2p/blocks?start=0&limit=10").json()["blocks"]
    assert [b["header"]["height"] for b in blocks] == [0, 1]
    assert c.get(f"/p2p/block/{blocks[1]['hash']}").json()["hash"] == blocks[1]["hash"]
    assert c.get("/p2p/block/" + "1" * 64).status_code == 404
    tx = alice.create_transaction(bob.address, 5, 1, 0, CID)
    assert c.post("/p2p/tx", json={"tx": tx.to_dict(), "sender": "127.0.0.1:5999"}).json()["status"] == "accepted"
    assert c.post("/p2p/tx", json={"tx": tx.to_dict()}).json()["status"] == "seen"
    assert c.post("/p2p/block", json={"block": blocks[1]}).json()["status"] == "seen"


def test_partition_hooks_are_off_by_default(env):
    c, *_ = env
    assert c.post("/p2p/partition", json={"addrs": []}).status_code in (404, 405)
    assert c.post("/p2p/heal").status_code in (404, 405)


def test_partition_hooks_when_enabled(tmp_path):
    node = Node(make_cfg(tmp_path, port=5557, test_hooks=True))
    try:
        c = TestClient(create_app(node, manage_node=False))
        assert c.post("/p2p/partition", json={"addrs": ["127.0.0.1:1"]}).json()["partitioned"] == ["127.0.0.1:1"]
        assert c.post("/p2p/heal").json() == {"partitioned": []}
    finally:
        node.stop()
