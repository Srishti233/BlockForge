"""Wire-level rules shared by both sides of a peer connection."""
from __future__ import annotations

import re
from typing import Any

PROTOCOL_VERSION = 1
SYNC_BATCH = 50              # blocks per /p2p/blocks response
MAX_BATCH = 100              # hard server-side cap
MAX_ANCESTOR_FETCH = 64      # how far back to walk for a missing parent
SEEN_CAPACITY = 20_000
BAN_SCORE = 100
BAN_SECONDS = 120
MAX_PEERS = 32

# Bad-data penalties (points). A peer reaching BAN_SCORE is banned temporarily.
PENALTY_MALFORMED = 20
PENALTY_BAD_TX = 10
PENALTY_BAD_BLOCK = 50
PENALTY_BAD_HANDSHAKE = 100

_ADDR_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}:[0-9]{1,5}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ProtocolError(Exception):
    """A peer violated the protocol. `code` is a stable machine-readable string."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def valid_peer_addr(addr: Any) -> bool:
    if not isinstance(addr, str) or not _ADDR_RE.match(addr):
        return False
    port = int(addr.rsplit(":", 1)[1])
    return 0 < port < 65536


def check_handshake(info: Any, *, chain_id: str, genesis_hash: str) -> dict[str, Any]:
    """Validate a peer's handshake payload. Raises ProtocolError on any mismatch."""
    if not isinstance(info, dict):
        raise ProtocolError("BAD_HANDSHAKE", "handshake must be an object")
    try:
        node_id = info["node_id"]
        addr = info["addr"]
        tip_height = info["tip_height"]
        work = info["cumulative_work"]
        if not (isinstance(node_id, str) and 0 < len(node_id) <= 64):
            raise ValueError("node_id")
        if not valid_peer_addr(addr):
            raise ValueError("addr")
        if not (isinstance(tip_height, int) and not isinstance(tip_height, bool) and tip_height >= 0):
            raise ValueError("tip_height")
        if not (isinstance(work, str) and work.isdigit() and len(work) < 100):
            raise ValueError("cumulative_work")
        if info["chain_id"] != chain_id:
            raise ProtocolError("WRONG_CHAIN", f"peer chain_id {info['chain_id']!r} != {chain_id!r}")
        if info["genesis_hash"] != genesis_hash:
            raise ProtocolError("GENESIS_MISMATCH", "peer genesis hash differs from ours")
    except KeyError as exc:
        raise ProtocolError("BAD_HANDSHAKE", f"missing field {exc}") from exc
    except ValueError as exc:
        raise ProtocolError("BAD_HANDSHAKE", f"invalid field {exc}") from exc
    return info
