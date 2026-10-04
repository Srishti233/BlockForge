"""Structured key=value logging that always includes node_id."""
from __future__ import annotations

import logging
import sys
from typing import Any

_node_id = "-"


class _KVFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        base = (f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
                f"node={_node_id} {record.getMessage()}")
        return f"{base} {kv}".rstrip()


def setup_logging(level: str = "INFO", node_id: str = "-") -> None:
    global _node_id
    _node_id = node_id
    root = logging.getLogger("blockforge")
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_KVFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def set_node_id(node_id: str) -> None:
    global _node_id
    _node_id = node_id


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})
