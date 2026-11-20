"""Automatic end of a recording: monitor state machine, runner races, controller stop path."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from callrec.audio.capture import DeviceSpec, DualTrackRecorder, TrackWriter, dbfs_to_level
from callrec.autostop import (
    CALL_ENDED,
    MAX_DURATION,
    SILENCE,
    AutoStopMonitor,
    AutoStopRunner,
    Observation,
    ProbeResult,
    fmt_minutes,
)
from callrec.calendar.service import Suggestion
from callrec.config import AutoStopConfig, Config, ConfigError, config_from_dict
from callrec.detect import match_call_process
from callrec.setupcheck import OK, WARN, check_autostop
from tests.test_controller import FakeUi, _controller, _decision

CFG = AutoStopConfig()


def obs(t: float, mic: float = 0.0, remote: float = 0.0, active: bool | None = None, ended: bool = False) -> Observation:
    return Observation(elapsed_s=t, mic_silent_s=mic, remote_silent_s=remote, call_app_active=active,
                       call_app_desc="ms-teams.exe (PID 7) auf Headset", event_ended=ended)


def run(mon: AutoStopMonitor, cfg: AutoStopConfig, frames: list[tuple[float, Observation]]) -> list:
    return [e for t, o in frames if (e := mon.update(cfg, o, t)) is not None]


# --- call app signal ---------------------------------------------------------------------
def test_teams_gone_with_quiet_remote_prompts_after_debounce():
    m = AutoStopMonitor()
    assert m.update(CFG, obs(0, active=True), 0) is None
    assert m.call_app_seen.startswith("ms-teams.exe")
    # Teams releases the mic, the remote track goes quiet (silence grows with time)
    assert m.update(CFG, obs(10, remote=5, active=False), 10) is None
    assert m.update(CFG, obs(25, remote=20, active=False), 25) is None  # gone only 15 s
    ev = m.update(CFG, obs(31, remote=26, active=False), 31)
    assert ev is not None and ev.kind == "prompt" and ev.reason == CALL_ENDED
    v = m.view(31)
    assert v is not None and v.headline == "Call scheint beendet – Aufnahme endet in 30 s"
    assert m.view(41).seconds_left == 20


def test_bluetooth_profile_switch_short_drop_does_not_prompt():
    m = AutoStopMonitor()
    m.update(CFG, obs(0, active=True), 0)
    # devices vanish: probe errors (None) and then "no session" for 12 s, then Teams is back
    evs = run(m, CFG, [(3, obs(3, remote=3, active=None)), (6, obs(6, remote=6, active=False)),
                       (18, obs(18, remote=18, active=False)), (21, obs(21, remote=21, active=True)),
                       (60, obs(60, remote=60, active=True))])
    assert evs == []


def test_probe_errors_never_count_as_gone():
    m = AutoStopMonitor()
    m.update(CFG, obs(0, active=True), 0)
    assert run(m, CFG, [(t, obs(t, remote=t, active=None)) for t in range(3, 120, 3)]) == []


def test_teams_gone_but_something_plays_needs_the_long_timeout():
    m = AutoStopMonitor()
    m.update(CFG, obs(0, active=True), 0)
    frames = [(t, obs(t, remote=0.5, active=False)) for t in range(3, 200, 3)]  # music/video after the call
    evs = run(m, CFG, frames)
    assert [e.kind for e in evs] == ["prompt", "expired"] and evs[0].reason == CALL_ENDED
    assert m.countdown is not None and m.countdown.started >= 3 + CFG.session_gone_max_s


def test_muted_user_while_others_talk_is_no_end():
    # Teams keeps its session (call running), the mic is silent (muted), the remote talks
    m = AutoStopMonitor()
    assert run(m, CFG, [(t, obs(t, mic=t, remote=0.3, active=True)) for t in range(0, 1200, 5)]) == []


def test_music_on_hold_is_no_end():
    m = AutoStopMonitor()
    assert run(m, CFG, [(t, obs(t, mic=t, remote=0.2, active=None)) for t in range(0, 1200, 5)]) == []


def test_teams_never_seen_does_not_use_the_session_signal():
    m = AutoStopMonitor()
    assert run(m, CFG, [(t, obs(t, active=False)) for t in range(0, 150, 3)]) == []


# --- silence -------------------------------------------------------------------------------
def test_silence_on_both_tracks_without_teams():
    m = AutoStopMonitor()
    assert m.update(CFG, obs(100, mic=179, remote=500), 100) is None
    ev = m.update(CFG, obs(101, mic=180, remote=500), 101)
    assert ev is not None and ev.reason == SILENCE and "3 min" in ev.text


def test_long_pause_shorter_than_threshold_is_no_end():
    m = AutoStopMonitor()
    assert run(m, CFG, [(t, obs(t, mic=t % 150, remote=t % 150)) for t in range(0, 1500, 5)]) == []


def test_calendar_end_is_only_a_weak_hint():
    m = AutoStopMonitor()
    # after the event: 60 s of silence suffice
    ev = m.update(CFG, obs(500, mic=61, remote=61, ended=True), 500)
    assert ev is not None and ev.reason == SILENCE and "Kalendertermin" in ev.text
    # alone (sound on the tracks) it never triggers
    m2 = AutoStopMonitor()
    assert run(m2, CFG, [(t, obs(t, mic=1, remote=1, ended=True)) for t in range(0, 900, 5)]) == []


def test_sound_during_countdown_clears_it():
    m = AutoStopMonitor()
    m.update(CFG, obs(200, mic=200, remote=200), 200)
    assert m.countdown is not None
    ev = m.update(CFG, obs(210, mic=0.2, remote=210), 210)
    assert ev is not None and ev.kind == "cleared" and m.countdown is None
    assert [h["action"] for h in m.history] == ["prompt", "cleared"]


def test_teams_back_during_countdown_clears_it_but_silence_does_not_clear_call_end():
    m = AutoStopMonitor()
    m.update(CFG, obs(0, active=True), 0)
    m.update(CFG, obs(5, remote=30, active=False), 5)
    assert m.update(CFG, obs(26, remote=30, active=False), 26).kind == "prompt"
    assert m.update(CFG, obs(30, mic=0, remote=0, active=False), 30) is None  # someone speaks in the room: no clear
    assert m.update(CFG, obs(33, remote=30, active=True), 33).kind == "cleared"


def test_countdown_expires_once():
    m = AutoStopMonitor()
    m.update(CFG, obs(200, mic=200, remote=200), 200)
    ev = m.update(CFG, obs(230, mic=230, remote=230), 230)
    assert ev is not None and ev.kind == "expired"
    assert m.update(CFG, obs(231, mic=231, remote=231), 231) is None


def test_keep_recording_snoozes_and_consumes_the_teams_evidence():
    m = AutoStopMonitor()
    m.update(CFG, obs(0, active=True), 0)
    m.update(CFG, obs(5, remote=30, active=False), 5)
    m.update(CFG, obs(26, remote=30, active=False), 26)
    text = m.keep_recording(CFG, 27, 27)
    assert "15 min" in text and m.countdown is None
    # within the snooze nothing, not even silence
    assert run(m, CFG, [(t, obs(t, mic=t, remote=t, active=False)) for t in range(30, 27 + 900, 10)]) == []
    # after the snooze: Teams evidence is used up, but silence still counts
    ev = m.update(CFG, obs(1000, mic=0, remote=1000, active=False), 1000)
    assert ev is None
    ev = m.update(CFG, obs(1001, mic=500, remote=1001, active=False), 1001)
    assert ev is not None and ev.reason == SILENCE
    # Teams active again re-arms the call-app signal
    m.keep_recording(CFG, 1002, 1002)
    m.update(CFG, obs(2000, active=True), 2000)
    m.update(CFG, obs(2003, remote=40, active=False), 2003)
    assert m.update(CFG, obs(2024, remote=40, active=False), 2024).reason == CALL_ENDED


# --- safety nets ---------------------------------------------------------------------------
def test_max_duration_warns_then_counts_down_and_can_be_extended():
    cfg = dataclasses.replace(CFG, max_duration_minutes=60, max_duration_warning_minutes=10)
    m = AutoStopMonitor()
    assert m.update(cfg, obs(49 * 60), 1) is None
    ev = m.update(cfg, obs(50 * 60), 2)
    assert ev.kind == "warning" and "10 min" in ev.text
    assert m.view(2).kind == "warning"
    assert m.update(cfg, obs(55 * 60), 3) is None  # warned once
    ev = m.update(cfg, obs(60 * 60), 4)
    assert ev.kind == "prompt" and ev.reason == MAX_DURATION
    assert m.view(4).headline.startswith("Höchstdauer erreicht")
    assert m.update(cfg, obs(60 * 60 + 5, mic=0, remote=0), 9) is None  # sound does not lift the limit
    text = m.keep_recording(cfg, 10, 60 * 60 + 6)
    assert "2 h" in text or "120" in text
    assert m.max_limit_s(cfg) == pytest.approx(60 * 60 + 6 + 3600)
    # the snooze of the max duration does not suppress the call-end signals
    assert m.update(cfg, obs(3700, mic=200, remote=200), 11).reason == SILENCE


def test_max_duration_is_not_suppressed_by_a_snooze():
    cfg = dataclasses.replace(CFG, max_duration_minutes=60, max_duration_warning_minutes=0)
    m = AutoStopMonitor()
    m.update(cfg, obs(100, mic=200, remote=200), 100)
    m.keep_recording(cfg, 101, 101)
    assert m.update(cfg, obs(3600, mic=0, remote=0), 102).reason == MAX_DURATION


def test_disabled_detection_keeps_the_safety_nets():
    cfg = dataclasses.replace(CFG, enabled=False)
    m = AutoStopMonitor()
    m.update(cfg, obs(0, active=True), 0)
    assert run(m, cfg, [(t, obs(t, mic=t, remote=t, active=False)) for t in range(3, 899, 3)]) == []
    ev = m.update(cfg, obs(900, mic=900, remote=900, active=False), 900)
    assert ev.reason == SILENCE and "nur Stille" in ev.text
    off = dataclasses.replace(cfg, silence_safety_minutes=0, max_duration_minutes=0)
    m2 = AutoStopMonitor()
    assert run(m2, off, [(t, obs(t, mic=t, remote=t)) for t in range(0, 30000, 600)]) == []


def test_fmt_minutes():
    assert fmt_minutes(180) == "3 min" and fmt_minutes(14400) == "4 h" and fmt_minutes(90) == "1,5 min"


# --- runner: threading boundaries and races ----------------------------------------------
class FakeCtl:
    def __init__(self) -> None:
        self.recording = True
        self.session = 1
        self.t = 0.0
        self.sil = {"mic": 0.0, "remote": 0.0}
        self.end: datetime | None = None
        self.stops: list[tuple] = []

    @property
    def is_recording(self) -> bool:
        return self.recording

    @property
    def session_id(self) -> int:
        return self.session if self.recording else 0

    def elapsed(self) -> float:
        return self.t

    def silence_durations(self):
        return dict(self.sil)

    def current_event_end(self):
        return self.end

    def stop(self, reason="manual", *, session=None, by="user", detail=""):
        if not self.recording or (session is not None and session != self.session):
            return None
        self.stops.append((reason, session, by))
        self.recording = False
        return object()


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _runner(ctl: FakeCtl, probe=None, cfg: AutoStopConfig = CFG):
    clock = Clock()
    posted: list = []
    notes: list = []
    r = AutoStopRunner(lambda: cfg, ctl, posted.append, lambda t, m: notes.append((t, m)), probe=probe, clock=clock,
                       probe_interval_s=0)
    return r, clock, posted, notes


def _silent_until(r, ctl, clock, t: float) -> None:
    clock.t = ctl.t = t
    ctl.sil = {"mic": t, "remote": t}
    r.step()


def test_runner_expiry_stops_through_controller_on_the_posted_call():
    ctl = FakeCtl()
    r, clock, posted, notes = _runner(ctl)
    _silent_until(r, ctl, clock, 180)
    assert notes and notes[-1][0] == "Call scheint beendet"
    assert r.view() is not None and r.view().seconds_left == 30
    _silent_until(r, ctl, clock, 211)
    assert ctl.stops == [] and len(posted) == 1  # the stop runs on the Tk thread, not here
    posted[0]()
    assert ctl.stops == [(SILENCE, 1, "automatic")]
    posted[0]()  # posted twice / run twice: no second stop
    assert len(ctl.stops) == 1


def test_runner_keep_recording_after_expiry_was_posted_wins():
    ctl = FakeCtl()
    r, clock, posted, _ = _runner(ctl)
    _silent_until(r, ctl, clock, 180)
    _silent_until(r, ctl, clock, 211)
    r.keep_recording()  # user clicked while the stop was queued for the Tk thread
    posted[0]()
    assert ctl.stops == [] and r.view() is None and "15 min" in r.message()


def test_runner_manual_stop_and_new_recording_ignore_a_stale_expiry():
    ctl = FakeCtl()
    r, clock, posted, _ = _runner(ctl)
    _silent_until(r, ctl, clock, 180)
    _silent_until(r, ctl, clock, 211)
    ctl.recording = False  # manual stop meanwhile ...
    assert r.view() is None
    r.step()
    ctl.recording, ctl.session = True, 2  # ... and the next recording started
    clock.t, ctl.t, ctl.sil = 212, 1, {"mic": 1, "remote": 1}
    r.step()
    posted[0]()
    assert ctl.stops == [] and ctl.recording


def test_runner_stop_now_and_double_click():
    ctl = FakeCtl()
    r, clock, posted, _ = _runner(ctl)
    r.stop_now()  # nothing pending: nothing happens
    assert ctl.stops == []
    _silent_until(r, ctl, clock, 180)
    r.stop_now()
    r.stop_now()
    assert ctl.stops == [(SILENCE, 1, "user")]
    assert r.info(1)["prompts"][-1]["action"] == "stop_now"


def test_runner_shutdown_stops_the_thread():
    ctl = FakeCtl()
    ctl.recording = False
    r = AutoStopRunner(lambda: CFG, ctl, lambda fn: None, tick_s=0.01)
    r.start()
    r.shutdown()
    assert r._thread is not None
    r._thread.join(2)
    assert not r._thread.is_alive()


def test_runner_probe_and_status_text():
    ctl = FakeCtl()
    state = {"active": True, "fail": False}

    def probe():
        if state["fail"]:
            raise OSError("COM weg")
        return ProbeResult(state["active"], "ms-teams.exe (PID 1) auf Headset" if state["active"] else "",
                           ["ms-teams.exe (PID 1) auf Headset aktiv"])

    r, clock, posted, notes = _runner(ctl, probe)
    assert "Teams-Mikrofon" in r.status_text()
    clock.t = 1
    r.step()
    assert "Anruf-App erkannt: ms-teams.exe" in r.status_text()
    state["fail"] = True
    for t in range(2, 100):
        clock.t = ctl.t = t
        r.step()  # errors = unknown, never "gone"
    assert r.view() is None
    state.update(fail=False, active=False)
    for t in range(100, 125):
        clock.t = ctl.t = t
        ctl.sil = {"mic": 0, "remote": t - 99}
        r.step()
    assert r.view() is not None and r.view().reason == CALL_ENDED
    info = r.info(1)
    assert info["call_app_detected"].startswith("ms-teams.exe") and info["prompts"][0]["reason"] == CALL_ENDED


def test_runner_without_teams_says_so():
    ctl = FakeCtl()
    r, clock, _, _ = _runner(ctl, lambda: ProbeResult(False))
    clock.t = 1
    r.step()
    assert "nicht erkannt" in r.status_text()
    assert "aus" in AutoStopRunner(lambda: dataclasses.replace(CFG, enabled=False), ctl, lambda f: None).status_text()


def test_runner_event_end_is_used_as_hint():
    ctl = FakeCtl()
    now = datetime(2026, 11, 20, 15, 1, tzinfo=UTC)
    ctl.end = now - timedelta(minutes=1)
    r = AutoStopRunner(lambda: CFG, ctl, lambda f: None, clock=Clock(), wall=lambda: now)
    ctl.sil = {"mic": 61, "remote": 61}
    ev = r.step()
    assert ev is not None and ev.reason == SILENCE


# --- controller: one stop path, meta.json, debounce ---------------------------------------
def test_controller_stop_records_reason_and_ignores_stale_sessions(tmp_path: Path):
    ui = FakeUi(_decision)
    ctl, q = _controller(ui, tmp_path)
    ctl.clock = lambda: datetime.now(UTC)
    ctl.stop_info = lambda s: {"enabled": True, "prompts": [{"action": "prompt", "reason": "silence"}]}
    assert ctl.start(Suggestion(None)) is True
    s = ctl.session_id
    assert s == 1 and ctl.silence_durations()["mic"] >= 0
    assert ctl.stop(SILENCE, session=s + 1, by="automatic") is None and ctl.is_recording  # stale
    rec = ctl.stop(SILENCE, session=s, by="automatic", detail="Seit 3 min still.")
    assert rec is not None and not ctl.is_recording and ctl.just_stopped()
    meta = rec.load_meta()["recording"]
    assert meta["stop"]["reason"] == "silence" and meta["stop"]["by"] == "automatic" and meta["stop"]["at"]
    assert meta["autostop"]["prompts"][0]["reason"] == "silence"
    assert q.jobs and q.jobs[0][0] is rec  # same processing as a manual stop
    assert ui.notes[-1][0] == "Aufnahme automatisch beendet"
    ctl.toggle()  # hotkey right after the automatic stop: no new consent dialog
    assert len(ui.asked) == 1
    # a manual stop of the next recording
    ctl._stopped_at = float("-inf")
    assert ctl.start(Suggestion(None)) is True and ctl.session_id == 2
    rec2 = ctl.stop()
    assert rec2.load_meta()["recording"]["stop"]["reason"] == "manual"


def test_controller_after_stop_hook(tmp_path: Path):
    ui = FakeUi(_decision)
    ctl, q = _controller(ui, tmp_path)
    ctl.clock = lambda: datetime.now(UTC)
    seen = []
    ctl.after_stop = lambda rec, reason: seen.append(reason)
    ctl.start(Suggestion(None))
    ctl.stop(MAX_DURATION, by="automatic")
    assert seen == [MAX_DURATION] and q.jobs == []


# --- capture: silence per track -------------------------------------------------------------
def test_track_writer_tracks_last_loud_chunk(tmp_path: Path):
    dev = DeviceSpec("1", "Mic", 16000, 1, "mic")
    w = TrackWriter(tmp_path / "m.wav", dev, t0=0.0, loud_level=dbfs_to_level(-45))
    quiet = (np.ones(1600) * 50).astype(np.int16).tobytes()  # about -56 dBFS
    loud = (np.ones(1600) * 3000).astype(np.int16).tobytes()
    w.process_chunk(quiet, 0.1)
    assert w.last_loud_t is None
    w.process_chunk(loud, 0.2)
    w.process_chunk(quiet, 0.3)
    assert w.last_loud_t == 0.2
    w.finish(0.3)


def test_recorder_silence_durations(tmp_path: Path):
    from callrec.consent import confirm_consent
    from tests.test_controller import FakeBackend

    clock = Clock()
    clock.t = 100.0
    mic = DeviceSpec("1", "Mic", 16000, 1, "mic")
    loop = DeviceSpec("2", "Loop", 16000, 1, "loopback")
    r = DualTrackRecorder(FakeBackend(), tmp_path, mic, loop, clock=clock)
    assert r.silence_durations() == {"mic": 0.0, "remote": 0.0}
    r.start(confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[], method="dialog"))
    r._writers["mic"].last_loud_t = 130.0
    clock.t = 160.0
    assert r.silence_durations() == {"mic": 30.0, "remote": 60.0}  # loopback never loud: since the start
    r.stop()


# --- process matching, config, setup ----------------------------------------------------------
@pytest.mark.parametrize("name, parents, expected", [
    ("ms-teams.exe", [], "ms-teams.exe"),
    ("MS-Teams.EXE", [], "MS-Teams.EXE"),
    ("msedgewebview2.exe", ["msedgewebview2.exe", "ms-teams.exe", "explorer.exe"],
     "msedgewebview2.exe (gestartet von ms-teams.exe)"),
    ("msedgewebview2.exe", ["olk.exe"], None),  # Outlook's WebView is no call
    ("Teams.exe", [], "Teams.exe"),
    ("zoom.exe", [], None),
])
def test_match_call_process(name, parents, expected):
    assert match_call_process(name, parents, ["ms-teams.exe", "teams"]) == expected
    assert match_call_process(name, parents, []) is None


def test_autostop_config_defaults_and_validation():
    cfg = Config()
    assert cfg.autostop.enabled and cfg.autostop.countdown_s == 30 and cfg.autostop.max_duration_minutes == 240
    c = config_from_dict({"autostop": {"countdown_s": 45, "max_duration_minutes": 0}})
    assert c.autostop.countdown_s == 45 and c.autostop.max_duration_minutes == 0
    for bad in ({"countdown_s": 0}, {"silence_dbfs": 0.0}, {"session_gone_s": 30.0, "session_gone_max_s": 10.0},
                {"max_duration_minutes": 2.0}, {"snooze_minutes": 0.0}, {"silence_safety_minutes": -1.0}):
        with pytest.raises(ConfigError):
            config_from_dict({"autostop": bad})


def test_default_template_matches_autostop_defaults():
    import tomllib

    from callrec.config import DEFAULT_CONFIG_TEMPLATE

    data = tomllib.loads(DEFAULT_CONFIG_TEMPLATE)["autostop"]
    assert data == dataclasses.asdict(AutoStopConfig())


def test_setup_check_explains_what_happens():
    cfg = Config()
    item = check_autostop(cfg, windows=True)
    assert item.state == OK and "30 s" in item.detail and "4 h" in item.detail and "ms-teams.exe" in item.detail
    cfg.autostop.enabled = False
    item = check_autostop(cfg, windows=True)
    assert item.state == WARN and "vergessene Aufnahme" in item.detail
