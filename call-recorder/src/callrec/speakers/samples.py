"""Which audio to play for "Anhören" in the speaker review (shared by both UIs).

A voice is recognised from several seconds of clean speech, not from a 2-second snippet. The
passages offered for one speaker are, best first:

1. stretches of at least ``TARGET_MIN_S`` where only this speaker talks - no other remote speaker
   and not you on the microphone track at the same time - cut into windows of at most
   ``TARGET_MAX_S``, longest first, padded by ``PAD_S`` where the padding is free of other speech;
2. if there is no such stretch (or after those), the speaker's longest clean pieces concatenated
   (with ``JOIN_GAP_S`` of silence in between) up to ``CONCAT_TARGET_S``.

"Weiteres Beispiel" steps through the list (index modulo its length).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from callrec.models import SpeakerTurn
from callrec.speakers.diarize import clean_intervals

TARGET_MIN_S = 8.0
TARGET_MAX_S = 15.0
CONCAT_TARGET_S = 12.0
PAD_S = 0.25
JOIN_GAP_S = 0.3
MIN_PIECE_S = 0.5
MAX_VARIANTS = 8

Span = tuple[float, float]


@dataclass
class Sample:
    pieces: list[Span]  # played one after the other (JOIN_GAP_S silence between)
    index: int = 0
    count: int = 1

    @property
    def duration(self) -> float:
        return sum(e - s for s, e in self.pieces) + JOIN_GAP_S * max(0, len(self.pieces) - 1)


def merge_spans(spans: list[Span], gap: float = 0.0) -> list[Span]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def subtract(pieces: list[Span], busy: list[Span]) -> list[Span]:
    """``pieces`` without any part covered by ``busy``."""
    busy = merge_spans(busy)
    out: list[Span] = []
    for s, e in pieces:
        cur = s
        for bs, be in busy:
            if be <= cur or bs >= e:
                continue
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return [p for p in out if p[1] - p[0] > 0.05]


def _join_free(pieces: list[Span], busy: list[Span], gap: float) -> list[Span]:
    """Join pieces separated by a short pause that nobody else speaks in."""
    out: list[Span] = []
    for s, e in sorted(pieces):
        if out and s - out[-1][1] <= gap and not any(bs < s and be > out[-1][1] for bs, be in busy):
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _pad(span: Span, busy: list[Span], starts: list[float]) -> Span:
    s, e = span
    lo, hi = max(0.0, s - PAD_S), e + PAD_S
    i = bisect.bisect_left(starts, e)
    for bs, be in busy[max(0, i - 50):i + 1]:
        if be <= s:
            lo = max(lo, be)
        if bs >= e:
            hi = min(hi, bs)
    return (lo, max(hi, e))


def sample_list(own: list[Span], busy: list[Span]) -> list[Sample]:
    """All passages for one speaker, best first (see module docstring)."""
    busy = merge_spans(busy)
    starts = [b[0] for b in busy]
    pieces = _join_free(subtract(own, busy), busy, JOIN_GAP_S)
    windows: list[Span] = []
    rest: list[Span] = []
    for s, e in pieces:
        if e - s < TARGET_MIN_S:
            rest.append((s, e))
            continue
        cur = s
        while e - cur >= TARGET_MIN_S:
            end = min(cur + TARGET_MAX_S, e)
            windows.append((cur, end))
            cur = end
    windows.sort(key=lambda w: (-(w[1] - w[0]), w[0]))
    out = [Sample([_pad(w, busy, starts)]) for w in windows]
    usable = sorted((p for p in rest if p[1] - p[0] >= MIN_PIECE_S), key=lambda p: (-(p[1] - p[0]), p[0]))
    bundles: list[list[Span]] = []
    cur_b: list[Span] = []
    total = 0.0
    for p in usable:
        cur_b.append(p)
        total += p[1] - p[0] + JOIN_GAP_S
        if total >= CONCAT_TARGET_S:
            bundles.append(cur_b)
            cur_b, total = [], 0.0
    if cur_b:
        bundles.append(cur_b)
    for b in bundles:
        smp = Sample([_pad(p, busy, starts) for p in sorted(b)])
        if not out or smp.duration >= TARGET_MIN_S:
            out.append(smp)
    if not out and pieces:
        out.append(Sample([_pad(max(pieces, key=lambda p: p[1] - p[0]), busy, starts)]))
    if not out and own:  # always overlapped: the longest own stretch rather than nothing
        s, e = max(merge_spans(own), key=lambda p: p[1] - p[0])
        out.append(Sample([(s, min(e, s + TARGET_MAX_S))]))
    out = out[:MAX_VARIANTS]
    for i, smp in enumerate(out):
        smp.index, smp.count = i, len(out)
    return out


def remote_samples(turns: list[SpeakerTurn], speaker: str, mic_spans: list[Span]) -> list[Sample]:
    """Passages of a remote speaker: no other remote speaker, not you talking at the same time."""
    own = clean_intervals(turns).get(speaker, [])
    others = [(t.start, t.end) for t in turns if t.speaker != speaker]
    return sample_list(own, others + list(mic_spans))


def mic_samples(mic_spans: list[Span], turns: list[SpeakerTurn]) -> list[Sample]:
    """Passages of your own track while nobody on the remote track talks."""
    return sample_list(merge_spans(mic_spans, 0.5), [(t.start, t.end) for t in turns])


def pick(samples: list[Sample], index: int) -> Sample | None:
    return samples[index % len(samples)] if samples else None
