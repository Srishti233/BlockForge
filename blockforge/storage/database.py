"""SQLite persistence: WAL mode, parameterized queries only, atomic commits."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from blockforge.blockchain.block import Block

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    hash        TEXT PRIMARY KEY,
    height      INTEGER NOT NULL,
    prev_hash   TEXT NOT NULL,
    cum_work    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('main','side','invalid')),
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_height ON blocks(height);
CREATE INDEX IF NOT EXISTS idx_blocks_status ON blocks(status, height);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peers (
    addr TEXT PRIMARY KEY, node_id TEXT, last_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fork_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """All-or-nothing unit of work. Any exception rolls everything back."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ----------------------------------------------------------------- blocks
    @staticmethod
    def _block_json(block: Block) -> str:
        return json.dumps(block.to_dict(), sort_keys=True, separators=(",", ":"))

    def _upsert_block(self, conn: sqlite3.Connection, block: Block, cum_work: int, status: str) -> None:
        conn.execute(
            "INSERT INTO blocks(hash,height,prev_hash,cum_work,status,data) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(hash) DO UPDATE SET status=excluded.status, cum_work=excluded.cum_work",
            (block.hash, block.height, block.header.prev_hash, str(cum_work), status,
             self._block_json(block)))

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def commit_main_block(self, block: Block, cum_work: int,
                          _fail_hook: Optional[Callable[[], None]] = None) -> None:
        """Store `block` as the new main-chain tip atomically.

        `_fail_hook` exists for tests: it runs after the block row is written but
        before COMMIT, so raising from it proves the commit is atomic.
        """
        with self.transaction() as conn:
            self._upsert_block(conn, block, cum_work, "main")
            if _fail_hook is not None:
                _fail_hook()
            self._set_meta(conn, "tip", block.hash)

    def save_side_block(self, block: Block, cum_work: int) -> None:
        with self.transaction() as conn:
            self._upsert_block(conn, block, cum_work, "side")

    def commit_reorg(self, old_hashes: list[str], new_blocks: list[tuple[Block, int]],
                     new_tip: str, fork_event: dict[str, Any]) -> None:
        """Flip statuses of the old and new branch, set the tip and log the fork - atomically."""
        with self.transaction() as conn:
            for h in old_hashes:
                conn.execute("UPDATE blocks SET status='side' WHERE hash=?", (h,))
            for blk, work in new_blocks:
                self._upsert_block(conn, blk, work, "main")
            self._set_meta(conn, "tip", new_tip)
            conn.execute("INSERT INTO fork_events(data) VALUES(?)",
                         (json.dumps(fork_event, sort_keys=True),))

    def mark_invalid(self, hashes: list[str]) -> None:
        with self.transaction() as conn:
            for h in hashes:
                conn.execute("UPDATE blocks SET status='invalid' WHERE hash=?", (h,))

    def load_blocks(self, status: str) -> list[tuple[Block, int]]:
        """Blocks with the given status ordered by height; each paired with cumulative work."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, cum_work FROM blocks WHERE status=? ORDER BY height, hash",
                (status,)).fetchall()
        return [(Block.from_dict(json.loads(d)), int(w)) for d, w in rows]

    def get_tip(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key='tip'").fetchone()
        return row[0] if row else None

    def count_blocks(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            self._set_meta(conn, key, value)

    # ------------------------------------------------------------------ peers
    def save_peer(self, addr: str, node_id: Optional[str], last_seen: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO peers(addr,node_id,last_seen) VALUES(?,?,?) "
                "ON CONFLICT(addr) DO UPDATE SET node_id=excluded.node_id, last_seen=excluded.last_seen",
                (addr, node_id, last_seen))

    def remove_peer(self, addr: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM peers WHERE addr=?", (addr,))

    def load_peers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT addr,node_id,last_seen FROM peers ORDER BY addr").fetchall()
        return [{"addr": a, "node_id": n, "last_seen": s} for a, n, s in rows]

    # ------------------------------------------------------------ fork events
    def load_fork_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM fork_events ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]
