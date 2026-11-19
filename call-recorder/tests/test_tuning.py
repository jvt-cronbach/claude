from __future__ import annotations

from pathlib import Path

import pytest

from callrec import paths
from callrec.config import Config, ConfigError, list_presets, load_config, save_config, save_preset
from callrec.models import SpeakerInfo, Utterance
from callrec.transcript import fmt_ts, render_markdown
from callrec.userfiles import (
    apply_replacements,
    ensure_user_files,
    load_glossary,
    load_prompts,
    parse_glossary,
    parse_replacements,
    render_template,
)
from callrec.versions import list_versions
from tests.test_pipeline import make_pipeline, recording_with_voices


def test_user_files_created_once(isolated_home: Path):
    files = ensure_user_files()
    assert paths.glossary_file() in files and (paths.prompts_dir() / "summary_final.txt").exists()
    paths.glossary_file().write_text("Kubernetes\n", encoding="utf-8")
    ensure_user_files()  # never overwrites
    assert load_glossary() == ["Kubernetes"]
    p = load_prompts()
    assert "{material}" in p.final and "{chunk}" in p.chunk and p.fingerprint()


def test_glossary_and_replacements():
    assert parse_glossary("# Kommentar\nJour fixe\n\nKubernetes\nJour fixe\n") == ["Jour fixe", "Kubernetes"]
    rules, errors = parse_replacements(
        "# c\nKuba Netties => Kubernetes\nre: \\bSchur fix\\b => Jour fixe\nohne Pfeil\nre: ( => x\nTeams => MS\\Teams\n"
    )
    assert len(rules) == 3 and len(errors) == 2
    text = "Wir nutzen kuba netties im Schur fix. Teamsraum bleibt, Teams nicht."
    assert apply_replacements(text, rules) == "Wir nutzen Kubernetes im Jour fixe. Teamsraum bleibt, MS\\Teams nicht."


def test_render_template_safe():
    assert render_template("A {x} {y} {{z}}", {"x": "1"}) == "A 1 {y} {{z}}"
    assert render_template("ohne", {"material": "M"}, "material").endswith("\n\nM")


def test_timestamp_formats_and_no_merge():
    utts = [Utterance(65, 66, "S1", "Eins.", "remote"), Utterance(66.5, 67, "S1", "Zwei.", "remote")]
    sp = {"S1": SpeakerInfo("S1", "Sprecher 1", name="Anna")}
    assert fmt_ts(3725, "mm:ss") == "62:05" and fmt_ts(3725, "none") == ""
    md = render_markdown("T", None, 70, [], utts, sp, ts_format="mm:ss")
    assert "**[01:05] Anna:** Eins. Zwei." in md
    md2 = render_markdown("T", None, 70, [], utts, sp, ts_format="none", join_gap=None)
    assert "**Anna:** Eins." in md2 and "**Anna:** Zwei." in md2


def test_presets(isolated_home: Path):
    cfg = Config()
    cfg.transcription.model = "large-v3-turbo"
    cfg.transcription.beam_size = 8
    cfg.calendar.client_id = "darf-nicht-ins-preset"
    path = save_preset(cfg, "Schnell")
    assert "client_id" not in path.read_text(encoding="utf-8")
    assert list_presets() == ["Schnell"]
    base = Config()
    base.general.preset = "Schnell"
    save_config(base)
    loaded = load_config()
    assert loaded.active_preset == "Schnell" and loaded.transcription.beam_size == 8
    assert loaded.calendar.client_id == ""
    assert load_config(preset="").transcription.beam_size == 5  # explicit "no preset"
    path.write_text('[calendar]\nclient_id = "x"\n[transcription]\nbeam_size = 3\n', encoding="utf-8")
    c = load_config()
    assert c.transcription.beam_size == 3 and c.calendar.client_id == ""
    assert any("nicht erlaubt" in w for w in c.warnings)
    with pytest.raises(ConfigError):
        load_config(preset="gibt-es-nicht")
    with pytest.raises(ConfigError):
        save_preset(cfg, "a/b")


def test_versioned_outputs_and_settings_in_meta(tmp_path: Path):
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from callrec.userfiles import parse_replacements

    profiles = ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline, tr, _ = make_pipeline(profiles)
    pipeline.run(rec)
    v = list_versions(rec, "transcript")
    assert [x["version"] for x in v] == [1] and (rec.folder / "transcript.v1.md").exists()
    assert v[0]["settings"]["transcription"]["model"] == "large-v3"
    assert v[0]["settings"]["rendering"]["timestamp_format"] == "hh:mm:ss"
    assert "**Version:** v1" in rec.transcript_path.read_text(encoding="utf-8")
    assert [x["version"] for x in list_versions(rec, "summary")] == [1]

    # re-render with unchanged settings (e.g. after a speaker assignment) -> same version
    pipeline.run(rec, {"render"})
    assert len(list_versions(rec, "transcript")) == 1

    # change the tuning: glossary, prompt, replacements, rendering, new model -> retranscribe
    cfg = Config()
    cfg.transcription.model = "medium"
    cfg.transcription.initial_prompt = "Projektbesprechung Firma Muster."
    cfg.transcript.timestamp_format = "mm:ss"
    cfg.active_preset = "Test"
    pipeline2, tr2, _ = make_pipeline(profiles, cfg=cfg)
    pipeline2.c.glossary = ["Kubernetes", "Jour fixe"]
    pipeline2.c.replacements = parse_replacements("zweite Stimme => 2. Stimme")[0]
    result = pipeline2.run(rec, {"transcribe", "identify", "render", "summarize"})
    assert tr2.calls[0][1] == "Projektbesprechung Firma Muster. Gespräch mit Ich Selbst, Olga Organisatorin, Anna Beispiel, Bernd Muster."
    assert tr2.hotwords == "Kubernetes, Jour fixe"
    versions = list_versions(rec, "transcript")
    assert [x["version"] for x in versions] == [1, 2]
    assert any("v2" in m for m in result.messages)
    old = (rec.folder / "transcript.v1.md").read_text(encoding="utf-8")
    new = (rec.folder / "transcript.v2.md").read_text(encoding="utf-8")
    assert "[00:00:03] Sprecher 2:** Und zweite Stimme." in old
    assert "[00:03] Sprecher 2:** Und 2. Stimme." in new and new == rec.transcript_path.read_text(encoding="utf-8")
    s2 = versions[1]["settings"]
    assert s2["transcription"]["model"] == "medium" and s2["transcription"]["hotwords"] == "Kubernetes, Jour fixe"
    assert s2["rendering"]["replacements"] == ["zweite Stimme => 2. Stimme"] and s2["preset"] == "Test"
    assert list((rec.data_dir / "archive").glob("asr_mic.*.json"))  # previous ASR kept
    sv = list_versions(rec, "summary")
    assert [x["version"] for x in sv] == [1, 2] and sv[1]["settings"]["transcript_version"] == 2
    assert (rec.folder / "summary.v1.md").exists() and (rec.folder / "summary.v2.md").exists()


def test_worker_receives_tuning_arguments(tmp_path: Path):
    from callrec.asr import worker
    from callrec.asr.transcriber import WhisperSubprocessTranscriber
    from callrec.config import TranscriptionConfig

    cfg = TranscriptionConfig(vad_threshold=0.35, vad_min_silence_ms=800, vad_speech_pad_ms=200,
                              hallucination_silence_threshold=2.0, beam_size=7)
    cmd = WhisperSubprocessTranscriber(cfg, tmp_path).build_command(Path("a.flac"), Path("o.json"), "cpu", "P", "Kubernetes, Jour fixe")
    a = worker.build_parser().parse_args(cmd[3:])
    assert (a.vad_threshold, a.vad_min_silence_ms, a.vad_speech_pad_ms) == (0.35, 800, 200)
    assert a.hotwords == "Kubernetes, Jour fixe" and a.hallucination_silence_threshold == 2.0 and a.beam_size == 7
