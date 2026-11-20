"""WASAPI capture via PyAudioWPatch, default-device lookup via pycaw (Windows only).

Everything Windows-specific is imported lazily inside functions so that this
module can be imported (and type-checked) on any platform.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from callrec.audio.capture import DataCallback, DeviceSpec
from callrec.audio.devices import base_name, match_device
from callrec.detect import match_call_process

log = logging.getLogger(__name__)

# Windows Core Audio constants (also in pycaw.constants)
E_RENDER = 0
E_CAPTURE = 1
E_COMMUNICATIONS = 2
DEVICE_STATE_ACTIVE = 0x1


class WasapiUnavailable(RuntimeError):
    pass


def _require_windows() -> None:
    if sys.platform != "win32":
        raise WasapiUnavailable("Audioaufnahme ist nur unter Windows verfügbar.")


@contextlib.contextmanager
def com_initialized() -> Iterator[None]:
    """COM must be initialised in every thread that calls pycaw."""
    import comtypes

    comtypes.CoInitialize()
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def default_endpoint_name(flow: int, role: int = E_COMMUNICATIONS) -> tuple[str, str] | None:
    """(friendly name, endpoint id) of the Windows default device for ``role``."""
    _require_windows()
    from pycaw.pycaw import AudioUtilities

    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        dev = enumerator.GetDefaultAudioEndpoint(flow, role)
        wrapped = AudioUtilities.CreateDevice(dev)
    except Exception:
        log.exception("Standardgerät (flow=%s, role=%s) nicht ermittelbar", flow, role)
        return None
    if wrapped is None:
        return None
    return str(wrapped.FriendlyName or ""), str(wrapped.id or "")


@dataclass(frozen=True)
class CallAppSession:
    """An audio session of the call app (or a helper process it started) on a capture endpoint."""

    endpoint: str
    process: str  # e.g. "ms-teams.exe" or "msedgewebview2.exe (gestartet von ms-teams.exe)"
    pid: int
    active: bool  # AudioSessionStateActive (the app is capturing right now)

    def describe(self) -> str:
        return f"{self.process} (PID {self.pid}) auf {self.endpoint}"


def _ancestor_names(proc: Any, limit: int = 4) -> list[str]:
    names: list[str] = []
    try:
        for parent in proc.parents()[:limit]:
            with contextlib.suppress(Exception):
                names.append(str(parent.name()))
    except Exception:
        pass
    return names


def call_app_sessions(flow: int, process_names: set[str] | list[str]) -> list[CallAppSession]:
    """All audio sessions (active or not) of the configured call app on active endpoints of ``flow``.

    Must run in a thread with COM initialised (:func:`com_initialized`). Errors for single
    devices/sessions are skipped (devices come and go, e.g. Bluetooth profile switches)."""
    _require_windows()
    from pycaw.pycaw import AudioUtilities, IAudioSessionControl2
    from pycaw.utils import AudioSession

    found: list[CallAppSession] = []
    for dev in AudioUtilities.GetAllDevices(flow, DEVICE_STATE_ACTIVE):
        if dev is None:
            continue
        try:
            enum = dev.AudioSessionManager.GetSessionEnumerator()
            count = enum.GetCount()
        except Exception:
            continue
        for i in range(count):
            try:
                ctl = enum.GetSession(i)
                if ctl is None:
                    continue
                session = AudioSession(ctl.QueryInterface(IAudioSessionControl2))
                proc = session.Process
                if proc is None:
                    continue
                matched = match_call_process(str(proc.name()), _ancestor_names(proc), process_names)
                if matched is None:
                    continue
                found.append(CallAppSession(str(dev.FriendlyName), matched, int(proc.pid),
                                            int(session.State) == 1))  # 1 = AudioSessionStateActive
            except Exception:
                continue
    return found


def endpoints_with_active_session(flow: int, process_names: set[str]) -> list[str]:
    """Friendly names of endpoints on which one of ``process_names`` (or a helper process started
    by one of them) has an *active* audio session."""
    names: list[str] = []
    for s in call_app_sessions(flow, process_names):
        if s.active and s.endpoint not in names:
            names.append(s.endpoint)
    return names


class _PaStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def start(self) -> None:
        self._stream.start_stream()

    def stop(self) -> None:
        try:
            if self._stream.is_active():
                self._stream.stop_stream()
        finally:
            self._stream.close()

    def is_active(self) -> bool:
        return bool(self._stream.is_active())


class WasapiBackend:
    """CaptureBackend implementation for Windows."""

    def __init__(self, teams_processes: list[str] | None = None) -> None:
        _require_windows()
        import pyaudiowpatch as pyaudio

        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        self.teams_processes = set(teams_processes or [])

    # -- enumeration ---------------------------------------------------
    def _wasapi_devices(self) -> list[dict[str, Any]]:
        return list(self._pa.get_device_info_generator_by_host_api(host_api_type=self._pyaudio.paWASAPI))

    @staticmethod
    def _spec(info: dict[str, Any], kind: str) -> DeviceSpec:
        return DeviceSpec(
            id=str(info["index"]),
            name=str(info["name"]),
            sample_rate=int(info["defaultSampleRate"]),
            channels=max(1, int(info["maxInputChannels"])),
            kind=kind,
        )

    def capture_devices(self) -> list[DeviceSpec]:
        return [
            self._spec(i, "mic")
            for i in self._wasapi_devices()
            if int(i["maxInputChannels"]) > 0 and not i.get("isLoopbackDevice", False)
        ]

    def loopback_devices(self) -> list[DeviceSpec]:
        return [self._spec(i, "loopback") for i in self._pa.get_loopback_device_info_generator()]

    def render_device_names(self) -> list[str]:
        return [base_name(d.name) for d in self.loopback_devices()]

    # -- resolution ----------------------------------------------------
    def _auto_name(self, flow: int) -> str:
        # 1) the endpoint Teams is actively using, 2) Windows default communications device
        if self.teams_processes:
            try:
                with com_initialized():
                    active = endpoints_with_active_session(flow, self.teams_processes)
                if active:
                    log.info("Teams nutzt aktiv: %s", active[0])
                    return active[0]
            except Exception:
                log.exception("Teams-Gerät nicht ermittelbar")
        with com_initialized():
            found = default_endpoint_name(flow, E_COMMUNICATIONS)
        return found[0] if found else ""

    def resolve_devices(self, mic_pref: str, loopback_pref: str) -> tuple[DeviceSpec, DeviceSpec]:
        mics = self.capture_devices()
        loops = self.loopback_devices()
        if not mics:
            raise WasapiUnavailable("Kein WASAPI-Mikrofon gefunden.")
        if not loops:
            raise WasapiUnavailable("Kein WASAPI-Loopback-Gerät gefunden.")

        mic_name = self._auto_name(E_CAPTURE) if mic_pref.strip().lower() == "auto" else mic_pref
        mic = match_device(mics, mic_name)
        if mic is None:
            log.warning("Mikrofon %r nicht gefunden - verwende WASAPI-Standardeingang", mic_name)
            info = self._pa.get_default_wasapi_device(d_in=True)
            mic = self._spec(info, "mic")

        loop_name = self._auto_name(E_RENDER) if loopback_pref.strip().lower() == "auto" else loopback_pref
        loop = match_device(loops, loop_name)
        if loop is None:
            log.warning("Ausgabegerät %r nicht gefunden - verwende Standard-Loopback", loop_name)
            loop = self._spec(self._pa.get_default_wasapi_loopback(), "loopback")
        return mic, loop

    # -- streams -------------------------------------------------------
    def open_stream(self, device: DeviceSpec, on_data: DataCallback, frames_per_buffer: int) -> _PaStream:
        pa = self._pyaudio

        def callback(in_data: bytes | None, frame_count: int, time_info: dict[str, float], status: int) -> tuple[None, int]:
            if in_data:
                on_data(in_data, time.monotonic())
            return (None, pa.paContinue)

        stream = self._pa.open(
            format=pa.paInt16,
            channels=device.channels,
            rate=device.sample_rate,
            input=True,
            input_device_index=int(device.id),
            frames_per_buffer=frames_per_buffer,
            stream_callback=callback,
            start=False,
        )
        return _PaStream(stream)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._pa.terminate()
