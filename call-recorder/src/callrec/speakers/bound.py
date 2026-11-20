"""Limit the number of remote speakers to what the participant list allows.

Clustering without a known speaker count tends to split one remote voice into several clusters
(changing microphone distance, codec, emotion), and each fragment can then be mistaken for
somebody else. The participant list is a strong prior: a call with the user and ONE invited
person has one remote voice. ``merge_to_bound`` merges the most similar clusters (cosine of their
cached embeddings, weighted by speech time - no audio needed) until the count fits the bound.
It never merges two clusters you assigned to different people, and leaves clusters out that are
probably your own voice (sidetone) - they are not a remote participant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from callrec.models import SpeakerTurn


@dataclass
class BoundResult:
    turns: list[SpeakerTurn]
    embeddings: dict[str, np.ndarray]
    mapping: dict[str, str]  # raw speaker id -> new speaker id
    merged: list[tuple[str, str, float]] = field(default_factory=list)  # (a, b, similarity) raw ids


def _norm(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def merge_to_bound(
    turns: list[SpeakerTurn],
    embeddings: dict[str, np.ndarray],
    speech: dict[str, float],
    bound: int | None,
    keep_apart: dict[str, str] | None = None,
    exclude: set[str] | None = None,
) -> BoundResult:
    """``bound``: maximum number of remote speakers (None = no limit). ``keep_apart``: raw id ->
    name you assigned (clusters with different names are never merged). ``exclude``: raw ids that
    are neither counted nor merged (your own voice on the remote track)."""
    keep_apart = keep_apart or {}
    exclude = exclude or set()
    order = list(dict.fromkeys(t.speaker for t in sorted(turns, key=lambda t: (t.start, t.end))))
    group = {sid: sid for sid in order}  # raw id -> representative raw id
    cent = {sid: _norm(embeddings[sid]) for sid in order if sid in embeddings}
    weight = {sid: max(speech.get(sid, 0.0), 0.1) for sid in order}
    names = {sid: keep_apart.get(sid, "") for sid in order}
    merged: list[tuple[str, str, float]] = []

    def active() -> list[str]:
        return [s for s in order if group[s] == s and s not in exclude]

    while bound is not None and len(active()) > max(bound, 1):
        reps = active()
        best: tuple[float, str, str] | None = None
        for i, a in enumerate(reps):
            for b in reps[i + 1:]:
                if names[a] and names[b] and names[a].casefold() != names[b].casefold():
                    continue
                if a in cent and b in cent:
                    sim = float(np.dot(cent[a], cent[b]))
                else:
                    sim = -2.0  # no embedding: merge only if nothing else is possible
                if best is None or sim > best[0]:
                    best = (sim, a, b)
        if best is None:
            break  # only clusters you named differently are left
        sim, a, b = best
        if a in cent and b in cent:
            cent[a] = _norm(cent[a] * weight[a] + cent[b] * weight[b])
        elif b in cent:
            cent[a] = cent[b]
        weight[a] += weight[b]
        names[a] = names[a] or names[b]
        for s in order:
            if group[s] == b:
                group[s] = a
        merged.append((a, b, round(sim, 4)))
    # renumber by first appearance (S1, S2, ...)
    mapping: dict[str, str] = {}
    new_id: dict[str, str] = {}
    for s in order:
        rep = group[s]
        if rep not in new_id:
            new_id[rep] = f"S{len(new_id) + 1}"
        mapping[s] = new_id[rep]
    out_turns = [SpeakerTurn(t.start, t.end, mapping[t.speaker]) for t in sorted(turns, key=lambda t: (t.start, t.end))]
    out_emb = {new_id[rep]: cent[rep] for rep in new_id if rep in cent}
    return BoundResult(out_turns, out_emb, mapping, merged)
