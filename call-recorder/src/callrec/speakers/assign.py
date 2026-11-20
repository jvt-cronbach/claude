"""Attach diarization speakers to recognised words and group them into utterances."""

from __future__ import annotations

import bisect
from collections.abc import Callable

from callrec.models import AsrResult, SpeakerTurn, Utterance, Word

UNKNOWN = "S?"


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


class TurnIndex:
    def __init__(self, turns: list[SpeakerTurn]) -> None:
        self.turns = sorted(turns, key=lambda t: t.start)
        self.starts = [t.start for t in self.turns]

    def speaker_for(self, start: float, end: float, max_distance: float = 1.0) -> str | None:
        if not self.turns:
            return None
        # candidates: turns starting before `end`
        hi = bisect.bisect_right(self.starts, end)
        best: tuple[float, str] | None = None
        for t in self.turns[max(0, hi - 50):hi]:
            ov = _overlap(start, end, t.start, t.end)
            if ov > 0 and (best is None or ov > best[0]):
                best = (ov, t.speaker)
        if best:
            return best[1]
        # no overlap: nearest turn within max_distance
        mid = (start + end) / 2
        nearest: tuple[float, str] | None = None
        for t in self.turns[max(0, hi - 3):hi + 3]:
            d = min(abs(mid - t.start), abs(mid - t.end))
            if d <= max_distance and (nearest is None or d < nearest[0]):
                nearest = (d, t.speaker)
        return nearest[1] if nearest else None


def _join(words: list[Word]) -> str:
    # faster-whisper words carry their leading space
    return "".join(w.text for w in words).strip()


def words_of(asr: AsrResult) -> list[Word]:
    out: list[Word] = []
    for seg in asr.segments:
        if seg.words:
            out.extend(seg.words)
        elif seg.text.strip():
            out.append(Word(seg.start, seg.end, " " + seg.text.strip()))
    return out


def group_words(
    words: list[Word],
    speaker_of: Callable[[Word], str],
    track: str,
    max_gap: float = 2.0,
    max_len: float = 60.0,
) -> list[Utterance]:
    """Group consecutive words into utterances; split on speaker change, long pause or length."""
    utterances: list[Utterance] = []
    current: list[Word] = []
    current_spk = ""

    def flush() -> None:
        if current:
            text = _join(current)
            if text:
                utterances.append(Utterance(current[0].start, current[-1].end, current_spk, text, track))

    for w in sorted(words, key=lambda w: w.start):
        spk = speaker_of(w)
        if current:
            gap = w.start - current[-1].end
            too_long = w.end - current[0].start > max_len and current[-1].text.rstrip().endswith((".", "?", "!"))
            if spk != current_spk or gap > max_gap or too_long:
                flush()
                current = []
        if not current:
            current_spk = spk
        current.append(w)
    flush()
    return utterances


def assign_remote(asr: AsrResult, turns: list[SpeakerTurn], max_gap: float = 2.0, max_len: float = 60.0,
                  self_ids: set[str] | frozenset[str] = frozenset(), self_speaker: str = "ME",
                  mic_words: list[Word] | None = None, tolerance: float = 0.5) -> list[Utterance]:
    """Utterances of the remote track.

    ``self_ids``: clusters the user marked as their own voice ("das bin ich", e.g. sidetone).
    Their words are attributed to ``self_speaker``; words that the mic track also has at the same
    time (within ``tolerance``) are dropped, so nothing appears twice."""
    index = TurnIndex(turns)
    words = words_of(asr)
    last = [UNKNOWN]

    def raw_speaker(w: Word) -> str:
        spk = index.speaker_for(w.start, w.end)
        if spk is None:
            spk = last[0] if last[0] != UNKNOWN else (turns[0].speaker if len(turns) == 1 else UNKNOWN)
        last[0] = spk
        return spk

    labelled = [(w, raw_speaker(w)) for w in sorted(words, key=lambda w: w.start)]
    if self_ids:
        mic = sorted((m.start, m.end) for m in mic_words or [])
        starts = [s for s, _ in mic]

        def on_mic(w: Word) -> bool:
            i = bisect.bisect_right(starts, w.end + tolerance)
            return any(e + tolerance >= w.start for _s, e in mic[max(0, i - 20):i])

        labelled = [(w, self_speaker if s in self_ids else s) for w, s in labelled
                    if not (s in self_ids and on_mic(w))]
    spk_of = {id(w): s for w, s in labelled}
    return group_words([w for w, _ in labelled], lambda w: spk_of[id(w)], "remote", max_gap=max_gap, max_len=max_len)


def assign_single(asr: AsrResult, speaker: str, track: str, max_gap: float = 2.0, max_len: float = 60.0) -> list[Utterance]:
    return group_words(words_of(asr), lambda w: speaker, track, max_gap=max_gap, max_len=max_len)
