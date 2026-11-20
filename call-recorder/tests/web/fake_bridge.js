// Fake js_api for the Playwright tests: answers with the DTOs that tests/web_fixtures.py built with
// the real Python code, records every call in window.__calls and plays the Python side of the main
// flows (consent dialog, recording, post-call question, progress). Injected with addInitScript
// before the page's own scripts run; FIX is the parsed fixtures.json (window.__FIX).
(() => {
  const FIX = window.__FIX;
  const MODE = window.__MODE || "ask";
  const calls = (window.__calls = []);
  const answers = (window.__answers = []);
  let state = JSON.parse(JSON.stringify(FIX.state_idle));
  let dlgN = 0;
  const emit = (n, p) => setTimeout(() => window.cr && window.cr.emit(n, p), 0);
  const dialog = (kind, data) => { const id = `d${++dlgN}`; window.__lastDialog = { id, kind }; emit("dialog", { id, kind, data }); return id; };
  const pending = {};
  function startRecording(title) {
    state = { ...state, recording: { on: true, title, elapsed: 0, id: "new", levels: { mic: 0.6, remote: 0.3 } } };
    emit("recording", { on: true, title });
    let t = 0;
    window.__recTimer = setInterval(() => { t += 1; state.recording.elapsed = t; emit("state", JSON.parse(JSON.stringify(state))); }, 200);
  }
  window.__countdown = (left = 24) => {
    state.autostop = { status: "Teams erkannt", message: "", view: { kind: "countdown", headline: `Call scheint beendet – Aufnahme endet in ${left} s`, detail: "Teams hat das Mikrofon freigegeben.", seconds_left: left, keep_label: "Weiter aufnehmen", stop_label: "Jetzt beenden" } };
    emit("state", JSON.parse(JSON.stringify(state)));
  };
  function stopRecording() {
    clearInterval(window.__recTimer);
    state = { ...state, recording: { on: false, title: "", elapsed: 0, id: "", levels: {} }, autostop: { status: "", message: "", view: null } };
    emit("recording", { on: false, title: "" });
    pending.after = dialog("after_recording", FIX.after_recording);
  }
  function progress() {
    const steps = ["finalize", "transcribe", "transcribe", "diarize", "identify", "summarize"];
    let i = 0;
    const tick = () => {
      if (i >= steps.length) { state.queue = { current: null, pending: [] }; emit("state", JSON.parse(JSON.stringify(state))); emit("job_done", { rec: "x", state: "done" }); emit("notify", { title: "Fertig: Jour fixe", message: "Die Verarbeitung ist abgeschlossen.", level: "info" }); return; }
      state.queue = { current: { id: 7, rec: "2026-11-13_1400_Jour-fixe", title: "Jour fixe", step: steps[i], step_label: steps[i], fraction: i / steps.length, message: "", line: "", steps: ["finalize", "transcribe", "diarize", "identify", "render", "summarize"], cancel_requested: false }, pending: [] };
      emit("state", JSON.parse(JSON.stringify(state))); i += 1; setTimeout(tick, 150);
    };
    tick();
  }
  const R = (v) => Promise.resolve(JSON.parse(JSON.stringify(v)));
  const api = {
    ready: () => R({ version: "CallRecorder 0.3.0 (Test)", theme: window.__THEME || "system", hotkey: "ctrl+alt+shift+r", consent_mode: MODE, dialogs: [], state, route: window.__ROUTE || "home", toast: {} }),
    log_js_error: (m, s, l, st) => { (window.__jsErrors = window.__jsErrors || []).push(String(m)); return R(true); },
    open_external: (u) => R(true),
    get_state: () => R(state),
    get_home: () => R(FIX.home),
    get_calendar: () => R(window.__NOCAL ? FIX.calendar_none : FIX.calendar),
    start_recording: (choice) => {
      const d = MODE === "ask" ? (choice === 0 ? FIX.start_ask : FIX.start_ask) : FIX.start_confirm;
      pending.start = dialog("start", window.__CLOUD ? FIX.start_ask_cloud : d);
      return R({ ok: true });
    },
    stop_recording: () => { stopRecording(); return R({ ok: true }); },
    autostop: (what) => { if (what === "stop") stopRecording(); if (what === "keep") { state.autostop = { status: "", message: "", view: null }; emit("state", JSON.parse(JSON.stringify(state))); } return R({ ok: true }); },
    answer: (id, result) => {
      answers.push({ id, result });
      if (id === pending.start && result && result.action === "start") startRecording(result.title || "Anruf");
      if (id === pending.after && result && result.choice === "full") progress();
      emit("dialog_close", { id });
      return R(true);
    },
    import_ics: () => R({ cancelled: true }),
    pending_dialogs: () => R([]),
    play_sample: () => R({ url: "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=" }),
    list_recordings: (f, q) => {
      const L = JSON.parse(JSON.stringify(FIX.list));
      const qq = String(q || "").toLowerCase();
      if (qq) L.items = L.items.filter(i => (i.title + " " + i.participants.join(" ")).toLowerCase().includes(qq));
      if (f && f !== "all") L.items = L.items.filter(i => (f === "speakers_open" ? i.stages.speakers === "open" : f === "problems" ? i.status.tone === "err" : f === "not_handed_over" ? i.stages.handoff === "missing" : true));
      L.filter = f || "all"; L.query = q || "";
      return R(L);
    },
    get_recording: (id) => R(FIX.detail[id] || { error: "Diese Aufnahme gibt es nicht (mehr)." }),
    get_text: (id, kind) => R(FIX.text[`${id}|${kind}`] || { blocks: [], empty: true }),
    recording_action: (id, key) => {
      if (key === "review") dialog("review", FIX.review);
      if (key === "handoff") dialog("handoff", FIX.handoff);
      return R({ ok: true });
    },
    rename: (id, t) => R({ ok: true, title: t }),
    delete_info: (id) => R({ title: (FIX.detail[id] || {}).title, recycle_bin: true, handed_over: false, busy: false }),
    delete: () => R({ ok: true, where: "Papierkorb" }),
    cancel_processing: () => R({ ok: true, text: "abgebrochen" }),
    cancel_pending: () => R({ ok: true }),
    open_folder: () => R({ ok: true }),
    run_checks: () => R(FIX.checks),
    setup_action: (k) => R(k.startsWith("settings:") ? { navigate: "settings", page: "summary" } : { ok: true }),
    get_settings: () => R(FIX.settings),
    save_settings: (patch, confirmed) => {
      window.__saved = { patch, confirmed };
      if (patch["summary.provider"] === "anthropic" && !(confirmed || []).includes("cloud")) return R({ confirm: { kind: "cloud", title: "Transkripte an Anthropic senden?", message: "Warnung", confirm: "Claude verwenden", cancel: "Abbrechen" } });
      if (patch["hotkey.toggle"] === "kaputt") return R({ error: "Tastenkürzel ungültig: kaputt" });
      return R({ ok: true });
    },
    pick_folder: () => R({ path: "D:\\Aufnahmen" }),
    save_preset: () => R({ ok: true, path: "C:\\presets\\x.toml" }),
    api_key: (a) => R({ ok: true, present: a === "save", masked: a === "save" ? "sk-a…1234" : "", source: "gespeichert" }),
    test_anthropic: () => R({ ok: true, text: "Verbindung ok" }),
    llm_models: () => R({ models: ["gemma3:12b"], version: "0.9", can_pull: true }),
    pull_llm_model: () => R({ ok: true }),
    whisper_models: () => R({ models: [], total: "0 B", root: "C:\\models", effective: "CPU, large-v3-turbo (int8)" }),
    prompts: (a) => R(a === "get" ? { prompts: [{ key: "system", label: "System-Anweisung", text: "Du bist …", placeholders: {}, default: "Standard" }, { key: "final", label: "Protokoll-Vorlage", text: "{material}", placeholders: { material: "Transkript" }, default: "Std" }], dir: "C:\\prompts" } : { ok: true, text: "Gespeichert." }),
    text_file: (k, t) => R(t === undefined || t === null ? { text: "Hafenkante\n", path: "C:\\glossar.txt" } : { ok: true }),
    profiles: () => R({ profiles: [], text: "" }),
    check_updates: () => R({ ok: true }),
    restore_previous_version: () => R({ ok: true }),
    about: () => R({ version: "CallRecorder 0.3.0", python: "3.12", log: "C:\\log.txt", recordings: "C:\\Aufnahmen", previous: "" }),
    open_path: () => R({ ok: true }),
    set_theme: () => R({ ok: true }),
    switch_frontend: (f, r) => R({ ok: true }),
    hide_window: () => R(true),
    quit_app: () => R(true),
    save_window_state: () => R(true),
  };
  const wrapped = {};
  for (const [k, fn] of Object.entries(api)) wrapped[k] = (...args) => { calls.push([k, ...args]); return fn(...args); };
  window.pywebview = { api: wrapped, __fake: true };
  window.__emit = emit;
  window.__dialog = dialog;
  window.__state = () => state;
  setTimeout(() => window.dispatchEvent(new Event("pywebviewready")), 0);
})();
