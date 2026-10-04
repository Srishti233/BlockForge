"""Configuration: TOML file < environment variables (BLOCKFORGE_*) < CLI flags."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping, Optional


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 5001
    peers: list[str] = field(default_factory=list)
    difficulty_bits: int = 12
    block_reward: int = 50
    max_tx_per_block: int = 100
    max_block_bytes: int = 1_000_000
    chain_id: str = "blockforge-dev"
    genesis_allocations: dict[str, int] = field(default_factory=dict)
    genesis_timestamp: int = 1_700_000_000
    data_dir: str = "data"
    db_path: str = ""
    max_reorg_depth: int = 100
    log_level: str = "INFO"
    mempool_max: int = 5000
    max_request_bytes: int = 2_000_000
    rate_limit_per_sec: float = 50.0
    health_check_interval: float = 5.0
    test_hooks: bool = False   # DEMO/TEST ONLY: exposes /p2p/partition and /p2p/heal (never enable on a real node)
    miner_address: str = ""   # non-empty: mine to this address as soon as the node starts

    def __post_init__(self) -> None:
        if not self.db_path:
            self.db_path = str(Path(self.data_dir) / f"node-{self.port}.db")

    @property
    def advertised_addr(self) -> str:
        return f"{self.host}:{self.port}"

    def validate(self) -> "Config":
        def need(cond: bool, msg: str) -> None:
            if not cond:
                raise ConfigError(msg)

        need(isinstance(self.port, int) and 0 < self.port < 65536, "port must be 1..65535")
        need(isinstance(self.difficulty_bits, int) and 1 <= self.difficulty_bits <= 40,
             "difficulty_bits must be an integer in 1..40 (0 would give blocks zero chain work)")
        need(isinstance(self.block_reward, int) and self.block_reward >= 0,
             "block_reward must be a non-negative integer")
        need(isinstance(self.max_tx_per_block, int) and self.max_tx_per_block >= 1,
             "max_tx_per_block must be >= 1")
        need(isinstance(self.max_block_bytes, int) and self.max_block_bytes >= 1000,
             "max_block_bytes must be >= 1000")
        need(isinstance(self.chain_id, str) and self.chain_id != "",
             "chain_id must be a non-empty string")
        need(isinstance(self.max_reorg_depth, int) and self.max_reorg_depth >= 1,
             "max_reorg_depth must be >= 1")
        need(self.log_level.upper() in {"DEBUG", "INFO", "WARNING", "ERROR"},
             "log_level must be DEBUG, INFO, WARNING or ERROR")
        need(isinstance(self.mempool_max, int) and self.mempool_max >= 1, "mempool_max must be >= 1")
        for addr, amt in self.genesis_allocations.items():
            need(isinstance(addr, str) and addr.startswith("bf") and len(addr) == 42,
                 f"genesis allocation address {addr!r} is not a valid address")
            need(isinstance(amt, int) and not isinstance(amt, bool) and amt > 0,
                 f"genesis allocation for {addr} must be a positive integer")
        need(self.miner_address == "" or (self.miner_address.startswith("bf") and len(self.miner_address) == 42),
             "miner_address must be empty or a valid address")
        need(self.rate_limit_per_sec > 0, "rate_limit_per_sec must be > 0")
        for p in self.peers:
            need(":" in p and p.rsplit(":", 1)[1].isdigit(), f"peer {p!r} must look like host:port")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


_ENV_PREFIX = "BLOCKFORGE_"


def _coerce(name: str, raw: str, current: Any) -> Any:
    try:
        if isinstance(current, bool):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(current, int):
            return int(raw)
        if isinstance(current, float):
            return float(raw)
        if isinstance(current, list):
            return [p.strip() for p in raw.split(",") if p.strip()]
        if isinstance(current, dict):
            return json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid value for {name}: {raw!r} ({exc})") from exc
    return raw


def load_config(
    toml_path: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    overrides: Optional[Mapping[str, Any]] = None,
) -> Config:
    """Build a validated Config. Precedence: defaults < TOML < env < overrides."""
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    known = {f.name for f in fields(Config)}
    if toml_path:
        p = Path(toml_path)
        if not p.is_file():
            raise ConfigError(f"config file not found: {toml_path}")
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {toml_path}: {exc}") from exc
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")
        values.update(data)
    defaults = Config()
    for name in known:
        key = _ENV_PREFIX + name.upper()
        if key in env:
            values[name] = _coerce(name, env[key], getattr(defaults, name))
    if overrides:
        for k, v in overrides.items():
            if k not in known:
                raise ConfigError(f"unknown config override: {k}")
            if v is not None:
                values[k] = v
    try:
        cfg = Config(**values)
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc
    return cfg.validate()
