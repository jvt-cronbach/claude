from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

from callrec.calendar.graph import (
    SCOPES,
    GraphAuth,
    GraphClient,
    GraphError,
    NotSignedIn,
    parse_graph_datetime,
    parse_graph_event,
)
from callrec.calendar.service import CalendarService

ITEM = {
    "id": "AAMk1",
    "subject": "Projekt Jour fixe",
    "start": {"dateTime": "2026-11-19T13:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-11-19T14:00:00.0000000", "timeZone": "UTC"},
    "isAllDay": False,
    "isCancelled": False,
    "isOnlineMeeting": True,
    "onlineMeetingProvider": "teamsForBusiness",
    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc"},
    "responseStatus": {"response": "accepted"},
    "showAs": "busy",
    "organizer": {"emailAddress": {"name": "Olga Organisatorin", "address": "olga@firma.de"}},
    "attendees": [
        {"type": "required", "emailAddress": {"name": "Anna Beispiel", "address": "anna@firma.de"}},
        {"type": "resource", "emailAddress": {"name": "Raum 4.12", "address": "raum412@firma.de"}},
        {"type": "optional", "emailAddress": {"name": "", "address": "extern@partner.de"}},
    ],
}


def test_parse_datetime():
    dt = parse_graph_datetime({"dateTime": "2026-11-19T13:00:00.1234567", "timeZone": "UTC"})
    assert dt == datetime(2026, 11, 19, 13, 0, 0, 123456, tzinfo=UTC)
    # Prefer header not honoured: Windows zone names are converted instead of dropping the event (DST aware)
    summer = parse_graph_datetime({"dateTime": "2026-07-01T13:00:00", "timeZone": "W. Europe Standard Time"})
    winter = parse_graph_datetime({"dateTime": "2026-11-19T13:00:00", "timeZone": "Europe/Berlin"})
    assert summer.astimezone(UTC).hour == 11 and winter.astimezone(UTC).hour == 12
    assert parse_graph_datetime({"dateTime": "2026-11-19T13:00:00", "timeZone": "Mars Time"}).tzinfo == UTC


def test_parse_event():
    ev = parse_graph_event(ITEM)
    assert ev.is_online and ev.join_url
    assert [p.name for p in ev.attendees] == ["Anna Beispiel", "extern@partner.de"]  # room skipped
    assert ev.participants()[0].name == "Olga Organisatorin"
    declined = parse_graph_event({**ITEM, "responseStatus": {"response": "declined"}})
    assert declined.declined


class FakeResp:
    def __init__(self, status: int, data: Any) -> None:
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self) -> Any:
        return self._data


class FakeSession:
    def __init__(self, responses: list[FakeResp]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Any, dict[str, str]]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_calendar_view_pagination_and_headers():
    page2 = {"value": [{**ITEM, "id": "2", "subject": "Zweiter"}]}
    sess = FakeSession([FakeResp(200, {"value": [ITEM], "@odata.nextLink": "https://graph/next"}), FakeResp(200, page2)])
    client = GraphClient(lambda: "tok", session=sess)  # type: ignore[arg-type]
    start = datetime(2026, 11, 19, 12, tzinfo=UTC)
    events = client.calendar_view(start, start + timedelta(hours=4))
    assert [e.subject for e in events] == ["Projekt Jour fixe", "Zweiter"]
    url, params, headers = sess.calls[0]
    assert url.endswith("/me/calendarView")
    assert params["startDateTime"] == "2026-11-19T12:00:00Z"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Prefer"] == 'outlook.timezone="UTC"'
    assert sess.calls[1][0] == "https://graph/next" and sess.calls[1][1] is None


def test_errors():
    with pytest.raises(NotSignedIn):
        GraphClient(lambda: None).get_me()
    with pytest.raises(NotSignedIn):
        GraphClient(lambda: "t", session=FakeSession([FakeResp(401, {})])).get_me()  # type: ignore[arg-type]
    with pytest.raises(GraphError):
        GraphClient(lambda: "t", session=FakeSession([requests.ConnectionError("offline")])).get_me()  # type: ignore[arg-type,list-item]


def test_me():
    sess = FakeSession([FakeResp(200, {"displayName": "Ich Selbst", "mail": None, "userPrincipalName": "ich@firma.de"})])
    me = GraphClient(lambda: "t", session=sess).get_me()  # type: ignore[arg-type]
    assert me.name == "Ich Selbst" and me.email == "ich@firma.de"


class FakeMsalApp:
    def __init__(self, accounts: list[dict[str, str]], silent: dict[str, str] | None) -> None:
        self.accounts = accounts
        self.silent = silent
        self.interactive_calls: list[Any] = []

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        assert scopes == SCOPES
        return self.silent

    def acquire_token_interactive(self, scopes, prompt=None, timeout=None):
        self.interactive_calls.append((scopes, prompt))
        return {"access_token": "interactive"}

    def remove_account(self, acc):
        self.accounts.remove(acc)


def test_auth_silent_and_interactive(tmp_path: Path):
    app = FakeMsalApp([{"username": "ich@firma.de"}], {"access_token": "silent"})
    auth = GraphAuth("cid", "tid", tmp_path / "cache.bin", app_factory=lambda: app)
    assert auth.authority == "https://login.microsoftonline.com/tid"
    assert auth.token_silent() == "silent"
    assert auth.account_username() == "ich@firma.de"
    assert auth.login_interactive() == "interactive"
    assert app.interactive_calls == [(SCOPES, "select_account")]
    auth.logout()
    assert auth.token_silent() is None
    assert SCOPES == ["Calendars.Read", "User.Read"]
    with pytest.raises(NotSignedIn):
        GraphAuth("", "tid", tmp_path / "c")


class FakeSource:
    def __init__(self, events=None, fail=False):
        self.events = events or []
        self.fail = fail

    def calendar_view(self, start, end):
        if self.fail:
            raise GraphError("offline")
        return self.events

    def get_me(self):  # pragma: no cover
        raise NotImplementedError


def test_service_uses_cache_when_offline(tmp_path: Path):
    now = datetime(2026, 11, 19, 13, 5, tzinfo=UTC)
    ev = parse_graph_event(ITEM)
    cache = tmp_path / "cache.json"
    online = CalendarService(FakeSource([ev]), cache, timedelta(minutes=10), timedelta(minutes=10), clock=lambda: now)
    s = online.suggest()
    assert s.event is not None and s.source == "graph" and cache.exists()
    offline = CalendarService(FakeSource(fail=True), cache, timedelta(minutes=10), timedelta(minutes=10), clock=lambda: now)
    s2 = offline.suggest()
    assert s2.source == "cache" and s2.event is not None and s2.event.subject == "Projekt Jour fixe"
    assert "nicht verfügbar" in s2.message
    nothing = CalendarService(None, tmp_path / "none.json", timedelta(minutes=10), timedelta(minutes=10), clock=lambda: now)
    s3 = nothing.suggest()
    assert s3.event is None and s3.source == "manual"
    assert offline.upcoming_online_meetings()[0].subject == "Projekt Jour fixe"
