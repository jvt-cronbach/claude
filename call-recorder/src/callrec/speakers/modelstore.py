"""Download and verify the (non-gated) sherpa-onnx diarization models.

Models come from the k2-fsa/sherpa-onnx GitHub releases; no HuggingFace
account or token is needed. SHA-256 sums were computed from the release files.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelFile:
    key: str
    url: str
    sha256: str
    filename: str  # final file name inside the models dir
    archive_member: str = ""  # for .tar.bz2 downloads: member to extract


SEGMENTATION = ModelFile(
    key="pyannote-segmentation-3-0",
    url=f"{_RELEASES}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    sha256="24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
    filename="pyannote-segmentation-3-0.onnx",
    archive_member="sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
)

EMBEDDING_MODELS: dict[str, ModelFile] = {
    m.key: m
    for m in (
        ModelFile(
            key="wespeaker_en_voxceleb_resnet34_LM",
            url=f"{_RELEASES}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx",
            sha256="e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
            filename="wespeaker_en_voxceleb_resnet34_LM.onnx",
        ),
        ModelFile(
            key="3dspeaker_speech_campplus_sv_en_voxceleb_16k",
            url=f"{_RELEASES}/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
            sha256="98daf9bcf1c8abf3db374fec4c27a5eda7c58705bb3f180d057b085a72a88bb5",
            filename="3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
        ),
        ModelFile(
            key="nemo_en_titanet_small",
            url=f"{_RELEASES}/speaker-recongition-models/nemo_en_titanet_small.onnx",
            sha256="ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
            filename="nemo_en_titanet_small.onnx",
        ),
    )
}


class ModelError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dst: Path, progress: Callable[[float], None] | None = None) -> None:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dst, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)


def ensure_model(
    model: ModelFile,
    models_dir: Path,
    downloader: Callable[[str, Path], None] | None = None,
) -> Path:
    """Return the local path of ``model``, downloading + verifying it if needed."""
    target = models_dir / "sherpa" / model.filename
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("Lade Modell %s herunter ...", model.key)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmpdir:
        tmp = Path(tmpdir) / "download"
        (downloader or _download)(model.url, tmp)
        digest = sha256_of(tmp)
        if digest != model.sha256:
            raise ModelError(f"Prüfsumme von {model.key} stimmt nicht ({digest})")
        if model.archive_member:
            with tarfile.open(tmp, "r:*") as tar:
                member = tar.getmember(model.archive_member)
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ModelError(f"{model.archive_member} fehlt im Archiv")
                part = Path(tmpdir) / "member"
                with open(part, "wb") as out:
                    shutil.copyfileobj(extracted, out)
            part.replace(target)
        else:
            tmp.replace(target)
    return target


def embedding_model(key: str) -> ModelFile:
    try:
        return EMBEDDING_MODELS[key]
    except KeyError:
        raise ModelError(
            f"Unbekanntes Embedding-Modell {key!r}. Verfügbar: {', '.join(EMBEDDING_MODELS)}"
        ) from None
