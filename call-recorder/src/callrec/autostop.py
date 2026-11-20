"""Automatic END of a recording (never the start).

Signals, combined (see README "Automatisches Ende"):

* the call app's (Teams) microphone session: seen active during this recording, then gone or
  inactive for ``session_gone_s`` while the remote track is quiet as long - or gone for
  ``session_gone_max_s`` whatever is played. A probe error counts as "unknown", not as "gone",
  so a Bluetooth profile switch (devices vanish for a moment) does not end anything;
* sustained silence on BOTH tracks (also for calls without Teams). The user being muted while
  others talk, or music on hold, keeps the remote track loud and therefore never counts;
* the calendar event's end time as a weak hint only: after it, a shorter silence suffices.
  It never triggers on its own.

Safety nets independent of ``enabled``: a maximum duration (with a warning beforehand) and a
prompt when only silence has been recorded for a long time.

Nothing is cut off silently: a detected end starts a countdown that is shown in the main
window and the recording overlay; "Weiter aufnehmen" snoozes, "Jetzt beenden" stops at once,
no reaction stops when the countdown has run out. Every stop goes through
:meth:`callrec.controller.Controller.stop` on the Tk thread - the same path as a manual stop.

:class:`AutoStopMonitor` is the pure state machine (no threads, no clocks of its own);
:class:`AutoStopRunner` polls the controller and the session probe on its own thread.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from callrec.config import AutoStopConfig

log = logging.getLogger(__name__)

# stop reasons recorded in meta.json (recording.stop.reason)
MANUAL, CALL_ENDED, SILENCE, MAX_DURATION, APP_QUIT = "manual", "call_ended", "silence", "max_duration", "app_quit"
REASON_TEXT = {
    MANUAL: "von Hand beendet",
    CALL_ENDED: "Gespräch beendet (Anruf-App nutzt das Mikrofon nicht mehr)",
    SILENCE: "längere Stille auf beiden Spuren",
    MAX_DURATION: "Höchstdauer erreicht",
    APP_QUIT: "CallRecorder wurde beendet",
}


def fmt_minutes(seconds: float) -> str:
    m = seconds / 60.0
    if m >= 60 and abs(m / 60 - round(m / 60)) < 1e-6:
        return f"{int(round(m / 60))} h"
    if abs(m - round(m)) < 1e-6:
        return f"{int(round(m))} min"
    return f"{m:.1f} min".replace(".", ",")


@dataclass
class Observation:
    elapsed_s: float
    mic_silent_s: float
    remote_silent_s: float
    call_app_active: bool | None  # None = unknown (probe off, failed or not on Windows)
    call_app_desc: str = ""  # which process/endpoint was seen, for UI and log
    event_ended: bool = False  # the recording's calendar event is over (weak hint)

    @property
    def both_silent_s(self) -> float:
        return min(self.mic_silent_s, self.remote_silent_s)


@dataclass
class Countdown:
    id: int
    reason: str
    started: float
    deadline: float
    detail: str
    expired: bool = False


@dataclass
class AutoStopView:
    """What the main window and the overlay show (plain data, read from the Tk thread)."""

    kind: str  # "countdown" | "warning"
    reason: str
    headline: str
    detail: str
    seconds_left: int
    keep_label: str = "Weiter aufnehmen"
    stop_label: str = "Jetzt beenden"


@dataclass
class Event:
    kind: str  # "prompt" | "cleared" | "expired" | "warning"
    reason: str
    countdown_id: int = 0
    text: str = ""


@dataclass
class AutoStopMonitor:
    """Pure state machine. ``now`` is a monotonic time in seconds; all inputs are passed in."""

    history: list[dict[str, Any]] = field(default_factory=list)
    countdown: Countdown | None = None
    call_app_seen: str = ""  # description of the session once seen active during this recording
    _gone_since: float | None = None
    _consumed: bool = False  # "Weiter aufnehmen" after a call-app prompt: wait until it is active again
    _snooze_until: float = float("-inf")
    _extra_limit_s: float = 0.0
    _warned: bool = False
    _warning_text: str = ""
    _ids: int = 0
    wall: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def _log(self, action: str, reason: str, text: str = "") -> None:
        entry = {"at": self.wall().astimezone().isoformat(timespec="seconds"), "action": action, "reason": reason}
        if text:
            entry["detail"] = text
        self.history.append(entry)

    # -- limits --------------------------------------------------------------
    def max_limit_s(self, cfg: AutoStopConfig) -> float | None:
        if cfg.max_duration_minutes <= 0:
            return None
        return cfg.max_duration_minutes * 60.0 + self._extra_limit_s

    def silence_threshold_s(self, cfg: AutoStopConfig, obs: Observation) -> float:
        base = cfg.silence_minutes * 60.0
        return min(base, cfg.silence_after_event_end_s) if obs.event_ended else base

    def snoozed(self, now: float) -> bool:
        return now < self._snooze_until

    # -- main step -----------------------------------------------------------
    def update(self, cfg: AutoStopConfig, obs: Observation, now: float) -> Event | None:
        self._track_call_app(cfg, obs, now)
        cd = self.countdown
        if cd is not None:
            if cd.expired:
                return None
            if self._cleared(cd, cfg, obs):
                self.countdown = None
                self._log("cleared", cd.reason)
                return Event("cleared", cd.reason, cd.id, "Gespräch läuft offenbar weiter – Aufnahme wird fortgesetzt.")
            if now >= cd.deadline:
                cd.expired = True
                self._log("expired", cd.reason)
                return Event("expired", cd.reason, cd.id)
            return None

        limit = self.max_limit_s(cfg)
        if limit is not None:
            if obs.elapsed_s >= limit:
                return self._start(MAX_DURATION, now, cfg, f"Die Aufnahme läuft seit {fmt_minutes(limit)}.")
            warn = cfg.max_duration_warning_minutes * 60.0
            if warn > 0 and not self._warned and obs.elapsed_s >= limit - warn:
                self._warned = True
                left = max(0.0, limit - obs.elapsed_s)
                self._warning_text = f"Höchstdauer: Die Aufnahme endet in {fmt_minutes(round(left / 60) * 60 or 60)}."
                self._log("warning", MAX_DURATION)
                return Event("warning", MAX_DURATION, text=self._warning_text)

        if self.snoozed(now):
            return None
        reason = self._end_reason(cfg, obs, now)
        if reason is not None:
            return self._start(reason[0], now, cfg, reason[1])
        return None

    def _track_call_app(self, cfg: AutoStopConfig, obs: Observation, now: float) -> None:
        if obs.call_app_active is True:
            if not self.call_app_seen:
                self.call_app_seen = obs.call_app_desc or "Anruf-App"
            self._gone_since = None
            self._consumed = False  # active again -> a later end counts anew
        elif obs.call_app_active is False and self.call_app_seen and self._gone_since is None:
            self._gone_since = now
        # None (unknown): keep the state - neither "gone" nor "back"

    def call_app_gone_s(self, now: float) -> float:
        return 0.0 if self._gone_since is None else now - self._gone_since

    def _end_reason(self, cfg: AutoStopConfig, obs: Observation, now: float) -> tuple[str, str] | None:
        if cfg.enabled and cfg.call_app_session and self.call_app_seen and not self._consumed:
            gone = self.call_app_gone_s(now)
            quiet_remote = obs.remote_silent_s >= cfg.session_gone_s
            if self._gone_since is not None and (
                (gone >= cfg.session_gone_s and quiet_remote) or gone >= cfg.session_gone_max_s
            ):
                return CALL_ENDED, f"{self.call_app_seen.split(' (PID')[0]} nutzt das Mikrofon seit {int(gone)} s nicht mehr."
        silent = obs.both_silent_s
        if cfg.enabled:
            threshold = self.silence_threshold_s(cfg, obs)
            if silent >= threshold:
                hint = " (der Kalendertermin ist vorbei)" if obs.event_ended and threshold < cfg.silence_minutes * 60 else ""
                return SILENCE, f"Seit {fmt_minutes(silent)} ist auf beiden Spuren nichts zu hören{hint}."
        if cfg.silence_safety_minutes > 0 and silent >= cfg.silence_safety_minutes * 60.0:
            return SILENCE, f"Seit {fmt_minutes(silent)} wurde nur Stille aufgenommen."
        return None

    def _cleared(self, cd: Countdown, cfg: AutoStopConfig, obs: Observation) -> bool:
        if cd.reason == CALL_ENDED:
            return obs.call_app_active is True
        if cd.reason == SILENCE:
            return obs.both_silent_s < 2.0  # someone speaks again
        return False  # the maximum duration is not lifted by sound

    def _start(self, reason: str, now: float, cfg: AutoStopConfig, detail: str) -> Event:
        self._ids += 1
        self.countdown = Countdown(self._ids, reason, now, now + cfg.countdown_s, detail)
        self._log("prompt", reason, detail)
        return Event("prompt", reason, self._ids, detail)

    # -- user responses ----------------------------------------------------------
    def keep_recording(self, cfg: AutoStopConfig, now: float, elapsed_s: float) -> str:
        """'Weiter aufnehmen' (countdown or max-duration warning). Returns the confirmation text."""
        cd = self.countdown
        reason = cd.reason if cd is not None else (MAX_DURATION if self._warning_text else "")
        self.countdown = None
        if reason == MAX_DURATION:
            limit = self.max_limit_s(cfg) or 0.0
            # extend from now on (at least), so a late click still gives the full extension
            new_limit = max(limit, elapsed_s) + cfg.max_duration_extend_minutes * 60.0
            self._extra_limit_s = new_limit - cfg.max_duration_minutes * 60.0
            self._warned = False
            self._warning_text = ""
            self._log("continue", reason)
            return f"Aufnahme läuft weiter – neue Höchstdauer {fmt_minutes(new_limit)}."
        if reason:
            self._snooze_until = now + cfg.snooze_minutes * 60.0
            if reason == CALL_ENDED:
                self._consumed = True
            self._log("continue", reason)
            return f"Aufnahme läuft weiter – {fmt_minutes(cfg.snooze_minutes * 60)} lang keine weitere Nachfrage."
        return ""

    def dismiss_warning(self) -> None:
        self._warning_text = ""

    def view(self, now: float) -> AutoStopView | None:
        cd = self.countdown
        if cd is not None:
            left = max(0, int(round(cd.deadline - now)))
            if cd.reason == MAX_DURATION:
                head = f"Höchstdauer erreicht – Aufnahme endet in {left} s"
            else:
                head = f"Call scheint beendet – Aufnahme endet in {left} s"
            return AutoStopView("countdown", cd.reason, head, cd.detail, left)
        if self._warning_text:
            return AutoStopView("warning", MAX_DURATION, self._warning_text,
                                "„Weiter aufnehmen“ verlängert die Höchstdauer.", 0, stop_label="Jetzt beenden")
        return None


# ---------------------------------------------------------------------------
# runner (thread)
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    active: bool | None
    desc: str = ""
    sessions: list[str] = field(default_factory=list)


class ControllerLike(Protocol):
    @property
    def is_recording(self) -> bool: ...

    @property
    def session_id(self) -> int: ...

    def elapsed(self) -> float: ...

    def silence_durations(self) -> dict[str, float]: ...

    def current_event_end(self) -> datetime | None: ...

    def stop(self, reason: str = ..., *, session: int | None = ..., by: str = ...,
             detail: str = ...) -> Any: ...


def windows_probe(process_names: Callable[[], list[str]]) -> Callable[[], ProbeResult]:
    """Session probe for the real PC (pycaw). Needs COM in the calling thread."""

    def probe() -> ProbeResult:
        from callrec.audio.wasapi import E_CAPTURE, call_app_sessions

        sessions = call_app_sessions(E_CAPTURE, process_names())
        active = [s for s in sessions if s.active]
        return ProbeResult(bool(active), active[0].describe() if active else "",
                           [s.describe() + (" aktiv" if s.active else " inaktiv") for s in sessions])

    return probe


def windows_thread_context() -> contextlib.AbstractContextManager[None]:
    from callrec.audio.wasapi import com_initialized

    return com_initialized()


@contextlib.contextmanager
def _no_context() -> Iterator[None]:
    yield


class AutoStopRunner:
    """Polls the controller (and the call-app probe) off the Tk thread and drives the monitor.

    UI hooks: ``post(fn)`` runs ``fn`` on the Tk thread; ``notify(title, text)`` shows a tray
    notification; ``attention()`` is called when a countdown starts (e.g. show the window when
    the overlay is switched off). The Tk side reads :meth:`view` / :meth:`status_text` and calls
    :meth:`keep_recording` / :meth:`stop_now` (both on the Tk thread)."""

    def __init__(
        self,
        cfg: Callable[[], AutoStopConfig],
        controller: ControllerLike,
        post: Callable[[Callable[[], object]], None],
        notify: Callable[[str, str], None] = lambda t, m: None,
        probe: Callable[[], ProbeResult] | None = None,
        thread_context: Callable[[], contextlib.AbstractContextManager[None]] = _no_context,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        tick_s: float = 1.0,
        probe_interval_s: float = 3.0,
    ) -> None:
        self.cfg = cfg
        self.controller = controller
        self.post = post
        self.notify = notify
        self.attention: Callable[[], None] = lambda: None
        self.probe = probe
        self.thread_context = thread_context
        self.clock = clock
        self.wall = wall
        self.tick_s = tick_s
        self.probe_interval_s = probe_interval_s
        self._lock = threading.RLock()
        self._session = 0
        self._monitor = AutoStopMonitor(wall=wall)
        self._probe_state = ProbeResult(None)
        self._last_probe = float("-inf")
        self._probe_error_logged = False
        self._logged_sessions = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_obs: Observation | None = None
        self._message = ""

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="autostop", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        with self.thread_context():
            while not self._stop.wait(self.tick_s):
                try:
                    self.step()
                except Exception:  # never let the safety net die
                    log.exception("Automatisches Aufnahme-Ende: Prüfung fehlgeschlagen")

    # -- one poll ------------------------------------------------------------
    def _poll_probe(self, now: float) -> ProbeResult:
        cfg = self.cfg()
        if self.probe is None or not cfg.call_app_session:
            return ProbeResult(None)
        if now - self._last_probe < self.probe_interval_s:
            return self._probe_state
        self._last_probe = now
        try:
            res = self.probe()
            self._probe_error_logged = False
        except Exception:
            if not self._probe_error_logged:
                log.info("Sitzungsabfrage der Anruf-App fehlgeschlagen (zählt als unbekannt)", exc_info=True)
                self._probe_error_logged = True
            res = ProbeResult(None)
        if not self._logged_sessions and res.sessions:
            self._logged_sessions = True
            log.info("Mikrofon-Sitzungen der Anruf-App: %s", "; ".join(res.sessions))
        self._probe_state = res
        return res

    def step(self) -> Event | None:
        c = self.controller
        if not c.is_recording:
            with self._lock:
                self._session = 0
                self._last_obs = None
            return None
        session = c.session_id
        now = self.clock()
        with self._lock:
            if session != self._session:
                self._session = session
                self._monitor = AutoStopMonitor(wall=self.wall)
                self._logged_sessions = False
                self._last_probe = float("-inf")
                self._message = ""
        probe = self._poll_probe(now)
        sil = c.silence_durations()
        end = c.current_event_end()
        obs = Observation(
            elapsed_s=c.elapsed(), mic_silent_s=sil.get("mic", 0.0), remote_silent_s=sil.get("remote", 0.0),
            call_app_active=probe.active, call_app_desc=probe.desc,
            event_ended=end is not None and self.wall() >= end,
        )
        cfg = self.cfg()
        with self._lock:
            if session != self._session:
                return None
            seen_before = self._monitor.call_app_seen
            ev = self._monitor.update(cfg, obs, now)
            self._last_obs = obs
            if not seen_before and self._monitor.call_app_seen:
                log.info("Anruf-App erkannt: %s", self._monitor.call_app_seen)
        if ev is None:
            return None
        log.info("Automatisches Ende: %s (%s) %s", ev.kind, ev.reason, ev.text)
        if ev.kind == "prompt":
            self._message = ""
            self.notify("Call scheint beendet" if ev.reason != MAX_DURATION else "Höchstdauer erreicht",
                        f"{ev.text} Die Aufnahme endet in {cfg.countdown_s} s – „Weiter aufnehmen“ im Fenster "
                        "oder im roten Balken verhindert das.")
            self.attention()
        elif ev.kind == "warning":
            self.notify("Aufnahme: Höchstdauer", ev.text)
        elif ev.kind == "cleared":
            self._message = ev.text
        elif ev.kind == "expired":
            cid = ev.countdown_id
            self.post(lambda: self._fire(session, cid))
        return ev

    # -- Tk thread -----------------------------------------------------------
    def _fire(self, session: int, countdown_id: int) -> None:
        """Countdown ran out: stop through the controller - unless the user reacted meanwhile,
        stopped by hand or a new recording runs (each case: do nothing)."""
        with self._lock:
            cd = self._monitor.countdown
            if session != self._session or cd is None or cd.id != countdown_id or not cd.expired:
                return
            self._monitor.countdown = None
            reason, detail = cd.reason, cd.detail
        self.controller.stop(reason, session=session, by="automatic", detail=detail)

    def keep_recording(self) -> str:
        with self._lock:
            elapsed = self._last_obs.elapsed_s if self._last_obs is not None else self.controller.elapsed()
            text = self._monitor.keep_recording(self.cfg(), self.clock(), elapsed)
            self._message = text
        if text:
            log.info("Automatisches Ende: Benutzer nimmt weiter auf (%s)", text)
        return text

    def stop_now(self) -> None:
        with self._lock:
            session = self._session
            cd = self._monitor.countdown
            view = self._monitor.view(self.clock())
            if not session or view is None:
                return
            reason = cd.reason if cd is not None else MAX_DURATION
            detail = cd.detail if cd is not None else ""
            self._monitor.countdown = None
            self._monitor.history.append({"at": self.wall().astimezone().isoformat(timespec="seconds"),
                                          "action": "stop_now", "reason": reason})
        self.controller.stop(reason, session=session, by="user", detail=detail)

    def dismiss_message(self) -> None:
        with self._lock:
            self._message = ""
            self._monitor.dismiss_warning()

    # -- read by the UI (any thread) ------------------------------------------
    def view(self) -> AutoStopView | None:
        with self._lock:
            if not self._session or not self.controller.is_recording or self.controller.session_id != self._session:
                return None
            return self._monitor.view(self.clock())

    def message(self) -> str:
        with self._lock:
            return self._message if self._session and self.controller.is_recording else ""

    def status_text(self) -> str:
        """One line for the record panel: what will happen."""
        cfg = self.cfg()
        with self._lock:
            obs = self._last_obs if self._session else None
            mon = self._monitor
            limit = mon.max_limit_s(cfg) if obs is not None else (
                cfg.max_duration_minutes * 60.0 if cfg.max_duration_minutes > 0 else None)
            parts: list[str] = []
            if cfg.enabled:
                if cfg.call_app_session and self.probe is not None:
                    if obs is not None:
                        parts.append(f"Anruf-App erkannt: {mon.call_app_seen}" if mon.call_app_seen
                                     else "Anruf-App (Teams) nicht erkannt – Ende über Stille")
                    else:
                        parts.append("Teams-Mikrofon")
                parts.append(f"Stille {fmt_minutes(cfg.silence_minutes * 60)}")
            if limit is not None:
                parts.append(f"höchstens {fmt_minutes(limit)}")
            if obs is not None and obs.both_silent_s >= 10:
                parts.append(f"Stille seit {int(obs.both_silent_s // 60)}:{int(obs.both_silent_s % 60):02d}")
            if obs is not None and mon.snoozed(self.clock()):
                parts.append("Nachfrage pausiert")
        if not cfg.enabled:
            head = "Automatisches Ende: aus"
            return head + (" – " + ", ".join(parts) if parts else "")
        return "Automatisches Ende: " + ", ".join(parts)

    def info(self, session: int) -> dict[str, Any]:
        """Stored in meta.json when the recording stops (``recording.autostop``)."""
        cfg = self.cfg()
        with self._lock:
            if session != self._session:
                return {"enabled": cfg.enabled}
            mon = self._monitor
            return {
                "enabled": cfg.enabled,
                "call_app_detected": mon.call_app_seen or None,
                "max_duration_s": mon.max_limit_s(cfg),
                "prompts": list(mon.history),
            }
