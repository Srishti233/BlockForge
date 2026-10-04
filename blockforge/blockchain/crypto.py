"""Cryptographic primitives: hashing, canonical JSON, ECDSA secp256k1, keystore."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

CURVE = ec.SECP256K1()
# Order of the secp256k1 group.
CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
HALF_ORDER = CURVE_ORDER // 2
ADDRESS_PREFIX = "bf"


class CryptoError(Exception):
    """Raised for malformed keys, signatures or keystores."""


class KeystoreError(CryptoError):
    """Raised when a keystore cannot be decrypted or parsed."""


# ----------------------------------------------------------------- hashing / JSON

def _reject_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise ValueError("floats are not allowed in canonical JSON")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError("canonical JSON keys must be strings")
            _reject_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_floats(v)


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8, integers only."""
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------- keys

def generate_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(CURVE)


def private_key_to_int(key: ec.EllipticCurvePrivateKey) -> int:
    return key.private_numbers().private_value


def private_key_from_hex(hex_str: str) -> ec.EllipticCurvePrivateKey:
    try:
        raw = bytes.fromhex(hex_str)
        value = int.from_bytes(raw, "big")
        if len(raw) != 32 or not (1 <= value < CURVE_ORDER):
            raise ValueError("private key out of range")
        return ec.derive_private_key(value, CURVE)
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"invalid private key: {exc}") from exc


def private_key_to_hex(key: ec.EllipticCurvePrivateKey) -> str:
    return private_key_to_int(key).to_bytes(32, "big").hex()


def public_key_bytes(key: ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey) -> bytes:
    """33-byte compressed SEC1 public key."""
    pub = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return pub.public_bytes(Encoding.X962, PublicFormat.CompressedPoint)


def load_public_key(pub_hex: str) -> ec.EllipticCurvePublicKey:
    try:
        raw = bytes.fromhex(pub_hex)
        if len(raw) != 33:
            raise ValueError("expected 33-byte compressed key")
        return ec.EllipticCurvePublicKey.from_encoded_point(CURVE, raw)
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"invalid public key: {exc}") from exc


def address_from_public_key(pub_hex: str) -> str:
    """'bf' + first 40 hex chars of SHA-256(compressed pubkey)."""
    try:
        raw = bytes.fromhex(pub_hex)
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"invalid public key hex: {exc}") from exc
    return ADDRESS_PREFIX + sha256_hex(raw)[:40]


def is_valid_address(addr: object) -> bool:
    if not isinstance(addr, str) or len(addr) != 42 or not addr.startswith(ADDRESS_PREFIX):
        return False
    return all(c in "0123456789abcdef" for c in addr[2:])


# ------------------------------------------------------------------ signatures

def sign(key: ec.EllipticCurvePrivateKey, message: bytes) -> str:
    """ECDSA/SHA-256 signature as fixed-width r||s (64 bytes, hex), low-S normalized."""
    r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    if s > HALF_ORDER:
        s = CURVE_ORDER - s
    return (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()


def verify(pub_hex: str, message: bytes, signature_hex: str) -> bool:
    """Return True only for a well-formed, low-S, valid signature. Never raises."""
    try:
        sig = bytes.fromhex(signature_hex)
        if len(sig) != 64:
            return False
        r = int.from_bytes(sig[:32], "big")
        s = int.from_bytes(sig[32:], "big")
        if not (1 <= r < CURVE_ORDER and 1 <= s <= HALF_ORDER):
            return False
        pub = load_public_key(pub_hex)
        pub.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, CryptoError, ValueError, TypeError):
        return False


# -------------------------------------------------------------------- keystore

def encrypt_keystore(private_hex: str, passphrase: str, *, scrypt_n: int = 2 ** 15) -> dict[str, Any]:
    """Encrypt a private key with scrypt + AES-256-GCM. Returns a JSON-able dict."""
    key = private_key_from_hex(private_hex)
    pub_hex = public_key_bytes(key).hex()
    address = address_from_public_key(pub_hex)
    salt, nonce = os.urandom(16), os.urandom(12)
    r, p = 8, 1
    aes_key = Scrypt(salt=salt, length=32, n=scrypt_n, r=r, p=p).derive(passphrase.encode("utf-8"))
    ct = AESGCM(aes_key).encrypt(nonce, bytes.fromhex(private_hex), address.encode("ascii"))
    return {
        "version": 1,
        "address": address,
        "public_key": pub_hex,
        "kdf": {"name": "scrypt", "n": scrypt_n, "r": r, "p": p, "salt": salt.hex()},
        "cipher": {"name": "aes-256-gcm", "nonce": nonce.hex(), "ciphertext": ct.hex()},
    }


def decrypt_keystore(ks: dict[str, Any], passphrase: str) -> str:
    """Return the private key hex. Raises KeystoreError on wrong passphrase / corruption."""
    try:
        kdf, cipher = ks["kdf"], ks["cipher"]
        if kdf["name"] != "scrypt" or cipher["name"] != "aes-256-gcm":
            raise KeystoreError("unsupported keystore algorithms")
        n = int(kdf["n"])
        if n < 2 ** 10 or n > 2 ** 20 or n & (n - 1):
            raise KeystoreError("unreasonable scrypt parameter")
        aes_key = Scrypt(salt=bytes.fromhex(kdf["salt"]), length=32, n=n,
                         r=int(kdf["r"]), p=int(kdf["p"])).derive(passphrase.encode("utf-8"))
        pt = AESGCM(aes_key).decrypt(bytes.fromhex(cipher["nonce"]),
                                     bytes.fromhex(cipher["ciphertext"]),
                                     str(ks["address"]).encode("ascii"))
    except InvalidTag as exc:
        raise KeystoreError("wrong passphrase or corrupted keystore") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise KeystoreError(f"malformed keystore: {exc}") from exc
    return pt.hex()
