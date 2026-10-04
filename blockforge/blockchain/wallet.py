"""Client-side wallets. Private keys never leave this module / the local machine."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from blockforge.blockchain import crypto
from blockforge.blockchain.transaction import Transaction

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class Wallet:
    """A secp256k1 keypair plus the ability to build and sign transactions."""

    def __init__(self, private_key) -> None:
        self._key = private_key
        self.public_key: str = crypto.public_key_bytes(private_key).hex()
        self.address: str = crypto.address_from_public_key(self.public_key)

    @classmethod
    def generate(cls) -> "Wallet":
        return cls(crypto.generate_private_key())

    @classmethod
    def from_private_hex(cls, private_hex: str) -> "Wallet":
        return cls(crypto.private_key_from_hex(private_hex))

    def private_hex(self) -> str:
        return crypto.private_key_to_hex(self._key)

    def sign_message(self, message: bytes) -> str:
        return crypto.sign(self._key, message)

    def create_transaction(self, recipient: str, amount: int, fee: int, nonce: int,
                           chain_id: str, timestamp: Optional[int] = None) -> Transaction:
        """Build and sign a transaction locally."""
        return Transaction.create_signed(
            self, recipient=recipient, amount=amount, fee=fee, nonce=nonce,
            chain_id=chain_id, timestamp=int(time.time()) if timestamp is None else timestamp)

    def to_keystore(self, passphrase: str, scrypt_n: int = 2 ** 15) -> dict[str, Any]:
        return crypto.encrypt_keystore(self.private_hex(), passphrase, scrypt_n=scrypt_n)

    @classmethod
    def from_keystore(cls, ks: dict[str, Any], passphrase: str) -> "Wallet":
        wallet = cls.from_private_hex(crypto.decrypt_keystore(ks, passphrase))
        if wallet.address != ks.get("address"):
            raise crypto.KeystoreError("keystore address does not match decrypted key")
        return wallet


class WalletStore:
    """A directory of encrypted keystore files, one JSON file per wallet name."""

    def __init__(self, directory: str | Path, scrypt_n: int = 2 ** 15) -> None:
        self.dir = Path(directory)
        self.scrypt_n = scrypt_n

    def _path(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise ValueError("wallet name must be 1-64 chars of letters, digits, '_', '.', '-'")
        return self.dir / f"{name}.json"

    def _write(self, name: str, wallet: Wallet, passphrase: str) -> Path:
        path = self._path(name)
        if path.exists():
            raise FileExistsError(f"wallet {name!r} already exists")
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(wallet.to_keystore(passphrase, self.scrypt_n), indent=2),
                        encoding="utf-8")
        return path

    def create(self, name: str, passphrase: str) -> Wallet:
        wallet = Wallet.generate()
        self._write(name, wallet, passphrase)
        return wallet

    def import_private_key(self, name: str, private_hex: str, passphrase: str) -> Wallet:
        wallet = Wallet.from_private_hex(private_hex)
        self._write(name, wallet, passphrase)
        return wallet

    def list(self) -> list[dict[str, str]]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                ks = json.loads(p.read_text(encoding="utf-8"))
                out.append({"name": p.stem, "address": ks["address"]})
            except (ValueError, KeyError, OSError):
                continue
        return out

    def load(self, name: str, passphrase: str) -> Wallet:
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(f"wallet {name!r} not found")
        return Wallet.from_keystore(json.loads(path.read_text(encoding="utf-8")), passphrase)

    def export_private_key(self, name: str, passphrase: str) -> str:
        return self.load(name, passphrase).private_hex()
