"""Microsoft Graph access (delegated Calendars.Read + User.Read, MSAL public client).

The token cache is persisted with msal-extensions; on Windows that is
``FilePersistenceWithDataProtection`` (DPAPI, bound to the Windows user).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from callrec.models import CalendarEvent, Person

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Calendars.Read", "User.Read"]
_EVENT_FIELDS = (
    "id,subject,start,end,organizer,attendees,isAllDay,isCancelled,isOnlineMeeting,"
    "onlineMeetingProvider,onlineMeeting,responseStatus,showAs"
)


class GraphError(RuntimeError):
    pass


class NotSignedIn(GraphError):
    pass


def build_persistence(location: Path) -> Any:
    """DPAPI-protected file on Windows; plain file elsewhere (development only)."""
    from msal_extensions import FilePersistence

    location.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from msal_extensions import FilePersistenceWithDataProtection

        return FilePersistenceWithDataProtection(str(location))
    log.warning("Nicht-Windows-System: Token-Cache wird UNVERSCHLÜSSELT gespeichert (%s)", location)
    return FilePersistence(str(location))


class GraphAuth:
    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        cache_path: Path,
        app_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not client_id:
            raise NotSignedIn("Keine Client-ID konfiguriert (calendar.client_id).")
        self.client_id = client_id
        self.authority = f"https://login.microsoftonline.com/{tenant_id or 'organizations'}"
        self.cache_path = cache_path
        self._app_factory = app_factory
        self._app: Any = None

    def _get_app(self) -> Any:
        if self._app is None:
            if self._app_factory is not None:
                self._app = self._app_factory()
            else:
                import msal
                from msal_extensions import PersistedTokenCache

                cache = PersistedTokenCache(build_persistence(self.cache_path))
                self._app = msal.PublicClientApplication(
                    self.client_id, authority=self.authority, token_cache=cache
                )
        return self._app

    def token_silent(self) -> str | None:
        """Return an access token without user interaction, or None."""
        app = self._get_app()
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return str(result["access_token"])
        if result and "error" in result:
            log.info("Stille Anmeldung fehlgeschlagen: %s", result.get("error_description", result["error"]))
        return None

    def login_interactive(self, timeout: int = 300) -> str:
        """Open the system browser for sign-in (redirect URI http://localhost)."""
        app = self._get_app()
        result = app.acquire_token_interactive(SCOPES, prompt="select_account", timeout=timeout)
        if "access_token" not in result:
            raise GraphError(
                "Anmeldung fehlgeschlagen: " + str(result.get("error_description") or result.get("error"))
            )
        return str(result["access_token"])

    def token_for_scopes(self, scopes: list[str], interactive: bool = True) -> str:
        """Incremental consent: request an additional scope (e.g. Sites.Selected) only when needed.

        Used for the optional SharePoint upload; the calendar login keeps requesting
        only Calendars.Read + User.Read.
        """
        app = self._get_app()
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(scopes, account=accounts[0])
            if result and "access_token" in result:
                return str(result["access_token"])
        if not interactive:
            raise NotSignedIn("Für den Upload ist eine zusätzliche Zustimmung nötig.")
        hint = str(accounts[0].get("username")) if accounts else None
        result = app.acquire_token_interactive(scopes, login_hint=hint, timeout=300)
        if "access_token" not in result:
            raise GraphError(
                "Zustimmung/Anmeldung fehlgeschlagen: " + str(result.get("error_description") or result.get("error"))
            )
        return str(result["access_token"])

    def account_username(self) -> str | None:
        accounts = self._get_app().get_accounts()
        return str(accounts[0].get("username")) if accounts else None

    def logout(self) -> None:
        app = self._get_app()
        for acc in app.get_accounts():
            app.remove_account(acc)


def parse_graph_datetime(obj: dict[str, Any]) -> datetime:
    """Graph returns {"dateTime": "2026-11-19T13:00:00.0000000", "timeZone": "UTC"}."""
    raw = str(obj["dateTime"])
    # Python's fromisoformat accepts at most 6 fractional digits on 3.11
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    tz = str(obj.get("timeZone", "UTC"))
    if dt.tzinfo is None:
        if tz.upper() in ("UTC", "Z", "COORDINATED UNIVERSAL TIME", "ETC/UTC"):
            return dt.replace(tzinfo=UTC)
        zone = _zone(tz)
        if zone is None:
            # dropping the event would make the meeting vanish from the picker; keep it, assume UTC
            log.warning("Unbekannte Zeitzone %r in einem Termin (Prefer-Header nicht beachtet?) - als UTC gelesen", tz)
            return dt.replace(tzinfo=UTC)
        dt = dt.replace(tzinfo=zone)
    return dt


# Windows time-zone names Graph may return when the Prefer header is not honoured (most common ones)
_WINDOWS_ZONES = {
    "w. europe standard time": "Europe/Berlin", "central europe standard time": "Europe/Budapest",
    "romance standard time": "Europe/Paris", "central european standard time": "Europe/Warsaw",
    "gmt standard time": "Europe/London", "greenwich standard time": "Atlantic/Reykjavik",
    "e. europe standard time": "Europe/Chisinau", "fle standard time": "Europe/Kiev",
    "eastern standard time": "America/New_York", "pacific standard time": "America/Los_Angeles",
    "central standard time": "America/Chicago", "mountain standard time": "America/Denver",
    "india standard time": "Asia/Kolkata", "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo", "aus eastern standard time": "Australia/Sydney",
}


def _zone(name: str) -> Any:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    for candidate in (_WINDOWS_ZONES.get(name.strip().lower()), name.strip()):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def _email_person(obj: dict[str, Any] | None, role: str) -> Person | None:
    if not obj:
        return None
    ea = obj.get("emailAddress") or {}
    name = str(ea.get("name") or "").strip()
    email = str(ea.get("address") or "").strip()
    if not name and not email:
        return None
    return Person(name=name or email, email=email, role=role)


def parse_graph_event(item: dict[str, Any]) -> CalendarEvent:
    attendees: list[Person] = []
    status: dict[str, dict[str, str]] = {}
    for a in item.get("attendees") or []:
        kind = str(a.get("type", "")).lower()
        if kind == "resource":  # meeting rooms
            continue
        p = _email_person(a, "attendee")
        if p:
            attendees.append(p)
            response = str((a.get("status") or {}).get("response") or "").lower()
            status[p.key()] = {"type": kind or "required", "response": response or "none"}
    online = item.get("onlineMeeting") or {}
    provider = str(item.get("onlineMeetingProvider") or "")
    response = str((item.get("responseStatus") or {}).get("response") or "").lower()
    all_day = bool(item.get("isAllDay"))
    start, end = parse_graph_datetime(item["start"]), parse_graph_datetime(item["end"])
    if all_day:
        # all-day entries are dates, not instants: midnight of the LOCAL day (Graph sends 00:00 in the
        # requested zone, i.e. UTC midnight = 01:00 in Berlin otherwise)
        start = start.replace(tzinfo=None).astimezone()
        end = end.replace(tzinfo=None).astimezone()
    return CalendarEvent(
        id=str(item.get("id", "")),
        subject=str(item.get("subject") or "").strip(),
        start=start,
        end=end,
        organizer=_email_person(item.get("organizer"), "organizer"),
        attendees=attendees,
        is_online=bool(item.get("isOnlineMeeting")) or provider.lower() in ("teamsforbusiness", "skypeforbusiness"),
        is_all_day=all_day,
        is_cancelled=bool(item.get("isCancelled")),
        declined=response == "declined",
        show_as=str(item.get("showAs") or ""),
        source="graph",
        join_url=str(online.get("joinUrl") or ""),
        attendee_status=status,
    )


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphClient:
    def __init__(
        self,
        token_provider: Callable[[], str | None],
        session: requests.Session | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._token_provider = token_provider
        self._session = session or requests.Session()
        self._timeout = timeout

    def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        token = self._token_provider()
        if not token:
            raise NotSignedIn("Nicht bei Microsoft angemeldet.")
        headers = {
            "Authorization": f"Bearer {token}",
            "Prefer": 'outlook.timezone="UTC"',
            "Accept": "application/json",
        }
        try:
            resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            raise GraphError(f"Microsoft Graph nicht erreichbar: {exc}") from exc
        if resp.status_code == 401:
            raise NotSignedIn("Anmeldung abgelaufen - bitte erneut anmelden.")
        if resp.status_code >= 400:
            raise GraphError(f"Graph-Fehler {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise GraphError("Unerwartete Antwort von Graph")
        return data

    def get_me_raw(self) -> dict[str, Any]:
        return self._get(f"{GRAPH}/me", {"$select": "displayName,givenName,mail,userPrincipalName"})

    def get_me(self) -> Person:
        data = self.get_me_raw()
        return Person(
            name=str(data.get("displayName") or ""),
            email=str(data.get("mail") or data.get("userPrincipalName") or ""),
            role="self",
        )

    def calendar_view(self, start: datetime, end: datetime, max_pages: int = 10) -> list[CalendarEvent]:
        url: str | None = f"{GRAPH}/me/calendarView"
        params: dict[str, str] | None = {
            "startDateTime": _iso_utc(start),
            "endDateTime": _iso_utc(end),
            "$select": _EVENT_FIELDS,
            "$orderby": "start/dateTime",
            "$top": "50",
        }
        events: list[CalendarEvent] = []
        pages = 0
        while url and pages < max_pages:
            data = self._get(url, params)
            for item in data.get("value", []):
                try:
                    events.append(parse_graph_event(item))
                except (KeyError, ValueError, GraphError):
                    log.exception("Termin konnte nicht gelesen werden: %r", item.get("id"))
            url = data.get("@odata.nextLink")
            params = None  # nextLink already contains the query
            pages += 1
        if url:
            log.warning("Kalender: mehr als %d Seiten - weitere Termine nicht abgerufen", max_pages)
        log.info("Kalender: %d Termin(e) zwischen %s und %s (%d Seite(n))", len(events), _iso_utc(start),
                 _iso_utc(end), pages)
        return events
