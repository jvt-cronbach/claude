from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

import pytest

from callrec import paths
from callrec.config import (
    DEFAULT_CONFIG_TEMPLATE,
    Config,
    ConfigError,
    config_from_dict,
    config_to_dict,
    load_config,
    save_config,
)


def test_default_template_matches_dataclass_defaults():
    data = tomllib.loads(DEFAULT_CONFIG_TEMPLATE)
    cfg = config_from_dict(data)
    assert cfg.warnings == []
    assert config_to_dict(cfg) == config_to_dict(Config())


def test_load_creates_default_file(isolated_home: Path):
    cfg = load_config()
    assert paths.config_file().exists()
    assert paths.config_file().is_relative_to(isolated_home)
    assert cfg.transcription.model == "auto"
    assert cfg.transcription.language == "de"
    assert cfg.summary.ollama_url.startswith("http://127.0.0.1")


def test_roundtrip(tmp_path: Path):
    cfg = Config()
    cfg.general.user_name = "Jörg Prüfer"
    cfg.audio.format = "opus"
    cfg.detection.teams_processes = ["ms-teams.exe"]
    p = tmp_path / "c.toml"
    save_config(cfg, p)
    again = load_config(p)
    assert again.general.user_name == "Jörg Prüfer"
    assert again.audio.format == "opus"
    assert again.detection.teams_processes == ["ms-teams.exe"]


def test_unknown_keys_warn_and_int_to_float():
    cfg = config_from_dict({"audio": {"gap_threshold_s": 1, "bogus": 3}, "nonsense": {"a": 1}})
    assert cfg.audio.gap_threshold_s == 1.0
    assert any("bogus" in w for w in cfg.warnings)
    assert any("nonsense" in w for w in cfg.warnings)


@pytest.mark.parametrize(
    "data",
    [
        {"audio": {"format": "mp3"}},
        {"transcription": {"device": "gpu"}},
        {"speakers": {"match_threshold": 1.5}},
        {"audio": {"keep_wav": "yes"}},
        {"detection": {"teams_processes": "ms-teams.exe"}},
    ],
)
def test_invalid_values(data):
    with pytest.raises(ConfigError):
        config_from_dict(data)


def test_consent_cannot_be_configured_away():
    cfg = config_from_dict(
        {
            "consent": {"skip": True},
            "general": {"skip_consent": True, "auto_consent": True},
            "audio": {"einwilligung_ueberspringen": True},
        }
    )
    assert len([w for w in cfg.warnings if "Einwilligung" in w]) == 4
    # no field anywhere in the config model mentions consent
    for section in dataclasses.fields(Config):
        value = getattr(cfg, section.name)
        if dataclasses.is_dataclass(value):
            for f in dataclasses.fields(value):
                assert "consent" not in f.name and "einwillig" not in f.name


def test_invalid_toml(tmp_path: Path):
    p = tmp_path / "bad.toml"
    p.write_text("[audio\nformat=", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_onedrive_detection(tmp_path: Path):
    od = tmp_path / "OneDrive - Firma"
    od.mkdir()
    env = {"OneDriveCommercial": str(od)}
    assert paths.cloud_sync_root_for(od / "Dokumente" / "Aufnahmen", env) == od.resolve()
    assert paths.cloud_sync_root_for(tmp_path / "Anrufaufnahmen", env) is None
