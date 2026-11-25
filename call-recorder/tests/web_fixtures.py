"""Realistic recordings and the DTOs the web UI gets for them - built with the REAL dto/viewmodel
functions, written to JSON for the Playwright tests (tests/web/fake_bridge.js loads them).

Run ``python tests/web_fixtures.py <out-dir>`` to (re)write the files by hand; the test suite
builds them in a temp folder on every run, so a DTO change shows up in the UI tests at once.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from callrec.calendar.service import Suggestion  # noqa: E402
from callrec.consent import confirm_consent  # noqa: E402
from callrec.models import AsrResult, AsrSegment, Person, SpeakerInfo, Word  # noqa: E402
from callrec.recording import Recording, new_meta  # noqa: E402
from tests.conftest import BERLIN, make_event  # noqa: E402

HOSTILE = '<img src=x onerror="window.__xss=1">'
HOSTILE_MD = '<script>window.__xss=2</script> **<b onmouseover="window.__xss=3">fett</b>** [Link](javascript:window.__xss=4)'


def _rec(root: Path, name: str, title: str, start: datetime, minutes: float, participants: list[Person],
         status: str = "stopped", processing: dict[str, Any] | None = None) -> Recording:
    folder = root / name
    folder.mkdir(parents=True)
    rec = Recording(folder)
    me = Person("Martina Ehlers", "m.ehlers@nordhafen.example", "self")
    consent = confirm_consent(user_checked_box=True, confirmed_by=me.name, participants=participants, method="dialog")
    meta = new_meta(title=title, event=None, participants=participants, self_person=me, consent=consent,
                    devices={"mic": {"name": "Headset"}, "loopback": {"name": "Headset"}}, started_at=start)
    meta["recording"]["status"] = status
    meta["recording"]["duration_s"] = minutes * 60
    if processing:
        meta["processing"] = processing
    rec.save_meta(meta)
    return rec


SUMMARY = """# Zusammenfassung: Jour fixe Neubau Hafenkante

## Kurzfassung
Der Rohbau von Haus B liegt zwei Wochen hinter Plan, weil der Stahl erst am 2. Dezember geliefert wird.

## Entscheidungen
- Die Fassade von Haus A wird vorgezogen; Beginn am 7. Dezember.
- Winterbaumaßnahmen werden beauftragt.

## Aufgaben
| Aufgabe | Verantwortlich | Frist |
|---|---|---|
| Angebot Winterbau | Jonas Pfeiffer | **Di 24.11.** |
| Kostenaufteilung klären | Martina Ehlers | offen |

## Projektstand und Updates
1. Stahl: Lieferung 2. Dezember
2. Fassade Haus A: vorgezogen

## Offene Fragen
> Wer trägt die Mehrkosten?

## Nächste Termine
- Jour fixe am 27.11., 14:00 Uhr
"""


def build(root: Path) -> dict[str, Any]:
    """Recordings in ``root`` + the matching DTOs (dict name -> JSON-able)."""
    from callrec.library import RecordingIndex
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from callrec.ui import dto, settingsmodel
    from callrec.ui.startflow import start_texts
    from callrec.ui.webui import start_dialog_data

    now = datetime.now(BERLIN).replace(second=0, microsecond=0)
    people = [Person("Jonas Pfeiffer", "jp@kessler.example"), Person("Leonie Hartwig", "lh@kessler.example"),
              Person("Selin Aydın", "sa@aydin.example"), Person("Paul Wiesner", "pw@nordhafen.example")]
    # 1) finished, handed over, changed since - with a 2-hour transcript
    r1 = _rec(root, "2026-11-13_1400_Jour-fixe", "Jour fixe Neubau Hafenkante", now - timedelta(days=1, hours=2), 118,
              people, processing={"status": "done", "speakers": {"unassigned": 0, "spoken": 4},
                                  "transcription": {"mic": {"language": "de"}, "remote": {"language": "de"}}})
    r1.summary_path.write_text(SUMMARY, encoding="utf-8")
    lines = ["# Transkript: Jour fixe Neubau Hafenkante", "", "- **Dauer:** 01:58:00", "", "---", ""]
    names = ["Martina Ehlers", "Jonas Pfeiffer", "Leonie Hartwig", "Selin Aydın", "Paul Wiesner"]
    for i in range(2400):
        t = i * 3
        lines += [f"**[{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}] {names[i % 5]}:** Beitrag Nummer {i + 1} – "
                  "die Lieferung kommt am 2. Dezember, dann geht es mit Haus B weiter.", ""]
    lines += [f"**[02:00:00] Paul Wiesner:** {HOSTILE} {HOSTILE_MD}", ""]
    r1.transcript_path.write_text("\n".join(lines), encoding="utf-8")
    r1.update_meta(["handoffs"], [{"at": (now - timedelta(days=1)).isoformat(), "location": "SharePoint › Protokolle",
                                   "sha256": "0" * 64, "summary_version": 2}])
    # 2) speakers open - for the review dialog
    r2 = _rec(root, "2026-11-20_1100_Kundengespraech", "Kundengespräch Lindqvist Logistik", now - timedelta(hours=3), 52,
              [Person("Henrik Lindqvist", "hl@lindqvist.example"), Person("Carla Neumann", "cn@lindqvist.example")],
              processing={"status": "done", "speakers": {"unassigned": 2, "spoken": 2},
                          "identification": {"trackcheck": {"warnings": []}}})
    seg = AsrResult("de", 30, [AsrSegment(0, 6, "Wir haben die Touren geplant",
                                          [Word(0, 6, " Wir haben die Touren für Januar geplant.")])])
    r2.save_asr("mic", seg)
    r2.save_asr("remote", AsrResult("de", 30, [
        AsrSegment(0, 9, "x", [Word(0, 9, " Das Angebot für die Halle liegt seit letzter Woche vor.")]),
        AsrSegment(12, 20, "y", [Word(12, 20, f" Für uns zählt die Rampe. {HOSTILE}")])]))
    r2.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Martina Ehlers", source="self"),
                      "S1": SpeakerInfo("S1", "Sprecher 1"), "S2": SpeakerInfo("S2", "Sprecher 2")})
    r2.turns_path.write_text('[{"start": 0, "end": 9, "speaker": "S1"}, {"start": 12, "end": 20, "speaker": "S2"}]',
                             encoding="utf-8")
    r2.save_embeddings({"S1": np.ones(8, dtype=np.float32), "S2": np.ones(8, dtype=np.float32)}, "m")
    from tests.test_pipeline import make_pipeline

    store = ProfileStore(PlainFilePersistence(root.parent / "vp.json"))
    pipeline = make_pipeline(store)[0]
    pipeline.run(r2, {"identify", "render"})
    r2.update_meta(["processing", "speakers"], {"unassigned": 2, "spoken": 2})
    # 3) not processed ("Später")
    _rec(root, "2026-11-20_0900_Vertriebsrunde", "Vertriebsrunde Nord", now - timedelta(hours=5), 31, people[:2],
         processing={"status": "deferred"})
    # 4) summary failed (VPN)
    r4 = _rec(root, "2026-11-19_1600_Kessler", "Abstimmung Kessler Bau – Nachträge", now - timedelta(days=1), 44, people[:3],
              processing={"status": "done", "speakers": {"unassigned": 0, "spoken": 3},
                          "summary": {"skipped": "keine Verbindung zu Anthropic (Zeitüberschreitung nach 20 s) – VPN?"}})
    r4.transcript_path.write_text("# Transkript\n\n**[00:00:01] Jonas Pfeiffer:** Hallo.\n", encoding="utf-8")
    # 5) hostile title / participants, very long title
    r5 = _rec(root, "2026-11-18_1000_Hostile", HOSTILE + " Ein sehr langer Titel für eine Besprechung mit vielen "
              "Beteiligten aus mehreren Unternehmen und Abteilungen, der kaum in eine Zeile passt",
              now - timedelta(days=2), 12, [Person('"><svg onload="window.__xss=5">', "x@y.example")],
              processing={"status": "done", "speakers": {"unassigned": 0, "spoken": 1}})
    r5.summary_path.write_text(f"# Zusammenfassung: x\n\n## Kurzfassung\n{HOSTILE_MD}\n\n- {HOSTILE}\n", encoding="utf-8")

    index = RecordingIndex(root)
    infos = index.scan()
    out: dict[str, Any] = {}
    out["list"] = dto.recordings_list(infos, "all", "", set(), "")
    out["home"] = dto.home(infos, set(), "", None)
    out["detail"] = {i.id: dto.recording_detail(i, False, False, True) for i in infos}
    out["text"] = {}
    for i in infos:
        for kind in ("summary", "transcript"):
            try:
                out["text"][f"{i.id}|{kind}"] = dto.read_text(i.folder, kind, "")
            except (OSError, ValueError):
                pass
    ev_now = make_event("Jour fixe Neubau Hafenkante", start=now - timedelta(minutes=5), minutes=45, is_online=True,
                        attendees=people)
    ev_next = make_event("Rückruf Steuerberatung Krämer", start=now + timedelta(minutes=50), minutes=30)
    ev_past = make_event("Vertriebsrunde Nord", start=now - timedelta(hours=5), minutes=60, is_online=True)
    events = [ev_now, ev_next, ev_past]
    out["calendar"] = {**dto.calendar_events(events, "graph", "", lambda e: e is ev_now, now), "connected": True}
    out["calendar_none"] = {"events": [], "source": "manual", "connected": False,
                            "message": "Kein Microsoft-Kalender verbunden (optional) – Titel und Teilnehmende beim Start eingeben."}
    me = Person("Martina Ehlers", "m.ehlers@nordhafen.example", "self")
    sugg = Suggestion(ev_now, ev_now.participants(), source="graph")
    out["start_ask"] = start_dialog_data(start_texts("ask", "ollama"), me, sugg)
    out["start_ask_cloud"] = start_dialog_data(start_texts("ask", "anthropic"), me, sugg)
    out["start_confirm"] = start_dialog_data(start_texts("confirm", "ollama"), me, Suggestion(None, [], "", "manual"))
    from callrec.config import Config as _Config
    from callrec.postcall import default_plan

    # the plan the real question starts from (decision: hand-over box ticked by default)
    out["after_recording"] = dto.after_recording_data(r1, default_plan(_Config()), "", "")
    out["review"] = dto.speaker_review(r2, store, None, in_chain=False, can_handoff=True, can_allow_more=True)
    for row in [out["review"]["me"], *out["review"]["rows"]]:
        row.pop("_span", None)
        row.pop("_track", None)
    # the same dialog with a pre-selected suggestion for the first voice ("Alle Vorschläge übernehmen")
    import copy as _copy

    sug = _copy.deepcopy(out["review"])
    first = sug["rows"][0]
    first["preselect"] = first["initial"] = first["choices"][0]["value"]
    first["preselect_score"] = 0.61
    sug["suggestions"] = 1
    out["review_suggest"] = sug
    from callrec.config import Config
    from callrec.handoff import HANDOFF_WARNING
    from callrec.ui.viewmodel import markdown_blocks

    out["handoff"] = {"title": r1.title(), "warning": HANDOFF_WARNING, "target": "C:\\Users\\x\\SharePoint\\Protokolle",
                      "filename": "2026-11-13_1400_Jour-fixe.md", "previous": None, "blocks": markdown_blocks(SUMMARY)}
    out["settings"] = {"pages": settingsmodel.schema(Config()), "mics": ["Headset Mikrofon (USB Audio)"],
                       "outs": ["Headset Kopfhörer (USB Audio)"], "presets": ["Schnell"],
                       "autostart": {"state": "off", "detail": "Startet nicht mit Windows.", "on": False},
                       "apikey": {"present": False, "masked": "", "source": ""},
                       "anthropic_models": [["claude-opus-5-5", "Opus – Standard"]], "remote_allowed": False,
                       "paths": {"presets": "C:\\presets", "prompts": "C:\\prompts"},
                       "speed": {"plan": "Whisper rechnet mit 2 parallelen Spuren à 4 Threads (automatisch, 8 physische Kerne).",
                                 "lines": ["Noch nicht gemessen."], "power": "Akkubetrieb – Windows drosselt den Prozessor.",
                                 "measured": False, "running": False}}
    from callrec.asr.profiles import DESCRIPTIONS, LABELS, PROFILE_NAMES

    out["retranscribe"] = {"title": "Jour fixe", "presets": ["Schnell"],
                           "profiles": [{"key": k, "label": LABELS[k], "help": DESCRIPTIONS[k]} for k in PROFILE_NAMES]}
    from callrec.setupcheck import CheckItem

    out["checks"] = dto.checks([
        CheckItem("audio", "Audiogeräte", "ok", "Mikrofon: Headset · Gegenseite: Headset"),
        CheckItem("whisper", "Whisper-Modell", "ok", "large-v3-turbo geladen."),
        CheckItem("summary", "Zusammenfassung", "warn", "Verbindung nicht getestet.", True, [("Verbindung testen", "test_summary")]),
        CheckItem("microsoft", "Microsoft", "optional", "Nicht angemeldet.", False, [("Bei Microsoft anmelden …", "login")]),
    ])
    for i in infos:
        out["detail"][i.id]["_title_raw"] = i.title
    return out


STATE_IDLE = {"recording": {"on": False, "title": "", "elapsed": 0, "id": "", "levels": {}},
              "autostop": {"status": "", "message": "", "view": None}, "queue": {"current": None, "pending": []},
              "status": "", "setup": {"ready": False, "text": "1 Punkt offen", "open": 1}, "me": "Martina Ehlers"}


def write(out_dir: Path, data: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "fixtures.json"
    p.write_text(json.dumps({**data, "state_idle": STATE_IDLE}, ensure_ascii=False, default=str), encoding="utf-8")
    return p


if __name__ == "__main__":  # pragma: no cover
    import os
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    os.environ["CALLREC_HOME"] = str(tmp / "home")
    print(write(Path(sys.argv[1]), build(tmp / "recs")))
