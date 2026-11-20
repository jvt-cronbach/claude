from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from callrec.models import Person, SpeakerTurn
from callrec.speakers.diarize import l2_normalize, renumber_turns, speaker_embeddings
from callrec.speakers.profiles import PlainFilePersistence, ProfileStore, cosine

DIM = 16
MODEL = "wespeaker_en_voxceleb_resnet34_LM"


def vec(seed: int, noise: float = 0.0, base: np.ndarray | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = base.copy() if base is not None else rng.normal(size=DIM)
    if noise:
        v = v + rng.normal(scale=noise, size=DIM)
    return l2_normalize(v)


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(PlainFilePersistence(tmp_path / "voiceprints.json"))


ANNA, BERND, CARLA = vec(1), vec(2), vec(3)


def test_add_update_persist_delete(store: ProfileStore, tmp_path: Path):
    p = store.add_sample("Anna Beispiel", "anna@firma.de", ANNA, MODEL)
    assert p.samples == 1
    p2 = store.add_sample("anna beispiel", "", vec(9, 0.1, ANNA), MODEL)  # same person by name
    assert p2.id == p.id and p2.samples == 2
    assert cosine(p2.embedding, ANNA) > 0.9
    store.add_sample("Bernd Muster", "bernd@firma.de", BERND, MODEL)
    again = ProfileStore(PlainFilePersistence(tmp_path / "voiceprints.json"))
    assert [x.name for x in again.all_profiles()] == ["Anna Beispiel", "Bernd Muster"]
    assert [x.name for x in again.delete("ANNA@firma.de")] == ["Anna Beispiel"]
    assert again.delete("niemand") == []
    assert again.delete_all() == 1
    assert ProfileStore(PlainFilePersistence(tmp_path / "voiceprints.json")).all_profiles() == []


def test_match_threshold_and_one_to_one(store: ProfileStore):
    store.add_sample("Anna Beispiel", "anna@firma.de", ANNA, MODEL)
    store.add_sample("Bernd Muster", "bernd@firma.de", BERND, MODEL)
    emb = {"S1": vec(11, 0.2, BERND), "S2": vec(12, 0.2, ANNA), "S3": CARLA, "S4": vec(13, 0.15, ANNA)}
    m = store.match(emb, MODEL, candidates=[], threshold=0.6)
    assert m["S1"].profile.name == "Bernd Muster"
    assert "S3" not in m
    # Anna is assigned once (to the closer one of S2/S4); the other one only gets a suggestion
    anna = [k for k, v in m.items() if v.profile.name == "Anna Beispiel"]
    assert len(anna) == 1
    dec = store.decide(emb, MODEL, participants=None, threshold=0.6)
    other = ({"S2", "S4"} - set(anna)).pop()
    assert dec[other].match is None and dec[other].suggestion is not None
    assert "gleiche Stimme" in dec[other].reason


def test_invitee_bonus_in_all_profiles_mode(store: ProfileStore):
    store.add_sample("Anna Beispiel", "anna@firma.de", ANNA, MODEL)
    probe = {"S1": vec(22, 0.3, ANNA)}
    sim = cosine(probe["S1"], store.find("Anna Beispiel", "", MODEL).embedding)  # type: ignore[union-attr]
    m = store.match(probe, MODEL, candidates=[Person("Anna Beispiel", "anna@firma.de")], threshold=sim + 0.1,
                    invitee_bonus=0.2)
    assert m["S1"].score == pytest.approx(sim + 0.2)
    assert store.match(probe, MODEL, candidates=[], threshold=sim + 0.01) == {}


def test_model_isolation_and_exclude_self(store: ProfileStore):
    store.add_sample("Ich Selbst", "ich@firma.de", ANNA, MODEL)
    store.add_sample("Anna Beispiel", "anna@firma.de", ANNA, "other_model")
    assert store.match({"S1": ANNA}, MODEL, [], 0.5, exclude=[Person("Ich Selbst", "ich@firma.de")]) == {}
    assert store.match({"S1": ANNA}, "other_model", [], 0.5)["S1"].profile.name == "Anna Beispiel"


def test_corrupt_or_undecryptable_store_is_not_overwritten(tmp_path: Path):
    class Broken:
        def load(self):
            raise PermissionError("DPAPI decryption failed")

        def save(self, content):  # pragma: no cover
            raise AssertionError("must not overwrite")

    s = ProfileStore(Broken())  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        s.add_sample("X", "", ANNA, MODEL)


def test_renumber_and_speaker_embeddings():
    turns = renumber_turns([SpeakerTurn(5, 9, "raw1"), SpeakerTurn(0, 4, "raw0"), SpeakerTurn(10, 10.5, "raw2")])
    assert [(t.speaker, t.start) for t in turns] == [("S1", 0), ("S2", 5), ("S3", 10)]

    class FakeEmb:
        model_id = MODEL
        calls: list[int] = []

        def embed(self, samples, sr):
            FakeEmb.calls.append(len(samples))
            return np.full(DIM, float(samples[0]) + 1.0)

    sr = 100
    samples = np.zeros(1200, dtype=np.float32)
    samples[0:400] = 1.0  # S1
    samples[500:900] = 2.0  # S2
    samples[1000:1050] = 3.0  # S3 (short only -> concatenated fallback)
    embs = speaker_embeddings(turns, samples, sr, FakeEmb())
    assert set(embs) == {"S1", "S2", "S3"}
    assert all(abs(np.linalg.norm(v) - 1) < 1e-5 for v in embs.values())
    assert max(FakeEmb.calls) <= 10 * sr


def test_protected_profile_file_is_replaced_atomically(tmp_path: Path, monkeypatch):
    import os

    from callrec.speakers.profiles import AtomicProtectedFile

    class Agent:  # stands in for DPAPI
        def protect(self, s: str) -> bytes:
            return s.encode("utf-8")[::-1]

        def unprotect(self, b: bytes) -> str:
            return b[::-1].decode("utf-8")

    path = tmp_path / "voiceprints.bin"
    store = ProfileStore(AtomicProtectedFile(path, Agent()))
    assert store.all_profiles() == []  # missing file = no profiles
    store.add_sample("Jürgen Müller", "j@x.de", np.ones(4), "m")
    before = path.read_bytes()

    def crash(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        store.add_sample("Anna", "", np.ones(4), "m")
    assert path.read_bytes() == before  # old profiles intact after a failed save
    monkeypatch.undo()
    assert [p.name for p in ProfileStore(AtomicProtectedFile(path, Agent())).all_profiles()] == ["Jürgen Müller"]


def test_replace_file_retries_transient_sharing_violation(tmp_path: Path, monkeypatch):
    import os

    from callrec.recording import replace_file

    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "used by another process")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    (tmp_path / "a").write_text("x")
    replace_file(tmp_path / "a", tmp_path / "b", delay_s=0)
    assert (tmp_path / "b").read_text() == "x" and calls["n"] == 3
