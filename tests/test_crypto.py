import json

import pytest

from blockforge.blockchain import crypto
from blockforge.blockchain.wallet import Wallet, WalletStore

FAST = 2 ** 10


def test_sign_verify_roundtrip():
    w = Wallet.generate()
    sig = w.sign_message(b"hello")
    assert len(bytes.fromhex(sig)) == 64
    assert crypto.verify(w.public_key, b"hello", sig)


def test_signature_is_low_s():
    w = Wallet.generate()
    for i in range(30):
        s = int.from_bytes(bytes.fromhex(w.sign_message(b"m%d" % i))[32:], "big")
        assert s <= crypto.HALF_ORDER


def test_high_s_rejected():
    w = Wallet.generate()
    sig = bytes.fromhex(w.sign_message(b"x"))
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    high = (r.to_bytes(32, "big") + (crypto.CURVE_ORDER - s).to_bytes(32, "big")).hex()
    assert not crypto.verify(w.public_key, b"x", high)


def test_tampered_message_fails():
    w = Wallet.generate()
    assert not crypto.verify(w.public_key, b"hellp", w.sign_message(b"hello"))


def test_wrong_key_fails():
    a, b = Wallet.generate(), Wallet.generate()
    assert not crypto.verify(b.public_key, b"m", a.sign_message(b"m"))


@pytest.mark.parametrize("bad", ["", "zz", "00" * 63, "00" * 64, "ff" * 64, "00" * 65])
def test_malformed_signature_never_raises(bad):
    w = Wallet.generate()
    assert crypto.verify(w.public_key, b"m", bad) is False


def test_malformed_public_key():
    assert crypto.verify("nothex", b"m", "00" * 64) is False
    with pytest.raises(crypto.CryptoError):
        crypto.load_public_key("02" + "00" * 10)


def test_address_derivation():
    w = Wallet.generate()
    assert w.address == "bf" + crypto.sha256_hex(bytes.fromhex(w.public_key))[:40]
    assert crypto.is_valid_address(w.address)
    assert not crypto.is_valid_address("bf12")
    assert not crypto.is_valid_address("xx" + "0" * 40)
    assert not crypto.is_valid_address("bf" + "G" * 40)
    assert not crypto.is_valid_address(5)


def test_private_key_hex_roundtrip_and_errors():
    w = Wallet.generate()
    assert Wallet.from_private_hex(w.private_hex()).address == w.address
    for bad in ["00" * 32, "zz", "11" * 31, "ff" * 32]:
        with pytest.raises(crypto.CryptoError):
            crypto.private_key_from_hex(bad)


def test_canonical_json():
    assert crypto.canonical_json({"b": 1, "a": [2, {"d": 1, "c": 2}]}) == b'{"a":[2,{"c":2,"d":1}],"b":1}'
    with pytest.raises(ValueError):
        crypto.canonical_json({"a": 1.5})
    with pytest.raises(ValueError):
        crypto.canonical_json({"a": [1.0]})
    with pytest.raises(ValueError):
        crypto.canonical_json({1: 2})


def test_keystore_roundtrip_and_wrong_passphrase():
    w = Wallet.generate()
    ks = w.to_keystore("pw", scrypt_n=FAST)
    assert w.private_hex() not in json.dumps(ks)
    assert Wallet.from_keystore(ks, "pw").address == w.address
    with pytest.raises(crypto.KeystoreError):
        Wallet.from_keystore(ks, "wrong")


def test_keystore_tamper_and_malformed():
    w = Wallet.generate()
    ks = w.to_keystore("pw", scrypt_n=FAST)
    ks["address"] = "bf" + "0" * 40  # AAD mismatch
    with pytest.raises(crypto.KeystoreError):
        crypto.decrypt_keystore(ks, "pw")
    with pytest.raises(crypto.KeystoreError):
        crypto.decrypt_keystore({}, "pw")
    ks2 = w.to_keystore("pw", scrypt_n=FAST)
    ks2["kdf"]["n"] = 2 ** 30
    with pytest.raises(crypto.KeystoreError):
        crypto.decrypt_keystore(ks2, "pw")
    ks3 = w.to_keystore("pw", scrypt_n=FAST)
    ks3["kdf"]["name"] = "pbkdf2"
    with pytest.raises(crypto.KeystoreError):
        crypto.decrypt_keystore(ks3, "pw")


def test_wallet_store_create_list_export_import(tmp_path):
    store = WalletStore(tmp_path / "w", scrypt_n=FAST)
    assert store.list() == []
    w = store.create("alice", "pw")
    with pytest.raises(FileExistsError):
        store.create("alice", "pw")
    assert store.list() == [{"name": "alice", "address": w.address}]
    priv = store.export_private_key("alice", "pw")
    imported = store.import_private_key("alice2", priv, "pw2")
    assert imported.address == w.address
    assert store.load("alice2", "pw2").address == w.address
    with pytest.raises(FileNotFoundError):
        store.load("nobody", "pw")
    with pytest.raises(ValueError):
        store.create("../evil", "pw")
    (tmp_path / "w" / "junk.json").write_text("not json")
    assert len(store.list()) == 2
