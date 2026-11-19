from __future__ import annotations

import json
from pathlib import Path

import pytest

from callrec.handoff import (
    FolderTarget,
    GraphTarget,
    HandoffError,
    SummaryDocument,
    build_summary_document,
    build_target,
    hand_over,
    handoff_summary,
    last_handoff,
)
from callrec.models import AsrResult, AsrSegment, SpeakerInfo, Utterance, Word
from tests.conftest import make_recording

SECRET = "STRENG-GEHEIMER-TRANSKRIPT-SATZ"


def processed_recording(root: Path):
    rec = make_recording(root)  # has audio part files too
    rec.save_asr("mic", AsrResult("de", 3, [AsrSegment(0, 1, SECRET, [Word(0, 1, " " + SECRET)])]))
    rec.save_utterances([Utterance(0, 1, "ME", SECRET, "mic")])
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Ich Selbst")})
    rec.transcript_path.write_text(f"# Transkript\n\n**[00:00:00] Ich Selbst:** {SECRET}\n", encoding="utf-8")
    rec.summary_path.write_text(
        "# Zusammenfassung: Projekt Jour fixe\n\n_Automatisch lokal erstellt._\n\n## Kurzfassung\nRelease am 1.12.\n",
        encoding="utf-8",
    )
    rec.update_meta(["versions", "summary"], [{"version": 2, "file": "summary.v2.md", "fingerprint": "x"}])
    rec.update_meta(["processing", "summary"], {"model": "gemma3:12b"})
    return rec


def test_document_contains_only_summary_and_header(tmp_path: Path):
    rec = processed_recording(tmp_path / "recs")
    doc = build_summary_document(rec)
    md = doc.markdown
    assert doc.filename == "2026-11-19_1400_Projekt-Jour-fixe.md" and doc.source_file == "summary.md"
    assert md.startswith("# Projekt Jour fixe\n")
    assert "Donnerstag, 19.11.2026, 14:00" in md
    assert "Olga Organisatorin, Anna Beispiel" in md and "anna@firma.de" not in md
    assert "Einwilligung aller Teilnehmenden (bestätigt am" in md
    assert "Zusammenfassung v2 (erstellt mit gemma3:12b)" in md
    assert "## Kurzfassung\nRelease am 1.12." in md
    # nothing from transcript, utterances, ASR data, audio or meta.json
    assert SECRET not in md
    meta_raw = rec.meta_path.read_text(encoding="utf-8")
    assert '"devices"' in meta_raw and "devices" not in md and "Headset" not in md
    assert "RIFF" not in md and doc.data == md.encode("utf-8")
    assert "anna@firma.de" in build_summary_document(rec, include_emails=True).markdown


def test_structural_guarantee_only_summary_documents(tmp_path: Path):
    rec = processed_recording(tmp_path / "recs")
    target = FolderTarget(tmp_path)
    with pytest.raises(TypeError):
        SummaryDocument("x.md", "beliebiger Text", None, "summary.md")  # cannot be forged
    for not_a_summary in (rec.transcript_path, str(rec.transcript_path), rec.transcript_path.read_bytes(), rec, {"markdown": "x"}):
        with pytest.raises(TypeError):
            hand_over(not_a_summary, target)  # type: ignore[arg-type]

    class Sub(SummaryDocument):
        pass

    doc = build_summary_document(rec)
    with pytest.raises(TypeError):
        hand_over(object.__new__(Sub), target)  # type: ignore[arg-type]
    assert hand_over(doc, target)["file"] == doc.filename


def test_no_or_foreign_summary_rejected(tmp_path: Path):
    rec = make_recording(tmp_path / "recs", with_parts=False)
    with pytest.raises(HandoffError, match="noch keine Zusammenfassung"):
        build_summary_document(rec)
    rec.summary_path.write_text("# Transkript: versehentlich kopiert\n...", encoding="utf-8")
    with pytest.raises(HandoffError, match="Format"):
        build_summary_document(rec)


def test_folder_handoff_records_meta_and_allows_rehandoff(tmp_path: Path):
    rec = processed_recording(tmp_path / "recs")
    share = tmp_path / "OneDrive - Firma" / "Team - Protokolle"
    share.mkdir(parents=True)
    entry = handoff_summary(rec, FolderTarget(share))
    out = share / "2026-11-19_1400_Projekt-Jour-fixe.md"
    assert out.exists() and entry["location"] == str(out) and entry["mode"] == "folder"
    assert sorted(p.name for p in share.iterdir()) == [out.name]  # exactly one file, no temp leftovers
    assert SECRET not in out.read_text(encoding="utf-8")
    # summary regenerated -> hand over again, same file name is replaced
    rec.summary_path.write_text("# Zusammenfassung: Projekt Jour fixe\n\n## Kurzfassung\nNeu.\n", encoding="utf-8")
    handoff_summary(rec, FolderTarget(share))
    assert "Neu." in out.read_text(encoding="utf-8")
    history = rec.load_meta()["handoffs"]
    assert len(history) == 2 and history[0]["sha256"] != history[1]["sha256"]
    assert last_handoff(rec) == history[1]
    with pytest.raises(HandoffError):
        FolderTarget(tmp_path / "fehlt").put("a.md", b"x")
    with pytest.raises(HandoffError):
        FolderTarget(Path(""))


class FakeGraphSession:
    def __init__(self, status=201):
        self.calls = []
        self.status = status

    def put(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data, headers))

        class R:
            status_code = self.status
            text = "err"

            def json(self_inner):
                return {"webUrl": "https://firma.sharepoint.com/sites/Team/Protokolle/x.md"}

        return R()


def test_graph_target_upload(tmp_path: Path):
    rec = processed_recording(tmp_path / "recs")
    sess = FakeGraphSession()
    target = GraphTarget("b!abc123", "/Gesprächs protokolle/2026/", lambda: "TOKEN", session=sess)
    entry = handoff_summary(rec, target)
    url, data, headers = sess.calls[0]
    assert url == (
        "https://graph.microsoft.com/v1.0/drives/b!abc123/root:/Gespr%C3%A4chs%20protokolle/2026/"
        "2026-11-19_1400_Projekt-Jour-fixe.md:/content"
    )
    assert headers == {"Authorization": "Bearer TOKEN", "Content-Type": "text/plain"}
    assert SECRET.encode() not in data and data.startswith(b"# Projekt Jour fixe")
    assert entry["location"].startswith("https://firma.sharepoint.com/")
    with pytest.raises(HandoffError, match="403"):
        GraphTarget("d", "", lambda: "t", session=FakeGraphSession(403)).put("a.md", b"x")
    with pytest.raises(HandoffError):
        GraphTarget("", "", lambda: "t")


def test_graph_incremental_consent(tmp_path: Path):
    from callrec.calendar.graph import SCOPES, GraphAuth

    class App:
        def __init__(self):
            self.silent_scopes, self.interactive = [], []

        def get_accounts(self):
            return [{"username": "ich@firma.de"}]

        def acquire_token_silent(self, scopes, account):
            self.silent_scopes.append(scopes)
            return None  # not yet consented

        def acquire_token_interactive(self, scopes, login_hint=None, timeout=None, prompt=None):
            self.interactive.append((scopes, login_hint))
            return {"access_token": "files"}

    app = App()
    auth = GraphAuth("cid", "tid", tmp_path / "c.bin", app_factory=lambda: app)
    assert auth.token_for_scopes(["Sites.Selected"]) == "files"
    assert app.interactive == [(["Sites.Selected"], "ich@firma.de")]
    assert "Sites.Selected" not in SCOPES  # the normal calendar login never asks for file access


def test_build_target_from_config(tmp_path: Path):
    from callrec.config import Config

    cfg = Config()
    cfg.handoff.folder = str(tmp_path)
    assert isinstance(build_target(cfg), FolderTarget)
    cfg.handoff.mode = "graph"
    cfg.handoff.graph_drive_id = "b!x"
    assert isinstance(build_target(cfg, lambda: "t"), GraphTarget)
    json.dumps(cfg.handoff.__dict__)
