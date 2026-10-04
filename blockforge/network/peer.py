"""Peer bookkeeping (scores, bans), loop-prevention seen-set, rate limiting, HTTP client."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from blockforge.network.protocol import BAN_SCORE, BAN_SECONDS, MAX_PEERS
from blockforge.storage.database import Database


@dataclass
class Peer:
    addr: str
    node_id: Optional[str] = None
    tip_height: int = 0
    cumulative_work: int = 0
    last_seen: float = 0.0
    failures: int = 0
    score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"addr": self.addr, "node_id": self.node_id, "tip_height": self.tip_height,
                "cumulative_work": str(self.cumulative_work), "last_seen": int(self.last_seen),
                "failures": self.failures, "score": self.score}


class PeerTable:
    """Thread-safe peer set with bad-data scoring and temporary bans."""

    def __init__(self, db: Optional[Database] = None, clock: Callable[[], float] = time.time,
                 max_peers: int = MAX_PEERS) -> None:
        self._peers: dict[str, Peer] = {}
        self._bans: dict[str, float] = {}
        self._scores: dict[str, int] = {}
        self._lock = threading.RLock()
        self._db = db
        self._clock = clock
        self.max_peers = max_peers

    def add(self, addr: str, node_id: Optional[str], tip_height: int = 0,
            work: int = 0) -> Optional[Peer]:
        with self._lock:
            if self.is_banned(addr):
                return None
            peer = self._peers.get(addr)
            if peer is None:
                if len(self._peers) >= self.max_peers:
                    return None
                peer = self._peers[addr] = Peer(addr)
            peer.node_id, peer.tip_height, peer.cumulative_work = node_id, tip_height, work
            peer.last_seen, peer.failures = self._clock(), 0
            if self._db is not None:
                self._db.save_peer(addr, node_id, int(peer.last_seen))
            return peer

    def get(self, addr: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(addr)

    def remove(self, addr: str) -> bool:
        with self._lock:
            existed = self._peers.pop(addr, None) is not None
            if self._db is not None:
                self._db.remove_peer(addr)
            return existed

    def all(self) -> list[Peer]:
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: p.addr)

    def addrs(self) -> list[str]:
        return [p.addr for p in self.all()]

    def record_failure(self, addr: str) -> int:
        with self._lock:
            peer = self._peers.get(addr)
            if peer is None:
                return 0
            peer.failures += 1
            return peer.failures

    # ---- scoring / bans
    def penalize(self, key: str, points: int) -> bool:
        """Add bad-data points. Returns True if this call caused a ban (and disconnects)."""
        with self._lock:
            score = self._scores.get(key, 0) + points
            self._scores[key] = score
            if key in self._peers:
                self._peers[key].score = score
            if score >= BAN_SCORE:
                self._bans[key] = self._clock() + BAN_SECONDS
                self._scores[key] = 0
                self.remove(key)
                return True
            return False

    def is_banned(self, key: str) -> bool:
        with self._lock:
            until = self._bans.get(key)
            if until is None:
                return False
            if self._clock() >= until:
                del self._bans[key]
                return False
            return True

    def bans(self) -> dict[str, int]:
        with self._lock:
            now = self._clock()
            return {k: int(v - now) for k, v in self._bans.items() if v > now}


class SeenSet:
    """Bounded set of ids already processed, so gossip cannot loop."""

    def __init__(self, capacity: int) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._cap = capacity
        self._lock = threading.Lock()

    def add(self, item: str) -> bool:
        """Returns True if the item is new."""
        with self._lock:
            if item in self._d:
                self._d.move_to_end(item)
                return False
            self._d[item] = None
            if len(self._d) > self._cap:
                self._d.popitem(last=False)
            return True

    def __contains__(self, item: str) -> bool:
        with self._lock:
            return item in self._d


class RateLimiter:
    """Token bucket per key. `rate` tokens/second, burst of 2*rate."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = rate
        self.capacity = max(2.0, rate * 2)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            if len(self._buckets) > 10_000:  # bound memory under address spraying
                self._buckets.pop(next(iter(self._buckets)))
            return True


# --------------------------------------------------------------------- transport

class PeerClient(Protocol):
    """What Node needs from a connection to one peer. Methods raise on failure."""

    def handshake(self, info: dict[str, Any]) -> dict[str, Any]: ...
    def peers(self) -> list[str]: ...
    def status(self) -> dict[str, Any]: ...
    def send_tx(self, tx: dict[str, Any], sender: str) -> dict[str, Any]: ...
    def send_block(self, block: dict[str, Any], sender: str) -> dict[str, Any]: ...
    def blocks(self, start: int, limit: int) -> list[dict[str, Any]]: ...
    def block(self, block_hash: str) -> dict[str, Any]: ...


class HttpPeerClient:
    """Real transport: HTTP/JSON over real sockets with short timeouts."""

    def __init__(self, addr: str, timeout: float = 5.0) -> None:
        import httpx  # imported lazily so the pure-logic modules stay importable without it

        self.addr = addr
        self._c = httpx.Client(base_url=f"http://{addr}", timeout=timeout,
                               headers={"User-Agent": "blockforge-peer/1"})

    def _json(self, resp) -> Any:
        resp.raise_for_status()
        return resp.json()

    def handshake(self, info: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._c.post("/p2p/handshake", json=info))

    def peers(self) -> list[str]:
        return self._json(self._c.get("/p2p/peers"))["peers"]

    def status(self) -> dict[str, Any]:
        return self._json(self._c.get("/p2p/status"))

    def send_tx(self, tx: dict[str, Any], sender: str) -> dict[str, Any]:
        return self._json(self._c.post("/p2p/tx", json={"tx": tx, "sender": sender}))

    def send_block(self, block: dict[str, Any], sender: str) -> dict[str, Any]:
        return self._json(self._c.post("/p2p/block", json={"block": block, "sender": sender}))

    def blocks(self, start: int, limit: int) -> list[dict[str, Any]]:
        return self._json(self._c.get("/p2p/blocks", params={"start": start, "limit": limit}))["blocks"]

    def block(self, block_hash: str) -> dict[str, Any]:
        return self._json(self._c.get(f"/p2p/block/{block_hash}"))

    def close(self) -> None:
        self._c.close()
