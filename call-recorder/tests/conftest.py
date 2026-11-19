from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from callrec.audio.capture import MIC_PART, REMOTE_PART  # noqa: E402
from callrec.audio.wavwriter import StreamingWavWriter  # noqa: E402
from callrec.consent import confirm_consent  # noqa: E402
from callrec.models import CalendarEvent, Person  # noqa: E402
from callrec.recording import Recording, new_meta  # noqa: E402

BERLIN = timezone(timedelta(hours=1))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("CALLREC_HOME", str(home))
    return home


def make_event(subject: str = "Projekt Jour fixe", start: datetime | None = None, minutes: int = 60, **kw) -> CalendarEvent:
    start = start or datetime(2026, 11, 19, 14, 0, tzinfo=BERLIN)
    return CalendarEvent(
        id=kw.pop("id", subject),
        subject=subject,
        start=start,
        end=start + timedelta(minutes=minutes),
        organizer=kw.pop("organizer", Person("Olga Organisatorin", "olga@firma.de", "organizer")),
        attendees=kw.pop("attendees", [Person("Anna Beispiel", "anna@firma.de"), Person("Bernd Muster", "bernd@firma.de")]),
        **kw,
    )


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with StreamingWavWriter(path, rate, 1) as w:
        w.write((np.clip(samples, -1, 1) * 32767).astype(np.int16))


def tone(freq: float, seconds: float, rate: int, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def make_recording(root: Path, title: str = "Projekt Jour fixe", seconds: float = 6.0, with_parts: bool = True) -> Recording:
    folder = root / "2026-11-19_1400_Projekt-Jour-fixe"
    folder.mkdir(parents=True)
    rec = Recording(folder)
    me = Person("Ich Selbst", "ich@firma.de", "self")
    participants = [
        Person("Olga Organisatorin", "olga@firma.de", "organizer"),
        Person("Anna Beispiel", "anna@firma.de"),
        Person("Bernd Muster", "bernd@firma.de"),
        Person("Ich Selbst", "ich@firma.de"),
    ]
    consent = confirm_consent(user_checked_box=True, confirmed_by=me.name, participants=participants, method="dialog")
    started = datetime(2026, 11, 19, 14, 0, tzinfo=BERLIN)
    meta = new_meta(
        title=title, event=None, participants=participants, self_person=me, consent=consent,
        devices={"mic": {"name": "Headset Mic"}, "loopback": {"name": "Headset [Loopback]"}}, started_at=started,
    )
    meta["recording"]["status"] = "stopped"
    meta["recording"]["duration_s"] = seconds
    rec.save_meta(meta)
    if with_parts:
        write_wav(folder / "audio" / MIC_PART, tone(220, seconds, 48000), 48000)
        write_wav(folder / "audio" / REMOTE_PART, tone(330, seconds, 44100), 44100)
    return rec
