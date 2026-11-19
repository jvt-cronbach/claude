"""Merge both tracks chronologically and render transcript.md."""

from __future__ import annotations

from datetime import datetime

from callrec.models import Person, SpeakerInfo, Utterance

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def fmt_ts(seconds: float, fmt: str = "hh:mm:ss") -> str:
    s = max(0, int(seconds))
    if fmt == "mm:ss":
        return f"{s // 60:02d}:{s % 60:02d}"
    if fmt == "none":
        return ""
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _prefix(start: float, fmt: str) -> str:
    ts = fmt_ts(start, fmt)
    return f"[{ts}] " if ts else ""


def merge_utterances(*tracks: list[Utterance], join_gap: float | None = 1.5) -> list[Utterance]:
    """Merge utterance lists by start time; fuse adjacent same-speaker utterances (None = never)."""
    allu = sorted((u for t in tracks for u in t), key=lambda u: (u.start, u.end, u.speaker))
    out: list[Utterance] = []
    for u in allu:
        if join_gap is not None and out and out[-1].speaker == u.speaker and u.start - out[-1].end <= join_gap:
            prev = out[-1]
            out[-1] = Utterance(prev.start, max(prev.end, u.end), prev.speaker, f"{prev.text} {u.text}".strip(), prev.track)
        else:
            out.append(u)
    return out


def display_names(speakers: dict[str, SpeakerInfo]) -> dict[str, str]:
    return {sid: info.display for sid, info in speakers.items()}


def render_lines(
    utterances: list[Utterance], speakers: dict[str, SpeakerInfo], ts_format: str = "hh:mm:ss", join_gap: float | None = 1.5
) -> list[str]:
    names = display_names(speakers)
    lines = []
    for u in merge_by_name(utterances, names, join_gap):
        lines.append(f"{_prefix(u.start, ts_format)}{names.get(u.speaker, u.speaker)}: {u.text}")
    return lines


def merge_by_name(utterances: list[Utterance], names: dict[str, str], join_gap: float | None = 1.5) -> list[Utterance]:
    """After assignment two clusters may carry the same name - fuse them for display."""
    out: list[Utterance] = []
    for u in sorted(utterances, key=lambda u: (u.start, u.end)):
        name = names.get(u.speaker, u.speaker)
        if join_gap is not None and out and names.get(out[-1].speaker, out[-1].speaker) == name and u.start - out[-1].end <= join_gap:
            prev = out[-1]
            out[-1] = Utterance(prev.start, max(prev.end, u.end), prev.speaker, f"{prev.text} {u.text}".strip(), prev.track)
        else:
            out.append(u)
    return out


def render_markdown(
    title: str,
    start: datetime | None,
    duration_s: float,
    participants: list[Person],
    utterances: list[Utterance],
    speakers: dict[str, SpeakerInfo],
    model_note: str = "",
    ts_format: str = "hh:mm:ss",
    join_gap: float | None = 1.5,
    version_note: str = "",
) -> str:
    names = display_names(speakers)
    parts = [f"# Transkript: {title}", ""]
    if start is not None:
        parts.append(f"- **Datum:** {WEEKDAYS[start.weekday()]}, {start:%d.%m.%Y, %H:%M} Uhr")
    parts.append(f"- **Dauer:** {fmt_ts(duration_s)}")
    if participants:
        parts.append("- **Eingeladen:** " + ", ".join(p.display for p in participants))
    spoken = []
    for sid in dict.fromkeys(u.speaker for u in utterances):
        n = names.get(sid, sid)
        if n not in spoken:
            spoken.append(n)
    if spoken:
        parts.append("- **Sprechende:** " + ", ".join(spoken))
    unassigned = [s for s in speakers.values() if not s.name and s.id != "ME"]
    if unassigned:
        parts.append(
            "- **Hinweis:** " + ", ".join(s.label for s in unassigned)
            + " noch nicht zugeordnet (Tray-Menü: „Sprecher zuordnen“)."
        )
    if model_note:
        parts.append(f"- **Erstellt lokal mit:** {model_note}")
    if version_note:
        parts.append(f"- **Version:** {version_note}")
    parts += ["", "---", ""]
    for u in merge_by_name(utterances, names, join_gap):
        ts = fmt_ts(u.start, ts_format)
        head = f"[{ts}] {names.get(u.speaker, u.speaker)}" if ts else names.get(u.speaker, u.speaker)
        parts.append(f"**{head}:** {u.text}")
        parts.append("")
    if not utterances:
        parts.append("_Keine Sprache erkannt._")
        parts.append("")
    return "\n".join(parts)
