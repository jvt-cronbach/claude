"""Plain data types shared across modules (no heavy imports here)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

SELF_SPEAKER_ID = "ME"
# SpeakerInfo.source values
SOURCE_AUTO = "auto"  # automatic match of a voice profile (may be re-evaluated)
SOURCE_MANUAL = "manual"  # assigned or confirmed by the user (always wins, survives re-runs)
SOURCE_SELF = "self"  # the mic track
SOURCE_SELF_ECHO = "self_echo"  # a remote cluster the user marked "das bin ich"
USER_DECIDED = (SOURCE_MANUAL, SOURCE_SELF_ECHO)
# display name of text from a short fragment that could not be attributed to anybody
UNCLEAR_LABEL = "Unklar"


@dataclass(frozen=True)
class Person:
    name: str
    email: str = ""
    role: str = "attendee"  # organizer | attendee | manual | self

    @property
    def display(self) -> str:
        return self.name or self.email

    def key(self) -> str:
        """Identity key used for de-duplication (email preferred)."""
        return (self.email or self.name).strip().lower()

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Person:
        return Person(name=str(d.get("name", "")), email=str(d.get("email", "")), role=str(d.get("role", "attendee")))


@dataclass
class CalendarEvent:
    id: str
    subject: str
    start: datetime  # timezone-aware
    end: datetime  # timezone-aware
    organizer: Person | None = None
    attendees: list[Person] = field(default_factory=list)
    is_online: bool = False
    is_all_day: bool = False
    is_cancelled: bool = False
    declined: bool = False
    show_as: str = ""
    source: str = "graph"  # graph | ics | manual
    join_url: str = ""

    def participants(self) -> list[Person]:
        """Organizer first, then attendees, de-duplicated."""
        seen: set[str] = set()
        out: list[Person] = []
        people = ([self.organizer] if self.organizer else []) + list(self.attendees)
        for p in people:
            if not p.display:
                continue
            k = p.key()
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "organizer": self.organizer.to_dict() if self.organizer else None,
            "attendees": [a.to_dict() for a in self.attendees],
            "is_online": self.is_online,
            "is_all_day": self.is_all_day,
            "source": self.source,
            "join_url": self.join_url,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> CalendarEvent:
        return CalendarEvent(
            id=str(d.get("id", "")),
            subject=str(d.get("subject", "")),
            start=datetime.fromisoformat(d["start"]),
            end=datetime.fromisoformat(d["end"]),
            organizer=Person.from_dict(d["organizer"]) if d.get("organizer") else None,
            attendees=[Person.from_dict(a) for a in d.get("attendees", [])],
            is_online=bool(d.get("is_online", False)),
            is_all_day=bool(d.get("is_all_day", False)),
            source=str(d.get("source", "graph")),
            join_url=str(d.get("join_url", "")),
        )


@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float = 1.0


@dataclass
class AsrSegment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    # decoding statistics of faster-whisper (None in results written by earlier versions)
    avg_logprob: float | None = None
    compression_ratio: float | None = None
    temperature: float | None = None


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


@dataclass
class AsrResult:
    language: str
    duration: float
    segments: list[AsrSegment]
    model: str = ""
    device: str = ""
    compute_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> AsrResult:
        segs = [
            AsrSegment(
                start=float(s["start"]),
                end=float(s["end"]),
                text=str(s["text"]),
                words=[Word(**w) for w in s.get("words", [])],
                avg_logprob=_opt_float(s.get("avg_logprob")),
                compression_ratio=_opt_float(s.get("compression_ratio")),
                temperature=_opt_float(s.get("temperature")),
            )
            for s in d.get("segments", [])
        ]
        return AsrResult(
            language=str(d.get("language", "")),
            duration=float(d.get("duration", 0.0)),
            segments=segs,
            model=str(d.get("model", "")),
            device=str(d.get("device", "")),
            compute_type=str(d.get("compute_type", "")),
        )


@dataclass
class SpeakerTurn:
    """A diarization turn on the remote track."""

    start: float
    end: float
    speaker: str  # internal id, e.g. "S1"


@dataclass
class Utterance:
    start: float
    end: float
    speaker: str  # internal speaker id ("ME", "S1", ...)
    text: str
    track: str  # mic | remote

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Utterance:
        return Utterance(
            start=float(d["start"]),
            end=float(d["end"]),
            speaker=str(d["speaker"]),
            text=str(d["text"]),
            track=str(d.get("track", "")),
        )


@dataclass
class SpeakerInfo:
    """Mapping from an internal speaker id to a display name."""

    id: str
    label: str  # "Sprecher 1" - stable fallback name
    name: str = ""  # assigned person name ("" = unassigned)
    email: str = ""
    profile_id: str = ""
    score: float | None = None  # similarity of an automatic match
    source: str = ""  # auto | manual | self | self_echo
    # shown in the assignment dialog only, never applied automatically:
    suggested_name: str = ""  # "klingt wie ..." (e.g. a profile of a non-participant)
    suggested_email: str = ""
    suggested_score: float | None = None
    note: str = ""  # why there is no automatic assignment
    self_suspect: bool = False  # probably the user's own voice on the remote track
    preselect_name: str = ""  # pre-filled in the dialog: the only participant not assigned yet (you confirm)
    # too little speech (speakers.min_named_speech_s): a fragment, not offered for naming, not "open"
    minor: bool = False

    @property
    def display(self) -> str:
        return self.name or (UNCLEAR_LABEL if self.minor else self.label)

    def clear_hints(self) -> None:
        self.suggested_name, self.suggested_email, self.suggested_score, self.note = "", "", None, ""
        self.self_suspect = False
        self.preselect_name = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> SpeakerInfo:
        score = d.get("score")
        return SpeakerInfo(
            id=str(d["id"]),
            label=str(d.get("label", d["id"])),
            name=str(d.get("name", "")),
            email=str(d.get("email", "")),
            profile_id=str(d.get("profile_id", "")),
            score=float(score) if score is not None else None,
            source=str(d.get("source", "")),
            suggested_name=str(d.get("suggested_name", "")),
            suggested_email=str(d.get("suggested_email", "")),
            suggested_score=float(d["suggested_score"]) if d.get("suggested_score") is not None else None,
            note=str(d.get("note", "")),
            self_suspect=bool(d.get("self_suspect", False)),
            preselect_name=str(d.get("preselect_name", "")),
            minor=bool(d.get("minor", False)),
        )
