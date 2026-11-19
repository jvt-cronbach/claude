from __future__ import annotations

from datetime import datetime

from callrec.models import AsrResult, AsrSegment, Person, SpeakerInfo, SpeakerTurn, Utterance, Word
from callrec.people import format_participants, parse_participants_text
from callrec.speakers.assign import UNKNOWN, TurnIndex, assign_remote, assign_single
from callrec.transcript import fmt_ts, merge_utterances, render_lines, render_markdown
from tests.conftest import BERLIN


def W(s, e, t):
    return Word(s, e, " " + t)


def test_fmt_ts():
    assert fmt_ts(0) == "00:00:00"
    assert fmt_ts(3725.9) == "01:02:05"
    assert fmt_ts(-3) == "00:00:00"


def test_assign_remote_splits_on_speaker_change():
    asr = AsrResult("de", 10, [AsrSegment(0, 6, "Hallo zusammen. Hallo Anna.", [
        W(0.0, 0.5, "Hallo"), W(0.5, 1.2, "zusammen."), W(3.0, 3.4, "Hallo"), W(3.4, 3.9, "Anna."),
    ])])
    turns = [SpeakerTurn(0.0, 1.5, "S1"), SpeakerTurn(2.8, 4.2, "S2")]
    utts = assign_remote(asr, turns)
    assert [(u.speaker, u.text) for u in utts] == [("S1", "Hallo zusammen."), ("S2", "Hallo Anna.")]
    assert utts[1].start == 3.0 and utts[1].track == "remote"


def test_assign_uses_overlap_nearest_and_previous():
    idx = TurnIndex([SpeakerTurn(0, 2, "S1"), SpeakerTurn(2, 4, "S2")])
    assert idx.speaker_for(1.5, 2.3) == "S1"  # more overlap with S1
    assert idx.speaker_for(4.5, 4.8) == "S2"  # nearest within 1 s
    assert idx.speaker_for(9, 10) is None
    asr = AsrResult("de", 20, [AsrSegment(0, 20, "", [W(0.5, 1, "eins"), W(10, 10.5, "zwei")])])
    utts = assign_remote(asr, [SpeakerTurn(0, 2, "S1"), SpeakerTurn(3, 4, "S2")])
    assert all(u.speaker == "S1" for u in utts)  # falls back to previous speaker
    utts2 = assign_remote(AsrResult("de", 5, [AsrSegment(0, 1, "", [W(30, 31, "x")])]), [SpeakerTurn(0, 1, "S1"), SpeakerTurn(1, 2, "S2")])
    assert utts2[0].speaker == UNKNOWN


def test_assign_single_and_gap_split():
    asr = AsrResult("de", 30, [
        AsrSegment(0, 2, "Guten Morgen.", [W(0, 0.6, "Guten"), W(0.6, 1.2, "Morgen.")]),
        AsrSegment(10, 12, "Noch was.", []),  # no word timestamps -> segment as one word
    ])
    utts = assign_single(asr, "ME", "mic")
    assert [u.text for u in utts] == ["Guten Morgen.", "Noch was."]
    assert all(u.speaker == "ME" for u in utts)


def test_merge_chronological_and_fuse():
    mic = [Utterance(0, 2, "ME", "Hallo.", "mic"), Utterance(10, 12, "ME", "Gut.", "mic"), Utterance(12.5, 13, "ME", "Danke.", "mic")]
    remote = [Utterance(3, 5, "S1", "Hi.", "remote"), Utterance(6, 8, "S2", "Moin.", "remote")]
    merged = merge_utterances(mic, remote)
    assert [(u.speaker, u.text) for u in merged] == [
        ("ME", "Hallo."), ("S1", "Hi."), ("S2", "Moin."), ("ME", "Gut. Danke."),
    ]


def test_render_markdown_and_lines():
    utts = [Utterance(5, 7, "ME", "Willkommen.", "mic"), Utterance(8, 9, "S1", "Danke.", "remote"),
            Utterance(9.5, 10, "S2", "Ebenfalls.", "remote")]
    speakers = {
        "ME": SpeakerInfo("ME", "Ich", name="Ich Selbst", source="self"),
        "S1": SpeakerInfo("S1", "Sprecher 1", name="Anna Beispiel", source="manual"),
        "S2": SpeakerInfo("S2", "Sprecher 2"),
    }
    md = render_markdown("Projekt Jour fixe", datetime(2026, 11, 19, 14, 0, tzinfo=BERLIN), 2712,
                         [Person("Anna Beispiel", "anna@firma.de")], utts, speakers, "faster-whisper large-v3 (cuda)")
    assert md.startswith("# Transkript: Projekt Jour fixe")
    assert "Donnerstag, 19.11.2026, 14:00 Uhr" in md
    assert "**Dauer:** 00:45:12" in md
    assert "**[00:00:05] Ich Selbst:** Willkommen." in md
    assert "**[00:00:08] Anna Beispiel:** Danke." in md
    assert "**[00:00:09] Sprecher 2:** Ebenfalls." in md
    assert "Sprecher 2 noch nicht zugeordnet" in md
    assert render_lines(utts, speakers)[1] == "[00:00:08] Anna Beispiel: Danke."


def test_same_name_clusters_fused_for_display():
    utts = [Utterance(0, 1, "S1", "Teil eins", "remote"), Utterance(1.5, 2, "S3", "Teil zwei", "remote")]
    speakers = {"S1": SpeakerInfo("S1", "Sprecher 1", name="Anna"), "S3": SpeakerInfo("S3", "Sprecher 3", name="Anna")}
    assert render_lines(utts, speakers) == ["[00:00:00] Anna: Teil eins Teil zwei"]


def test_people_parsing():
    people = parse_participants_text('Anna Beispiel <anna@firma.de>\nbernd@firma.de; Carla\n\n"Müller, Dieter" <d.mueller@firma.de>\nCarla')
    assert [(p.name, p.email) for p in people] == [
        ("Anna Beispiel", "anna@firma.de"), ("bernd@firma.de", "bernd@firma.de"), ("Carla", ""),
        ("Müller, Dieter", "d.mueller@firma.de"),
    ]
    assert parse_participants_text(format_participants(people)) == people
