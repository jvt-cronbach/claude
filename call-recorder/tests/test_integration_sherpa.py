"""Real sherpa-onnx diarization + embeddings + voice-profile learning on synthetic German speech.

Skipped unless CALLREC_INTEGRATION_MODELS points to a directory containing
  sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 (as 'seg.tar.bz2'),
  wespeaker_en_voxceleb_resnet34_LM.onnx and the piper TTS voices
  and speech/TEXTS_<n>_<i>_<voice>.wav: the sentences below rendered with the piper
  voices vits-piper-de_DE-{thorsten-medium,eva_k-x_low,karlsson-low} (sherpa-onnx TTS;
  all models from the k2-fsa/sherpa-onnx GitHub releases).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

MODELS = os.environ.get("CALLREC_INTEGRATION_MODELS")
pytestmark = pytest.mark.skipif(not MODELS, reason="CALLREC_INTEGRATION_MODELS nicht gesetzt")

VOICES = {
    "Thorsten": "vits-piper-de_DE-thorsten-medium/de_DE-thorsten-medium.onnx",
    "Eva": "vits-piper-de_DE-eva_k-x_low/de_DE-eva_k-x_low.onnx",
    "Karl": "vits-piper-de_DE-karlsson-low/de_DE-karlsson-low.onnx",
}  # TTS voices used to render the speech files

TEXTS_1 = [
    ("Thorsten", "Guten Tag zusammen, ich fasse kurz den Stand des Projekts zusammen. Die Migration ist zu achtzig Prozent abgeschlossen."),
    ("Eva", "Danke. Bei uns im Vertrieb gibt es noch offene Fragen zur Preisgestaltung, die wir bis Freitag klären müssen."),
    ("Thorsten", "Das nehme ich mit. Ich spreche morgen mit der Geschäftsführung und melde mich dann bei allen."),
    ("Karl", "Von der technischen Seite sehe ich keine Probleme. Die Tests laufen stabil und die Server sind bereit."),
    ("Eva", "Sehr gut, dann halten wir fest: Der Termin für den Start bleibt der erste Dezember."),
    ("Karl", "Einverstanden. Ich schicke heute Nachmittag noch die Zusammenfassung der Testergebnisse herum."),
]
TEXTS_2 = [
    ("Karl", "Hallo, kurze Rückmeldung zur Infrastruktur: Das neue Rechenzentrum ist seit gestern in Betrieb."),
    ("Eva", "Prima, die Kunden haben schon gefragt, wann die neue Version verfügbar ist."),
    ("Thorsten", "Wir planen die Freigabe für nächste Woche Mittwoch, wenn nichts dazwischen kommt."),
    ("Eva", "Dann bereite ich die Ankündigung für den Newsletter vor und stimme sie mit dem Marketing ab."),
    ("Karl", "Ich kümmere mich um das Monitoring und richte die Alarme für die erste Woche ein."),
]


def build_conversation(models: Path, set_name: str, texts, rate: int = 16000):
    """Concatenate pre-rendered utterances (speech/<set>_<i>_<voice>.wav) with pauses."""
    import soundfile as sf
    import soxr

    parts, truth, t = [], [], 0.0
    for i, (voice, _text) in enumerate(texts):
        data, sr = sf.read(str(models / "speech" / f"{set_name}_{i}_{voice}.wav"), dtype="float32")
        samples = soxr.resample(data, sr, rate).astype(np.float32)
        parts.append(samples)
        truth.append((t, t + len(samples) / rate, voice))
        t += len(samples) / rate
        parts.append(np.zeros(int(0.8 * rate), dtype=np.float32))
        t += 0.8
    return np.concatenate(parts), truth


def majority_truth(turn, truth):
    best, voice = 0.0, None
    for s, e, v in truth:
        ov = max(0.0, min(turn.end, e) - max(turn.start, s))
        if ov > best:
            best, voice = ov, v
    return voice


def test_real_diarization_and_profile_learning(tmp_path: Path):
    from callrec.speakers.diarize import SherpaDiarizer, SherpaEmbedder, speaker_embeddings
    from callrec.speakers.modelstore import EMBEDDING_MODELS, SEGMENTATION, ensure_model
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore

    models = Path(MODELS)  # type: ignore[arg-type]
    store_dir = tmp_path / "models"

    def local_download(url: str, dst: Path) -> None:  # offline "download" from the prepared dir
        name = "seg.tar.bz2" if url.endswith(".tar.bz2") else url.rsplit("/", 1)[1]
        shutil.copy(models / name, dst)

    seg = ensure_model(SEGMENTATION, store_dir, downloader=local_download)
    emb_model = EMBEDDING_MODELS["wespeaker_en_voxceleb_resnet34_LM"]
    emb = ensure_model(emb_model, store_dir, downloader=local_download)
    assert seg.name == "pyannote-segmentation-3-0.onnx" and seg.stat().st_size > 1_000_000

    diarizer = SherpaDiarizer(seg, emb)  # default threshold
    embedder = SherpaEmbedder(emb, emb_model.key)
    assert diarizer.sample_rate == 16000

    audio1, truth1 = build_conversation(models, "TEXTS_1", TEXTS_1)
    progress: list[float] = []
    turns = diarizer.diarize(audio1, 16000, progress.append)
    assert progress and progress[-1] == pytest.approx(1.0)
    mapping: dict[str, set[str]] = {}
    for t in turns:
        mapping.setdefault(t.speaker, set()).add(majority_truth(t, truth1))  # type: ignore[arg-type]
    print("Diarisierung Gespräch 1:", {k: sorted(v) for k, v in mapping.items()})
    assert len(mapping) == 3, mapping
    assert all(len(v) == 1 for v in mapping.values()), mapping

    embs = speaker_embeddings(turns, audio1, 16000, embedder)
    store = ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))
    for spk, vec in embs.items():
        (name,) = mapping[spk]
        store.add_sample(name, "", vec, emb_model.key)

    # second call with different sentences and a different order
    audio2, truth2 = build_conversation(models, "TEXTS_2", TEXTS_2)
    turns2 = diarizer.diarize(audio2, 16000, lambda f: None)
    embs2 = speaker_embeddings(turns2, audio2, 16000, embedder)
    matches = store.match(embs2, emb_model.key, candidates=[], threshold=0.55)
    print("Erkennung Gespräch 2:", {k: (m.profile.name, round(m.similarity, 3)) for k, m in matches.items()})
    truth_by_spk = {}
    for t in turns2:
        truth_by_spk.setdefault(t.speaker, set()).add(majority_truth(t, truth2))
    for spk, m in matches.items():
        assert {m.profile.name} == truth_by_spk[spk]
    assert len(matches) == 3


class TruthTranscriber:
    """ASR stand-in: on the remote track one word in the middle of every true utterance (with its voice
    as text), so that every voice the diarization finds has text in the transcript."""

    def __init__(self, truth):
        self.truth = truth

    def __call__(self):
        return self

    def transcribe(self, audio: Path, out_json: Path, *, initial_prompt: str = "", hotwords: str = "",
                   progress=lambda f: None):
        from callrec.models import AsrResult, AsrSegment, Word

        segs = []
        if not audio.name.startswith("mic"):
            for s, e, voice in self.truth:
                m = (s + e) / 2
                segs.append(AsrSegment(m - 0.3, m + 0.3, voice, [Word(m - 0.3, m + 0.3, f" {voice}")]))
        return AsrResult("de", self.truth[-1][1], segs, "fake-large", "cpu", "int8")


@pytest.mark.parametrize("child", [False, True], ids=["in-process", "speaker-worker-process"])
def test_pipeline_with_real_diarization(tmp_path: Path, child: bool):
    """Full pipeline (fake ASR, real sherpa-onnx) on a recording folder with 44.1 kHz remote track.

    Up to 0.4.0 this test expected ``unassigned_speakers`` to equal the number of raw clusters. That
    was a wrong expectation, not a defect: ``unassigned_speakers`` counts the open speakers *with text*
    (the old fake ASR only had words in the first 6 s, i.e. one speaker), and a short fragment (here a
    4-s second cluster of one voice) is by design not offered for naming - its text goes to the voice it
    resembles. The checks below test exactly that with real diarization output."""
    import soxr

    from callrec.audio.capture import REMOTE_PART
    from callrec.audio.finalize import load_mono
    from callrec.config import Config
    from callrec.pipeline import Components, Pipeline
    from callrec.speakers.diarize import SherpaDiarizer, SherpaEmbedder
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from tests.conftest import make_recording, write_wav
    from tests.test_pipeline import FakeSummarizer

    models = Path(MODELS)  # type: ignore[arg-type]
    seg = models / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    emb = models / "wespeaker_en_voxceleb_resnet34_LM.onnx"
    audio, truth = build_conversation(models, "TEXTS_1", TEXTS_1)
    rec = make_recording(tmp_path / "r", seconds=len(audio) / 16000)
    write_wav(rec.folder / "audio" / REMOTE_PART, soxr.resample(audio, 16000, 44100), 44100)
    store = ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))
    cfg = Config()
    speaker_worker = None
    if child:
        from callrec.speakers.worker import run_in_child

        d = cfg.diarization

        def speaker_worker(remote, threads, work_dir, progress):
            job = {"audio": str(remote), "segmentation": str(seg), "embedding": str(emb),
                   "embedding_id": "wespeaker_en_voxceleb_resnet34_LM", "cluster_threshold": d.cluster_threshold,
                   "num_speakers": d.num_speakers, "min_duration_on": d.min_duration_on,
                   "min_duration_off": d.min_duration_off, "threads": threads}
            return run_in_child(job, work_dir, progress)

    comps = Components(
        transcriber_factory=TruthTranscriber(truth),
        diarizer_factory=lambda: SherpaDiarizer(seg, emb),
        embedder_factory=lambda: SherpaEmbedder(emb, "wespeaker_en_voxceleb_resnet34_LM"),
        profiles=store,
        summarizer_factory=FakeSummarizer,
        audio_loader=load_mono,
        speaker_worker=speaker_worker,
    )
    result = Pipeline(cfg, comps).run(rec)
    from callrec.review import load_turns

    purity: dict[str, set[str]] = {}
    for t in load_turns(rec):
        purity.setdefault(t.speaker, set()).add(majority_truth(t, truth))  # type: ignore[arg-type]
    print("Cluster -> Stimme:", {k: sorted(v) for k, v in purity.items()})
    # every cluster contains exactly one real voice (over-splitting is tolerated and fixed in review)
    assert all(len(v) == 1 for v in purity.values())
    assert set().union(*purity.values()) == {"Thorsten", "Eva", "Karl"}
    n = len(purity)
    embs, model_id = rec.load_embeddings()
    assert model_id == "wespeaker_en_voxceleb_resnet34_LM" and len(embs) == n
    assert all(v.shape == (256,) for v in embs.values())
    assert rec.load_meta()["processing"]["diarization"]["speakers"] == n
    speakers = rec.load_speakers()
    minor = {sid for sid, s in speakers.items() if s.minor}
    # offered for naming: every cluster with text except the short fragments - one per real voice here
    assert result.unassigned_speakers == n - len(minor) == 3
    # no text is lost or given to a wrong voice: each utterance's word names its true voice and lands
    # at a speaker whose cluster(s) contain only that voice
    utts = [u for u in rec.load_utterances() if u.track == "remote"]
    assert sorted(u.text.strip() for u in utts) == sorted(v for _s, _e, v in truth)
    by_speaker: dict[str, set[str]] = {}
    for u in utts:
        by_speaker.setdefault(u.speaker, set()).add(u.text.strip())
    assert all(len(v) == 1 for v in by_speaker.values()), by_speaker
    assert not minor & set(by_speaker), "a fragment kept its own text instead of going to its voice"
