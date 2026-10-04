import pytest

from blockforge.config import Config, ConfigError, load_config


def test_defaults_and_validation():
    cfg = load_config(env={})
    assert cfg.port == 5001 and cfg.db_path.endswith("node-5001.db")
    for bad in [{"port": 0}, {"difficulty_bits": 0}, {"difficulty_bits": 99}, {"block_reward": -1},
                {"max_tx_per_block": 0}, {"chain_id": ""}, {"log_level": "LOUD"}, {"max_reorg_depth": 0},
                {"peers": ["nocolon"]}, {"genesis_allocations": {"short": 5}},
                {"genesis_allocations": {"bf" + "0" * 40: 0}}, {"miner_address": "x"}]:
        with pytest.raises(ConfigError):
            Config(**bad).validate()


def test_precedence_toml_env_cli(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('port = 6001\ndifficulty_bits = 9\nchain_id = "from-toml"\n'
                 '[genesis_allocations]\n' + "bf" + "1" * 40 + " = 5\n")
    cfg = load_config(str(f), env={}, overrides={})
    assert (cfg.port, cfg.difficulty_bits, cfg.chain_id) == (6001, 9, "from-toml")
    assert cfg.genesis_allocations == {"bf" + "1" * 40: 5}
    cfg = load_config(str(f), env={"BLOCKFORGE_PORT": "6002", "BLOCKFORGE_PEERS": "a:1, b:2"}, overrides={})
    assert cfg.port == 6002 and cfg.peers == ["a:1", "b:2"] and cfg.difficulty_bits == 9
    cfg = load_config(str(f), env={"BLOCKFORGE_PORT": "6002"}, overrides={"port": 6003, "chain_id": None})
    assert cfg.port == 6003 and cfg.chain_id == "from-toml"      # None override = not given


def test_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "missing.toml"), env={})
    bad = tmp_path / "bad.toml"
    bad.write_text("port = [")
    with pytest.raises(ConfigError):
        load_config(str(bad), env={})
    unk = tmp_path / "unk.toml"
    unk.write_text("bogus = 1")
    with pytest.raises(ConfigError):
        load_config(str(unk), env={})
    with pytest.raises(ConfigError):
        load_config(env={"BLOCKFORGE_PORT": "abc"})
    with pytest.raises(ConfigError):
        load_config(env={}, overrides={"nope": 1})


def test_example_config_loads():
    from pathlib import Path
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config" / "blockforge.example.toml"), env={})
    assert cfg.difficulty_bits == 12
