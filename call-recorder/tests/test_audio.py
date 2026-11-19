from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from callrec.audio.capture import (
    MIC_PART,
    REMOTE_PART,
    DeviceSpec,
    DualTrackRecorder,
    TrackWriter,
    downmix_int16,
)
from callrec.audio.devices import base_name, match_device
from callrec.audio.finalize import (
    convert,
    finalize_recording,
    find_audio,
    has_unfinished_parts,
    load_mono,
    write_clip,
)
from callrec.audio.wavwriter import StreamingWavWriter, read_wav_header, repair_wav
from callrec.consent import confirm_consent


def _consent():
    return confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[], method="dialog")


def test_wav_header_is_valid_during_recording(tmp_path: Path):
    p = tmp_path / "a.wav"
    w = StreamingWavWriter(p, 16000, 1, checkpoint_interval_s=0.0)
    w.write(np.arange(1000, dtype=np.int16))
    # file is readable while still open (header checkpointed)
    info = sf.info(str(p))
    assert info.frames == 1000 and info.samplerate == 16000
    w.write_silence(500)
    w.close()
    data, rate = sf.read(str(p), dtype="int16")
    assert rate == 16000 and len(data) == 1500 and data[1200] == 0 and data[999] == 999


def test_repair_after_crash(tmp_path: Path):
    p = tmp_path / "crash.wav"
    w = StreamingWavWriter(p, 8000, 1, checkpoint_interval_s=3600)  # never checkpoints by itself
    w.write(np.ones(8000, dtype=np.int16))
    w._fh.flush()  # simulate: data reached disk, process died before the header update
    assert read_wav_header(p)[3] == 0
    with open(p, "ab") as fh:
        fh.write(b"\x01")  # half a frame
    assert repair_wav(p) is True
    assert read_wav_header(p)[3] == 16000
    assert sf.info(str(p)).frames == 8000
    assert repair_wav(p) is False
    w._fh.close()


def test_header_layout(tmp_path: Path):
    p = tmp_path / "h.wav"
    with StreamingWavWriter(p, 44100, 2) as w:
        w.write(np.zeros((10, 2), dtype=np.int16))
    head = p.read_bytes()[:44]
    assert head[:4] == b"RIFF" and head[8:16] == b"WAVEfmt "
    assert struct.unpack("<I", head[40:44])[0] == 40


def test_downmix():
    stereo = np.array([[100, 300], [-100, -300], [32767, 32767]], dtype=np.int16).tobytes()
    assert downmix_int16(stereo, 2).tolist() == [200, -200, 32767]
    assert downmix_int16(np.array([1, 2], dtype=np.int16).tobytes(), 1).tolist() == [1, 2]


def test_trackwriter_fills_loopback_gaps(tmp_path: Path):
    dev = DeviceSpec("2", "Lautsprecher [Loopback]", 1000, 2, "loopback")
    tw = TrackWriter(tmp_path / "r.wav", dev, t0=100.0, gap_threshold_s=0.5)
    chunk = np.full((100, 2), 1000, dtype=np.int16).tobytes()  # 0.1 s
    tw.process_chunk(chunk, 100.1)  # first chunk on time
    tw.process_chunk(chunk, 100.2)
    # nothing played for ~2 s: next packet arrives at t=102.3
    tw.process_chunk(chunk, 102.3)
    stats = tw.finish(total_seconds=3.0)
    data, rate = sf.read(str(tmp_path / "r.wav"), dtype="int16")
    assert rate == 1000 and len(data) == 3000
    assert stats.gaps == 1
    assert data[150] == 1000 and data[1000] == 0
    assert data[2200:2300].tolist() == [1000] * 100  # positioned at its real time
    assert data[2500] == 0  # padded at the end


def test_trackwriter_small_jitter_not_filled(tmp_path: Path):
    dev = DeviceSpec("1", "Mic", 1000, 1, "mic")
    tw = TrackWriter(tmp_path / "m.wav", dev, t0=0.0, gap_threshold_s=0.5)
    chunk = np.ones(100, dtype=np.int16).tobytes()
    for i in range(1, 11):
        tw.process_chunk(chunk, i * 0.1 + 0.2)  # constant 200 ms latency
    s = tw.finish(1.0)
    assert s.gaps == 0 and s.frames == 1000


class FakeStream:
    def __init__(self, dev, cb):
        self.dev, self.cb, self.active = dev, cb, False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def is_active(self):
        return self.active

    def push(self, seconds: float, t: float, value: int = 500):
        n = int(seconds * self.dev.sample_rate)
        frames = np.full((n, self.dev.channels), value, dtype=np.int16)
        self.cb(frames.tobytes(), t)


class FakeBackend:
    def __init__(self, fail_second=False):
        self.streams: list[FakeStream] = []
        self.fail_second = fail_second

    def resolve_devices(self, a, b):
        return (DeviceSpec("1", "Mic", 48000, 1, "mic"), DeviceSpec("2", "Spk [Loopback]", 44100, 2, "loopback"))

    def open_stream(self, device, on_data, fpb):
        if self.fail_second and self.streams:
            raise OSError("device busy")
        s = FakeStream(device, on_data)
        self.streams.append(s)
        return s

    def close(self):
        pass


def test_dual_track_recorder_aligned_lengths(tmp_path: Path):
    clock = {"t": 10.0}
    be = FakeBackend()
    mic, loop = be.resolve_devices("auto", "auto")
    r = DualTrackRecorder(be, tmp_path, mic, loop, clock=lambda: clock["t"])
    r.start(_consent())
    assert all(s.active for s in be.streams)
    be.streams[0].push(1.0, 11.0)
    be.streams[1].push(0.5, 12.5)  # remote talks only at the end
    clock["t"] = 13.0
    res = r.stop()
    assert res.duration_s == pytest.approx(3.0)
    mic_info = sf.info(str(tmp_path / "audio" / MIC_PART))
    rem_info = sf.info(str(tmp_path / "audio" / REMOTE_PART))
    assert mic_info.samplerate == 48000 and rem_info.samplerate == 44100
    assert mic_info.channels == rem_info.channels == 1
    assert mic_info.duration == pytest.approx(3.0, abs=0.01)
    assert rem_info.duration == pytest.approx(3.0, abs=0.01)
    assert res.remote.inserted_silence_s > 2.0


def test_recorder_health_and_open_failure(tmp_path: Path):
    be = FakeBackend()
    mic, loop = be.resolve_devices("auto", "auto")
    r = DualTrackRecorder(be, tmp_path, mic, loop, clock=lambda: 0.0)
    r.start(_consent())
    be.streams[1].active = False
    assert r.health_warnings() and "Loopback" in r.health_warnings()[0]
    r.stop()

    be2 = FakeBackend(fail_second=True)
    r2 = DualTrackRecorder(be2, tmp_path / "b", mic, loop, clock=lambda: 0.0)
    with pytest.raises(OSError):
        r2.start(_consent())
    assert not r2.is_recording


def test_match_device():
    devs = [
        DeviceSpec("3", "Lautsprecher (Realtek(R) Audio) [Loopback]", 48000, 2, "loopback"),
        DeviceSpec("7", "Kopfhörer (Jabra Evolve2 65) [Loopback]", 48000, 2, "loopback"),
        DeviceSpec("8", "Headset (Jabra Evolve2 65 Hands-Free AG Audio) [Loopback]", 16000, 1, "loopback"),
    ]
    assert base_name(devs[0].name) == "Lautsprecher (Realtek(R) Audio)"
    assert match_device(devs, "Kopfhörer (Jabra Evolve2 65)").id == "7"
    assert match_device(devs, "headset (jabra evolve2 65 hands-free ag audio)").id == "8"
    assert match_device(devs, "Hands-Free").id == "8"
    assert match_device(devs, "Lautsprecher (Realtek(R) Audio) mit sehr langem Namen").id == "3"
    assert match_device(devs, "USB Speaker") is None
    assert match_device(devs, "") is None


@pytest.mark.parametrize("fmt, ext", [("flac", ".flac"), ("opus", ".opus")])
def test_finalize(tmp_path: Path, fmt: str, ext: str):
    from tests.conftest import tone, write_wav

    folder = tmp_path / "rec"
    write_wav(folder / "audio" / MIC_PART, tone(200, 2.0, 48000), 48000)
    write_wav(folder / "audio" / REMOTE_PART, tone(300, 2.0, 44100), 44100)
    assert has_unfinished_parts(folder)
    fa = finalize_recording(folder, fmt=fmt, write_mix=True, keep_wav=False)
    assert fa.mic.suffix == ext and fa.mic.exists() and fa.remote.exists() and fa.mix and fa.mix.exists()
    assert not has_unfinished_parts(folder)
    info = sf.info(str(fa.remote))
    assert info.duration == pytest.approx(2.0, abs=0.05)
    if fmt == "opus":
        assert info.samplerate == 48000  # 44.1 kHz resampled for Opus
    found = find_audio(folder)
    assert found is not None and found.mic == fa.mic
    mono, rate = load_mono(fa.mix, 16000)
    assert rate == 16000 and mono.dtype == np.float32 and abs(len(mono) - 32000) < 400
    clip = write_clip(fa.remote, 0.5, 1.0, tmp_path / "clip.wav")
    assert sf.info(str(clip)).duration == pytest.approx(0.5, abs=0.02)


def test_finalize_recovers_crashed_part(tmp_path: Path):
    from tests.conftest import tone, write_wav

    folder = tmp_path / "rec"
    write_wav(folder / "audio" / MIC_PART, tone(200, 1.0, 16000), 16000)
    write_wav(folder / "audio" / REMOTE_PART, tone(300, 1.0, 16000), 16000)
    # zero out the size fields like after a power loss
    for name in (MIC_PART, REMOTE_PART):
        p = folder / "audio" / name
        b = bytearray(p.read_bytes())
        b[4:8] = b[40:44] = b"\0\0\0\0"
        p.write_bytes(bytes(b))
    fa = finalize_recording(folder, "flac", write_mix=False)
    assert sf.info(str(fa.mic)).frames == 16000


def test_convert_is_blockwise_exact(tmp_path: Path):
    from tests.conftest import tone, write_wav

    src = tmp_path / "long.wav"
    write_wav(src, tone(440, 25.0, 8000), 8000)  # > BLOCK_SECONDS
    dst = convert(src, tmp_path / "long.flac", "flac")
    a, _ = sf.read(str(src), dtype="int16")
    b, _ = sf.read(str(dst), dtype="int16")
    assert np.array_equal(a, b)


def test_wav_writer_rolls_back_partial_write_on_disk_full(tmp_path: Path):
    from callrec.audio.wavwriter import StreamingWavWriter

    path = tmp_path / "w.wav"
    w = StreamingWavWriter(path, 8000, 1)
    w.write(np.arange(100, dtype=np.int16))
    real = w._fh

    class Full:
        """Writes 3 bytes (half a sample pair), then ENOSPC - like a disk filling up."""

        def __init__(self):
            self.calls = 0

        def write(self, b):
            self.calls += 1
            if self.calls == 1:
                return real.write(bytes(b[:3]))
            raise OSError(28, "No space left on device")

        def __getattr__(self, name):
            return getattr(real, name)

    w._fh = Full()  # type: ignore[assignment]
    with pytest.raises(OSError):
        w.write(np.full(50, 7, dtype=np.int16))
    w._fh = real
    w.write(np.full(10, 9, dtype=np.int16))  # space is available again
    w.close()
    data, _ = sf.read(str(path), dtype="int16")
    assert list(data[:100]) == list(range(100)) and list(data[100:]) == [9] * 10  # no shifted garbage


def test_recorder_warns_when_mic_stops_delivering(tmp_path: Path):
    clock = {"t": 0.0}
    be = FakeBackend()
    mic, loop = be.resolve_devices("auto", "auto")
    r = DualTrackRecorder(be, tmp_path, mic, loop, clock=lambda: clock["t"])
    r.start(_consent())
    be.streams[0].push(0.1, 0.1)
    clock["t"] = 3.0
    assert r.health_warnings() == []  # loopback silence alone is normal
    clock["t"] = 8.0  # mic silent for 7.9 s although the stream claims to be active
    w = r.health_warnings()
    assert len(w) == 1 and "Mikrofon liefert seit 7 s keine Daten" in w[0]
    r._writers["remote"].stats.errors.append("[Errno 28] No space left on device")
    assert any("Datenträger voll" in x for x in r.health_warnings())
    res = r.stop()
    assert len(res.warnings) == 2  # each problem recorded once in meta.json


def test_blockwise_load_and_mix_match_whole_file_result(tmp_path: Path):
    import soxr

    from callrec.audio.finalize import build_mix
    from tests.conftest import tone, write_wav

    a = tone(220, 23.0, 48000)  # longer than BLOCK_SECONDS, different lengths and rates
    b = np.concatenate([tone(330, 31.0, 44100), np.zeros(10, dtype=a.dtype)])
    write_wav(tmp_path / "a.wav", a, 48000)
    write_wav(tmp_path / "b.wav", b, 44100)
    ref_b = soxr.resample(sf.read(str(tmp_path / "b.wav"), dtype="float32")[0], 44100, 16000)
    got_b, rate = load_mono(tmp_path / "b.wav", 16000)
    assert rate == 16000 and abs(got_b.size - ref_b.size) <= 2
    m = min(got_b.size, ref_b.size)
    assert np.max(np.abs(got_b[1000 : m - 1000] - ref_b[1000 : m - 1000])) < 1e-3
    same, r2 = load_mono(tmp_path / "a.wav", 48000)
    assert r2 == 48000 and np.allclose(same, sf.read(str(tmp_path / "a.wav"), dtype="float32")[0])
    mix = build_mix(tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "mix.flac", "flac")
    info = sf.info(str(mix))
    assert info.samplerate == 48000 and info.duration == pytest.approx(31.0, abs=0.01)
    data, _ = sf.read(str(mix), dtype="float32")
    fa, _ = sf.read(str(tmp_path / "a.wav"), dtype="float32")
    fb = soxr.resample(sf.read(str(tmp_path / "b.wav"), dtype="float32")[0], 44100, 48000)
    ref = np.zeros(max(fa.size, fb.size), dtype=np.float32)
    ref[: fa.size] += fa
    ref[: fb.size] += fb
    k = min(ref.size, data.size) - 2000
    assert np.max(np.abs(np.clip(ref[2000:k], -1, 1) - data[2000:k])) < 2e-3
