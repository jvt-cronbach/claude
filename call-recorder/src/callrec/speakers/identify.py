"""Who is which remote voice - the decision rules of 0.4.1 (pure: no files, no store writes).

What changed against 0.4.0, and why (measured with the real wespeaker model on synthetic voices
through Teams-like channels - Opus 12-24 kbit/s, Bluetooth narrowband, GSM, laptop microphone
with room echo, noise, loudness changes):

* The channel dominates the embedding: the same person through two different devices scored
  0.36-0.66 (cosine), two *different* people through the same kind of device 0.47-0.75 (up to
  0.96). A fixed similarity threshold against *every* profile cannot work there. But a call has
  only a few participants: the question is "which of these k people is this voice", not "who in
  the world". So only the participants' profiles compete, and the decision is relative:
  the best participant must beat the second-best participant and the other voices of this call.
  Profiles of people who are *not* invited only block a decision when they are clearly closer
  (``guard_margin``) - "a colleague came instead".
* A profile is a set of references (one per learned recording), not one average: a person
  learned on a headset and once on a laptop is recognised on both (see ``VoiceProfile.score``).
* In a call with exactly one other expected participant the remote voice IS that person - named
  without any profile, and the normal way a profile is created and keeps learning.
* When all but one expected participant are recognised and one voice is left, that voice is the
  remaining participant (elimination) - unless it sounds like a voice already named in this call.
* Nothing that is not certain is lost: every open voice gets a pre-selected suggestion with its
  score, confirmed with one click (or "Alle Vorschläge übernehmen").

Every decision carries an explanation (``Outcome.explain``): the candidates with their scores,
the rule that decided, or exactly why not (below the threshold by how much, the rival and the
margin, too little speech, no participant list, a name not linked to a profile, a fragment).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from callrec.models import Person
from callrec.speakers.profiles import VoiceProfile, cosine

# rule names (stored in speakers.json "rule" and in the explanation)
RULE_SINGLE = "single"
RULE_PROFILE = "profile"
RULE_ELIMINATION = "elimination"
RULE_ALWAYS = "always"
RULE_TEXT = {RULE_SINGLE: "einzige weitere Person des Termins", RULE_PROFILE: "Stimmprofil",
             RULE_ELIMINATION: "Ausschluss (alle anderen Teilnehmenden erkannt)",
             RULE_ALWAYS: "Stimmprofil („immer erkennen“)"}


@dataclass
class Params:
    threshold: float = 0.45  # a participant's profile must reach this ...
    margin: float = 0.06  # ... and beat the second-best participant / the other voices by this much
    alone_threshold: float = 0.60  # no rival to compare with (one voice, one profiled participant)
    guard_margin: float = 0.10  # a non-participant profile this much closer: maybe somebody else came
    guard_min: float = 0.65  # ... and at least this close
    # the single-participant rule: the invitation is strong evidence - only a clearly closer known voice
    # (and only against an existing profile of the invitee) makes it a question
    single_guard_margin: float = 0.15
    single_guard_min: float = 0.75
    # ... without a profile of the invitee: a known voice of somebody else this close makes it a
    # pre-selected question (0.4.1 named anyway: "colleague came instead" became a confident wrong name)
    single_other: float = 0.70
    # ... and no profile is created/taught from a voice this close to somebody else's profile (0.4.0
    # did not learn when another profile matched at 0.55; 0.4.1 had raised this to 0.90)
    no_learn_other: float = 0.55
    # an automatic name whose voice evidence is below this (or that rests on the invitation / elimination
    # alone, without a profile) needs your confirmation before the summary is written (0 = never)
    confirm_below: float = 0.75
    # elimination with a remaining participant without a profile: a known uninvited voice this close blocks it
    elimination_other: float = 0.80
    min_match_speech_s: float = 6.0
    min_learn_speech_s: float = 12.0
    learn_threshold: float = 0.75  # an automatic match teaches the profile from this score (config: auto_update_threshold)
    suggest_min: float = 0.30  # pre-select a candidate from this score
    adhoc_suggest: float = 0.70  # no participant list: pre-select a known voice only from this score
    # "immer erkennen" names somebody who is NOT invited: the relative bar among the participants (0.45) is far
    # too low for that (different people on the same kind of device score up to 0.75) - below this it is a
    # suggestion only
    always_min: float = 0.75
    # an open voice this close to a voice already named in this call: one person split in two? (two
    # clusters of the diarization are below ~0.6 by construction; a split person is usually far above)
    duplicate_sim: float = 0.80
    single: bool = True
    elimination: bool = True

    @staticmethod
    def from_config(sc: Any) -> Params:
        p = Params()
        for k, attr in (("threshold", "match_threshold"), ("margin", "match_margin"),
                        ("alone_threshold", "alone_threshold"), ("guard_margin", "guard_margin"),
                        ("min_match_speech_s", "min_match_speech_s"), ("min_learn_speech_s", "min_learn_speech_s"),
                        ("learn_threshold", "auto_update_threshold"), ("single", "single_participant_rule"),
                        ("elimination", "elimination_rule"), ("confirm_below", "confirm_below")):
            if hasattr(sc, attr):
                setattr(p, k, type(getattr(p, k))(getattr(sc, attr)))
        return p


@dataclass
class Voice:
    """One remote speaker of the recording as the identification sees it."""

    sid: str
    embedding: np.ndarray | None
    speech_s: float
    decided: str = ""  # name you assigned ("" = open)
    blocked: str = ""  # why it is not a participant's voice at all (fragment, your own voice) - not decided


@dataclass
class Outcome:
    sid: str
    name: str = ""
    email: str = ""
    profile_id: str = ""
    rule: str = ""
    score: float | None = None
    learn: str = ""  # "" | "sole" | "auto": what the profile learns from this decision
    confirm: bool = False  # named automatically, but not sure enough: you confirm it before the summary
    preselect: str = ""  # pre-filled in the dialog (you confirm with one click)
    preselect_email: str = ""
    preselect_profile_id: str = ""  # profile to learn into when you confirm (a free-text profile)
    preselect_score: float | None = None
    suggestion: tuple[str, str, float] | None = None  # (name, email, score): a non-participant
    why: str = ""  # one line, German
    explain: dict[str, Any] = field(default_factory=dict)


@dataclass
class Cand:
    person: Person
    expected: bool
    hard: list[VoiceProfile] = field(default_factory=list)  # linked profiles (same person)
    probable: list[tuple[VoiceProfile, str]] = field(default_factory=list)  # (profile, "first_name"|"alias")
    kind: str = "participant"  # participant | always

    @property
    def label(self) -> str:
        return self.person.name or self.person.email

    @property
    def auto_ok(self) -> bool:
        return bool(self.hard) and all(p.mode != "never" for p in self.hard)


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), 3)


def build_candidates(participants: list[Person], expected: list[Person], profiles: list[VoiceProfile],
                     me: list[Person]) -> tuple[list[Cand], list[VoiceProfile]]:
    """Participants with their linked profiles; returns also the profiles of nobody in this call."""
    from callrec.speakers.names import is_one_of, persons_match

    usable = [p for p in profiles if not is_one_of(p.person, me)]
    cands: list[Cand] = []
    used: set[str] = set()
    linked_somehow: set[str] = set()  # a profile probably of a participant never counts as "somebody else"
    for person in participants:
        c = Cand(person, any(persons_match(person, e) for e in expected))
        for prof in usable:
            kind = prof.link_kind(person)
            if kind == "hard":
                c.hard.append(prof)
                used.add(prof.id)
            elif kind:
                c.probable.append((prof, kind))
                linked_somehow.add(prof.id)
        cands.append(c)
    # a probable link counts only when it is unambiguous: one profile <-> one participant
    for c in cands:
        c.probable = [(p, k) for p, k in c.probable if p.id not in used
                      and sum(1 for o in cands if any(q.id == p.id for q, _k in o.probable)) == 1
                      and len(c.probable) == 1 and not c.hard]
        used.update(p.id for p, _k in c.probable)
    for prof in usable:  # "immer erkennen": may be named although not invited
        if prof.id not in used and prof.mode == "always":
            cands.append(Cand(prof.person, False, [prof], kind="always"))
            used.add(prof.id)
    others = [p for p in usable if p.id not in used and p.id not in linked_somehow]
    return cands, others


def _score(c: Cand, vec: np.ndarray, rid: str) -> float | None:
    vals = [s for s in (p.score(vec, rid) for p in c.hard) if s is not None]
    return max(vals) if vals else None


def _prob_score(c: Cand, vec: np.ndarray, rid: str) -> tuple[float | None, VoiceProfile | None, str]:
    best: tuple[float | None, VoiceProfile | None, str] = (None, None, "")
    for p, kind in c.probable:
        s = p.score(vec, rid)
        if s is not None and (best[0] is None or s > best[0]):
            best = (s, p, kind)
    return best


def _joint(voices: list[Voice], cand_idx: list[int], scores: dict[str, dict[int, float | None]],
           null_value: float, margin: float, top: int = 3) -> dict[str, tuple[int, float, float]]:
    """Best assignment of ``voices`` to candidates (each candidate at most once, a voice may get none -
    worth ``null_value``). Returns, for each voice the best assignment gives a candidate,
    (candidate index, score, gap): how much worse the best assignment *without* this pair is."""
    opts: list[list[tuple[int, float]]] = []
    for v in voices:
        cand = sorted(((i, s) for i, s in scores[v.sid].items() if i in cand_idx and s is not None),
                      key=lambda x: -x[1])[:top]
        opts.append(cand)
    totals: list[tuple[float, tuple[int, ...]]] = []

    def rec(k: int, used: frozenset[int], acc: float, chosen: tuple[int, ...]) -> None:
        if k == len(voices):
            totals.append((acc, chosen))
            return
        rec(k + 1, used, acc + null_value, chosen + (-1,))
        for i, s in opts[k]:
            if i not in used:
                rec(k + 1, used | {i}, acc + s, chosen + (i,))

    if not voices:
        return {}
    rec(0, frozenset(), 0.0, ())
    best_total, best = max(totals, key=lambda t: t[0])
    out: dict[str, tuple[int, float, float]] = {}
    for k, i in enumerate(best):
        if i < 0:
            continue
        without = max((t for t, ch in totals if ch[k] != i), default=-1e9)
        s = next(x for j, x in opts[k] if j == i)
        out[voices[k].sid] = (i, s, best_total - without)
    for k, i in enumerate(best):
        if i < 0:
            out[voices[k].sid] = (-1, null_value, 0.0)  # best: nobody with a profile
    return out


def identify(voices: list[Voice], cands: list[Cand], others: list[VoiceProfile], params: Params, *,
             recording: str = "", has_participants: bool = True, mic_unreliable: bool = False,
             merged_apart: bool = False) -> dict[str, Outcome]:
    """Decide for every open voice. ``voices``: remote speakers (fragments and your own voice with
    ``blocked`` set, your assignments with ``decided``). ``cands``/``others``: from
    :func:`build_candidates`. ``merged_apart``: the speaker limit merged clearly different voices."""
    P = params
    out: dict[str, Outcome] = {v.sid: Outcome(v.sid) for v in voices}
    open_v = [v for v in voices if not v.decided and not v.blocked]
    taken = {v.decided.casefold() for v in voices if v.decided}

    def is_taken(c: Cand) -> bool:
        from callrec.speakers.names import same_person

        return any(same_person(c.person.name, c.person.email, t, "") for t in taken) or c.label.casefold() in taken

    free = [c for c in cands if not is_taken(c)]
    scores: dict[str, dict[int, float | None]] = {}
    world: dict[str, tuple[float, VoiceProfile] | None] = {}
    for v in open_v:
        if v.embedding is None:
            scores[v.sid], world[v.sid] = {}, None
            continue
        scores[v.sid] = {i: _score(c, v.embedding, recording) for i, c in enumerate(cands)}
        best_w: tuple[float, VoiceProfile] | None = None
        for p in others:
            s = p.score(v.embedding, recording)
            if s is not None and (best_w is None or s > best_w[0]):
                best_w = (s, p)
        world[v.sid] = best_w

    def second_world(v: Voice) -> float:
        if v.embedding is None:
            return 1.0
        vals = sorted((x for x in (p.score(v.embedding, recording) for p in others) if x is not None), reverse=True)
        return vals[1] if len(vals) > 1 else -1.0

    def table(sid: str) -> list[dict[str, Any]]:
        rows = []
        for i, c in enumerate(cands):
            s = scores.get(sid, {}).get(i)
            emb = voices_by[sid].embedding
            ps, pp, pk = _prob_score(c, emb, recording) if emb is not None else (None, None, "")
            rows.append({"name": c.label, "score": _r(s), "expected": c.expected, "kind": c.kind,
                         "profile": "linked" if c.hard else (f"probable:{pk}" if c.probable else "none"),
                         "profile_name": pp.name if pp is not None else "", "probable_score": _r(ps),
                         "never": any(p.mode == "never" for p in c.hard), "taken": is_taken(c)})
        w = world.get(sid)
        if w is not None:
            rows.append({"name": w[1].name, "score": _r(w[0]), "expected": False, "kind": "other",
                         "profile": "linked", "never": w[1].mode == "never", "taken": False})
        return sorted(rows, key=lambda r: -(r["score"] if r["score"] is not None else -9))

    voices_by = {v.sid: v for v in voices}
    params_rec = {"threshold": P.threshold, "margin": P.margin, "alone_threshold": P.alone_threshold,
                  "guard_margin": P.guard_margin, "min_match_speech_s": P.min_match_speech_s,
                  "min_learn_speech_s": P.min_learn_speech_s}

    def explain(o: Outcome, decision: str, **kw: Any) -> None:
        v = voices_by[o.sid]
        o.explain = {"decision": decision, "rule": o.rule, "speech_s": round(v.speech_s, 1),
                     "candidates": table(o.sid) if o.sid in scores else [], "params": params_rec, **kw}

    for v in voices:
        o = out[v.sid]
        if v.decided:
            o.why = "selbst zugeordnet"
            o.explain = {"decision": "manual", "rule": "", "speech_s": round(v.speech_s, 1)}
        elif v.blocked:
            o.why = v.blocked
            o.explain = {"decision": "blocked", "rule": "", "speech_s": round(v.speech_s, 1), "reason": v.blocked}

    def guard(sid: str, own: float | None, single: bool = False, unprofiled_min: float | None = None,
              ) -> tuple[float, VoiceProfile] | None:
        """A known person who is not in this call sounds clearly closer: maybe they came instead."""
        w = world.get(sid)
        if w is None:
            return None
        if own is None:
            return w if unprofiled_min is not None and w[0] >= unprofiled_min else None
        if single:
            return w if w[0] >= max(P.single_guard_min, own + P.single_guard_margin) else None
        return w if w[0] >= max(P.guard_min, own + P.guard_margin) else None

    def name_as(o: Outcome, c: Cand, rule: str, score: float | None) -> None:
        o.name, o.email, o.rule, o.score = c.label, c.person.email, rule, _r(score)
        o.profile_id = c.hard[0].id if c.hard else ""

    expected_free = [c for c in free if c.expected]
    expected_all = [c for c in cands if c.expected]

    # 1. exactly one other expected participant and exactly one remote voice
    remote = [v for v in voices if not v.blocked]
    if P.single and len(expected_all) == 1 and len(remote) == 1 and open_v and remote[0] is open_v[0]:
        v, c = open_v[0], expected_all[0]
        o = out[v.sid]
        own = scores.get(v.sid, {}).get(cands.index(c))
        g = guard(v.sid, own, single=True)
        never = any(p.mode == "never" for p in c.hard)
        w_s = world.get(v.sid)
        if never:
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(own)
            o.why = f"{c.label}: „nie automatisch“ eingestellt – bitte bestätigen"
            explain(o, "never")
        elif own is not None and own < P.threshold and v.speech_s >= P.min_match_speech_s:
            # the invitee has a voice profile and this voice does not match it: somebody else may have come
            # (a colleague instead, the invitee never joined) - a question with one click, never a name
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(own)
            if w_s is not None and w_s[0] >= P.guard_min:
                o.suggestion = (w_s[1].name, w_s[1].email, float(w_s[0]))
            o.why = (f"einzige weitere Person des Termins ist {c.label}, aber die Stimme passt nicht zu deren "
                     f"Stimmprofil ({own:.2f}, nötig {P.threshold:.2f})"
                     + (f"; klingt eher wie {o.suggestion[0]} ({o.suggestion[2]:.2f}, nicht eingeladen)"
                        if o.suggestion is not None else "") + " – kam jemand anderes? Bitte bestätigen")
            explain(o, "mismatch", own_score=_r(own), reason=o.why)
        elif g is not None or (own is None and w_s is not None and w_s[0] >= P.single_other
                               and v.speech_s >= P.min_match_speech_s):
            gg = g if g is not None else w_s
            assert gg is not None
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(own)
            o.suggestion = (gg[1].name, gg[1].email, float(gg[0]))
            o.why = (f"einzige weitere Person des Termins ist {c.label}, die Stimme klingt aber eher wie "
                     f"{gg[1].name} ({gg[0]:.2f}, nicht eingeladen)"
                     + (f" als wie das Stimmprofil von {c.label} ({own:.2f})" if own is not None else "")
                     + " – bitte bestätigen")
            explain(o, "guard", rival=gg[1].name, rival_score=_r(gg[0]), own_score=_r(own), reason=o.why)
        else:
            name_as(o, c, RULE_SINGLE, own)
            linked = ""
            if not c.hard and c.probable and v.embedding is not None:
                ps, pp, pk = _prob_score(c, v.embedding, recording)
                if pp is not None and ps is not None and ps >= P.learn_threshold:
                    o.profile_id, linked = pp.id, pp.name  # the typed name "Anna" is this participant
                    own = ps
                    o.score = _r(ps)
            doubt = ""
            if merged_apart:
                doubt = "mehrere unterschiedliche Stimmen zusammengefasst"
            elif mic_unreliable:
                doubt = "Mikrofonspur unzuverlässig"
            elif v.speech_s < P.min_learn_speech_s:
                doubt = f"zu wenig Sprache zum Lernen ({v.speech_s:.0f} s, mindestens {P.min_learn_speech_s:.0f} s)"
            elif v.embedding is None:
                doubt = "keine Stimmmerkmale"
            elif w_s is not None and w_s[0] >= max(P.no_learn_other, own if own is not None else -1.0):
                doubt = f"Stimme ähnlich zu {w_s[1].name} ({w_s[0]:.2f}, nicht eingeladen)"
            elif own is not None and own < P.learn_threshold:
                doubt = f"Stimme passt nur schwach zum Stimmprofil ({own:.2f}, Lernen ab {P.learn_threshold:.2f})"
            o.learn = "" if doubt else "sole"
            # the name rests on the invitation; without voice evidence (no profile yet, or a weak match) you
            # confirm it once before the summary is written
            o.confirm = P.confirm_below > 0 and (own is None or own < P.confirm_below or merged_apart or (
                w_s is not None and own is not None and w_s[0] >= own - P.margin))
            o.why = RULE_TEXT[RULE_SINGLE] + (f" (Stimmprofil {own:.2f})" if own is not None else "") + (
                f"; Profil „{linked}“ mit {c.label} verknüpft" if linked else "") + (
                f"; Stimmprofil nicht aktualisiert: {doubt}" if doubt else "")
            explain(o, "named", no_learn=doubt, linked_profile=linked)
        return out

    # 2. profile matches among the participants: the best *joint* assignment of voices to people (one
    #    voice per person, a voice may also be "nobody with a profile"); a pair is decided only if every
    #    assignment that is nearly as good (within the margin) contains it - "which of these k people"
    done_v: set[str] = set()
    done_c: set[int] = set()
    able = [v for v in open_v if v.speech_s >= P.min_match_speech_s and scores.get(v.sid)]
    prof_c = [i for i, c in enumerate(cands) if c in free and c.auto_ok]
    unprofiled = any(not c.hard for c in free if c.expected)
    null_value = (P.alone_threshold if unprofiled else P.threshold) - P.margin
    pairs = _joint(able, prof_c, scores, null_value, P.margin)
    for sid, (i, s, gap) in pairs.items():
        if i < 0:
            continue
        c, o = cands[i], out[sid]
        rival_row = max((x for j, x in scores[sid].items() if j != i and x is not None and j in prof_c), default=None)
        rival_col = max((scores[v.sid].get(i) or -9 for v in able if v.sid != sid), default=None)
        rival_col = None if rival_col is None or rival_col <= -9 else rival_col
        if s < P.threshold or gap < P.margin:
            continue  # explained below (still open)
        if c.kind == "always" and s < P.always_min:
            continue  # not invited: only a clear match names them (explained below)
        if guard(sid, s) is not None:
            continue
        name_as(o, c, RULE_ALWAYS if c.kind == "always" else RULE_PROFILE, s)
        sure = s >= P.learn_threshold and gap >= 2 * P.margin
        o.learn = "auto" if sure and voices_by[sid].speech_s >= P.min_learn_speech_s and not mic_unreliable else ""
        # the relative decision cannot rule out (a) somebody NOT invited whose profile is about as close, or
        # (b) an invited person without a profile (the voice may be theirs - nothing to compare with): both
        # were the wrong names of the adversarial series of the review (a twin-like colleague instead, the
        # invitee absent and the profiled participant silent)
        w_p = world.get(sid)
        rival_out = w_p is not None and w_p[0] >= s - P.margin
        unprofiled_alt = any(not x.hard and x.expected for x in free if x is not c)
        o.confirm = P.confirm_below > 0 and (s < P.confirm_below or rival_out or unprofiled_alt)
        if rival_out:
            o.learn = ""
        o.why = (f"Stimmprofil {c.label} {s:.2f}"
                 + (f", nächste Person {rival_row:.2f}" if rival_row is not None else "")
                 + (f", andere Stimme {rival_col:.2f}" if rival_col is not None else "")
                 + f" (Vorsprung der Zuordnung {gap:.2f})")
        explain(o, "named", rival_row=_r(rival_row), rival_col=_r(rival_col), gap=_r(gap), null_value=_r(null_value))
        done_v.add(sid)
        done_c.add(i)

    # 3. elimination: one voice left, one expected participant left
    def still_open() -> list[Voice]:
        return [v for v in open_v if not out[v.sid].name]

    left_v = still_open()
    named_now = {cands[i].label.casefold() for i in done_c}
    left_c = [c for c in expected_free if c.label.casefold() not in named_now]
    if P.elimination and len(left_v) == 1 and len(left_c) == 1 and len(expected_all) >= 2:
        v, c = left_v[0], left_c[0]
        o = out[v.sid]
        own = scores.get(v.sid, {}).get(cands.index(c))
        dup = None
        if v.embedding is not None:
            for v2 in voices:
                if v2 is v or v2.blocked or v2.embedding is None or not (v2.decided or out[v2.sid].name):
                    continue
                sim = cosine(v.embedding, v2.embedding)
                if sim >= P.duplicate_sim and (dup is None or sim > dup[0]):
                    dup = (sim, v2.decided or out[v2.sid].name)
        g = guard(v.sid, own, unprofiled_min=P.elimination_other)
        if v.speech_s < P.min_match_speech_s:
            pass  # too little speech: stays a suggestion below
        elif dup is not None:
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(own)
            o.why = (f"übrig ist nur {c.label}, aber die Stimme ähnelt {dup[1]} ({dup[0]:.2f}) – gleiche Person? "
                     "Bitte bestätigen")
            explain(o, "duplicate", duplicate_of=dup[1], duplicate_sim=_r(dup[0]))
        elif g is not None:
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(own)
            o.suggestion = (g[1].name, g[1].email, float(g[0]))
            o.why = f"übrig ist nur {c.label}, die Stimme klingt aber eher wie {g[1].name} ({g[0]:.2f}) – bitte bestätigen"
            explain(o, "guard", rival=g[1].name, rival_score=_r(g[0]), own_score=_r(own))
        elif any(p.mode == "never" for p in c.hard):
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(own)
            o.why = f"übrig ist nur {c.label} („nie automatisch“ eingestellt) – bitte bestätigen"
            explain(o, "never")
        elif own is not None and own < P.threshold:
            # the remaining participant has a profile and this voice does not match it: an uninvited person
            # (or the remaining participant did not speak) - a question, not a name
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(own)
            o.why = (f"übrig ist nur {c.label}, aber die Stimme passt nicht zu deren Stimmprofil ({own:.2f}, nötig "
                     f"{P.threshold:.2f}) – bitte bestätigen")
            explain(o, "mismatch", own_score=_r(own), reason=o.why)
        else:
            name_as(o, c, RULE_ELIMINATION, own)
            o.confirm = P.confirm_below > 0 and (own is None or own < P.confirm_below)
            o.why = RULE_TEXT[RULE_ELIMINATION] + (f" (Stimmprofil {own:.2f})" if own is not None else "")
            explain(o, "named")

    # 4. everything still open: why not, and the best suggestion (pre-selected, one click)
    for v in still_open():
        o = out[v.sid]
        if o.explain:
            continue
        sc = scores.get(v.sid, {})
        ranked = sorted(((s, i) for i, s in sc.items() if s is not None and cands[i] in free and i not in done_c),
                        reverse=True)
        prob = []
        if v.embedding is not None:
            for i, c in enumerate(cands):
                if c in free and c.probable and i not in done_c:
                    ps, pp, pk = _prob_score(c, v.embedding, recording)
                    if ps is not None and pp is not None:
                        prob.append((ps, i, pp, pk))
        prob.sort(key=lambda x: -x[0])
        w = world.get(v.sid)
        reason, extra = "", dict[str, Any]()
        if not has_participants:
            reason = "keine Teilnehmerliste für diese Aufnahme (Teilnehmende eintragen oder Termin zuordnen)"
        elif v.embedding is None:
            reason = "keine Stimmmerkmale für diese Stimme"
        elif v.speech_s < P.min_match_speech_s:
            reason = (f"zu wenig Sprache für eine automatische Erkennung ({v.speech_s:.0f} s, mindestens "
                      f"{P.min_match_speech_s:.0f} s)")
        elif not any(c.hard for j, c in enumerate(cands) if c in free and j not in done_c):
            reason = ("kein Stimmprofil der Teilnehmenden" + (" (Name nicht mit einem Profil verknüpft)"
                                                             if prob else ""))
        elif ranked:
            s, i = ranked[0]
            c = cands[i]
            row = [x for x, j in ranked[1:]]
            col = [scores[v2.sid].get(i) for v2 in open_v if v2.sid != v.sid and not out[v2.sid].name
                   and scores.get(v2.sid, {}).get(i) is not None]
            rival_row = row[0] if row else None
            rival_col = max(col) if col else None  # type: ignore[type-var]
            need = P.threshold if (rival_row is not None or rival_col is not None) else P.alone_threshold
            g = guard(v.sid, s)
            if not c.auto_ok:
                reason = f"{c.label}: „nie automatisch“ eingestellt"
            elif s < need:
                reason = f"keine sichere Übereinstimmung: {c.label} {s:.2f}, nötig {need:.2f} (fehlen {need - s:.2f})"
            elif v.sid in pairs and pairs[v.sid][0] == i and 0 <= pairs[v.sid][2] < P.margin:
                gap = pairs[v.sid][2]
                reason = (f"nicht eindeutig: {c.label} {s:.2f}, aber eine andere Verteilung der Stimmen auf die "
                          f"Teilnehmenden ist fast gleich gut (Vorsprung {gap:.2f}, nötig {P.margin:.2f})")
                extra = {"gap": _r(gap)}
            elif rival_row is not None and s - rival_row < P.margin:
                rv = cands[ranked[1][1]].label
                reason = (f"nicht eindeutig: {c.label} {s:.2f} / {rv} {rival_row:.2f} (Abstand {s - rival_row:.2f}, "
                          f"nötig {P.margin:.2f})")
                extra = {"rival": rv, "rival_score": _r(rival_row)}
            elif rival_col is not None and s - rival_col < P.margin:
                reason = (f"nicht eindeutig: eine andere Stimme dieses Gesprächs ähnelt {c.label} ebenso "
                          f"({rival_col:.2f})")
            elif g is not None:
                reason = f"klingt eher wie {g[1].name} ({g[0]:.2f}, nicht eingeladen) als wie {c.label} ({s:.2f})"
                extra = {"rival": g[1].name, "rival_score": _r(g[0])}
            else:
                reason = "keine sichere Übereinstimmung"
        else:
            reason = "alle Teilnehmenden mit Stimmprofil sind schon zugeordnet"
        # pre-selection (one click): the person the best joint assignment gives this voice, else the only
        # participant without a profile who is left, else a probable (unlinked) profile; without a
        # participant list a known voice only when it is clearly the closest one
        joint = pairs.get(v.sid)
        best = ranked[0] if ranked else None
        unprof_left = [c for j, c in enumerate(cands) if c in free and c.expected and not c.hard and j not in done_c]
        if best is not None and not cands[best[1]].auto_ok and best[0] >= P.threshold:
            c = cands[best[1]]  # "nie automatisch": the closest person is still offered
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(best[0])
        elif prob and (best is None or prob[0][0] > best[0]) and prob[0][0] >= P.suggest_min:
            ps, i, pp, pk = prob[0]
            o.preselect, o.preselect_email = cands[i].label, cands[i].person.email
            o.preselect_profile_id, o.preselect_score = pp.id, _r(ps)
            link = "Vorname" if pk == "first_name" else "andere Adresse"
            reason += (f"; Vorschlag {cands[i].label}: Stimmprofil „{pp.name}“ ({ps:.2f}, {link}) – "
                       "einmal bestätigen, dann wird verknüpft")
        elif joint is not None and joint[0] >= 0 and joint[1] >= P.suggest_min:
            c = cands[joint[0]]
            o.preselect, o.preselect_email, o.preselect_score = c.label, c.person.email, _r(joint[1])
        elif joint is not None and joint[0] < 0 and len(unprof_left) == 1 and len(still_open()) <= len(
                [c for j, c in enumerate(cands) if c in free and c.expected and j not in done_c]):
            c = unprof_left[0]
            o.preselect, o.preselect_email = c.label, c.person.email
            reason += f"; vorausgewählt: {c.label} (einzige Person ohne Stimmprofil)"
        elif best is not None and best[0] >= P.suggest_min and joint is None:
            o.preselect, o.preselect_email, o.preselect_score = cands[best[1]].label, cands[best[1]].person.email, _r(best[0])
        elif not has_participants and w is not None and w[0] >= P.adhoc_suggest and w[0] - second_world(v) >= P.margin:
            o.suggestion = (w[1].name, w[1].email, float(w[0]))
            o.preselect, o.preselect_email, o.preselect_profile_id = w[1].name, w[1].email, w[1].id
            o.preselect_score = _r(w[0])
        elif len(left_c) == 1 and len(still_open()) == 1:
            o.preselect, o.preselect_email = left_c[0].label, left_c[0].person.email
            reason += f"; vorausgewählt: einzige noch nicht zugeordnete Person ({left_c[0].label})"
        if not has_participants and w is not None and not o.suggestion and w[0] >= P.threshold:
            o.suggestion = (w[1].name, w[1].email, float(w[0]))
        if w is not None and not o.suggestion and has_participants and w[0] >= P.guard_min:
            o.suggestion = (w[1].name, w[1].email, float(w[0]))
        o.why = reason
        explain(o, "open", reason=reason, **extra)
    return out


# -- presentation (CLI, both UIs) -------------------------------------------------------------------
def explain_lines(e: dict[str, Any] | None, name: str = "", source: str = "") -> list[str]:
    """The stored explanation of one speaker as short German lines. ``source``: the speaker's current
    source - an assignment you made after the identification comes first."""
    if source in ("manual", "self_echo") and (not e or e.get("decision") != "manual"):
        head = ("Entscheidung: selbst zugeordnet (wird nie automatisch geändert)" if source == "manual"
                else "Entscheidung: als eigene Stimme markiert")
        return [head] + ([f"Vorher (automatisch): {line}" for line in explain_lines(e, name)] if e else [])
    if not e:
        return ["keine Begründung gespeichert (Aufnahme einer früheren Version – „Sprecher neu erkennen“)"]
    out: list[str] = []
    d = e.get("decision", "")
    rule = e.get("rule", "")
    if d == "manual":
        out.append("Entscheidung: selbst zugeordnet (wird nie automatisch geändert)")
    elif d == "blocked":
        out.append(f"Nicht zur Zuordnung angeboten: {e.get('reason', '')}")
    elif d == "named":
        sc = e.get("score")
        out.append(f"Automatisch: {name or '?'} – Regel: {RULE_TEXT.get(rule, rule)}"
                   + (f" – Ähnlichkeit {sc:.2f}" if isinstance(sc, (int, float)) else " – ohne Stimmvergleich"))
        if e.get("confirm"):
            out.append("Unsicher: vor der Zusammenfassung bitte bestätigen")
        if e.get("no_learn"):
            out.append(f"Stimmprofil nicht aktualisiert: {e['no_learn']}")
    elif d in ("open", "guard", "duplicate", "never", "mismatch"):
        out.append("Nicht automatisch zugeordnet: " + str(e.get("reason") or {
            "guard": f"klingt eher wie {e.get('rival')} ({e.get('rival_score')}) – nicht eingeladen",
            "duplicate": f"ähnelt {e.get('duplicate_of')} ({e.get('duplicate_sim')})",
            "never": "„nie automatisch“ eingestellt"}.get(d, "")))
    sp = e.get("speech_s")
    if sp is not None:
        out.append(f"Saubere Sprache: {sp:.0f} s")
    cands = e.get("candidates") or []
    if cands:
        parts = []
        for c in cands[:6]:
            sc = "kein Profil" if c.get("score") is None else f"{c['score']:.2f}"
            if c.get("profile", "").startswith("probable") and c.get("probable_score") is not None:
                sc += f" (unverknüpftes Profil „{c.get('profile_name')}“ {c['probable_score']:.2f})"
            tag = " (nicht eingeladen)" if c.get("kind") == "other" else (
                "" if c.get("expected", True) else " (optional/nicht erwartet)")
            parts.append(f"{c.get('name')}{tag}: {sc}")
        out.append("Kandidaten: " + "; ".join(parts))
    p = e.get("params") or {}
    if p and d in ("open", "named"):
        out.append(f"Regeln: Schwelle {p.get('threshold')}, ohne Vergleich {p.get('alone_threshold')}, "
                   f"Abstand {p.get('margin')}, Mindestsprache {p.get('min_match_speech_s')} s")
    return out
