"""Calendar data for naming and speaker identification: setup walkthrough, preview, assigning an
event to an existing recording, and that the invitees really are the prioritised candidates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from callrec import setupcheck
from callrec.calendar.selection import events_for_recording
from callrec.calendar.service import CalendarService
from callrec.config import Config
from callrec.library import assign_event
from callrec.models import Person
from callrec.review import name_choices
from tests.conftest import BERLIN, make_event, make_recording

START = datetime(2026, 11, 19, 14, 0, tzinfo=BERLIN)


def test_events_for_recording_ranks_by_overlap():
    long_block = make_event("Blocker", START - timedelta(hours=1), minutes=240, id="b")
    call = make_event("Projekt", START + timedelta(minutes=2), minutes=45, is_online=True, id="p")
    other = make_event("Später", START + timedelta(hours=3), id="s")
    cancelled = make_event("Abgesagt", START, is_cancelled=True, id="c")
    found = events_for_recording([other, long_block, cancelled, call], START, START + timedelta(minutes=40))
    # the call, not the 4-h blocker that also covers it; the other events of the day follow, cancelled last
    assert [e.id for e in found] == ["p", "b", "s", "c"]
    assert events_for_recording([other], START, START + timedelta(minutes=30)) == [other]
    with pytest.raises(ValueError):
        events_for_recording([], START.replace(tzinfo=None), START)


def _service(tmp_path: Path, events, now):
    class Src:
        def calendar_view(self, a, b):
            return [e for e in events if e.end >= a and e.start <= b]

        def get_me(self):
            raise NotImplementedError

    return CalendarService(Src(), tmp_path / "c.json", timedelta(minutes=10), timedelta(minutes=10), clock=lambda: now)


def test_preview_shows_current_or_next_event_with_participants(tmp_path: Path):
    ev = make_event("Jour fixe", START, is_online=True)
    svc = _service(tmp_path, [ev], START - timedelta(hours=2))
    got, text = svc.preview()
    assert got is ev and text.startswith("Nächster Termin: Jour fixe") and "Anna Beispiel" in text
    got, text = _service(tmp_path, [ev], START + timedelta(minutes=5)).preview()
    assert text.startswith("Aktueller Termin") and "Teams" in text
    assert _service(tmp_path, [], START).preview()[0] is None


def test_setup_microsoft_walkthrough_and_result():
    cfg = Config()
    item = setupcheck.check_microsoft(cfg, lambda: None)
    assert item.state == setupcheck.OPTIONAL and "1. entra.microsoft.com" in item.detail and "Calendars.Read" in item.detail
    cfg.calendar.client_id = "abc"
    item = setupcheck.check_microsoft(cfg, lambda: None)
    assert ("Bei Microsoft anmelden …", setupcheck.A_LOGIN) in item.actions and "nächste Termin" in item.detail
    item = setupcheck.check_microsoft(cfg, lambda: "ich@firma.de", lambda: "Nächster Termin: X\nTeilnehmende: A, B")
    assert item.state == setupcheck.OK and "Teilnehmende: A, B" in item.detail

    def broken():
        raise RuntimeError("403")

    item = setupcheck.check_microsoft(cfg, lambda: "ich@firma.de", broken)
    assert item.state == setupcheck.WARN and "403" in item.detail


def test_assign_event_sets_participants_for_matching_and_dropdown(tmp_path: Path):
    rec = make_recording(tmp_path, title="Anruf", with_parts=False)
    rec.update_meta(["participants"], [Person("Hand Eingetragen", "").to_dict()])
    ev = make_event("Kunde Müller", START, attendees=[Person("Karl Kunde", "karl@kunde.de")])
    msgs = assign_event(rec, ev, take_title=True)
    meta = rec.load_meta()
    assert meta["event"]["subject"] == "Kunde Müller" and meta["event_assigned"]["by"] == "user"
    names = [p.name for p in rec.participants()]
    assert names[:2] == ["Olga Organisatorin", "Karl Kunde"] and "Hand Eingetragen" in names
    assert rec.title() == "Kunde Müller" and rec.folder.name.endswith("Projekt-Jour-fixe")  # folder id stays
    assert any("Titel geändert" in m for m in msgs)

    class NoProfiles:
        def all_profiles(self):
            return []

    choices = name_choices(rec, NoProfiles())  # type: ignore[arg-type]
    assert choices[:2] == ["Olga Organisatorin", "Karl Kunde"]


def test_recording_is_named_after_the_event(tmp_path: Path):
    from callrec.calendar.service import Suggestion
    from tests.test_controller import FakeUi, _controller, _decision

    ev = make_event("Projekt Jour fixe", datetime(2026, 11, 19, 13, 0, tzinfo=UTC), is_online=True)
    ui = FakeUi(_decision)
    ctl, _ = _controller(ui, tmp_path, [ev])
    ctl.clock = lambda: datetime.now(UTC)
    assert ctl.start(Suggestion(ev, ev.participants(), source="graph"))
    assert ctl.current is not None and ctl.current.folder.name.endswith("_Projekt-Jour-fixe")
    assert [p.name for p in ctl.current.participants()][0] == "Olga Organisatorin"
    ctl.stop()
