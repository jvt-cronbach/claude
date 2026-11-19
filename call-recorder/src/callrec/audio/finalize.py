"""Convert the streamed WAV parts to FLAC/Opus, build a listening mix, load audio for analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from callrec.audio.capture import MIC_PART, REMOTE_PART
from callrec.audio.wavwriter import repair_wav

log = logging.getLogger(__name__)

OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
BLOCK_SECONDS = 10


@dataclass
class FinalizedAudio:
    mic: Path
    remote: Path
    mix: Path | None


def _ext(fmt: str) -> str:
    return ".flac" if fmt == "flac" else ".opus"


def _sf_args(fmt: str) -> dict[str, str]:
    if fmt == "flac":
        return {"format": "FLAC", "subtype": "PCM_16"}
    if fmt == "opus":
        return {"format": "OGG", "subtype": "OPUS"}
    raise ValueError(f"Unbekanntes Format {fmt!r}")


def convert(src: Path, dst: Path, fmt: str) -> Path:
    """Block-wise conversion (constant memory, also for multi-hour calls)."""
    tmp = dst.with_name(dst.name + ".tmp")
    with sf.SoundFile(str(src)) as fin:
        in_rate = fin.samplerate
        channels = fin.channels
        out_rate = in_rate
        if fmt == "opus" and in_rate not in OPUS_RATES:
            out_rate = 48000
        resampler = soxr.ResampleStream(in_rate, out_rate, channels, dtype="float32") if out_rate != in_rate else None
        with sf.SoundFile(str(tmp), "w", samplerate=out_rate, channels=channels, **_sf_args(fmt)) as fout:
            for block in fin.blocks(blocksize=in_rate * BLOCK_SECONDS, dtype="float32", always_2d=True):
                if resampler is not None:
                    block = resampler.resample_chunk(block)
                fout.write(block)
            if resampler is not None:
                fout.write(resampler.resample_chunk(np.zeros((0, channels), dtype="float32"), last=True))
    tmp.replace(dst)
    return dst


def load_mono(path: Path, target_rate: int | None = 16000) -> tuple[np.ndarray, int]:
    """Load an audio file as float32 mono, optionally resampled."""
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1).astype(np.float32)
    if target_rate and rate != target_rate:
        mono = soxr.resample(mono, rate, target_rate).astype(np.float32)
        rate = target_rate
    return mono, int(rate)


def build_mix(mic: Path, remote: Path, dst: Path, fmt: str, rate: int = 48000) -> Path:
    a, _ = load_mono(mic, rate)
    b, _ = load_mono(remote, rate)
    n = max(a.size, b.size)
    mix = np.zeros(n, dtype=np.float32)
    mix[: a.size] += a
    mix[: b.size] += b
    np.clip(mix, -1.0, 1.0, out=mix)
    tmp = dst.with_name(dst.name + ".tmp")
    sf.write(str(tmp), mix, rate, **_sf_args(fmt))
    tmp.replace(dst)
    return dst


def audio_paths(folder: Path, fmt: str) -> FinalizedAudio:
    d = folder / "audio"
    mix = d / f"mix{_ext(fmt)}"
    return FinalizedAudio(mic=d / f"mic{_ext(fmt)}", remote=d / f"remote{_ext(fmt)}", mix=mix if mix.exists() else None)


def find_audio(folder: Path) -> FinalizedAudio | None:
    """Locate final audio files in whatever format they were written."""
    for fmt in ("flac", "opus"):
        p = audio_paths(folder, fmt)
        if p.mic.exists() and p.remote.exists():
            return p
    return None


def has_unfinished_parts(folder: Path) -> bool:
    d = folder / "audio"
    return (d / MIC_PART).exists() or (d / REMOTE_PART).exists()


def finalize_recording(folder: Path, fmt: str = "flac", write_mix: bool = True, keep_wav: bool = False) -> FinalizedAudio:
    """Repair (after a crash), convert and verify both tracks; optionally mix."""
    d = folder / "audio"
    out = audio_paths(folder, fmt)
    parts = {"mic": d / MIC_PART, "remote": d / REMOTE_PART}
    for key, part in parts.items():
        target = out.mic if key == "mic" else out.remote
        if not part.exists():
            if target.exists():
                continue
            raise FileNotFoundError(f"Audiospur fehlt: {part}")
        if repair_wav(part):
            log.warning("WAV-Kopf von %s repariert (Absturz während der Aufnahme?)", part.name)
        convert(part, target, fmt)
        info_in = sf.info(str(part))
        info_out = sf.info(str(target))
        if abs(info_out.duration - info_in.duration) > 0.1:
            raise RuntimeError(f"Konvertierung von {part.name} unvollständig - WAV bleibt erhalten")
    mix: Path | None = None
    if write_mix:
        mix = build_mix(out.mic, out.remote, d / f"mix{_ext(fmt)}", fmt)
    if not keep_wav:
        for part in parts.values():
            if part.exists():
                part.unlink()
    return FinalizedAudio(mic=out.mic, remote=out.remote, mix=mix)


def write_clip(src: Path, start: float, end: float, dst: Path) -> Path:
    """Cut [start, end) seconds into a 16-bit WAV (for 'listen to this speaker')."""
    info = sf.info(str(src))
    rate = info.samplerate
    s = max(0, int(start * rate))
    e = min(info.frames, int(end * rate))
    data, _ = sf.read(str(src), start=s, stop=e, dtype="float32", always_2d=True)
    sf.write(str(dst), data, rate, subtype="PCM_16", format="WAV")
    return dst
