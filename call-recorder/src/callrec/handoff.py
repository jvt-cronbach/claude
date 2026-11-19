"""Hand-over of a recording's SUMMARY (and nothing else) to a SharePoint folder.

This is the one place where data deliberately leaves the "local only" boundary,
and it only ever happens on an explicit user action.

Structural guarantee: targets receive bytes that can only be produced from a
:class:`SummaryDocument`. A SummaryDocument can only be created by
:func:`build_summary_document`, which reads ``summary.md`` and a few header fields
from meta.json (title, date, participant names, consent confirmation) - never the
transcript, audio, utterances, speaker data, voice profiles or meta.json itself.
:func:`hand_over` rejects anything that is not exactly a SummaryDocument.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from callrec.models import Person
from callrec.paths import cloud_sync_root_for
from callrec.recording import Recording
from callrec.transcript import WEEKDAYS

log = logging.getLogger(__name__)

_FACTORY_KEY = object()  # only build_summary_document holds this
GRAPH = "https://graph.microsoft.com/v1.0"


HANDOFF_WARNING = (
    "Dieser Schritt verlässt bewusst den rein lokalen Bereich: Die unten angezeigte Datei wird nach "
    "SharePoint übertragen. Übergeben wird ausschließlich die Zusammenfassung mit kurzem Kopf - "
    "kein Transkript, keine Audiodaten, keine Stimmprofile, keine Metadaten-Datei."
)


class HandoffError(RuntimeError):
    pass


@dataclass(frozen=True)
class SummaryDocument:
    filename: str
    markdown: str
    summary_version: int | None
    source_file: str  # always "summary.md"
    _key: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._key is not _FACTORY_KEY:
            raise TypeError("SummaryDocument kann nur über build_summary_document() erzeugt werden")

    @property
    def data(self) -> bytes:
        return self.markdown.encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _fmt_date(rec: Recording) -> str:
    start = rec.started_at()
    if start is None:
        return "-"
    dur = rec.duration()
    text = f"{WEEKDAYS[start.weekday()]}, {start:%d.%m.%Y, %H:%M}"
    if dur:
        from datetime import timedelta

        text += f"–{(start + timedelta(seconds=dur)):%H:%M}"
    return text + " Uhr"


def _strip_own_header(summary_md: str) -> str:
    """Drop the '# Zusammenfassung: ...' title line (the hand-over has its own header)."""
    lines = summary_md.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def build_summary_document(rec: Recording, include_emails: bool = False) -> SummaryDocument:
    if not rec.summary_path.exists():
        raise HandoffError("Für diese Aufnahme gibt es noch keine Zusammenfassung.")
    summary = rec.summary_path.read_text(encoding="utf-8")
    if not summary.startswith("# Zusammenfassung"):
        raise HandoffError("summary.md hat nicht das erwartete Format - Übergabe abgebrochen.")
    meta = rec.load_meta()
    consent = meta.get("consent") or {}
    people: list[Person] = rec.participants()
    names = [f"{p.name} <{p.email}>" if include_emails and p.email and p.email != p.name else p.display for p in people]
    try:
        confirmed = datetime.fromisoformat(consent["confirmed_at"]).strftime("%d.%m.%Y, %H:%M Uhr")
        consent_line = (
            f"Aufnahme mit Einwilligung aller Teilnehmenden (bestätigt am {confirmed}"
            f" durch {consent.get('confirmed_by') or 'den Aufnehmenden'})."
        )
    except (KeyError, ValueError):
        consent_line = "Einwilligungsbestätigung nicht auffindbar."
    versions = (meta.get("versions") or {}).get("summary") or []
    version = int(versions[-1]["version"]) if versions else None
    llm = (meta.get("processing") or {}).get("summary") or {}
    version_txt = f" v{version}" if version else ""
    model_txt = f" (erstellt mit {llm['model']})" if llm.get("model") else ""
    header = [
        f"# {rec.title()}",
        "",
        f"- **Datum:** {_fmt_date(rec)}",
        f"- **Teilnehmende:** {', '.join(names) if names else '-'}",
        f"- **Einwilligung:** {consent_line}",
        f"- **Quelle:** CallRecorder-Zusammenfassung{version_txt}{model_txt}. "
        "Enthält kein Transkript und keine Audiodaten.",
        "",
        "---",
        "",
    ]
    return SummaryDocument(
        filename=f"{rec.folder.name}.md",
        markdown="\n".join(header) + _strip_own_header(summary) + "\n",
        summary_version=version,
        source_file="summary.md",
        _key=_FACTORY_KEY,
    )


class HandoffTarget(Protocol):
    mode: str

    def describe(self) -> str: ...

    def put(self, filename: str, data: bytes) -> str: ...


class FolderTarget:
    """Copy into a local folder that OneDrive syncs with a SharePoint library."""

    mode = "folder"

    def __init__(self, folder: Path) -> None:
        if str(folder).strip() in ("", "."):
            raise HandoffError("Kein Übergabeordner eingestellt (Einstellungen > SharePoint).")
        self.folder = Path(folder).expanduser()

    def describe(self) -> str:
        return str(self.folder)

    def is_cloud_synced(self) -> bool:
        return cloud_sync_root_for(self.folder) is not None

    def put(self, filename: str, data: bytes) -> str:
        if not self.folder.is_dir():
            raise HandoffError(f"Übergabeordner existiert nicht: {self.folder}")
        target = self.folder / filename
        tmp = self.folder / f".{filename}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(target)
        return str(target)


class GraphTarget:
    """Upload via Microsoft Graph: PUT /drives/{drive-id}/root:/{folder}/{file}:/content."""

    mode = "graph"

    def __init__(self, drive_id: str, folder: str, token_provider: Any, session: Any = None, timeout: float = 30.0) -> None:
        if not drive_id.strip():
            raise HandoffError("handoff.graph_drive_id ist leer (siehe README).")
        self.drive_id = drive_id.strip()
        self.folder = folder.strip().strip("/")
        self.token_provider = token_provider
        self.timeout = timeout
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    def describe(self) -> str:
        return f"SharePoint (Graph) Laufwerk {self.drive_id[:12]}…/{self.folder}"

    def url_for(self, filename: str) -> str:
        parts = [p for p in self.folder.split("/") if p] + [filename]
        path = "/".join(quote(p, safe="") for p in parts)
        return f"{GRAPH}/drives/{quote(self.drive_id, safe='!')}/root:/{path}:/content"

    def put(self, filename: str, data: bytes) -> str:
        token = self.token_provider()
        if not token:
            raise HandoffError("Keine Berechtigung für den Datei-Upload erhalten.")
        r = self.session.put(
            self.url_for(filename),
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise HandoffError(f"Upload fehlgeschlagen ({r.status_code}): {str(r.text)[:300]}")
        try:
            return str(r.json().get("webUrl") or self.url_for(filename))
        except ValueError:
            return self.url_for(filename)


def hand_over(doc: SummaryDocument, target: HandoffTarget) -> dict[str, Any]:
    """Write one SummaryDocument to the target. Nothing else can be passed in."""
    if type(doc) is not SummaryDocument:  # noqa: E721 - exact type on purpose, no subclasses
        raise TypeError("Übergeben werden kann ausschließlich eine Zusammenfassung (SummaryDocument).")
    location = target.put(doc.filename, doc.data)
    return {
        "at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "mode": target.mode,
        "target": target.describe(),
        "location": location,
        "file": doc.filename,
        "summary_version": doc.summary_version,
        "sha256": doc.sha256,
    }


def handoff_summary(
    rec: Recording, target: HandoffTarget, include_emails: bool = False, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Hand over the summary. With ``expected_sha256`` (the document the user was shown),
    nothing is sent if summary.md or the header changed in the meantime (e.g. a
    re-summarisation finished while the preview was open)."""
    doc = build_summary_document(rec, include_emails)
    if expected_sha256 is not None and doc.sha256 != expected_sha256:
        raise HandoffError(
            "Die Zusammenfassung hat sich seit der Vorschau geändert - bitte die Übergabe erneut öffnen und prüfen."
        )
    entry = hand_over(doc, target)
    history = list(rec.load_meta().get("handoffs") or [])
    history.append(entry)
    rec.update_meta(["handoffs"], history)
    log.info("Zusammenfassung übergeben: %s -> %s", rec.folder.name, entry["location"])
    return entry


def last_handoff(rec: Recording) -> dict[str, Any] | None:
    h = rec.load_meta().get("handoffs") or []
    return h[-1] if h else None


def build_target(cfg: Any, graph_token: Any = None) -> HandoffTarget:
    h = cfg.handoff
    if h.mode == "graph":
        return GraphTarget(h.graph_drive_id, h.graph_folder, graph_token)
    return FolderTarget(Path(h.folder))
