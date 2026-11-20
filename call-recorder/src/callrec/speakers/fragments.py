"""Short diarization fragments ("speakers" with almost no speech).

Clustering without a known speaker count produces mini-speakers from a cough, a backchannel "mhm",
cross-talk or noise. Nobody should be asked to name such a voice. A remote speaker with less clean
speech than ``speakers.min_named_speech_s`` is a *fragment* (``SpeakerInfo.minor``): it is not
offered for naming, never counts as open, is never matched against or learned into a voice profile.

Its text is never dropped. Each of its turns goes to the most plausible real speaker when that is
well-founded - the fragment's voice is similar to that speaker (cosine >= the match threshold), or
there is only one real voice on the far side and the turn lies right next to (``ADJACENT_GAP_S``,
not overlapping) one of its turns - and is otherwise labelled neutrally ("Unklar"). Position alone
never decides between several people: a short "ja"/"mhm" inside or next to A's turn is as likely
B's interjection, and a turn overlapping someone's speech is by definition another voice. The plan only relabels the turns used for the
transcript; ``turns.json`` keeps the diarization as it was, so a fragment can still be revealed and
named ("Kurze Fragmente anzeigen").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from callrec.models import SELF_SPEAKER_ID, SOURCE_SELF_ECHO, USER_DECIDED, SpeakerInfo, SpeakerTurn
from callrec.speakers.diarize import l2_normalize, speech_seconds

ADJACENT_GAP_S = 1.0  # a fragment turn this close to a turn of one real speaker belongs to that turn


def minor_ids(turns: list[SpeakerTurn], min_speech_s: float, decided: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Speaker ids with less clean speech than ``min_speech_s`` (ids in ``decided`` - a decision of
    yours, e.g. a fragment you named - are never fragments)."""
    if min_speech_s <= 0:
        return set()
    speech = speech_seconds(turns)
    ids = {t.speaker for t in turns}
    return {sid for sid in ids if sid not in decided and speech.get(sid, 0.0) < min_speech_s}


@dataclass
class FragmentPlan:
    turns: list[SpeakerTurn]  # turns for the transcript (fragment turns relabelled)
    record: dict[str, Any] = field(default_factory=dict)  # for meta.json processing.fragments


def _gap(a: SpeakerTurn, b: SpeakerTurn) -> float:
    """0 when the turns overlap or touch, else the pause between them."""
    return max(0.0, max(a.start, b.start) - min(a.end, b.end))


def _overlap(a: SpeakerTurn, b: SpeakerTurn) -> float:
    return min(a.end, b.end) - max(a.start, b.start)


def plan_fragments(turns: list[SpeakerTurn], speakers: dict[str, SpeakerInfo], embeddings: dict[str, Any],
                   threshold: float, min_speech_s: float, gap_s: float = ADJACENT_GAP_S,
                   margin: float = 0.08) -> FragmentPlan:
    """``margin``: with several voices the fragment's voice must be closer to the best one than to the
    next by at least this much (``speakers.match_margin``) - the embedding of a few seconds is noisy."""
    minors = [sid for sid, s in speakers.items() if s.minor and not s.name and s.source not in USER_DECIDED]
    minors = [sid for sid in minors if any(t.speaker == sid for t in turns)]
    if not minors:
        return FragmentPlan(list(turns), {})
    targets = [sid for sid, s in speakers.items()
               if sid != SELF_SPEAKER_ID and sid not in minors and not s.minor and not s.self_suspect
               and s.source != SOURCE_SELF_ECHO and any(t.speaker == sid for t in turns)]
    speech = speech_seconds(turns)
    out: list[SpeakerTurn] = []
    entries: list[dict[str, Any]] = []
    by_target: dict[str, list[SpeakerTurn]] = {sid: [t for t in turns if t.speaker == sid] for sid in targets}
    similar: dict[str, tuple[str, float]] = {}
    for sid in minors:
        if sid not in embeddings:
            continue
        v = l2_normalize(np.asarray(embeddings[sid]))
        scores = sorted(((float(np.dot(v, l2_normalize(np.asarray(embeddings[tid])))), tid)
                         for tid in targets if tid in embeddings), reverse=True)
        if not scores:
            continue
        best_cos, best_id = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else -1.0
        if best_cos >= threshold and best_cos - runner_up >= margin:
            similar[sid] = (best_id, best_cos)
    summary: dict[str, dict[str, Any]] = {}
    for t in sorted(turns, key=lambda t: (t.start, t.end)):
        if t.speaker not in minors:
            out.append(t)
            continue
        to, why = "", "unklar: keine passende Stimme in der Nähe"
        sim: float | None = None
        if t.speaker in similar:
            to, sim = similar[t.speaker]
            why = "ähnliche Stimme"
        else:
            near = {tid for tid, tl in by_target.items() if any(_gap(t, o) <= gap_s for o in tl)}
            over = {tid for tid, tl in by_target.items() if any(_overlap(t, o) > 0 for o in tl)}
            if over:  # a second voice while somebody speaks: an interjection of another person or noise
                why = "unklar: gleichzeitig mit einer anderen Stimme"
            elif len(near) > 1:
                why = "unklar: zwischen mehreren Personen"
            elif near and len(targets) == 1:  # the only voice on the far side, right next to its turn
                to = near.pop()
                why = "direkt neben einem Beitrag der einzigen Stimme der Gegenseite"
            elif near:  # with several voices a short "ja"/"mhm" next to A is as likely B's: no guess
                why = "unklar: neben einem Beitrag, aber mehrere Stimmen im Gespräch"
        out.append(SpeakerTurn(t.start, t.end, to or t.speaker))
        e: dict[str, Any] = {"start": round(t.start, 2), "end": round(t.end, 2), "to": to, "why": why}
        if sim is not None:
            e["similarity"] = round(sim, 3)
        entries.append({"speaker": t.speaker, **e})
        s = summary.setdefault(t.speaker, {"speech_s": speech.get(t.speaker, 0.0), "attributed": {}, "unclear_s": 0.0})
        if to:
            s["attributed"][to] = round(s["attributed"].get(to, 0.0) + (t.end - t.start), 2)
        else:
            s["unclear_s"] = round(s["unclear_s"] + (t.end - t.start), 2)
    record = {"min_speech_s": min_speech_s, "threshold": threshold, "adjacent_gap_s": gap_s,
              "speakers": summary, "turns": entries}
    return FragmentPlan(out, record)
