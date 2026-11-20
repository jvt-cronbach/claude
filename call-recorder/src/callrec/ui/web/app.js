// CallRecorder – web UI (pywebview / Edge WebView2). Plain ES module, no build step, no network.
//
// Rules: Python decides, the page shows. Every action goes through window.pywebview.api (the
// bridge); texts from recordings, calendar and transcripts are always escaped (h`` template) or
// set with textContent – never inserted as HTML. Inline styles are not allowed (CSP): dynamic
// values are applied through the CSSOM after rendering (applyDyn).
"use strict";

/* =====================================================================
   1. HELPERS
   ===================================================================== */
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = s => new Raw(String(s));
const esc = s => String(s ?? "").replace(/[&<>"'`]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" }[c]));
const part = v => (v === null || v === undefined || v === false) ? "" : v instanceof Raw ? v.s : Array.isArray(v) ? v.map(part).join("") : esc(v);
function h(strings, ...vals) { let out = strings[0]; vals.forEach((v, i) => { out += part(v) + strings[i + 1]; }); return raw(out); }
const ICONS = new Set();
const I = (n, cls = "") => raw(`<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${String(n).replace(/[^a-z0-9-]/g, "")}"/></svg>`);
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const nb = s => String(s).replace(/ (?=(%|s|Min|Std|h)\b)/g, " ");
const clock = s => { s = Math.max(0, Math.floor(s || 0)); const p = n => String(n).padStart(2, "0"); return `${p(Math.floor(s / 3600))}:${p(Math.floor(s % 3600 / 60))}:${p(s % 60)}`; };
const WD = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
const WDL = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"];
const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
const p2 = n => String(n).padStart(2, "0");
function parseDate(iso) { if (!iso) return null; const d = new Date(iso); return isNaN(d) ? null : d; }
const hm = d => `${p2(d.getHours())}:${p2(d.getMinutes())}`;
function dayLabel(d, now = new Date()) {
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate()), b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((b - a) / 86400000);
  if (diff === 0) return "heute"; if (diff === 1) return "gestern";
  return `${WD[d.getDay()]} ${p2(d.getDate())}.${p2(d.getMonth() + 1)}.`;
}
function whenText(iso) { const d = parseDate(iso); if (!d) return ""; const l = dayLabel(d); return `${l[0].toUpperCase()}${l.slice(1)}, ${hm(d)}`; }
function groupOf(d, now = new Date()) {
  if (!d) return "Ohne Datum";
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate()), b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((b - a) / 86400000);
  if (diff === 0) return "Heute"; if (diff === 1) return "Gestern"; if (diff < 7) return "Diese Woche";
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}
const say = t => { const l = $("#live"); if (!l) return; l.textContent = ""; setTimeout(() => { l.textContent = t; }, 30); };
const SP_COLORS = 6;
function initials(name) { const w = String(name || "?").replace(/[<(].*$/, "").trim().split(/\s+/).filter(Boolean); return ((w[0] || "?")[0] + (w.length > 1 ? w[w.length - 1][0] : "")).toUpperCase(); }
function colorIndex(name) { let x = 0; for (const c of String(name)) x = (x * 31 + c.charCodeAt(0)) >>> 0; return (x % SP_COLORS) + 1; }
const avatar = (name, cls = "", me = false) => h`<span class="avatar ${cls}" data-c="${me ? "me" : colorIndex(name)}" aria-hidden="true">${initials(name)}</span>`;

// dynamic values without inline styles (CSP): data-w = width in %, data-c = speaker colour, data-p = ring percent
function applyDyn(root = document) {
  $$("[data-w]", root).forEach(el => { el.style.width = `${Math.max(0, Math.min(100, Number(el.dataset.w) || 0))}%`; });
  $$("[data-c]", root).forEach(el => { const c = el.dataset.c; el.style.setProperty("--c", c === "me" ? "var(--sp-me)" : `var(--sp-${Number(c) || 6})`); });
  $$("[data-p]", root).forEach(el => { el.style.setProperty("--p", String(Number(el.dataset.p) || 0)); });
  $$("[data-off]", root).forEach(el => { el.style.strokeDashoffset = String(Number(el.dataset.off) || 0); });
}

/* =====================================================================
   2. BRIDGE
   ===================================================================== */
// pywebview defines window.pywebview.api before its call machinery is complete: use the API only
// after "pywebviewready" (or once _jsApiCallback exists)
const api = () => (window.pywebview && window.pywebview.api && (window.pywebview._jsApiCallback || window.pywebview.__fake)) ? window.pywebview.api : null;
async function call(name, ...args) {
  const a = api();
  if (!a || typeof a[name] !== "function") return { error: "Die Verbindung zum Programm ist noch nicht bereit." };
  try {
    const r = await a[name](...args);
    return r === undefined ? null : r;
  } catch (e) {
    logError(`api.${name}: ${e && e.message ? e.message : e}`, "", 0, e && e.stack);
    return { error: "Interner Fehler – Details stehen in der Protokolldatei." };
  }
}
async function act(name, ...args) {
  const r = await call(name, ...args);
  if (r && r.error) showError(r.error, r.hint);
  return r;
}
let errorsLogged = 0;
function logError(message, source, line, stack) {
  if (errorsLogged++ > 50) return;
  const a = api();
  if (a && a.log_js_error) a.log_js_error(String(message), String(source || ""), Number(line) || 0, String(stack || "")).catch(() => {});
}
window.addEventListener("error", e => logError(e.message, e.filename, e.lineno, e.error && e.error.stack));
window.addEventListener("unhandledrejection", e => logError(`Promise: ${e.reason && e.reason.message ? e.reason.message : e.reason}`, "", 0, e.reason && e.reason.stack));

const HANDLERS = {};
window.cr = {
  emit(name, payload) {
    const fn = HANDLERS[name];
    if (!fn) return;
    try { fn(payload || {}); } catch (e) { logError(`event ${name}: ${e.message}`, "", 0, e.stack); }
  },
};

/* =====================================================================
   3. STATE
   ===================================================================== */
const S = {
  route: "home", setPage: "general", theme: "system", version: "", hotkey: "",
  state: { recording: { on: false }, autostop: {}, queue: { current: null, pending: [] }, setup: {} },
  home: null, calendar: null, calPick: null, list: null, filter: "all", query: "",
  selected: "", detail: null, tab: "summary", version_sel: { summary: "", transcript: "" }, text: null, textLimit: 300,
  mobileDetail: false, checks: null, checksBusy: "", settings: null, patch: {}, prompts: null, profiles: null,
  about: null, menu: "", notices: [], dialogs: [], downloads: "", pull: null,
  multi: [], ctx: null, retention: null,
};
const hotkeyText = () => (S.hotkey || "ctrl+alt+shift+r").split("+").map(k => ({ ctrl: "Strg", alt: "Alt", shift: "Umschalt", win: "Win" }[k] || k.toUpperCase()));
const kbd = () => raw(hotkeyText().map(k => `<kbd>${esc(k)}</kbd>`).join("+"));

/* =====================================================================
   4. THEME
   ===================================================================== */
const MQ_DARK = matchMedia("(prefers-color-scheme: dark)");
function applyTheme() {
  const dark = S.theme === "dark" || (S.theme === "system" && MQ_DARK.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.dataset.mode = S.theme;
}
MQ_DARK.addEventListener("change", applyTheme);

/* =====================================================================
   5. SHELL: top bar, rail, recording bar
   ===================================================================== */
const BRAND = `<svg viewBox="0 0 32 32" aria-hidden="true"><rect x="1" y="1" width="30" height="30" rx="8" fill="var(--accent-fill)"/><rect x="12" y="7" width="8" height="12" rx="4" fill="#fff"/><path d="M9 15.5a7 7 0 0 0 14 0" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/><path d="M16 22.5V25" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="24.5" cy="8" r="3.2" fill="#fff"/><circle cx="24.5" cy="8" r="1.8" fill="var(--rec)"/></svg>`;
const btn = (label, { cls = "", icon = "", act: a = "", attrs = raw(""), title = "" } = {}) =>
  h`<button class="btn ${cls}" ${a ? raw(`data-act="${esc(a)}"`) : ""} ${title ? raw(`title="${esc(title)}"`) : ""} ${attrs}>${icon ? I(icon, icon === "rec" || icon === "stop" ? "ico-sm" : "") : ""}<span class="lbl-t">${label}</span></button>`;
const data = (o) => raw(Object.entries(o).map(([k, v]) => `data-${k}="${esc(v)}"`).join(" "));

function renderTop() {
  const r = S.state.recording || {};
  const recBtn = r.on
    ? h`<span class="rec-pill" role="status"><span class="rec-dot" aria-hidden="true"></span><span class="num" data-live="clock">${clock(r.elapsed)}</span><button class="btn btn-record btn-sm" data-act="stop" title="Aufnahme beenden">${I("stop", "ico-sm")}Beenden</button></span>`
    : h`<button class="btn btn-record" data-act="start" title="Aufnahme starten (${hotkeyText().join("+")})">${I("rec", "ico-sm")}<span class="lbl-t">Aufnehmen</span></button>`;
  $("#topbar").innerHTML = h`
    <div class="brand"><span class="brand-mark">${raw(BRAND)}</span><span class="name">CallRecorder</span></div>
    <label class="top-search"><span class="sr-only">Aufnahmen durchsuchen</span>${I("search", "ico-sm")}<input type="search" id="topq" placeholder="Aufnahmen durchsuchen" autocomplete="off" value="${S.route === "library" ? S.query : ""}"><kbd>Strg+F</kbd></label>
    <div class="top-right">${recBtn}<button class="icon-btn" data-act="help" aria-label="Hilfe und Version" title="Hilfe und Version">${I("help")}</button></div>`;
}
function renderRail() {
  const open = (S.home && S.home.attention ? S.home.attention.length : 0);
  const setupOpen = (S.state.setup && S.state.setup.open) || 0;
  const items = [
    ["home", "home", "Übersicht", open ? h`<span class="badge" aria-label="${open} offen">${open}</span>` : ""],
    ["library", "list", "Aufnahmen", ""],
    ["setup", "checklist", "Einrichtung", setupOpen ? h`<span class="badge badge-warn" aria-label="${setupOpen} Hinweise">${setupOpen}</span>` : ""],
    ["settings", "gear", "Einstellungen", ""],
  ];
  const active = { prompts: "settings" }[S.route] || S.route;
  $("#nav").innerHTML = h`${items.map(([k, ic, t, b]) => h`<a href="#" class="rail-item" data-act="go" data-to="${k}" ${active === k ? raw('aria-current="page"') : ""} title="${t}">${I(ic)}<span class="lbl">${t}</span>${b}</a>`)}
    <div class="rail-spacer"></div>
    <a href="#" class="rail-item" data-act="hide" title="Fenster schließen – CallRecorder läuft im Infobereich weiter">${I("expand")}<span class="lbl">Ausblenden</span></a>`;
}
function renderRecbar() {
  const r = S.state.recording || {}, v = (S.state.autostop || {}).view;
  const el = $("#recbar");
  if (!r.on || S.route === "home") { el.innerHTML = ""; return; }
  el.innerHTML = v ? h`<div class="recbar warn" role="alert">${I("clock")}<span class="strong" data-live="cd-head">${v.headline}</span><span class="grow"></span>
      ${btn(v.keep_label || "Weiter aufnehmen", { cls: "btn-sm", act: "keep" })}${btn(v.stop_label || "Jetzt beenden", { cls: "btn-sm btn-record", icon: "stop", act: "stopnow" })}</div>`
    : h`<div class="recbar" role="status"><span class="rec-dot" aria-hidden="true"></span><span class="strong">Aufnahme läuft</span><span class="num strong" data-live="clock">${clock(r.elapsed)}</span>
      <span class="grow ellipsis title">${r.title}</span>${btn("Zur Aufnahme", { cls: "btn-sm btn-subtle", act: "go", attrs: data({ to: "home" }) })}${btn("Beenden", { cls: "btn-sm btn-record", icon: "stop", act: "stop" })}</div>`;
}

/* =====================================================================
   6. NOTICES (snackbar for confirmations, banners for problems)
   ===================================================================== */
let snackTimer = 0;
function snack(text, err = false) {
  say(text);
  const host = $("#snackhost");
  host.innerHTML = h`<div class="snack ${err ? "err" : ""}" role="status">${I(err ? "alert" : "check-circle")}<span>${text}</span><button class="icon-btn" data-act="snack-x" aria-label="Meldung schließen">${I("x", "ico-sm")}</button></div>`;
  clearTimeout(snackTimer);
  snackTimer = setTimeout(() => { host.innerHTML = ""; }, err ? 9000 : 4200);
}
function showError(text, hint) {
  S.notices = [{ id: Date.now() + Math.random(), level: "err", text, hint }, ...S.notices.filter(n => n.text !== text)].slice(0, 3);
  renderNotices();
}
function renderNotices() {
  $("#notices").innerHTML = h`${S.notices.map(n => h`<div class="banner banner-err has-x" role="alert">${I("alert")}
    <div><div class="bt">${n.title || "Problem"}</div><div class="bd pre">${n.text}</div>${n.hint && n.hint.next ? h`<div class="bd muted mt-2">Nächster Schritt: ${n.hint.next}</div>` : ""}
    ${n.hint && n.hint.action ? h`<div class="btn-row mt-2">${btn(n.hint.label || "Beheben", { cls: "btn-sm btn-primary", act: "hint", attrs: data({ hint: n.hint.action, rec: n.rec || "" }) })}</div>` : ""}</div>
    <button class="icon-btn x" data-act="notice-x" ${data({ id: n.id })} aria-label="Hinweis schließen">${I("x", "ico-sm")}</button></div>`)}`;
}

/* =====================================================================
   7. HOME  (what's next · what waits · what is running)
   ===================================================================== */
const STEP_NAMES = { finalize: "Audio sichern", transcribe: "Transkript", diarize: "Sprecher trennen", identify: "Sprecher erkennen", render: "Transkript erstellen", summarize: "Zusammenfassung" };
function heroIdle() {
  const cal = S.calendar;
  const events = (cal && cal.events) || [];
  const pick = S.calPick !== null && S.calPick !== undefined ? S.calPick : (events.find(e => e.current) || events.find(e => !e.past) || null);
  const picked = typeof pick === "object" ? pick : events.find(e => e.index === pick);
  if (!cal || !cal.connected || !picked) {
    return h`<section class="card hero" aria-labelledby="h-now">
      <div class="hero-top"><div class="grow"><div class="eyebrow">Aufnehmen</div>
        <h2 id="h-now">Bereit für den nächsten Call</h2>
        <p class="muted max-60 mt-2">${cal && cal.connected ? (cal.message || "Kein laufender oder kommender Termin heute.") : "Ohne Outlook-Kalender werden Titel und Teilnehmende beim Start eingetippt. Mit Kalender heißt jede Aufnahme wie der Termin, und die Sprechererkennung kennt die Teilnehmenden."}</p></div>
        ${events.length ? eventMenu(events) : ""}</div>
      <div class="hero-actions">${btn("Aufnahme ohne Termin starten …", { cls: "btn-record btn-lg", icon: "rec", act: "start", attrs: data({ choice: "manual" }) })}
        ${cal && !cal.connected ? btn("Mit Outlook verbinden …", { cls: "btn-lg", icon: "calendar", act: "go", attrs: data({ to: "setup" }) }) : ""}</div>
      <p class="hero-note">${I("keyboard")}<span>Jederzeit auch mit ${kbd()} – ${startNote()}</span></p>
    </section>`;
  }
  const start = parseDate(picked.start), end = parseDate(picked.end);
  const inMin = start ? Math.round((start - Date.now()) / 60000) : 0;
  const eyebrow = picked.current ? "Läuft gerade" : inMin > 0 ? `Als Nächstes · in ${inMin} Minuten` : "Termin";
  return h`<section class="card hero" aria-labelledby="h-now">
    <div class="hero-top"><div class="grow">
      <div class="eyebrow">${eyebrow}</div>
      <h2 id="h-now">${picked.subject}</h2>
      <div class="when">${I("clock", "ico-sm")}<span class="num">${start ? whenText(picked.start) : ""}${end ? `–${hm(end)}` : ""}</span>${picked.online ? h`<span aria-hidden="true">·</span>${I("video", "ico-sm")}<span>Online-Besprechung</span>` : ""}${picked.note ? h`<span class="chip">${picked.note}</span>` : ""}</div>
    </div>${eventMenu(events)}</div>
    ${picked.people && picked.people.length ? h`<div><div class="caption muted mb-2">Teilnehmende · ${picked.people.length} Personen</div>
      <div class="people">${picked.people.slice(0, 12).map(n => h`<span class="person">${avatar(n)}<span class="ellipsis">${n}</span></span>`)}${picked.people.length > 12 ? h`<span class="chip">+${picked.people.length - 12}</span>` : ""}</div></div>` : ""}
    <div class="hero-actions">${btn("Aufnahme starten …", { cls: "btn-record btn-lg", icon: "rec", act: "start", attrs: data({ choice: picked.index }) })}<span class="muted">oder ${kbd()}</span></div>
    <p class="hero-note">${I("shield")}<span>${startNote()} Endet der Call, beendet CallRecorder die Aufnahme nach einem sichtbaren Countdown.</span></p>
  </section>`;
}
function startNote() {
  return S.consentMode === "confirm" ? "Vor dem Start wird die Einwilligung aller Teilnehmenden ausdrücklich bestätigt."
    : "Vor dem Start kommt eine Frage mit dem Hinweis auf die Zustimmung aller Teilnehmenden.";
}
function eventMenu(events) {
  const open = S.menu === "events";
  return h`<div class="menu-wrap"><button class="btn btn-subtle" data-act="menu" data-menu="events" aria-haspopup="menu" aria-expanded="${open}">Anderer Termin ${I("down", "ico-sm")}</button>
    ${open ? h`<div class="menu" role="menu"><div class="menu-label">Termine heute</div>
      ${events.slice(0, 12).map(e => { const s = parseDate(e.start), en = parseDate(e.end); return h`<button role="menuitem" data-act="pick" ${data({ i: e.index })}>${I(e.online ? "video" : "calendar")}<span class="ellipsis"><b>${e.all_day ? "ganztägig" : `${s ? hm(s) : ""}–${en ? hm(en) : ""}`}</b> ${e.subject}${e.past ? " (vorbei)" : ""}</span></button>`; })}
      <div class="sep"></div><button role="menuitem" data-act="start" data-choice="manual">${I("pencil")}<span>Ohne Termin – Titel selbst eingeben</span></button></div>` : ""}</div>`;
}
function meterRow(icon, label, key, lv) {
  const pct = Math.round(Math.max(0, Math.min(1, lv || 0)) * 100);
  return h`<div class="meter"><span class="who">${I(icon, "ico-sm")}${label}</span><span class="lv" role="meter" aria-label="Pegel ${label}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i data-live="lv-${key}" data-w="${pct}"></i></span><span class="caption faint num levels-db" data-live="db-${key}">${pct ? `${pct} %` : "–"}</span></div>`;
}
function heroRecording() {
  const r = S.state.recording, a = S.state.autostop || {}, v = a.view;
  const lv = r.levels || {};
  const total = 30, left = v ? v.seconds_left : 0;
  return h`<section class="card hero recording" aria-labelledby="h-now">
    <div class="hero-top"><div class="grow">
      <div class="rec-head"><span class="rec-dot" aria-hidden="true"></span>Aufnahme läuft</div>
      <h2 id="h-now">${r.title}</h2></div>
      <div class="timer" data-live="clock" aria-label="Laufzeit">${clock(r.elapsed)}</div></div>
    ${v ? h`<div class="countdown" role="alert">
      <div class="ring" aria-hidden="true"><svg viewBox="0 0 64 64"><circle class="bg" cx="32" cy="32" r="28"/><circle class="fg" cx="32" cy="32" r="28" stroke-dasharray="175.9" data-off="${175.9 * (1 - Math.min(1, left / total))}" data-live="ring"/></svg><b data-live="cd">${left}</b></div>
      <div class="stack-sm"><h3 data-live="cd-head">${v.headline}</h3><p class="muted">${v.detail}</p>
        <div class="btn-row">${btn(v.stop_label || "Jetzt beenden", { cls: "btn-record", icon: "stop", act: "stopnow" })}${btn(v.keep_label || "Weiter aufnehmen", { act: "keep" })}</div></div></div>` : ""}
    <div class="meters">${meterRow("mic", "Eigenes Mikrofon", "mic", lv.mic)}${meterRow("headset", "Gegenseite", "remote", lv.remote)}</div>
    ${v ? "" : h`<div class="hero-actions">${btn("Aufnahme beenden", { cls: "btn-record btn-lg", icon: "stop", act: "stop" })}</div>`}
    ${a.message ? h`<div class="banner banner-warn has-x">${I("info")}<div class="bd">${a.message}</div><button class="icon-btn x" data-act="as-dismiss" aria-label="Ausblenden">${I("x", "ico-sm")}</button></div>` : ""}
    ${a.status ? h`<div class="autostop">${I("info", "ico-sm")}<span><b>Automatisches Ende:</b> ${a.status}</span></div>` : ""}
  </section>`;
}
function attentionCard() {
  const items = (S.home && S.home.attention) || [];
  if (!items.length) return h`<section class="card" aria-labelledby="h-att"><div class="card-head"><h2 id="h-att">Wartet</h2></div>
    <div class="empty compact"><div class="art">${I("check-circle")}</div><h3>Nichts offen</h3><p>Hier erscheinen Aufnahmen, die eine Entscheidung brauchen – zum Beispiel Stimmen benennen oder eine Zusammenfassung übergeben.</p></div></section>`;
  const tone = { warn: "warn", accent: "ok", err: "err", info: "info", rec: "info", ok: "ok" };
  const icon = { review: "people", handoff: "send", resummarize: "sparkle", process_now: "refresh", open: "alert" };
  return h`<section class="card" aria-labelledby="h-att"><div class="card-head"><h2 id="h-att">Wartet</h2><span class="badge inline">${items.length}</span><span class="grow"></span>${btn("Alle Aufnahmen", { cls: "btn-link btn-sm", act: "go", attrs: data({ to: "library" }) })}</div>
    <ul class="att-list">${items.map(i => h`<li class="att"><span class="ic ${tone[i.status.tone] || "warn"}">${I(icon[(i.action || {}).key] || "alert")}</span>
      <div class="rec-title"><div class="at">${i.title}</div><div class="att-meta">${whenText(i.started_at)} · ${i.duration}</div></div>
      <p class="ad">${i.reason}</p>
      <div class="btn-row">${i.action && i.action.key !== "open" ? btn(i.action.label + (i.action.key === "handoff" ? " …" : ""), { cls: "btn-primary btn-sm", act: "rec-action", attrs: data({ id: i.id, key: i.action.key }) }) : ""}${btn("Öffnen", { cls: "btn-sm btn-subtle", act: "open", attrs: data({ id: i.id }) })}</div></li>`)}</ul></section>`;
}
function retentionCard() {
  const R = S.retention;
  if (!R || !R.enabled || !R.items.length) return "";
  return h`<section class="card" aria-labelledby="h-ret"><div class="card-head"><h2 id="h-ret">Wird gelöscht</h2><span class="badge inline">${R.items.length}</span><span class="grow"></span><span class="caption muted">${R.scope} · nach ${R.days} Tagen${R.permanent ? " · endgültig" : ""}</span></div>
    ${R.notice ? h`<div class="card-body"><p class="strong">${R.notice}</p><div class="btn-row">${btn("Behalten", { cls: "btn-sm", icon: "lock", act: "ret-keep" })}${btn("Jetzt löschen …", { cls: "btn-sm btn-subtle btn-danger", icon: "trash", act: "ret-now" })}</div></div>` : ""}
    <ul class="att-list">${R.items.map(i => h`<li class="att"><span class="ic warn">${I("trash")}</span><div class="rec-title"><div class="at">${i.title}</div><div class="att-meta">${i.when}</div></div>
      ${i.warning ? h`<p class="ad warn-line">${i.warning}</p>` : ""}<div class="btn-row">${btn("Behalten", { cls: "btn-sm", act: "pin", attrs: data({ id: i.id, on: "1" }) })}${btn("Öffnen", { cls: "btn-sm btn-subtle", act: "open", attrs: data({ id: i.id }) })}</div></li>`)}</ul></section>`;
}
async function loadRetention() { const r = await call("retention_overview"); if (r && !r.error) S.retention = r; }
function workCard() {
  const q = S.state.queue || {}, cur = q.current;
  if (!cur && !(q.pending || []).length) return "";
  const pct = cur && cur.fraction >= 0 ? Math.round(cur.fraction * 100) : -1;
  const steps = cur ? cur.steps.filter(s => STEP_NAMES[s]) : [];
  const order = ["finalize", "transcribe", "diarize", "identify", "render", "summarize"];
  const curIdx = cur ? order.indexOf(cur.step) : -1;
  return h`<section class="card" aria-labelledby="h-work"><div class="card-head"><h2 id="h-work">In Arbeit</h2><span class="grow"></span>${(q.pending || []).length ? h`<span class="caption muted">${q.pending.length} weitere warten</span>` : ""}</div>
    ${cur ? h`<div class="card-body work"><div class="work-item">
      <div class="wt"><button class="btn btn-link title-link" data-act="open" ${data({ id: cur.rec })}>${cur.title}</button><span class="muted">· ${cur.step_label}${cur.message ? ` – ${cur.message}` : ""}</span><span class="spacer"></span>${pct >= 0 ? h`<span class="num strong">${pct} %</span>` : ""}</div>
      <div class="bar ${pct < 0 ? "indet" : ""}" role="progressbar" aria-label="Fortschritt ${cur.step_label}" ${pct >= 0 ? raw(`aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"`) : ""}><i ${pct >= 0 ? data({ w: pct }) : ""}></i></div>
      <div class="mini-steps" aria-label="Stand">${order.filter(s => steps.includes(s)).map(s => { const i = order.indexOf(s); const st = i < curIdx ? "done" : i === curIdx ? "busy" : "none"; return h`<span class="ms ${st}">${I(st === "done" ? "check-circle" : st === "busy" ? "clock" : "doc")}${STEP_NAMES[s]}</span>`; })}</div>
    </div></div>` : ""}
    <div class="card-foot">${cur ? btn(cur.cancel_requested ? "Abbruch angefordert" : "Abbrechen", { cls: "btn-sm", act: "cancel-job", attrs: raw(`data-job="${cur.id}" ${cur.cancel_requested ? "disabled" : ""}`) }) : ""}${(q.pending || []).length ? btn("Wartende entfernen …", { cls: "btn-sm btn-subtle", act: "cancel-pending" }) : ""}<span class="spacer"></span><span class="caption faint">Läuft lokal auf diesem Rechner</span></div></section>`;
}
function todayCard() {
  const cal = S.calendar;
  if (!cal) return h`<section class="card" aria-labelledby="h-today"><div class="card-head"><h2 id="h-today">Heute</h2></div><div class="card-body muted">Termine werden geladen …</div></section>`;
  if (!cal.connected) return h`<section class="card" aria-labelledby="h-today"><div class="card-head"><h2 id="h-today">Heute</h2></div>
    <div class="empty compact"><div class="art">${I("calendar")}</div><h3>Termine erscheinen hier</h3><p>Mit der Microsoft-Anmeldung kennt CallRecorder die Termine. Gelesen wird nur der eigene Kalender.</p>${btn("Einrichtung öffnen", { cls: "btn-primary", act: "go", attrs: data({ to: "setup" }) })}</div></section>`;
  const evs = [...(cal.events || [])].filter(e => !e.all_day).sort((a, b) => (a.start < b.start ? -1 : 1));
  const now = new Date();
  let nowShown = false;
  const rows = [];
  for (const e of evs) {
    const s = parseDate(e.start);
    if (!nowShown && s && s > now) { rows.push(h`<li class="nowline" aria-label="Jetzt ${hm(now)}"><span class="num">${hm(now)}</span><i></i></li>`); nowShown = true; }
    rows.push(h`<li class="ag ${e.past ? "past" : e.current ? "now" : ""}"><span class="tm">${s ? hm(s) : ""}</span><span class="ln" aria-hidden="true"></span><div class="rec-title"><div class="gt">${e.subject}</div><div class="gd">${I(e.online ? "video" : "calendar", "ico-sm")} ${e.online ? "Online" : "Termin"} · ${e.people.length} Personen${e.note ? ` · ${e.note}` : ""}</div></div></li>`);
  }
  return h`<section class="card" aria-labelledby="h-today"><div class="card-head"><h2 id="h-today">Heute</h2><span class="grow"></span><span class="caption muted">${WD[now.getDay()]} ${now.getDate()}. ${MONTHS[now.getMonth()]}</span>${btn("", { cls: "btn-subtle btn-sm", icon: "refresh", act: "refresh-cal", attrs: raw('aria-label="Termine aktualisieren"'), title: "Termine aktualisieren" })}</div>
    ${rows.length ? h`<ol class="agenda">${rows}</ol>` : h`<div class="card-body muted">${cal.message || "Heute stehen keine Termine im Kalender."}</div>`}</section>`;
}
function recentCard() {
  const rows = (S.home && S.home.recent) || [];
  if (!rows.length) return h`<section class="card" aria-labelledby="h-recent"><div class="card-head"><h2 id="h-recent">Zuletzt aufgenommen</h2></div>
    <div class="empty compact"><div class="art">${I("mic")}</div><h3>Noch keine Aufnahmen</h3><p>Die erste Aufnahme oben starten – oder jederzeit mit dem Tastenkürzel. Danach stehen hier Transkript und Zusammenfassung.</p>${btn("Einrichtung prüfen", { icon: "checklist", act: "go", attrs: data({ to: "setup" }) })}</div></section>`;
  return h`<section class="card" aria-labelledby="h-recent"><div class="card-head"><h2 id="h-recent">Zuletzt aufgenommen</h2><span class="grow"></span>${btn("Alle Aufnahmen", { cls: "btn-link btn-sm", act: "go", attrs: data({ to: "library" }) })}</div>
    <div class="rows">${rows.map(r => h`<button class="row" data-act="open" ${data({ id: r.id })}><span class="rec-title"><span class="rt ellipsis">${r.title}</span><span class="rd">${whenText(r.started_at)} · ${r.duration}</span></span>${statusChip(r)}</button>`)}</div></section>`;
}
const CHIP_ICON = { err: "alert", warn: "warning", info: "clock", accent: "send", ok: "check", rec: "rec", "": "info" };
const statusChip = r => h`<span class="chip chip-${r.status.tone || "x"}">${I(CHIP_ICON[r.status.tone] || "info")}${r.status.text}</span>`;
const MQ_WIDE = matchMedia("(min-width: 1560px)"), MQ_NARROW = matchMedia("(max-width: 1099px)");
[MQ_WIDE, MQ_NARROW].forEach(m => m.addEventListener("change", () => { if (S.route === "home") renderMain(); }));
function screenHome() {
  const now = new Date();
  const r = S.state.recording || {};
  const setup = S.state.setup || {};
  const ready = setup.ready === false ? h`<button class="chip chip-warn" data-act="go" data-to="setup">${I("warning")}${setup.open === 1 ? "1 Punkt" : `${setup.open || ""} Punkte`} in der Einrichtung offen</button>`
    : h`<span class="chip chip-ok">${I("check")}Bereit</span>`;
  const c = { now: r.on ? heroRecording() : heroIdle(), att: attentionCard(), work: workCard(), today: todayCard(), recent: recentCard(), ret: retentionCard() };
  const col = keys => h`<div class="home-col">${keys.map(k => c[k])}</div>`;
  const grid = MQ_NARROW.matches ? h`<div class="home-grid cols-1">${col(["now", "att", "ret", "work", "today", "recent"])}</div>`
    : MQ_WIDE.matches ? h`<div class="home-grid cols-3">${col(["now", "work"])}${col(["att", "ret"])}${col(["today", "recent"])}</div>`
    : h`<div class="home-grid">${col(["now", "work", "recent"])}${col(["att", "ret", "today"])}</div>`;
  return h`<div class="page"><header class="page-head"><div class="grow"><h1 tabindex="-1" data-focus>Übersicht</h1><p class="sub">${WDL[now.getDay()]}, ${now.getDate()}. ${MONTHS[now.getMonth()]} ${now.getFullYear()}</p></div>
    ${r.on ? h`<span class="chip chip-rec"><span class="rec-dot" aria-hidden="true"></span>Aufnahme läuft</span>` : ready}</header>${grid}</div>`;
}

/* =====================================================================
   8. LIBRARY + DETAIL
   ===================================================================== */
function listPane() {
  const L = S.list;
  if (!L) return h`<section class="lib-list" aria-label="Aufnahmeliste"><div class="lib-tools"><h1 tabindex="-1" data-focus>Aufnahmen</h1></div><div class="lib-scroll muted">Aufnahmen werden geladen …</div></section>`;
  let g = "", items = [];
  for (const r of L.items) {
    const grp = groupOf(parseDate(r.started_at));
    if (grp !== g) { g = grp; items.push(h`<li class="grp" role="presentation">${grp}</li>`); }
    const picked = S.multi.includes(r.id);
    items.push(h`<li role="presentation"><button class="rec-item ${picked ? "picked" : ""}" role="option" aria-selected="${r.id === S.selected || picked}" data-act="select" ${data({ id: r.id })}>
      <span class="t ellipsis">${r.title}</span><span class="m">${whenText(r.started_at)} · ${r.duration}</span>${statusChip(r)}${r.pinned ? h`<span class="chip">${I("lock")}Behalten</span>` : r.removed_text ? h`<span class="chip">${r.removed_text.startsWith("Löschen unvollständig") ? "Löschen unvollständig" : r.reduced ? "Nur Zusammenfassung" : "Audio gelöscht"}</span>` : r.due ? h`<span class="chip chip-warn">${I("trash")}${r.due.split(": ")[1] || r.due}</span>` : ""}</button></li>`);
  }
  return h`<section class="lib-list" aria-label="Aufnahmeliste"><div class="lib-tools">
      <h1 tabindex="-1" data-focus>Aufnahmen</h1>
      <label class="input-icon"><span class="sr-only">Suchen</span>${I("search", "ico-sm")}<input class="input" id="q" type="search" placeholder="Titel oder Person suchen" value="${S.query}" autocomplete="off"><kbd>Strg+F</kbd></label>
      <div class="seg" role="group" aria-label="Filter">${L.filters.filter(f => f.key === "all" || f.count || f.key === S.filter).map(f => h`<button aria-pressed="${S.filter === f.key}" data-act="filter" ${data({ f: f.key })}>${f.label} <span class="n">${f.count}</span></button>`)}</div></div>
    <div class="lib-scroll">${L.items.length ? h`<ul role="listbox" aria-label="Aufnahmen" aria-multiselectable="true" id="reclist" title="Strg+Klick oder Umschalt+Klick wählt mehrere Aufnahmen; Rechtsklick: Löschen und Behalten">${items}</ul>`
      : L.total ? h`<div class="empty compact"><h3>Keine Treffer</h3><p>${S.query ? `Für „${S.query}“ gibt es keine Aufnahme. Gesucht wird in Titeln und Namen der Teilnehmenden.` : "In diesem Filter ist nichts."}</p>${btn("Filter zurücksetzen", { act: "clearq" })}</div>`
      : h`<div class="empty compact"><div class="art">${I("mic")}</div><h3>Noch keine Aufnahmen</h3><p>Nach dem ersten Call stehen hier Transkript und Zusammenfassung.</p>${btn("Aufnahme starten …", { cls: "btn-record", icon: "rec", act: "start" })}</div>`}</div>
    ${S.multi.length > 1 ? h`<div class="lib-foot" role="toolbar" aria-label="Auswahl"><b>${S.multi.length} ausgewählt</b><span class="spacer"></span>${btn("Behalten", { cls: "btn-sm", act: "pin-multi" })}${btn("Auswahl löschen …", { cls: "btn-sm btn-danger", icon: "trash", act: "del-multi" })}${btn("Aufheben", { cls: "btn-sm btn-subtle", act: "multi-clear" })}</div>` : ""}
    ${S.retention && S.retention.notice ? h`<div class="lib-foot"><span class="caption">${I("trash", "ico-sm")} ${S.retention.notice}</span></div>` : ""}
    <div class="lib-foot">${L.items.length === L.total ? `${L.total} Aufnahmen` : `${L.items.length} von ${L.total} Aufnahmen`}<span class="spacer"></span>${btn("Ordner öffnen", { cls: "btn-sm btn-subtle", icon: "folder", act: "open-root" })}</div></section>${ctxMenu()}`;
}
function ctxMenu() {
  const c = S.ctx; if (!c) return "";
  const r = ((S.list || {}).items || []).find(x => x.id === c.id); if (!r) return "";
  return h`<div class="menu ctx" role="menu" data-x="${c.x}" data-y="${c.y}" aria-label="Aktionen für ${r.title}">
    <button role="menuitem" data-act="ctx" ${data({ key: "pin" })}>${I("lock")}${r.pinned ? "Nicht mehr behalten" : "Behalten (nicht automatisch löschen)"}</button><div class="sep"></div>
    ${r.has_audio ? h`<button role="menuitem" data-act="ctx" ${data({ key: "audio" })}>${I("trash")}Nur Audio löschen …</button>` : ""}
    ${!r.reduced ? h`<button role="menuitem" data-act="ctx" ${data({ key: "audio_transcript" })}>${I("trash")}Audio und Transkript löschen …</button>` : ""}
    <button role="menuitem" class="danger" data-act="ctx" ${data({ key: "recording" })}>${I("trash")}Aufnahme vollständig löschen …<span class="hint">Entf</span></button></div>`;
}
const MS_ICON = { ok: "check-circle", running: "clock", missing: "doc", failed: "alert", open: "people", na: "send" };
function miniSteps(d) {
  const st = d.stages;
  const item = (cls, icon, text) => h`<span class="ms ${cls}">${I(icon)}${text}</span>`;
  const t = st.transcript === "ok" ? item("done", "check-circle", "Transkript") : st.transcript === "running" ? item("busy", "clock", "Transkript läuft") : st.transcript === "failed" ? item("err", "alert", "Transkript fehlgeschlagen") : item("none", "doc", "Kein Transkript");
  const s = st.speakers === "ok" ? item("done", "check-circle", "Sprecher benannt") : st.speakers === "open" ? item("open", "people", `${d.unassigned} Stimme(n) offen`) : st.speakers === "running" ? item("busy", "clock", "Sprecher") : item("none", "people", "Sprecher");
  const z = st.summary === "ok" ? item("done", "check-circle", "Zusammenfassung") : st.summary === "failed" ? item("err", "alert", "Zusammenfassung fehlt") : st.summary === "running" ? item("busy", "clock", "Zusammenfassung") : item("none", "sparkle", "Zusammenfassung");
  const o = st.handoff === "ok" ? item("done", "check-circle", "Übergeben") : st.handoff === "open" ? item("changed", "refresh", "Übergabe veraltet") : st.handoff === "missing" ? item("none", "send", "Nicht übergeben") : "";
  return h`<div class="mini-steps" aria-label="Stand der Aufnahme">${t}${s}${z}${o}</div>`;
}
const MORE = [
  ["retranscribe", "lang", "Neu transkribieren …"], ["assign_event", "calendar", "Termin zuordnen …"], ["participants", "people", "Teilnehmende bearbeiten …"],
  ["rename", "pencil", "Umbenennen …"], "-", ["rerender", "doc", "Transkript neu darstellen"], ["recheck", "fingerprint", "Sprecherzuordnung neu prüfen …"],
  ["speaker_limit", "people", "Weitere Sprecher zulassen / begrenzen"], ["reprocess", "refresh", "Komplett neu verarbeiten …"], ["folder", "folder", "Ordner öffnen"], "-",
  ["pin", "lock", "Behalten (nicht automatisch löschen)"], ["delete_audio", "trash", "Nur Audio löschen …"],
  ["delete_material", "trash", "Audio und Transkript löschen …"], ["delete", "trash", "Aufnahme vollständig löschen …", "Entf"],
];
function detailPane() {
  const d = S.detail;
  if (!S.selected || !d) return h`<section class="lib-detail" aria-label="Details"><div class="empty"><div class="art">${I("list")}</div><h2>Keine Aufnahme ausgewählt</h2><p>Links eine Aufnahme auswählen.</p></div></section>`;
  if (d.error) return h`<section class="lib-detail" aria-label="Details"><div class="det-inner"><div class="banner banner-err" role="alert">${I("alert")}<div class="bd">${d.error}</div></div></div></section>`;
  const A = d.actions || {};
  const primary = d.action ? d.action : null;
  const prim = primary && A[primary.key] !== false ? btn(primary.label + (primary.key === "handoff" ? " …" : ""), { cls: "btn-primary", icon: { review: "people", handoff: "send", resummarize: "sparkle", process_now: "refresh" }[primary.key] || "", act: "rec-action", attrs: data({ id: d.id, key: primary.key }) }) : "";
  const second = [];
  if (!primary || primary.key !== "handoff") second.push(btn(d.handoff.done && !d.handoff.current ? "Erneut übergeben …" : "Übergeben …", { icon: "send", act: "rec-action", attrs: raw(`${data({ id: d.id, key: "handoff" })} ${A.handoff ? "" : "disabled"}`) }));
  if (!primary || primary.key !== "review") second.push(btn("Sprecher zuordnen …", { icon: "people", act: "rec-action", attrs: raw(`${data({ id: d.id, key: "review" })} ${A.review ? "" : "disabled"}`) }));
  if (!primary || primary.key !== "resummarize") second.push(btn("Zusammenfassung neu erstellen", { cls: "btn-subtle", icon: "sparkle", act: "rec-action", attrs: raw(`${data({ id: d.id, key: "resummarize" })} ${A.resummarize ? "" : "disabled"}`) }));
  const menuOpen = S.menu === "more";
  const actions = h`${prim}${second}<button class="btn btn-subtle" data-act="rec-action" ${data({ id: d.id, key: "play" })} ${A.play ? "" : raw("disabled")} title="${(d.disabled_why || {}).play || ""}">${I("play", "ico-sm")}Anhören</button>
    <button class="btn btn-subtle btn-danger" data-act="more" ${data({ key: "delete" })} ${A.delete ? "" : raw("disabled")}>${I("trash", "ico-sm")}Löschen …</button>
    <div class="menu-wrap"><button class="btn btn-subtle" data-act="menu" data-menu="more" aria-haspopup="menu" aria-expanded="${menuOpen}" aria-label="Weitere Aktionen" title="Weitere Aktionen">${I("more")}</button>
    ${menuOpen ? h`<div class="menu" role="menu">${MORE.map(m => m === "-" ? raw('<div class="sep"></div>') : h`<button role="menuitem" class="${m[0] === "delete" ? "danger" : ""}" data-act="more" ${data({ key: m[0] })} ${A[m[0]] === false ? raw("disabled") : ""} title="${(d.disabled_why || {})[m[0]] || ""}">${I(m[1])}${m[0] === "speaker_limit" ? (d.speaker_limit.lifted ? "Wieder auf Teilnehmende begrenzen" : "Weitere Sprecher zulassen") : m[0] === "pin" && d.pinned ? "Nicht mehr behalten" : m[2]}${m[3] ? h`<span class="hint">${m[3]}</span>` : ""}</button>`)}</div>` : ""}</div>`;
  const h0 = d.handoff;
  const handoff = !d.has_summary ? "" : h`<div class="handoff ${h0.done && !h0.current ? "changed" : ""}"><span class="ico-wrap">${I(h0.done ? (h0.current ? "check" : "refresh") : "send")}</span>
    <div><div class="strong">${h0.done ? (h0.current ? "An SharePoint übergeben" : "Seit der Übergabe geändert") : "Noch nicht übergeben"}</div><div class="hd">${h0.text.replace(/^SharePoint: /, "")}</div></div><span></span></div>`;
  const busy = d.busy || d.recording;
  const status = d.status_text ? h`<div class="banner ${d.summary_problem ? "banner-err" : "banner-warn"}" ${d.summary_problem ? raw('role="alert"') : ""}>${I(d.summary_problem ? "alert" : "info")}<div class="bd">${d.status_text}</div></div>` : "";
  return h`<section class="lib-detail" aria-label="Details">
    <div class="det-head">
      <button class="btn btn-subtle btn-sm back-to-list" data-act="back">${I("left", "ico-sm")}Alle Aufnahmen</button>
      <h1 id="det-title">${d.title}</h1>
      <div class="det-meta"><span>${I("calendar")}${whenText(d.started_at)}</span><span>${I("clock")}${d.duration}</span><span>${I("lang")}${d.language}</span>${statusChip(d)}${d.pinned ? h`<span class="chip">${I("lock")}Behalten</span>` : d.due ? h`<span class="chip chip-warn">${I("trash")}${d.due}</span>` : ""}</div>
      ${d.participants.length ? h`<div class="det-people">${d.participants.slice(0, 6).map(n => avatar(n))}<span class="ellipsis muted">${d.participants.join(", ")}</span></div>` : ""}
      ${miniSteps(d)}
    </div>
    <div class="det-actions" role="toolbar" aria-label="Aktionen">${busy ? h`${btn("Verarbeitung abbrechen", { act: "cancel-job" })}<span class="caption muted">${d.recording ? "Aufnahme läuft." : "Wird verarbeitet – Aktionen sind danach möglich."}</span>` : actions}</div>
    <div class="det-inner">${status}${handoff}<p class="caption muted">${I("shield", "ico-sm")} ${d.consent}</p>${readerBlock(d)}</div>
  </section>`;
}
function readerBlock(d) {
  const kind = S.tab, versions = (d.versions || {})[kind] || [];
  const sel = S.version_sel[kind] || (versions[0] && versions[0].file) || "";
  const T = S.text;
  let body;
  if (!versions.length) {
    body = kind === "summary" ? h`<div class="empty compact"><div class="art">${I("sparkle")}</div><h3>Noch keine Zusammenfassung</h3><p>${d.summary_problem || (d.has_transcript ? "Sie lässt sich aus dem fertigen Transkript erstellen." : "Sie entsteht nach dem Transkript.")}</p>${d.has_transcript ? btn("Zusammenfassung erstellen", { cls: "btn-primary", icon: "sparkle", act: "rec-action", attrs: data({ id: d.id, key: "resummarize" }) }) : ""}</div>`
      : h`<div class="empty compact"><div class="art">${I("doc")}</div><h3>Noch kein Transkript</h3><p>${d.actions && d.actions.process_now ? "Die Aufnahme ist noch nicht verarbeitet." : "Es entsteht bei der Verarbeitung."}</p></div>`;
  } else if (!T || T.kind !== kind || T.id !== d.id) {
    body = h`<p class="muted">Wird geladen …</p>`;
  } else if (T.error) {
    body = h`<div class="banner banner-err">${I("alert")}<div class="bd">${T.error}</div></div>`;
  } else {
    body = kind === "summary" ? h`<article class="reader" aria-label="Zusammenfassung">${renderBlocks(T.blocks, T.self)}</article>`
      : h`<article class="reader transcript" aria-label="Transkript">${renderBlocks(T.blocks.slice(0, S.textLimit), T.self)}${T.blocks.length > S.textLimit ? h`<button class="btn more" data-act="more-text" id="more-text">Weitere Beiträge anzeigen (${T.blocks.length - S.textLimit} übrig)</button>` : ""}</article>`;
  }
  return h`<div class="reader-tools">
    <div class="tabs" role="tablist" aria-label="Ansicht">
      <button class="tab" role="tab" aria-selected="${kind === "summary"}" data-act="tab" data-tab="summary">${I("sparkle", "ico-sm")}Zusammenfassung</button>
      <button class="tab" role="tab" aria-selected="${kind === "transcript"}" data-act="tab" data-tab="transcript">${I("doc", "ico-sm")}Transkript</button></div>
    ${versions.length ? h`<div class="reader-bar"><label class="sr-only" for="ver">Version</label><select class="select" id="ver">${versions.map(v => h`<option value="${v.file}" ${v.file === sel ? raw("selected") : ""}>${v.label}</option>`)}</select></div>` : ""}
  </div><div class="reader-sheet">${body}</div>`;
}
function inl(runs) {
  return raw((runs || []).map(([t, f]) => {
    let s = esc(t);
    if (f.includes("c")) s = `<code>${s}</code>`;
    if (f.includes("i")) s = `<em>${s}</em>`;
    if (f.includes("b")) s = `<strong>${s}</strong>`;
    return s;
  }).join(""));
}
// sanitised Markdown: only these block types exist, every text is escaped (inl/esc)
function renderBlocks(blocks, self) {
  const out = []; const colors = {}; let next = 1; let list = null;
  const flush = () => { if (list) { out.push(raw(`<${list.tag}>${list.items.join("")}</${list.tag}>`)); list = null; } };
  for (const b of blocks || []) {
    if (b.t !== "li") flush();
    if (b.t === "h") out.push(raw(`<h${b.level + 1}>${part(inl(b.inl))}</h${b.level + 1}>`));
    else if (b.t === "p") out.push(h`<p>${inl(b.inl)}</p>`);
    else if (b.t === "quote") out.push(h`<blockquote>${inl(b.inl)}</blockquote>`);
    else if (b.t === "rule") out.push(raw("<hr>"));
    else if (b.t === "li") {
      const tag = b.num ? "ol" : "ul";
      if (!list || list.tag !== tag) { flush(); list = { tag, items: [] }; }
      list.items.push(part(h`<li class="${b.level ? "l1" : ""}">${b.num ? h`<span class="num-mark">${b.num}</span>` : ""}${inl(b.inl)}</li>`));
    } else if (b.t === "table") {
      out.push(h`<table>${b.head.length ? h`<thead><tr>${b.head.map(c => h`<th>${inl(c)}</th>`)}</tr></thead>` : ""}<tbody>${b.rows.map(r => h`<tr>${r.map(c => h`<td>${inl(c)}</td>`)}</tr>`)}</tbody></table>`);
    } else if (b.t === "utt") {
      const me = self && b.who === self;
      if (!me && !colors[b.who]) colors[b.who] = ((next++ - 1) % SP_COLORS) + 1;
      out.push(h`<div class="utt ${me ? "me" : ""}" data-c="${me ? "me" : colors[b.who]}"><span class="ts">${b.ts}</span><div><div class="who">${b.who}</div><p>${inl(b.inl)}</p></div></div>`);
    }
  }
  flush();
  return out;
}
function screenLibrary() {
  return h`<div class="lib ${S.mobileDetail ? "has-detail" : ""}">${listPane()}${detailPane()}</div>`;
}

/* =====================================================================
   9. SETUP
   ===================================================================== */
const CK_ICON = { ok: "check", warn: "warning", error: "alert", optional: "info", unknown: "info" };
const CK_CLS = { ok: "ok", warn: "warn", error: "err", optional: "opt", unknown: "opt" };
const CK_LABEL = { ok: "In Ordnung", warn: "Hinweis", error: "Fehlt", optional: "Nicht eingerichtet", unknown: "Unbekannt" };
function screenSetup() {
  const C = S.checks;
  const done = C ? C.items.filter(i => i.state === "ok").length : 0, total = C ? C.items.length : 0;
  return h`<div class="page page-narrow"><header class="page-head"><div class="grow"><h1 tabindex="-1" data-focus>Einrichtung</h1><p class="sub">Was eingerichtet ist und was fehlt – mit je einem Schritt zur Lösung.</p></div>${btn("Erneut prüfen", { icon: "refresh", act: "setup", attrs: data({ key: "recheck" }) })}</header>
    ${!C ? h`<section class="card"><div class="card-body muted">${S.checksBusy || "Einrichtung wird geprüft …"}</div></section>` : h`
    <section class="card summary-strip mb-4" aria-label="Ergebnis der Prüfung"><span class="progress-ring" data-p="${total ? Math.round(done / total * 100) : 0}"><b>${done}/${total}</b></span>
      <div><div class="strong">${C.summary}</div><div class="muted">${S.checksBusy || "Optional heißt: alles andere funktioniert auch ohne."}</div></div><span></span></section>
    <section class="card"><ul class="checklist">${C.items.map(i => h`<li class="ck"><span class="st ${CK_CLS[i.state] || "opt"}" aria-label="${CK_LABEL[i.state] || i.state}">${I(CK_ICON[i.state] || "info")}</span>
      <div class="ct">${i.title}${i.required ? "" : h` <span class="chip">optional</span>`}</div>
      <div class="btn-row">${i.actions.map((a, n) => btn(a.label, { cls: `btn-sm ${n === 0 && i.state !== "ok" ? "btn-primary" : ""}`, act: "setup", attrs: data({ key: a.key }) }))}</div>
      <p class="cd pre">${i.detail}${i.key === "whisper" && S.downloads ? `\n${S.downloads}` : ""}</p></li>`)}</ul></section>`}</div>`;
}

/* =====================================================================
   10. SETTINGS
   ===================================================================== */
const EXTRA_PAGES = [["profiles", "Stimmprofile", "fingerprint"], ["updates", "Updates", "update"], ["about", "Über CallRecorder", "info"]];
const PAGE_ICON = { general: "sliders", recording: "mic", autostop: "clock", transcription: "doc", language: "lang", speakers: "people", rendering: "doc", summary: "sparkle", microsoft: "calendar", sharepoint: "send" };
function fieldValue(f) { return Object.prototype.hasOwnProperty.call(S.patch, f.id) ? S.patch[f.id] : f.value; }
function control(f) {
  const v = fieldValue(f), id = `f-${f.id.replace(".", "-")}`;
  const sel = (choices) => h`<select class="select" id="${id}" data-field="${f.id}">${choices.map(([val, lab]) => h`<option value="${val}" ${String(v) === String(val) ? raw("selected") : ""}>${lab}</option>`)}</select>`;
  switch (f.kind) {
    case "bool": return h`<label class="switch"><input type="checkbox" role="switch" id="${id}" data-field="${f.id}" ${v ? raw("checked") : ""} aria-label="${f.label}"></label>`;
    case "select": return sel(f.choices);
    case "radio": return h`<div class="radio-cards" role="radiogroup" aria-label="${f.label}">${f.choices.map(([val, lab]) => { const [t, ...rest] = lab.split(" – "); return h`<label class="rcard"><input type="radio" name="${id}" value="${val}" data-field="${f.id}" ${String(v) === String(val) ? raw("checked") : ""}><span class="rt">${t}</span><span></span>${rest.length ? h`<span class="rd">${rest.join(" – ")}</span>` : ""}</label>`; })}</div>`;
    case "device_mic": case "device_out": {
      const list = (f.kind === "device_mic" ? S.settings.mics : S.settings.outs) || [];
      const choices = [["auto", "Automatisch (Windows-Standard für Kommunikation)"], ...list.map(n => [n, n])];
      if (v && !choices.some(c => c[0] === v)) choices.push([v, `${v} (nicht gefunden)`]);
      return sel(choices);
    }
    case "preset": return sel([["", "Kein Preset"], ...(S.settings.presets || []).map(p => [p, p])]);
    case "model": return h`<input class="input" id="${id}" data-field="${f.id}" value="${v}" list="llm-models"><datalist id="llm-models">${((S.settings.llm || {}).models || []).map(m => h`<option value="${m}"></option>`)}</datalist>`;
    case "anthropic_model": return sel([...(S.settings.anthropic_models || []), ...((S.settings.anthropic_models || []).some(c => c[0] === v) ? [] : [[v, `${v} (eigene Modell-ID)`]])]);
    case "folder": return h`<span class="field-inline"><input class="input" id="${id}" data-field="${f.id}" value="${v}">${btn("Auswählen …", { cls: "btn-sm", act: "pick-folder", attrs: data({ field: f.id }) })}</span>`;
    case "list": return h`<input class="input" id="${id}" data-field="${f.id}" value="${(Array.isArray(v) ? v : [v]).join(", ")}">`;
    case "number": return h`<input class="input num-in num" id="${id}" data-field="${f.id}" inputmode="decimal" value="${String(v).replace(".", ",")}">`;
    default: return h`<input class="input" id="${id}" data-field="${f.id}" value="${v}">`;
  }
}
function settingRow(f) {
  const wide = f.kind === "radio";
  return h`<div class="setting ${wide ? "stack" : ""}"><div><label class="t" for="f-${f.id.replace(".", "-")}">${f.label}</label>${f.help ? h`<div class="d">${f.help}</div>` : ""}</div><div class="ctl">${control(f)}</div></div>`;
}
function extraPanel(key) {
  const st = S.settings;
  if (key === "autostart") {
    const a = st.autostart;
    const on = Object.prototype.hasOwnProperty.call(S.patch, "__autostart") ? S.patch.__autostart : a.on;
    return h`<section class="card"><div class="setting"><div><label class="t" for="f-autostart">Mit Windows starten</label><div class="d">${a.detail}</div></div><div class="ctl"><label class="switch"><input type="checkbox" role="switch" id="f-autostart" data-field="__autostart" ${on ? raw("checked") : ""} ${a.state === "unavailable" ? raw("disabled") : ""}></label></div></div></section>`;
  }
  if (key === "glossary") return h`<section class="card"><div class="setting"><div><div class="t">Glossar und Korrekturliste</div><div class="d">Namen, Firmen und Fachbegriffe helfen der Erkennung; die Korrekturliste ersetzt wiederkehrende Fehler im Transkript.</div></div><div class="ctl">${btn("Glossar bearbeiten …", { act: "textfile", attrs: data({ kind: "glossary" }) })}${btn("Korrekturliste bearbeiten …", { act: "textfile", attrs: data({ kind: "replacements" }) })}</div></div></section>`;
  if (key === "models") {
    const m = st.whisper;
    return h`<section class="card"><div class="card-head"><h2>Installierte Whisper-Modelle</h2><span class="grow"></span>${btn("Rechenwerk prüfen", { cls: "btn-sm", icon: "refresh", act: "whisper", attrs: data({ a: "gpu" }) })}</div><div class="card-body stack-sm">
      ${m ? h`<p class="muted">${m.effective}</p>${m.models.length ? h`<ul class="list-plain">${m.models.map(x => h`<li><span class="grow">${x.text}</span>${btn("Löschen …", { cls: "btn-sm btn-subtle btn-danger", act: "whisper-del", attrs: data({ name: x.name, size: x.size }) })}</li>`)}</ul><p class="caption muted">Zusammen ${m.total} in ${m.root}</p>` : h`<p class="muted">Keine Whisper-Modelle in ${m.root}.</p>`}`
        : h`<p class="muted">Wird ermittelt …</p>`}</div></section>`;
  }
  if (key === "llm_models") {
    const l = st.llm || {};
    const pull = S.pull;
    return h`<section class="card"><div class="card-head"><h2>Lokale Modelle</h2></div><div class="card-body stack-sm">
      <div class="btn-row">${btn("Installierte Modelle abfragen", { cls: "btn-sm", icon: "refresh", act: "llm-list" })}${btn("Modell laden / aktualisieren …", { cls: "btn-sm", icon: "download", act: "llm-pull" })}</div>
      ${l.text ? h`<p class="muted">${l.text}</p>` : ""}
      ${pull ? h`<div class="bar ${pull.fraction === null || pull.fraction === undefined ? "indet" : ""}"><i ${pull.fraction !== null && pull.fraction !== undefined ? data({ w: Math.round(pull.fraction * 100) }) : ""}></i></div><p class="caption muted">${pull.status}</p>` : ""}
      ${st.remote_allowed ? h`<div class="banner banner-err">${I("alert")}<div class="bd">In der Konfiguration sind entfernte KI-Server erlaubt (allow_remote_endpoint_data_leaves_this_computer). Gesprächsinhalte können dann diesen Rechner verlassen.</div></div>` : ""}
    </div></section>`;
  }
  if (key === "apikey") {
    const k = st.apikey;
    return h`<section class="card"><div class="card-head"><h2>Claude (Anthropic-API)</h2></div><div class="card-body stack-sm">
      <div class="banner banner-warn">${I("cloud")}<div class="bd">Bei diesem Anbieter wird das Transkript an Anthropic übertragen und verlässt diesen Rechner. Kosten pro Token über das eigene Anthropic-Konto.</div></div>
      <div class="field"><label class="label" for="apikey">API-Schlüssel</label><div class="row-flex"><input class="input input-sm" id="apikey" type="password" autocomplete="off" placeholder="${k.present ? "Neuen Schlüssel eingeben" : "sk-ant-…"}">${btn("Speichern", { cls: "btn-sm", act: "key-save" })}${k.present ? btn("Löschen", { cls: "btn-sm btn-subtle btn-danger", act: "key-delete" }) : ""}${btn("Verbindung testen", { cls: "btn-sm", act: "key-test" })}</div>
        <p class="help">${k.present ? `Schlüssel ${k.masked} (${k.source}). Gespeichert mit Windows verschlüsselt – nur das eigene Windows-Konto auf diesem Rechner kann ihn lesen; er steht nie in config.toml.` : "Kein Schlüssel hinterlegt (Anthropic-Konsole › API Keys)."}</p>
        ${st.keyTest ? h`<p class="help pre">${st.keyTest}</p>` : ""}</div></div></section>`;
  }
  if (key === "prompts") return h`<section class="card"><div class="setting"><div><div class="t">Anweisungen für die Zusammenfassung</div><div class="d">Vorlagen mit Prüfung, Standard wiederherstellen und Test an einer Aufnahme.</div></div><div class="ctl">${btn("Anweisungen bearbeiten …", { act: "go", attrs: data({ to: "prompts" }) })}</div></div></section>`;
  if (key === "login") return h`<section class="card"><div class="setting"><div><div class="t">Anmeldung</div><div class="d">Nach dem Speichern der IDs anmelden; es wird nur gelesen (eigener Kalender).</div></div><div class="ctl">${btn("Bei Microsoft anmelden …", { cls: "btn-primary", act: "setup", attrs: data({ key: "login" }) })}</div></div></section>`;
  if (key === "retention_log") {
    const R = S.retention, log = (R && R.log) || [];
    return h`<section class="card"><div class="card-head"><h2>Löschprotokoll</h2><span class="grow"></span><span class="caption muted">nur was, wann, warum – kein Inhalt</span></div>
      ${log.length ? h`<ul class="origins">${log.slice(0, 30).map(e => h`<li><span class="ellipsis">${(e.at || "").slice(0, 16).replace("T", " ")} · ${e.folder || ""}</span><span>${{ audio_transcript: "Audio und Transkript", recording: "ganze Aufnahme", audio: "Audio" }[e.scope] || e.scope || ""}${e.by === "retention" ? " · automatisch" : " · von Hand"}${e.error ? " · fehlgeschlagen" : ""}</span></li>`)}</ul>` : h`<div class="card-body muted">Noch nichts gelöscht.</div>`}</section>`;
  }
  if (key === "profiles_link") return h`<section class="card"><div class="setting"><div><div class="t">Stimmprofile</div><div class="d">Gelernte Stimmen ansehen, einzelne Einträge entfernen, neu aufbauen oder löschen.</div></div><div class="ctl">${btn("Stimmprofile verwalten …", { act: "setpage", attrs: data({ page: "profiles" }) })}</div></div></section>`;
  return "";
}
function pageProfiles() {
  const P = S.profiles;
  return h`<div class="banner mb-4">${I("shield")}<div class="bt">Biometrische Daten – nur auf diesem Rechner</div><div class="bd">Ein Profil entsteht nur, wenn eine Stimme bestätigt wird („Stimme merken“). Es ist mit Windows verschlüsselt und lässt sich jederzeit löschen – zum Beispiel, wenn eine Person das verlangt.</div></div>
    ${P && P.text ? h`<div class="banner banner-ok mb-4">${I("info")}<div class="bd">${P.text}</div></div>` : ""}
    <section class="card">${!P ? h`<div class="card-body muted">Wird geladen …</div>` : !P.profiles.length ? h`<div class="empty compact"><div class="art">${I("fingerprint")}</div><h3>Keine Stimmprofile</h3><p>Sie entstehen beim Zuordnen von Sprechern mit „Stimme merken“.</p></div>`
      : P.profiles.map(p => h`<div class="profile-row"><div class="rec-title"><div class="strong">${p.name}</div><div class="caption muted">${p.email ? `${p.email} · ` : ""}${p.samples} Proben · aktualisiert ${p.updated}</div></div>
        <div class="btn-row">${btn("Nur Bestätigtes behalten …", { cls: "btn-sm", act: "prof", attrs: data({ a: "reset", pid: p.id }) })}${btn("Neu aufbauen", { cls: "btn-sm", act: "prof", attrs: data({ a: "rebuild", pid: p.id }) })}${btn("Löschen …", { cls: "btn-sm btn-subtle btn-danger", act: "prof", attrs: data({ a: "delete", pid: p.id, name: p.name }) })}</div>
        ${p.origins.length ? h`<details class="adv origins-wrap"><summary>${I("right", "ico-sm")}Gelernt aus ${p.origins.length} Einträgen</summary><ul class="origins">${p.origins.map(o => h`<li><span class="ellipsis">${o.recording} · ${o.speaker}</span><span>${o.source}${o.seconds ? ` · ${o.seconds} s` : ""}</span>${btn("Entfernen …", { cls: "btn-sm btn-subtle", act: "prof", attrs: data({ a: "remove_origin", pid: p.id, i: o.index }) })}</li>`)}</ul></details>` : ""}</div>`)}</section>
    ${P && P.profiles.length ? h`<div class="btn-row mt-4">${btn("Alle Stimmprofile löschen …", { cls: "btn-danger", act: "prof", attrs: data({ a: "delete_all" }) })}</div>` : ""}`;
}
function pageUpdates() {
  const a = S.about || {};
  return h`<section class="card"><div class="setting"><div><div class="t">Installiert</div><div class="d">${a.version || S.version}</div></div><div class="ctl">${btn("Nach Updates suchen …", { cls: "btn-primary", icon: "update", act: "updates" })}</div></div>
    <div class="setting"><div><div class="t">Vorherige Version wiederherstellen</div><div class="d">${a.previous ? `Zurück zu ${a.previous}. Aufnahmen und Einstellungen bleiben.` : "Nach dem ersten Update in der App verfügbar."}</div></div><div class="ctl">${btn("Wiederherstellen …", { icon: "undo", act: "restore", attrs: a.previous ? raw("") : raw("disabled") })}</div></div></section>
    <p class="help mt-4">Für ein Update wird CallRecorder beendet und danach automatisch neu gestartet. Schlägt etwas fehl, wird der bisherige Stand wiederhergestellt. Geänderte Python-Pakete (z. B. für diese Oberfläche) werden dabei mit installiert.</p>`;
}
function pageAbout() {
  const a = S.about || {};
  return h`<section class="card"><div class="card-body stack-sm"><h2 class="card-title">CallRecorder</h2><p>${a.version || S.version}</p>
    <p class="muted">Lokale Gesprächsaufnahme mit Transkript, Sprechererkennung und Zusammenfassung. Eigenständiges Programm; Audio und Stimmprofile verlassen den Rechner nie.</p>
    <p class="caption muted">Python ${a.python || ""} · Oberfläche: neue Oberfläche (Vorschau)</p></div>
    <div class="setting"><div><div class="t">Protokolldatei</div><div class="d anywhere">${a.log || ""}</div></div><div class="ctl">${btn("Öffnen", { act: "open-path", attrs: data({ what: "log" }) })}</div></div>
    <div class="setting"><div><div class="t">Anleitung</div><div class="d">README mit allen Schritten und der Fehlerbehebung.</div></div><div class="ctl">${btn("Öffnen", { act: "open-path", attrs: data({ what: "readme" }) })}</div></div>
    <div class="setting"><div><div class="t">Aufnahmeordner</div><div class="d anywhere">${a.recordings || ""}</div></div><div class="ctl">${btn("Öffnen", { act: "open-path", attrs: data({ what: "recordings" }) })}</div></div>
    <div class="setting"><div><div class="t">Klassisches Fenster</div><div class="d">Zurück zur bisherigen Oberfläche. Auch per Kommandozeile: callrec tray --ui classic.</div></div><div class="ctl">${btn("Zum klassischen Fenster wechseln …", { act: "to-classic" })}</div></div>
    <div class="setting"><div><div class="t">Beenden</div><div class="d">Beendet CallRecorder ganz (auch im Infobereich). Eine laufende Aufnahme wird vorher beendet.</div></div><div class="ctl">${btn("CallRecorder beenden …", { act: "quit" })}</div></div></section>`;
}
function screenSettings() {
  const st = S.settings;
  const pages = st ? st.pages : [];
  const cur = S.setPage;
  const page = pages.find(p => p.key === cur);
  let content;
  if (!st) content = h`<p class="muted">Einstellungen werden geladen …</p>`;
  else if (cur === "profiles") content = pageProfiles();
  else if (cur === "updates") content = pageUpdates();
  else if (cur === "about") content = pageAbout();
  else if (page) {
    const basic = page.fields.filter(f => !f.adv), adv = page.fields.filter(f => f.adv);
    const pre = page.extras.filter(e => e === "autostart"), post = page.extras.filter(e => e !== "autostart");
    content = h`${page.intro ? h`<p class="muted max-80 mb-4">${page.intro}</p>` : ""}
      <section class="card">${basic.map(settingRow)}</section>${pre.map(extraPanel)}
      ${adv.length ? h`<details class="adv"><summary>${I("right", "ico-sm")}Feineinstellung</summary><section class="card">${adv.map(settingRow)}</section></details>` : ""}
      ${post.map(extraPanel)}`;
  }
  const dirty = Object.keys(S.patch).length > 0;
  const editable = page !== undefined;
  return h`<div class="page"><header class="page-head"><div class="grow"><h1 tabindex="-1" data-focus>Einstellungen</h1></div></header>
    <div class="set-layout"><nav class="subnav" aria-label="Bereiche">${pages.map(p => h`<button data-act="setpage" ${data({ page: p.key })} aria-current="${p.key === cur}">${I(PAGE_ICON[p.key] || "sliders")}${p.title}</button>`)}
      <div class="lbl">Weiteres</div>${EXTRA_PAGES.map(([k, t, ic]) => h`<button data-act="setpage" ${data({ page: k })} aria-current="${k === cur}">${I(ic)}${t}</button>`)}</nav>
      <div class="set-col">${content}</div></div></div>
    ${editable ? h`<div class="sticky-foot"><div class="grow">${dirty ? h`<b>Ungespeicherte Änderungen</b>` : h`<span class="muted">Änderungen gelten für neue Aufnahmen und Verarbeitungen; Tastenkürzel und Oberfläche nach einem Neustart.</span>`}</div>
      ${btn("Als Preset speichern …", { cls: "btn-subtle", act: "save-preset" })}${btn("Verwerfen", { act: "discard", attrs: dirty ? raw("") : raw("disabled") })}${btn("Speichern", { cls: "btn-primary", act: "save-settings", attrs: dirty ? raw("") : raw("disabled") })}</div>` : ""}`;
}

/* =====================================================================
   11. PROMPT EDITOR
   ===================================================================== */
function screenPrompts() {
  const P = S.prompts;
  if (!P) return h`<div class="page"><p class="muted">Wird geladen …</p></div>`;
  const cur = P.prompts.find(p => p.key === P.tab) || P.prompts[0];
  const ph = Object.entries(cur.placeholders || {});
  const recs = (S.list && S.list.items) || [];
  return h`<div class="page page-mid"><nav class="crumbs" aria-label="Pfad"><button data-act="go" data-to="settings">Einstellungen</button>${I("right", "ico-sm")}<span>Anweisungen</span></nav>
    <header class="page-head"><div class="grow"><h1 tabindex="-1" data-focus>Anweisungen für die Zusammenfassung</h1><p class="sub max-80">Änderungen gelten für künftige Zusammenfassungen; bestehende bleiben als Version erhalten. Gespeichert in ${P.dir}.</p></div></header>
    <div class="tabs prompt-tabs" role="tablist">${P.prompts.map(p => h`<button class="tab" role="tab" aria-selected="${p.key === cur.key}" data-act="ptab" ${data({ k: p.key })}>${p.label}</button>`)}</div>
    <div class="prompt-grid"><div class="stack-sm"><label class="sr-only" for="ptext">${cur.label}</label><textarea class="textarea code" id="ptext" spellcheck="false">${P.texts[cur.key]}</textarea>
      ${P.problems && P.problems.length ? h`<div class="banner banner-warn" role="alert">${I("warning")}<div class="bd pre">${P.problems.join("\n")}</div></div>` : ""}
      ${P.msg ? h`<div class="banner banner-ok">${I("check-circle")}<div class="bd">${P.msg}</div></div>` : ""}</div>
      <div class="stack"><section class="card"><div class="card-head"><h2>Platzhalter</h2></div><div class="card-body">${ph.length ? h`<ul class="list-plain">${ph.map(([k, v]) => h`<li><span class="ph">{${k}}</span><span class="muted">${v}</span></li>`)}</ul>` : h`<p class="muted">Keine Platzhalter. Die Sprache des Protokolls wird automatisch ergänzt.</p>`}</div></section>
      <section class="card"><div class="card-head"><h2>Mit einer Aufnahme testen</h2></div><div class="card-body stack-sm"><p class="muted">Speichert und erstellt die Zusammenfassung der gewählten Aufnahme als neue Version; die bisherige bleibt zum Vergleich.</p>
        <select class="select" id="ptest" aria-label="Aufnahme zum Testen">${recs.filter(r => r.stages.transcript === "ok").slice(0, 50).map(r => h`<option value="${r.id}">${r.title} · ${whenText(r.started_at)}</option>`)}</select>
        ${btn("Speichern und testen", { act: "prompt-test" })}</div></section></div></div>
    <div class="sticky-foot"><div class="grow"></div>${btn("Standard wiederherstellen …", { cls: "btn-subtle", act: "prompt-default" })}${btn("Abbrechen", { act: "go", attrs: data({ to: "settings" }) })}${btn("Speichern", { cls: "btn-primary", act: "prompt-save" })}</div></div>`;
}

/* =====================================================================
   12. DIALOGS (from Python via "dialog" events, or local)
   ===================================================================== */
function modal(d, head, body, foot, cls = "") {
  return h`<div class="scrim" data-act="scrim"><div class="modal ${cls}" role="dialog" aria-modal="true" aria-labelledby="dlg-t" data-dialog="${d.id}">
    <div class="modal-head"><div class="grow">${head}</div><button class="icon-btn" data-act="dlg-close" aria-label="Schließen">${I("x")}</button></div>
    <div class="modal-body">${body}</div><div class="modal-foot">${foot}</div></div></div>`;
}
const DLG = {};
DLG.start = d => {
  const x = d.data, ask = x.mode === "ask";
  return modal(d, h`<h2 id="dlg-t">${ask ? "Aufnehmen?" : "Aufnahme vorbereiten"}</h2><p class="muted" id="st-info">${x.info}</p>`, h`
    <div class="dlg-statement first" role="group" aria-labelledby="st-legend"><span class="legend" id="st-legend">Zustimmung aller Teilnehmenden (§ 201 StGB)</span>
      <p class="${ask ? "muted" : ""}" id="st-statement">${x.statement}</p>
      ${x.cloud_note ? h`<div class="banner banner-warn">${I("cloud", "ico-sm")}<div class="bd">${x.cloud_note}</div></div>` : ""}
      ${ask ? "" : h`<label class="check"><input type="checkbox" id="st-check"><span class="t"><b>${x.checkbox}</b></span></label>`}</div>
    <div class="field"><label class="label" for="st-title">Titel</label><input class="input" id="st-title" value="${x.title}" placeholder="Anruf"></div>
    <div class="field"><label class="label" for="st-people">Teilnehmende</label><textarea class="textarea" id="st-people" rows="4" spellcheck="false" placeholder="Eine Person pro Zeile, optional: Name &lt;E-Mail&gt;">${x.participants_text}</textarea>
      <div class="row-flex"><span class="help grow">Nur diese Personen werden später automatisch als Sprecher erkannt.</span>${btn(".ics-Einladung importieren …", { cls: "btn-subtle btn-sm", icon: "calendar", act: "import-ics" })}</div></div>
    ${x.error ? h`<div class="banner banner-err error-line" role="alert">${I("alert")}<div class="bd">${x.error}</div></div>` : ""}`,
  h`<span class="caption muted left">${x.record_note}</span>${btn(x.cancel_label, { act: "dlg-cancel" })}<button class="btn btn-record" data-act="st-go" id="st-go" ${ask ? "" : raw("disabled")}>${I("rec", "ico-sm")}${x.start_label}</button>`);
};
DLG.record_prompt = d => {
  const x = d.data;
  return modal(d, h`<h2 id="dlg-t">${x.question}</h2>`, h`<p class="muted">${x.message}</p>${x.reminder ? h`<p class="caption muted mt-2">${x.reminder}</p>` : ""}`,
    x.mode === "ask" ? h`${btn("Details …", { cls: "btn-subtle left", act: "rp", attrs: data({ c: "details" }) })}${btn("Nicht aufnehmen", { act: "rp", attrs: data({ c: "dismiss" }) })}${btn("Aufnehmen", { cls: "btn-record", icon: "rec", act: "rp", attrs: data({ c: "record" }) })}`
      : h`${btn("Nicht aufnehmen", { act: "rp", attrs: data({ c: "dismiss" }) })}${btn("Aufnahme vorbereiten …", { cls: "btn-primary", act: "rp", attrs: data({ c: "details" }) })}`);
};
DLG.after_recording = d => {
  const x = d.data, p = d.local.plan || (d.local.plan = { ...x.plan });
  const steps = [
    ["transcript", "Transkript", "Lokal auf diesem Rechner.", true],
    ["speakers", "Sprecher", "Erkennt bekannte Stimmen. Offene Stimmen werden danach abgefragt (überspringbar).", p.transcript],
    ["summary", "Zusammenfassung", x.summary_problem ? `Nicht möglich: ${x.summary_problem}` : "Mit dem eingestellten Anbieter.", p.transcript && !x.summary_problem],
    ["handoff", "An SharePoint übergeben", "Nur die Zusammenfassung, für den Assistenten in SharePoint – ohne weitere Vorschau.", p.transcript && p.summary],
  ];
  if (d.local.confirmDiscard) return modal(d, h`<h2 id="dlg-t">Aufnahme verwerfen?</h2>`, h`<p>„${x.title}“ in den Papierkorb verschieben?</p><p class="muted mt-2">Audio und alle Daten dieser Aufnahme werden gelöscht (aus dem Papierkorb wiederherstellbar).</p>`,
    h`${btn("Zurück", { act: "pc-back" })}${btn("Verwerfen", { cls: "btn-primary btn-danger", act: "pc", attrs: data({ c: "discard" }) })}`);
  return modal(d, h`<div class="eyebrow">${I("check-circle", "ico-sm")} Aufnahme gespeichert · ${x.duration}</div><h2 id="dlg-t">Aufnahme beendet – jetzt vollständig verarbeiten?</h2><p class="muted">${x.title}</p>`, h`
    <p class="mb-4">Das passiert nacheinander im Hintergrund. Weiterarbeiten ist währenddessen möglich.</p>
    <ol class="pipe" aria-label="Schritte">${steps.map(([k, t, desc, enabled], i) => h`<li class="pipe-step ${p[k] && enabled !== false ? "" : "off"}"><div class="top"><span class="n">${i + 1}</span><label class="switch"><input type="checkbox" role="switch" data-plan="${k}" aria-label="${t}" ${p[k] && enabled !== false ? raw("checked") : ""} ${k === "transcript" || enabled === false ? raw("disabled") : ""}></label></div><span class="st">${t}</span><span class="sd">${desc}</span>${k === "handoff" && x.handoff_problem ? h`<span class="warn-line">${x.handoff_problem}</span>` : ""}</li>`)}</ol>
    <p class="help mt-4">Vorauswahl: Einstellungen › Aufnahme. Ohne Übergabe ist später jederzeit eine Übergabe mit Vorschau möglich.</p>`,
  h`${btn("Verwerfen …", { cls: "btn-subtle btn-danger left", act: "pc-discard" })}${btn("Später", { act: "pc", attrs: data({ c: "later" }) })}${btn("Vollständig verarbeiten", { cls: "btn-primary", act: "pc", attrs: data({ c: "full" }) })}`, "wide");
};
DLG.review = d => {
  const x = d.data, L = d.local;
  if (!L.sel) { L.sel = {}; L.keep = {}; L.other = {}; for (const r of x.rows) { L.sel[r.id] = r.initial || ""; L.keep[r.id] = r.keep; } L.resum = !x.in_chain; }
  const named = x.rows.filter(r => !r.minor || L.sel[r.id]);
  const open = named.filter(r => !L.sel[r.id]).length;
  const rows = x.rows.filter(r => !r.minor || L.showFragments || L.sel[r.id]).map((r, n) => {
    const c = (n % SP_COLORS) + 1;
    const values = r.choices.map(c => c.value);
    const typed = L.sel[r.id] && !values.includes(L.sel[r.id]);
    return h`<section class="card voice" aria-labelledby="vh-${n}" data-c="${c}">
      <div class="rec-title"><div class="voice-head"><span class="swatch"></span><h3 id="vh-${n}">${r.label}</h3>${r.minor && !L.sel[r.id] ? h`<span class="chip">Kurzes Fragment · ${r.speech} s Sprache</span>` : r.source === "auto" && r.name ? h`<span class="chip chip-accent">${I("check")}Automatisch erkannt · ${r.name}</span>` : r.source === "self_echo" ? h`<span class="chip chip-info">${I("info")}Klingt wie die eigene Stimme</span>` : L.sel[r.id] ? "" : h`<span class="chip chip-warn">Noch ohne Namen</span>`}</div>
        <div class="play"><button class="pbtn" data-act="sample" ${data({ sid: r.id })} aria-label="Beispiel von ${r.label} anhören" ${r.can_play ? "" : raw("disabled")}>${I(L.playing === r.id ? "pause" : "play", "ico-sm")}</button><span class="caption muted num">${r.talk} Sprechzeit · ${r.utterances} Beiträge</span>${r.can_play ? h`<span class="caption muted num" data-sample-info="${r.id}">${(L.info || {})[r.id] || r.sample_info}</span>` : ""}${r.can_play && r.sample_count > 1 ? h`<button class="btn btn-link btn-sm" data-act="sample" ${data({ sid: r.id, step: "1" })}>Weiteres Beispiel</button>` : ""}</div>
        ${r.sample ? h`<div class="quotes"><div class="qt"><span class="ts">Beispiel</span><q>${r.sample}</q></div></div>` : ""}
        ${r.note ? h`<p class="note mt-2">${r.note}</p>` : ""}</div>
      <fieldset class="choices"><legend class="label">Diese Stimme gehört zu …</legend>
        ${r.choices.map(ch => h`<label class="choice ${ch.kind === "suggest" ? "suggest" : ch.kind === "self" ? "me" : ""}"><input type="radio" name="v-${r.id}" value="${ch.value}" data-sel="${r.id}" ${L.sel[r.id] === ch.value ? raw("checked") : ""}>${avatar(ch.kind === "self" ? "Ich" : ch.label, "", ch.kind === "self")}<span class="rec-title"><span class="cn">${ch.kind === "self" ? "Das bin ich" : ch.label.replace(/ \(kein Teilnehmer dieses Termins\)$/, "")}</span>${ch.kind === "suggest" ? h`<span class="cd">Vorschlag: klingt ähnlich · kein Teilnehmer dieses Termins</span>` : ch.kind === "self" ? h`<span class="cd">eigene Stimme auf der Spur der Gegenseite</span>` : ""}</span></label>`)}
        <div class="choice-other"><input class="input" placeholder="Andere Person eintippen …" aria-label="Name einer anderen Person für ${r.label}" data-other="${r.id}" value="${typed ? L.sel[r.id] : ""}"></div>
        <label class="check"><input type="checkbox" data-keep="${r.id}" ${L.keep[r.id] ? raw("checked") : ""}><span class="t">Stimme merken <span class="help">${r.source === "auto" ? "Bestätigt die automatische Erkennung und verbessert das Profil." : "Legt ein Stimmprofil an – bleibt verschlüsselt auf diesem Rechner."}</span></span></label>
      </fieldset></section>`;
  });
  return modal(d, h`<h2 id="dlg-t">Sprecher zuordnen – ${x.title}</h2><p class="muted max-80">Jede Stimme der Gegenseite einer Person zuordnen. Zur Auswahl stehen die Teilnehmenden dieses Termins; ein eingetippter Name wird als Teilnehmende ergänzt. Im Zweifel kurz hineinhören.</p>`, h`
    ${x.warnings.map(w => h`<div class="banner banner-warn mb-4">${I("mic")}<div class="bd">Mikrofon prüfen: ${w}</div></div>`)}
    ${x.bound && x.bound.merged ? h`<p class="help mb-4">Die Gegenseite wurde auf ${x.bound.participants} eingeladene Person(en) begrenzt (zuvor ${x.bound.raw} getrennte Stimmen). Waren nicht eingeladene Personen dabei? ${btn("Weitere Sprecher zulassen", { cls: "btn-link btn-sm", act: "rv", attrs: data({ a: "more" }) })}</p>` : ""}
    <div class="sp-grid"><section class="card"><div class="fixed-row">${avatar(x.me.name, "avatar-lg", true)}<div class="rec-title"><div class="strong">${x.me.name} <span class="chip">${I("lock")}Mikrofonspur, immer die eigene Stimme</span></div><div class="muted">${x.me.utterances} Beiträge · ${x.me.talk} Sprechzeit · Name ändern unter Einstellungen › Allgemein</div></div>
      <button class="btn" data-act="sample" data-sid="${x.me.id}" ${x.me.can_play ? "" : raw("disabled")}>${I(L.playing === x.me.id ? "pause" : "play", "ico-sm")}Anhören</button></div></section>
    ${x.fragments ? h`<p class="help">${btn(L.showFragments ? "Kurze Fragmente ausblenden" : `Kurze Fragmente anzeigen (${x.fragments})`, { cls: "btn-link btn-sm", act: "rv-frag" })} Sehr kurze Stimmen (Husten, „mhm“, Störgeräusche) werden nicht abgefragt; ihr Text steht bei der passenden Person oder als „Unklar“.</p>` : ""}
    ${x.rows.length ? rows : h`<div class="empty compact"><h3>Keine Stimmen der Gegenseite</h3><p>In dieser Aufnahme wurden keine Sprecher der Gegenseite erkannt.</p></div>`}</div>
    ${x.error ? h`<div class="banner banner-err mt-4" role="alert">${I("alert")}<div class="bd">${x.error}</div></div>` : ""}
    <audio id="sample-audio" preload="none"></audio>`,
  h`<span class="left"><b>${named.length - open} von ${named.length} Stimmen zugeordnet</b> <span class="muted">· Stimmprofile sind biometrische Daten und verlassen den Rechner nie.</span></span>
    ${x.in_chain ? h`<span class="caption muted">Danach wird die Zusammenfassung mit diesen Namen erstellt.</span>` : h`<label class="check"><input type="checkbox" id="rv-resum" ${L.resum ? raw("checked") : ""}><span class="t">Zusammenfassung danach neu erstellen</span></label>`}
    ${x.can_handoff ? btn("Übergeben …", { cls: "btn-subtle", icon: "send", act: "rv", attrs: data({ a: "handoff" }) }) : ""}
    ${btn(x.in_chain ? "Überspringen" : "Abbrechen", { act: "dlg-cancel" })}${x.rows.length ? btn("Übernehmen", { cls: "btn-primary", act: "rv", attrs: data({ a: "apply" }) }) : ""}`, "review");
};
DLG.participants = d => modal(d, h`<h2 id="dlg-t">Teilnehmende – ${d.data.title}</h2>`, h`<div class="field"><label class="label" for="pp">Eine Person pro Zeile, optional mit E-Mail: Name &lt;E-Mail&gt;</label><textarea class="textarea tall" id="pp" spellcheck="false">${d.data.text}</textarea>
  <p class="help">Stimmen werden automatisch nur Personen aus dieser Liste zugeordnet; manuelle Zuordnungen bleiben erhalten. Was bei der Aufnahme vermerkt wurde (Einwilligung bzw. Start mit Hinweis), bleibt unverändert.</p></div>`,
  h`${btn("Abbrechen", { act: "dlg-cancel" })}${btn("Speichern und Sprecher neu erkennen", { cls: "btn-primary", act: "pp-ok" })}`);
DLG.choice = d => modal(d, h`<h2 id="dlg-t">${d.data.title}</h2>`, h`<p class="pre">${d.data.message}</p>`,
  h`${d.data.choices.slice().reverse().map((c, i) => btn(c.label, { cls: i === d.data.choices.length - 1 ? "btn-primary" + (c.key === d.data.danger ? " btn-danger" : "") : (c.key === d.data.danger ? "btn-danger" : ""), act: "ch-ok", attrs: data({ c: c.key }) }))}`);
DLG.confirm = d => modal(d, h`<h2 id="dlg-t">${d.data.title}</h2>`, h`<p class="pre">${d.data.message}</p>`,
  h`${btn(d.data.cancel || "Abbrechen", { act: "dlg-cancel" })}${btn(d.data.confirm || "OK", { cls: `btn-primary ${d.data.danger ? "btn-danger" : ""}`, act: "cf-ok" })}`);
DLG.info = d => modal(d, h`<h2 id="dlg-t">${d.data.title}</h2>`, h`<p class="pre">${d.data.message}</p>`, h`${btn("Schließen", { cls: "btn-primary", act: "dlg-cancel" })}`);
DLG.handoff = d => {
  const x = d.data;
  return modal(d, h`<h2 id="dlg-t">Zusammenfassung an SharePoint übergeben</h2><p class="muted">${x.title}</p>`, h`
    <div class="banner banner-warn">${I("cloud")}<div class="bd">${x.warning}</div></div>
    <div class="handoff mt-4"><span class="ico-wrap">${I("folder")}</span><div><div class="strong">Ziel</div><div class="hd">${x.target} › ${x.filename}</div></div><span></span></div>
    ${x.previous ? h`<p class="help mt-2">Zuletzt übergeben am ${x.previous.at}${x.previous.same ? " (identischer Inhalt)." : " – die Zusammenfassung hat sich seitdem geändert."}</p>` : ""}
    <div class="handoff-preview" aria-label="Vorschau der Datei" tabindex="0"><div class="reader">${renderBlocks(x.blocks)}</div></div>
    ${x.error ? h`<div class="banner banner-err mt-4" role="alert">${I("alert")}<div class="bd">${x.error}</div></div>` : ""}`,
  h`${btn("Abbrechen", { act: "dlg-cancel" })}${btn("An SharePoint übergeben", { cls: "btn-primary", icon: "send", act: "ho-go" })}`, "reading");
};
DLG.event = d => {
  const x = d.data, L = d.local; if (L.i === undefined) { L.i = 0; L.take = true; }
  return modal(d, h`<h2 id="dlg-t">Termin zuordnen – ${x.title}</h2><p class="muted">Alle Termine des Aufnahmetags${x.when ? ` (${x.when})` : ""}, der zur Aufnahmezeit passende zuerst. Nur die Teilnehmenden des gewählten Termins werden bei der Sprechererkennung automatisch zugeordnet.</p>`,
    h`<div class="event-list" role="radiogroup" aria-label="Termine">${x.events.map((e, i) => { const s = parseDate(e.start), en = parseDate(e.end); return h`<label class="rcard"><input type="radio" name="ev" value="${i}" data-ev="${i}" ${L.i === i ? raw("checked") : ""}><span class="rt">${e.all_day ? "ganztägig" : `${s ? `${p2(s.getDate())}.${p2(s.getMonth() + 1)}. ${hm(s)}` : ""}–${en ? hm(en) : ""}`} · ${e.subject}${e.online ? " · online" : ""}</span><span>${e.note ? h`<span class="chip">${e.note}</span>` : ""}</span><span class="rd">Organisation: ${e.organizer || "–"} · ${e.people.length} Teilnehmende${e.people.length ? `: ${e.people.slice(0, 5).join(", ")}${e.people.length > 5 ? ` und ${e.people.length - 5} weitere` : ""}` : ""}</span></label>`; })}</div>
    <label class="check mt-4"><input type="checkbox" id="ev-take" ${L.take ? raw("checked") : ""}><span class="t">Betreff als Titel übernehmen</span></label>`,
    h`${btn("Abbrechen", { act: "dlg-cancel" })}${btn("Zuordnen", { cls: "btn-primary", act: "ev-ok" })}`);
};
DLG.retranscribe = d => {
  const x = d.data, L = d.local; if (!L.lang) { L.lang = "auto"; L.preset = ""; }
  return modal(d, h`<h2 id="dlg-t">Neu transkribieren – ${x.title}</h2>`, h`<p class="mb-4">Es entsteht eine neue Transkript-Version; die bisherige bleibt erhalten. Danach wird auch die Zusammenfassung neu erstellt. Manuelle Sprecherzuordnungen bleiben gültig.</p>
    <div class="field"><span class="label" id="rt-l">Sprache des Gesprächs</span><div class="radio-cards" role="radiogroup" aria-labelledby="rt-l">${[["auto", "Automatisch erkennen"], ["de", "Deutsch"], ["en", "Englisch"]].map(([v, t]) => h`<label class="rcard"><input type="radio" name="rt" value="${v}" data-lang="${v}" ${L.lang === v ? raw("checked") : ""}><span class="rt">${t}</span><span></span></label>`)}</div></div>
    <div class="field"><label class="label" for="rt-p">Preset (optional)</label><select class="select" id="rt-p"><option value="">Aktuelle Einstellungen</option>${x.presets.map(p => h`<option value="${p}" ${L.preset === p ? raw("selected") : ""}>${p}</option>`)}</select></div>`,
    h`${btn("Abbrechen", { act: "dlg-cancel" })}${btn("Neu transkribieren", { cls: "btn-primary", act: "rt-ok" })}`);
};
DLG.update = d => {
  const x = d.data, rel = x.release;
  return modal(d, h`<h2 id="dlg-t">Updates</h2><p class="muted">Installiert: ${x.installed}</p>`, h`
    ${x.error ? h`<div class="banner banner-err" role="alert">${I("alert")}<div class="bd">${x.error}</div></div>` : h`<p>${x.message}</p>`}
    ${rel ? h`<p class="caption muted mt-2">Freigegeben: ${rel.version} (${rel.commit})${rel.date ? ` vom ${rel.date}` : ""}</p>${rel.notes.length ? h`<ul class="list-plain mt-2">${rel.notes.map(n => h`<li>${I("check", "ico-sm")}<span>${n}</span></li>`)}</ul>` : ""}` : ""}
    ${x.available && x.requirements_changed ? h`<p class="help mt-2">Dieses Update installiert auch geänderte Python-Pakete (dauert einige Minuten; die bisherige Umgebung wird für eine Rückkehr gesichert).</p>` : ""}
    ${x.busy && x.available ? h`<div class="banner banner-warn mt-4">${I("info")}<div class="bd">${x.busy} Aktualisieren ist erst danach möglich.</div></div>` : ""}
    <p class="help mt-4">Für das Update wird CallRecorder beendet und danach automatisch neu gestartet. Schlägt etwas fehl, wird der bisherige Stand wiederhergestellt. Einstellungen, Stimmprofile und Aufnahmen bleiben unverändert.</p>`,
  h`${x.can_restore ? btn("Vorherige Version wiederherstellen …", { cls: "btn-subtle left", act: "up", attrs: data({ c: "restore" }) }) : ""}${btn("Schließen", { act: "dlg-cancel" })}${x.available ? btn(x.busy ? "Nach Abschluss aktualisieren" : "Jetzt aktualisieren", { cls: "btn-primary", act: "up", attrs: data({ c: x.busy ? "after" : "now" }) }) : ""}`);
};
// local dialogs (no Python request behind them)
DLG.rename = d => modal(d, h`<h2 id="dlg-t">Umbenennen</h2>`, h`<div class="field"><label class="label" for="rn">Angezeigter Titel</label><input class="input" id="rn" value="${d.data.title}"><p class="help">Der Ordnername bleibt gleich, damit SharePoint und der Assistent die Aufnahme wiedererkennen.</p></div>${d.local.error ? h`<div class="banner banner-err error-line" role="alert">${I("alert")}<div class="bd">${d.local.error}</div></div>` : ""}`,
  h`${btn("Abbrechen", { act: "dlg-cancel" })}${btn("Umbenennen", { cls: "btn-primary", act: "rn-ok" })}`);
DLG.delete = d => {
  const x = d.data;
  return modal(d, h`<h2 id="dlg-t">${x.title}</h2>`, h`${x.reason ? h`<div class="banner banner-warn mb-4">${I("warning")}<div class="bd">${x.reason} Stattdessen endgültig löschen? Das lässt sich nicht rückgängig machen.</div></div>` : ""}<p class="pre">${x.message}</p>`,
  h`${btn("Abbrechen", { act: "dlg-cancel" })}${btn(x.reason ? "Endgültig löschen" : x.confirm, { cls: "btn-primary btn-danger", icon: "trash", act: "del-ok" })}`);
};
DLG.textfile = d => modal(d, h`<h2 id="dlg-t">${d.data.title}</h2><p class="muted">${d.data.hint}</p>`, h`<label class="sr-only" for="tf">${d.data.title}</label><textarea class="textarea code" id="tf" spellcheck="false">${d.data.text}</textarea><p class="caption muted mt-2 anywhere">${d.data.path}</p>`,
  h`${btn("Abbrechen", { act: "dlg-cancel" })}${btn("Speichern", { cls: "btn-primary", act: "tf-ok" })}`, "reading");
DLG.ask = d => modal(d, h`<h2 id="dlg-t">${d.data.title}</h2>`, h`<p class="pre">${d.data.message}</p>${d.data.input !== undefined ? h`<input class="input mt-4" id="ask-in" value="${d.data.input}" aria-label="${d.data.title}">` : ""}`,
  h`${btn(d.data.cancel || "Abbrechen", { act: "dlg-cancel" })}${btn(d.data.confirm || "OK", { cls: `btn-primary ${d.data.danger ? "btn-danger" : ""}`, act: "ask-ok" })}`);

let lastFocus = null;
function topDialog() { return S.dialogs[S.dialogs.length - 1] || null; }
function renderDialog() {
  const d = topDialog(), ov = $("#overlay");
  if (!d) { ov.innerHTML = ""; if (lastFocus && document.body.contains(lastFocus)) lastFocus.focus(); lastFocus = null; return; }
  if (!lastFocus) lastFocus = document.activeElement;
  const prevScroll = ($(".modal-body", ov) || {}).scrollTop || 0;
  const same = ov.dataset.id === d.id;
  ov.innerHTML = String(DLG[d.kind] ? DLG[d.kind](d) : DLG.info({ id: d.id, data: { title: "Hinweis", message: "Unbekannter Dialog." } }));
  ov.dataset.id = d.id;
  applyDyn(ov);
  const body = $(".modal-body", ov); if (body && same) body.scrollTop = prevScroll;
  if (!same) {
    const m = $(".modal", ov);
    // never focus an action button by itself: the window may come up while the user is typing
    // elsewhere (e.g. after stopping with the hotkey), and Enter/Space would then answer the dialog
    // ("Vollständig verarbeiten" incl. hand-over, "Aufnehmen"). Without a text field the dialog
    // itself gets the focus; Tab reaches the buttons.
    const f = $(".modal-body input:not([disabled]):not([type=checkbox]):not([type=radio]), .modal-body textarea", m);
    if (m && !f) m.setAttribute("tabindex", "-1");
    (f || m).focus();
  }
}
function openDialog(d) { d.local = d.local || {}; S.dialogs = S.dialogs.filter(x => x.id !== d.id); S.dialogs.push(d); renderDialog(); }
function closeDialog(id) { S.dialogs = S.dialogs.filter(x => x.id !== id); renderDialog(); stopSample(); }
async function answer(d, result) {
  if (d.remote) await call("answer", d.id, result);
  closeDialog(d.id);
  if (d.resolve) d.resolve(result);
}
function local(kind, dataObj) { return new Promise(resolve => openDialog({ id: `l${Date.now()}${Math.random()}`.replace(".", ""), kind, data: dataObj, local: {}, resolve })); }
const confirmLocal = (title, message, confirm, opts = {}) => local("ask", { title, message, confirm, ...opts }).then(r => r && r.ok);

/* =====================================================================
   13. RENDER + LOAD
   ===================================================================== */
function renderMain() {
  const main = $("#main");
  const libMode = S.route === "library";
  $("#mainwrap").classList.toggle("lib-mode", libMode);
  const fn = { home: screenHome, library: screenLibrary, setup: screenSetup, settings: screenSettings, prompts: screenPrompts }[S.route] || screenHome;
  // keep scroll positions of the panes across live updates
  const keep = { main: main.scrollTop, list: ($(".lib-scroll") || {}).scrollTop, det: ($(".lib-detail") || {}).scrollTop };
  const focusId = document.activeElement && document.activeElement.id;
  const sel = document.activeElement && (document.activeElement.selectionStart ?? null);
  main.innerHTML = String(fn());
  applyDyn(main);
  main.scrollTop = keep.main;
  if ($(".lib-scroll") && keep.list) $(".lib-scroll").scrollTop = keep.list;
  if ($(".lib-detail") && keep.det) $(".lib-detail").scrollTop = keep.det;
  if (focusId && $(`#${CSS.escape(focusId)}`)) { const el = $(`#${CSS.escape(focusId)}`); el.focus(); if (sel !== null && el.setSelectionRange) try { el.setSelectionRange(sel, sel); } catch (e) { /* not a text field */ } }
  observeMore();
}
function render() { renderTop(); renderRail(); renderRecbar(); renderMain(); applyDyn(document); }
const TITLES = { home: "Übersicht", library: "Aufnahmen", setup: "Einrichtung", settings: "Einstellungen", prompts: "Anweisungen" };
async function go(route, opts = {}) {
  S.route = TITLES[route] ? route : "home";
  S.menu = "";
  if (opts.page) S.setPage = opts.page;
  document.title = `${TITLES[S.route]} – CallRecorder`;
  render();
  const h1 = $("#main [data-focus]"); if (h1 && !opts.noFocus) h1.focus({ preventScroll: true });
  call("save_window_state", { route: S.route, filter: S.filter });
  await load(S.route);
}
async function load(route) {
  if (route === "home") {
    const [home, cal] = await Promise.all([call("get_home"), S.calendar ? null : call("get_calendar", false), loadRetention()]);
    if (home && !home.error) S.home = home;
    if (cal && !cal.error) S.calendar = cal;
    if (S.route === "home") { renderMain(); renderRail(); }
  } else if (route === "library") {
    await Promise.all([loadList(), loadRetention()]);
    if (!S.selected && S.list && S.list.items.length && !MQ_SINGLE.matches) await select(S.list.items[0].id, false);
  } else if (route === "setup") {
    if (!S.checks) { const r = await call("run_checks", false); if (r && !r.error) S.checks = r; if (S.route === "setup") renderMain(); }
  } else if (route === "settings") {
    await loadSettings();
  } else if (route === "prompts") {
    const r = await act("prompts", "get");
    if (r && r.prompts) { S.prompts = { ...r, tab: "system", texts: Object.fromEntries(r.prompts.map(p => [p.key, p.text])) }; }
    if (!S.list) await loadList();
    if (S.route === "prompts") renderMain();
  }
}
const MQ_SINGLE = matchMedia("(max-width: 999px)");
async function loadList() {
  const r = await call("list_recordings", S.filter, S.query);
  if (r && !r.error) {
    S.list = r;
    // a selection only ever holds what the list shows (a filter/search change or a deletion elsewhere)
    const shown = new Set((r.items || []).map(x => x.id)); S.multi = S.multi.filter(id => shown.has(id));
  }
  if (S.route === "library") renderMain();
}
async function select(id, focus = true) {
  S.selected = id; S.menu = ""; S.textLimit = 300; S.version_sel = { summary: "", transcript: "" };
  S.mobileDetail = true;
  S.detail = { loading: true, ...(S.detail && S.detail.id === id ? S.detail : {}) };
  const d = await call("get_recording", id);
  if (S.selected !== id) return;
  S.detail = d && !d.error ? d : { error: (d && d.error) || "Die Aufnahme ist nicht lesbar." };
  if (S.route === "library") renderMain();
  if (focus) { const t = $("#det-title"); if (t) t.setAttribute("tabindex", "-1"); }
  await loadText();
}
async function loadText() {
  const d = S.detail; if (!d || d.error || !d.id) return;
  const kind = S.tab, versions = (d.versions || {})[kind] || [];
  if (!versions.length) { S.text = null; if (S.route === "library") renderMain(); return; }
  const file = S.version_sel[kind] || versions[0].file;
  const r = await call("get_text", d.id, kind, file);
  if (S.selected !== d.id || S.tab !== kind) return;
  S.text = r && !r.error ? { ...r, kind, id: d.id } : { kind, id: d.id, error: (r && r.error) || "Datei nicht lesbar." };
  if (S.route === "library") renderMain();
}
async function refreshDetail() {
  if (S.route !== "library") return;
  await loadList();
  if (S.selected) {
    const d = await call("get_recording", S.selected);
    if (d && !d.error) { S.detail = d; renderMain(); await loadText(); }
    else if (d && d.error) { S.selected = ""; S.detail = null; renderMain(); }
  }
}
async function loadSettings() {
  const r = await call("get_settings");
  if (r && !r.error) { S.settings = { ...r, whisper: null, llm: null }; }
  if (S.route === "settings") renderMain();
  if (S.setPage === "transcription") loadWhisper(false);
  if (S.setPage === "profiles") loadProfiles();
  if (S.setPage === "retention") { await loadRetention(); if (S.route === "settings") renderMain(); }
  if (S.setPage === "updates" || S.setPage === "about") { S.about = await call("about"); if (S.route === "settings") renderMain(); }
}
async function loadWhisper(gpu) { const r = await call("whisper_models", gpu ? "gpu" : "list", "", gpu); if (r && !r.error && S.settings) { S.settings.whisper = r; if (S.route === "settings") renderMain(); } }
async function loadProfiles(action = "list", pid = "", index = null) { const r = await act("profiles", action, pid, index); if (r && r.profiles) { S.profiles = r; if (S.route === "settings") renderMain(); } }
let moreObserver = null;
function observeMore() {
  if (moreObserver) moreObserver.disconnect();
  const b = $("#more-text"); if (!b || !("IntersectionObserver" in window)) return;
  moreObserver = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) { S.textLimit += 300; renderMain(); } }, { root: $(".lib-detail"), rootMargin: "600px" });
  moreObserver.observe(b);
}

/* =====================================================================
   14. LIVE UPDATES (events from Python)
   ===================================================================== */
function liveUpdate() {
  const r = S.state.recording || {}, v = (S.state.autostop || {}).view;
  $$("[data-live=clock]").forEach(el => { el.textContent = clock(r.elapsed); });
  const lv = r.levels || {};
  for (const k of ["mic", "remote"]) {
    const pct = Math.round(Math.max(0, Math.min(1, lv[k] || 0)) * 100);
    $$(`[data-live=lv-${k}]`).forEach(el => { el.style.width = `${pct}%`; el.parentElement.setAttribute("aria-valuenow", String(pct)); });
    $$(`[data-live=db-${k}]`).forEach(el => { el.textContent = pct ? `${pct} %` : "–"; });
  }
  if (v) {
    $$("[data-live=cd]").forEach(el => { el.textContent = String(v.seconds_left); });
    $$("[data-live=cd-head]").forEach(el => { el.textContent = v.headline; });
    $$("[data-live=ring]").forEach(el => { el.style.strokeDashoffset = String(175.9 * (1 - Math.min(1, v.seconds_left / 30))); });
  }
}
function shape(st) {
  const q = st.queue || {}, a = st.autostop || {};
  return JSON.stringify([st.recording && st.recording.on, st.recording && st.recording.title, !!a.view, a.view && a.view.kind, a.message, a.status, q.current && [q.current.id, q.current.step, q.current.cancel_requested, q.current.fraction < 0], (q.pending || []).length, st.setup && st.setup.open]);
}
HANDLERS.state = st => {
  const before = shape(S.state);
  S.state = st;
  if (shape(st) !== before) { renderTop(); renderRail(); renderRecbar(); if (S.route === "home" && !S.dialogs.some(d => d.kind === "start")) renderMain(); else if (S.route === "library" && S.detail && st.queue) { /* detail refresh comes with recording_changed */ } }
  else {
    liveUpdate();
    const cur = (st.queue || {}).current;
    if (cur && cur.fraction >= 0) $$(".work .bar > i").forEach(el => { el.style.width = `${Math.round(cur.fraction * 100)}%`; });
  }
};
HANDLERS.recording = async () => { const st = await call("get_state"); if (st && !st.error) HANDLERS.state(st); if (S.route === "home") load("home"); };
HANDLERS.status = () => {};
HANDLERS.notify = n => {
  if (n.level === "error") { S.notices = [{ id: Date.now(), level: "err", title: n.title, text: n.message, hint: n.hint }, ...S.notices].slice(0, 3); renderNotices(); }
  else snack(`${n.title}${n.message ? ` – ${n.message}` : ""}`);
};
HANDLERS.dialog = d => openDialog({ ...d, remote: true, local: {} });
HANDLERS.dialog_close = d => { if (S.dialogs.some(x => x.id === d.id)) closeDialog(d.id); };
HANDLERS.navigate = p => go(p.route, { page: p.page });
HANDLERS.recording_changed = () => { S.home = S.home; if (S.route === "home") load("home"); else if (S.route === "library") refreshDetail(); };
HANDLERS.job_done = HANDLERS.recording_changed;
HANDLERS.config_changed = async () => { S.calendar = null; if (S.route === "home") load("home"); };
HANDLERS.checks = c => { S.checks = c; S.checksBusy = ""; S.state.setup = { ready: c.ready, text: c.summary, open: c.open }; renderRail(); if (S.route === "setup") renderMain(); };
HANDLERS.checks_busy = c => { S.checksBusy = c.text; if (S.route === "setup") renderMain(); };
HANDLERS.download = c => { S.downloads = c.text; if (S.route === "setup") renderMain(); };
HANDLERS.pull = p => { S.pull = p; if (p.done) { snack(p.status, !!p.error); } if (S.route === "settings") renderMain(); };
HANDLERS.toast = () => {};

/* =====================================================================
   15. ACTIONS (event delegation)
   ===================================================================== */
const A = {};
A.go = el => go(el.dataset.to);
A.hide = () => call("hide_window");
A.help = () => go("settings", { page: "about" });
A.start = el => { S.menu = ""; const c = el.dataset.choice; act("start_recording", c === "manual" ? "manual" : c !== undefined ? Number(c) : null); };
A.stop = () => act("stop_recording");
A.stopnow = () => act("autostop", "stop");
A.keep = () => act("autostop", "keep");
A["as-dismiss"] = () => act("autostop", "dismiss");
A.menu = el => { S.menu = S.menu === el.dataset.menu ? "" : el.dataset.menu; renderMain(); const m = $(".menu [role=menuitem]:not([disabled])"); if (m) m.focus(); };
A.pick = el => { S.menu = ""; S.calPick = Number(el.dataset.i); renderMain(); };
A["refresh-cal"] = async () => { const r = await act("get_calendar", true); if (r && !r.error) { S.calendar = r; renderMain(); } };
A.open = el => { S.route = "library"; S.selected = ""; go("library").then(() => select(el.dataset.id)); };
A.select = (el, e) => {
  S.ctx = null;
  const id = el.dataset.id;
  if (e && (e.ctrlKey || e.metaKey || e.shiftKey)) {  // multi-select ("Auswahl löschen")
    const ids = ((S.list || {}).items || []).map(r => r.id);
    let m = S.multi.length ? [...S.multi] : (S.selected ? [S.selected] : []);
    if (e.shiftKey && S.selected) { const a = ids.indexOf(S.selected), b = ids.indexOf(id); m = ids.slice(Math.min(a, b), Math.max(a, b) + 1); }
    else m = m.includes(id) ? m.filter(x => x !== id) : [...m, id];
    S.multi = m; renderMain(); return;
  }
  S.multi = [];
  select(id);
};
A["multi-clear"] = () => { S.multi = []; renderMain(); };
A["del-multi"] = () => deleteFlow([...S.multi], "recording");
A["pin-multi"] = async () => { await call("pin", [...S.multi], true); S.multi = []; snack("Behalten: wird nie automatisch gelöscht."); loadRetention(); refreshDetail(); };
A.pin = async el => { await call("pin", el.dataset.id, el.dataset.on === "1"); snack("Behalten: wird nie automatisch gelöscht."); await loadRetention(); load(S.route); };
A["ret-keep"] = async () => { const R = S.retention; if (!R) return; await call("pin", R.next24, true); snack("Behalten."); await loadRetention(); load(S.route); };
A["ret-now"] = async () => {
  const R = S.retention, ids = R ? (R.next24_now || []) : []; if (!ids.length) return;
  if (!await confirmLocal("Jetzt löschen?", `${ids.length} Aufnahme(n): ${R.scope}${R.permanent ? " – endgültig, nicht rückgängig zu machen" : " – in den Papierkorb"}.`, "Jetzt löschen", { danger: true })) return;
  const r = await call("retention_now", ids); if (r && r.error) showError(r.error); else snack("Gelöscht.");
  await loadRetention(); load(S.route);
};
A.ctx = async el => {
  const c = S.ctx; S.ctx = null; renderMain(); if (!c) return;
  const r = ((S.list || {}).items || []).find(x => x.id === c.id); if (!r) return;
  if (el.dataset.key === "pin") { await call("pin", c.id, !r.pinned); await loadRetention(); loadList(); return; }
  deleteFlow(S.multi.length > 1 && S.multi.includes(c.id) ? [...S.multi] : [c.id], el.dataset.key);
};
document.addEventListener("contextmenu", e => {
  const item = e.target.closest && e.target.closest(".rec-item");
  if (!item) { if (S.ctx) { S.ctx = null; renderMain(); } return; }
  e.preventDefault(); S.ctx = { id: item.dataset.id, x: e.clientX, y: e.clientY }; renderMain();
  const m = $(".menu.ctx"); if (m) { m.style.left = `${Math.min(e.clientX, innerWidth - 300)}px`; m.style.top = `${Math.min(e.clientY, innerHeight - 200)}px`; const b = $("button", m); if (b) b.focus(); }
});
A.back = () => { S.mobileDetail = false; renderMain(); };
A.filter = el => { S.filter = el.dataset.f; loadList(); call("save_window_state", { route: S.route, filter: S.filter }); };
A.clearq = () => { S.query = ""; S.filter = "all"; loadList(); };
A.tab = el => { S.tab = el.dataset.tab; S.textLimit = 300; renderMain(); loadText(); };
A["more-text"] = () => { S.textLimit += 300; renderMain(); };
A["rec-action"] = el => { S.menu = ""; act("recording_action", el.dataset.id, el.dataset.key); };
A["open-root"] = () => act("open_folder");
A["cancel-job"] = el => act("cancel_processing", el.dataset.job ? Number(el.dataset.job) : null);
A["cancel-pending"] = async () => { if (await confirmLocal("Wartende Verarbeitungen entfernen?", "Diese Verarbeitungen werden nicht mehr ausgeführt. Die Aufnahmen bleiben erhalten.", "Entfernen")) act("cancel_pending"); };
A.more = async el => {
  const d = S.detail; const key = el.dataset.key; S.menu = ""; renderMain();
  if (!d) return;
  if (key === "rename") { openDialog({ id: `rn${Date.now()}`, kind: "rename", data: { title: d.title, id: d.id }, local: {} }); return; }
  if (key === "delete") return deleteFlow([d.id], "recording");
  if (key === "delete_audio") return deleteFlow([d.id], "audio");
  if (key === "delete_material") return deleteFlow([d.id], "audio_transcript");
  if (key === "pin") { await call("pin", d.id, !d.pinned); snack(d.pinned ? "Wird wieder automatisch gelöscht, wenn sie alt genug ist." : "Behalten: wird nie automatisch gelöscht."); refreshDetail(); return; }
  if (key === "reprocess") {
    if (await confirmLocal("Komplett neu verarbeiten?", `„${d.title}“ komplett neu verarbeiten?\n\nAudio, Transkription, Sprechertrennung und Zusammenfassung werden neu berechnet. Es entstehen neue Versionen, die bisherigen bleiben erhalten; manuelle Sprecherzuordnungen werden übernommen, soweit die Stimmen wiedererkannt werden.`, "Neu verarbeiten")) act("recording_action", d.id, "reprocess");
    return;
  }
  act("recording_action", d.id, key);
};
async function deleteFlow(ids, scope = "recording") {
  const info = await act("delete_info", ids, scope); if (!info || info.error) return;
  if (info.busy && info.busy.length) { showError(`Wird gerade aufgenommen oder verarbeitet – Löschen ist erst danach möglich: ${info.busy.join(", ")}`); return; }
  openDialog({ id: `del${Date.now()}`, kind: "delete", data: { ...info, id: ids }, local: {} });
}
A.setup = async el => {
  const r = await act("setup_action", el.dataset.key);
  if (r && r.navigate) go(r.navigate, { page: r.page });
  else if (el.dataset.key === "recheck") { S.checksBusy = "Einrichtung wird geprüft …"; renderMain(); }
};
A.setpage = el => { S.setPage = el.dataset.page; renderMain(); $("#main").scrollTop = 0; loadSettings(); };
A["pick-folder"] = async el => { const r = await act("pick_folder"); if (r && r.path) { S.patch[el.dataset.field] = r.path; renderMain(); } };
A.discard = () => { S.patch = {}; renderMain(); };
A["save-settings"] = () => saveSettings([]);
async function saveSettings(confirmed) {
  const patch = { ...S.patch }; const auto = patch.__autostart; delete patch.__autostart;
  const r = await call("save_settings", patch, confirmed, typeof auto === "boolean" ? auto : null);
  if (r && r.confirm) { if (await confirmLocal(r.confirm.title, r.confirm.message, r.confirm.confirm, { cancel: r.confirm.cancel, danger: true })) return saveSettings([...confirmed, r.confirm.kind]); return; }
  if (r && r.error) { showError(r.error); return; }
  if (patch["ui.theme"]) { S.theme = patch["ui.theme"]; applyTheme(); }
  S.patch = {}; snack(r && r.warning ? r.warning : "Einstellungen gespeichert.");
  await loadSettings();
}
A["save-preset"] = async () => {
  const r = await local("ask", { title: "Als Preset speichern", message: "Ein Preset enthält nur Transkriptions-, Sprecher-, Darstellungs- und Zusammenfassungs-Einstellungen (nie Anbieter-Freigaben).", input: "", confirm: "Speichern" });
  if (r && r.ok && r.value) { const s = await act("save_preset", r.value, Object.fromEntries(Object.entries(S.patch).filter(([k]) => k !== "__autostart"))); if (s && s.ok) { snack(`Preset gespeichert: ${s.path}`); loadSettings(); } }
};
A.textfile = async el => {
  const kind = el.dataset.kind; const r = await act("text_file", kind); if (!r || r.error) return;
  openDialog({ id: `tf${Date.now()}`, kind: "textfile", data: { ...r, kind, title: kind === "glossary" ? "Glossar" : "Korrekturliste", hint: kind === "glossary" ? "Ein Begriff pro Zeile: Namen, Firmen, Produkte, Fachbegriffe." : "Eine Regel pro Zeile: „falsch => richtig“ oder „re: Ausdruck => Ersatz“." }, local: {} });
};
A.whisper = () => loadWhisper(true);
A["whisper-del"] = async el => { if (await confirmLocal("Modell löschen?", `${el.dataset.name} (${el.dataset.size}) löschen? Wird es noch verwendet, lädt CallRecorder es bei der nächsten Transkription erneut.`, "Löschen", { danger: true })) { const r = await act("whisper_models", "remove", el.dataset.name, false); if (r && !r.error) { S.settings.whisper = r; renderMain(); } } };
const settingsPatch = () => Object.fromEntries(Object.entries(S.patch).filter(([k]) => k !== "__autostart"));
A["llm-list"] = async () => { const r = await act("llm_models", settingsPatch()); if (r && !r.error) { S.settings.llm = { ...r, text: `${r.version ? `Ollama-Version ${r.version}. ` : ""}${r.models.length} Modell(e) installiert: ${r.models.join(", ") || "–"}` }; renderMain(); } };
A["llm-pull"] = async () => {
  const f = (S.settings.pages.find(p => p.key === "summary") || { fields: [] }).fields.find(x => x.id === "summary.model");
  const model = f ? fieldValue(f) : "";
  if (await confirmLocal("Modell laden?", `Modell „${model}“ jetzt herunterladen bzw. aktualisieren? (mehrere GB möglich)`, "Laden")) { S.pull = { status: "Download startet …", fraction: null }; renderMain(); act("pull_llm_model", settingsPatch(), model); }
};
A["key-save"] = async () => { const k = $("#apikey").value; const r = await act("api_key", "save", k); if (r && r.ok) { S.settings.apikey = r; S.settings.keyTest = "Gespeichert (Windows-DPAPI-verschlüsselt, nicht in config.toml)."; renderMain(); } };
A["key-delete"] = async () => { if (await confirmLocal("Schlüssel löschen?", "Der gespeicherte API-Schlüssel wird gelöscht.", "Löschen", { danger: true })) { const r = await act("api_key", "delete"); if (r && r.ok) { S.settings.apikey = r; S.settings.keyTest = "Gespeicherter Schlüssel gelöscht."; renderMain(); } } };
A["key-test"] = async () => { S.settings.keyTest = "Verbindung zu api.anthropic.com wird getestet … (sendet nur „Antworte nur mit: OK“, höchstens ca. 20 s)"; renderMain(); const typed = ($("#apikey") || {}).value || ""; const r = await call("test_anthropic", settingsPatch(), typed); S.settings.keyTest = r && r.error ? r.error : (r && r.text) || ""; renderMain(); };
A.prof = async el => {
  const a = el.dataset.a, pid = el.dataset.pid || "";
  const Q = { reset: ["Nur Bestätigtes behalten?", "Nur behalten, was selbst zugeordnet oder bestätigt wurde? Automatisch Gelerntes und Einträge früherer Versionen werden entfernt.", "Behalten"],
    delete: ["Stimmprofil löschen?", `Stimmprofil von ${el.dataset.name || ""} endgültig löschen?`, "Löschen"], delete_all: ["Alle Stimmprofile löschen?", "ALLE Stimmprofile endgültig löschen?", "Alle löschen"],
    remove_origin: ["Eintrag entfernen?", "Diesen gelernten Eintrag aus dem Profil entfernen?", "Entfernen"] }[a];
  if (Q && !(await confirmLocal(Q[0], Q[1], Q[2], { danger: a !== "reset" }))) return;
  loadProfiles(a, pid, el.dataset.i !== undefined ? Number(el.dataset.i) : null);
};
A.updates = () => act("check_updates");
A.restore = () => act("restore_previous_version");
A["open-path"] = el => act("open_path", el.dataset.what);
A["to-classic"] = async () => {
  if (await confirmLocal("Zum klassischen Fenster wechseln?", "CallRecorder startet dafür neu (eine laufende Aufnahme wird vorher beendet). Zurück zur neuen Oberfläche: Einstellungen › Allgemein › Oberfläche, oder callrec tray --ui web.", "Wechseln und neu starten")) act("switch_frontend", "tk", true);
};
A.quit = async () => { if (await confirmLocal("CallRecorder beenden?", "CallRecorder wird ganz beendet, auch im Infobereich. Eine laufende Aufnahme wird vorher beendet und beim nächsten Start verarbeitet.", "Beenden")) call("quit_app"); };
A.ptab = el => { savePromptText(); S.prompts.tab = el.dataset.k; S.prompts.msg = ""; renderMain(); };
function savePromptText() { const t = $("#ptext"); if (t && S.prompts) S.prompts.texts[S.prompts.tab] = t.value; }
async function promptSave(action, force = false) {
  savePromptText();
  const rec = action === "test" ? (($("#ptest") || {}).value || "") : "";
  if (action === "test" && !rec) { showError("Keine Aufnahme mit Transkript zum Testen vorhanden."); return; }
  const r = await call("prompts", action, S.prompts.texts, force, rec);
  if (r && r.problems && r.problems.length && !force) {
    S.prompts.problems = r.problems; renderMain();
    if (await confirmLocal("Vorlage prüfen", `${r.problems.join("\n")}\n\nTrotzdem speichern?`, "Trotzdem speichern")) return promptSave(action, true);
    return;
  }
  if (r && r.error) { showError(r.error); return; }
  S.prompts.problems = []; S.prompts.msg = r.text; renderMain(); snack(r.text);
}
A["prompt-save"] = () => promptSave("save");
A["prompt-test"] = () => promptSave("test");
A["prompt-default"] = async () => {
  const cur = S.prompts.prompts.find(p => p.key === S.prompts.tab);
  if (await confirmLocal("Standard wiederherstellen?", "Diese Vorlage durch die Standardvorlage ersetzen? Erst „Speichern“ übernimmt sie.", "Ersetzen")) { S.prompts.texts[cur.key] = cur.default; renderMain(); }
};
A["snack-x"] = () => { $("#snackhost").innerHTML = ""; };
A["notice-x"] = el => { S.notices = S.notices.filter(n => String(n.id) !== el.dataset.id); renderNotices(); };
A.hint = el => {
  const k = el.dataset.hint;
  if (k.startsWith("settings:")) { const map = { Zusammenfassung: "summary", Allgemein: "general", SharePoint: "sharepoint" }; go("settings", { page: map[k.split(":")[1]] || "general" }); return; }
  if (k === "open_log") act("open_path", "log"); else if (k === "readme") act("open_path", "readme"); else if (k === "login") act("setup_action", "login");
  else if (S.selected && ["resummarize", "reprocess", "retranscribe", "review", "handoff"].includes(k)) act("recording_action", S.selected, k);
};
// dialog actions
const curDlg = el => S.dialogs.find(d => d.id === (el.closest("[data-dialog]") || {}).dataset?.dialog) || topDialog();
A.scrim = (el, e) => { if (e.target === el) { const d = topDialog(); if (d && !["start", "review", "after_recording"].includes(d.kind)) cancelDialog(d); } };
A["dlg-close"] = el => cancelDialog(curDlg(el));
A["dlg-cancel"] = el => cancelDialog(curDlg(el));
function cancelDialog(d) {
  if (!d) return;
  if (d.kind === "start") return answer(d, { action: "cancel" });
  if (d.kind === "after_recording") return answer(d, { choice: "later" });
  if (d.kind === "record_prompt") return answer(d, { choice: "dismiss" });
  if (d.kind === "review") return answer(d, { action: "cancel" });
  return answer(d, null);
}
A["st-go"] = el => {
  const d = curDlg(el), ask = d.data.mode === "ask";
  const checked = ask ? false : !!($("#st-check") || {}).checked;
  if (!ask && !checked) return;
  answer(d, { action: "start", title: $("#st-title").value, participants_text: $("#st-people").value, checked });
};
A["import-ics"] = async el => {
  const d = curDlg(el); const r = await act("import_ics", d.id);
  if (r && !r.error && !r.cancelled) { $("#st-title").value = r.title || $("#st-title").value; $("#st-people").value = r.participants_text; $("#st-info").textContent = r.info; d.data = { ...d.data, title: $("#st-title").value, participants_text: r.participants_text, info: r.info }; }
};
A.rp = el => answer(curDlg(el), { choice: el.dataset.c });
A.pc = el => { const d = curDlg(el); answer(d, el.dataset.c === "full" ? { choice: "full", plan: d.local.plan } : { choice: el.dataset.c }); };
A["pc-discard"] = el => { const d = curDlg(el); d.local.confirmDiscard = true; renderDialog(); };
A["pc-back"] = el => { const d = curDlg(el); d.local.confirmDiscard = false; renderDialog(); };
A.rv = el => {
  const d = curDlg(el), L = d.local, a = el.dataset.a;
  if (a === "apply") answer(d, { action: "apply", assignments: d.data.rows.filter(r => !r.minor || L.sel[r.id]).map(r => ({ id: r.id, name: L.sel[r.id] || "", keep: !!L.keep[r.id] })), resummarize: !!L.resum });
  else answer(d, { action: a });
};
A["rv-frag"] = el => { const d = curDlg(el); d.local.showFragments = !d.local.showFragments; renderDialog(); };
A.sample = async el => {
  const d = topDialog(); if (!d) return;
  const sid = el.dataset.sid, step = el.dataset.step === "1" ? 1 : 0, audio = $("#sample-audio");
  if (d.local.playing === sid && !step) { stopSample(); renderDialog(); return; }
  if (audio) audio.pause();
  const r = await act("play_sample", d.data.id, sid, step);
  if (!r || !r.url || !audio) return;
  d.local.info = { ...(d.local.info || {}), [sid]: r.caption || "" };
  audio.src = r.url; d.local.playing = sid;
  audio.onended = () => { d.local.playing = ""; renderDialog(); };
  renderDialog();
  const a2 = $("#sample-audio"); a2.src = r.url; a2.onended = () => { d.local.playing = ""; renderDialog(); };
  a2.play().catch(e => { d.local.playing = ""; showError(`Wiedergabe nicht möglich: ${e.message}`); renderDialog(); });
};
function stopSample() { const a = $("#sample-audio"); if (a) { a.pause(); } const d = topDialog(); if (d && d.local) d.local.playing = ""; }
A["pp-ok"] = el => answer(curDlg(el), { text: $("#pp").value });
A["cf-ok"] = el => answer(curDlg(el), { ok: true });
A["ch-ok"] = el => answer(curDlg(el), { choice: el.dataset.c });
A["ho-go"] = el => { el.disabled = true; answer(curDlg(el), { action: "go" }); snack("Übergabe läuft …"); };
A["ev-ok"] = el => { const d = curDlg(el); answer(d, { index: d.local.i, take_title: d.local.take }); };
A["rt-ok"] = el => { const d = curDlg(el); answer(d, { ok: true, language: d.local.lang, preset: ($("#rt-p") || {}).value || "" }); };
A.up = el => answer(curDlg(el), { choice: el.dataset.c });
A["rn-ok"] = async el => {
  const d = curDlg(el); const title = $("#rn").value;
  if (!title.trim() || title.trim() === d.data.title) { closeDialog(d.id); return; }
  const r = await call("rename", d.data.id, title);
  if (r && r.error) { d.local.error = r.error; renderDialog(); return; }
  closeDialog(d.id); snack("Umbenannt."); refreshDetail();
};
A["del-ok"] = async el => {
  const d = curDlg(el); el.disabled = true;
  const r = await call("delete", d.data.id, !!d.data.permanent, d.data.scope || "recording");
  if (r && r.confirm && r.confirm.kind === "permanent") { d.data = { ...d.data, permanent: true, reason: r.confirm.message }; renderDialog(); return; }
  closeDialog(d.id);
  if (r && r.error) { showError(r.error); return; }
  if (r && r.errors && r.errors.length) showError(r.errors.join(" "));
  snack(d.data.scope && d.data.scope !== "recording" ? "Gelöscht – die übrigen Teile bleiben." : d.data.permanent ? "Gelöscht." : "In den Papierkorb verschoben.");
  S.multi = [];
  if (!d.data.scope || d.data.scope === "recording") { S.selected = ""; S.detail = null; S.mobileDetail = false; }
  await loadRetention(); refreshDetail(); if (S.route === "home") load("home");
};
A["tf-ok"] = async el => { const d = curDlg(el); const r = await act("text_file", d.data.kind, $("#tf").value); if (r && r.ok) { closeDialog(d.id); snack("Gespeichert."); } };
A["ask-ok"] = el => { const d = curDlg(el); answer(d, { ok: true, value: ($("#ask-in") || {}).value }); };

// no navigation away from the app: a file dropped onto the window would open it in place, middle or
// Ctrl+click would open a second window (Python cancels both as well, see webapp.harden_pywebview)
["dragover", "drop"].forEach(t => document.addEventListener(t, e => e.preventDefault()));
document.addEventListener("auxclick", e => { if (e.target.closest && e.target.closest("a[href]")) e.preventDefault(); });
document.addEventListener("click", e => {
  const a = e.target.closest("a[href]");
  if (a && !a.dataset.act) {
    e.preventDefault();
    const href = a.getAttribute("href") || "";
    if (/^https:\/\//.test(href)) call("open_external", href);
    return;
  }
  const el = e.target.closest("[data-act]");
  if (S.ctx && !e.target.closest(".menu.ctx")) { S.ctx = null; if (!el) renderMain(); }
  if (!el || el.disabled) {
    if (S.menu && !e.target.closest(".menu-wrap")) { S.menu = ""; renderMain(); }
    return;
  }
  if (el.tagName === "A") e.preventDefault();
  const fn = A[el.dataset.act];
  if (S.menu && el.dataset.act !== "menu" && !e.target.closest(".menu")) S.menu = "";
  if (fn) fn(el, e);
});
document.addEventListener("change", e => {
  const t = e.target;
  if (t.id === "ver") { S.version_sel[S.tab] = t.value; loadText(); return; }
  if (t.id === "st-check") { const b = $("#st-go"); if (b) b.disabled = !t.checked; return; }
  if (t.dataset.plan) {
    const d = topDialog(); d.local.plan[t.dataset.plan] = t.checked;
    if (t.dataset.plan === "summary" && !t.checked) d.local.plan.handoff = false;
    renderDialog(); return;
  }
  if (t.dataset.sel) { const d = topDialog(); d.local.sel[t.dataset.sel] = t.value; const o = $(`[data-other="${CSS.escape(t.dataset.sel)}"]`); if (o) o.value = ""; renderDialog(); return; }
  if (t.dataset.keep) { topDialog().local.keep[t.dataset.keep] = t.checked; return; }
  if (t.id === "rv-resum") { topDialog().local.resum = t.checked; return; }
  if (t.dataset.ev !== undefined) { topDialog().local.i = Number(t.dataset.ev); return; }
  if (t.id === "ev-take") { topDialog().local.take = t.checked; return; }
  if (t.dataset.lang) { topDialog().local.lang = t.dataset.lang; return; }
  if (t.id === "rt-p") { topDialog().local.preset = t.value; return; }
  if (t.dataset.field) {
    const f = t.dataset.field;
    let v = t.type === "checkbox" ? t.checked : t.value;
    const field = S.settings && S.settings.pages.flatMap(p => p.fields).find(x => x.id === f);
    if (field && field.kind === "number") v = String(v).replace(",", ".");
    S.patch[f] = v;
    if (f === "ui.theme") { S.theme = v; applyTheme(); }
    // text fields are tracked on "input" already; re-rendering here (on blur) would swallow the click
    // that caused the blur (e.g. on "Speichern")
    if (t.tagName === "SELECT" || t.type === "checkbox" || t.type === "radio") renderMain();
  }
});
let qTimer = 0;
document.addEventListener("input", e => {
  const t = e.target;
  if (t.id === "q" || t.id === "topq") {
    S.query = t.value; clearTimeout(qTimer);
    qTimer = setTimeout(() => { if (S.route !== "library") go("library", { noFocus: true }).then(() => { const q = $("#q"); if (q) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); } }); else loadList(); }, 180);
  }
  if (t.dataset.other) { const d = topDialog(); d.local.sel[t.dataset.other] = t.value.trim(); $$(`input[data-sel="${CSS.escape(t.dataset.other)}"]`).forEach(r => { r.checked = false; }); }
  if (t.dataset.field && t.tagName !== "SELECT" && t.type !== "checkbox" && t.type !== "radio") {
    const field = S.settings && S.settings.pages.flatMap(p => p.fields).find(x => x.id === t.dataset.field);
    S.patch[t.dataset.field] = field && field.kind === "number" ? t.value.replace(",", ".") : t.value;
    const foot = $(".sticky-foot"); if (foot) { $$("[data-act=save-settings],[data-act=discard]", foot).forEach(b => { b.disabled = false; }); }
  }
});

/* keyboard: Esc, focus trap, Ctrl+F, arrows in list/menus, Delete */
document.addEventListener("keydown", e => {
  const d = topDialog();
  if (e.key === "Escape") {
    if (d) { e.preventDefault(); cancelDialog(d); return; }
    if (S.menu) { S.menu = ""; renderMain(); return; }
    if (e.target.id === "q" && S.query) { S.query = ""; loadList(); return; }
  }
  if (d && e.key === "Tab") {
    const m = $("#overlay .modal"); if (!m) return;
    const f = $$('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', m).filter(x => x.offsetParent !== null);
    if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); } else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
    return;
  }
  if (d) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") { e.preventDefault(); const go2 = () => { const q = $("#q"); if (q) { q.focus(); q.select(); } }; if (S.route !== "library") go("library", { noFocus: true }).then(go2); else go2(); return; }
  if (e.key === "F5") { e.preventDefault(); S.calendar = null; S.checks = null; load(S.route); return; }
  const inMenu = e.target.closest && e.target.closest(".menu");
  if (inMenu && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault(); const items = $$("[role=menuitem]:not([disabled])", inMenu); const i = items.indexOf(document.activeElement);
    (items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length] || items[0]).focus(); return;
  }
  const item = e.target.closest && e.target.closest(".rec-item");
  if ((item || e.target.id === "q") && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault(); const items = $$(".rec-item"); if (!items.length) return;
    const i = item ? items.indexOf(item) : -1; const n = items[Math.max(0, Math.min(items.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
    n.focus(); select(n.dataset.id, false).then(() => { const again = $(`.rec-item[data-id="${CSS.escape(n.dataset.id)}"]`); if (again) again.focus(); });
    return;
  }
  if (item && e.key === "Delete") { e.preventDefault(); deleteFlow(S.multi.length > 1 && S.multi.includes(item.dataset.id) ? [...S.multi] : [item.dataset.id], "recording"); return; }
  if (item && e.key === "Enter") { e.preventDefault(); const t = $(".reader-sheet"); if (t) { t.setAttribute("tabindex", "-1"); t.focus(); } }
});

/* =====================================================================
   16. START
   ===================================================================== */
let booted = false;
async function boot() {
  if (booted || !api()) return;
  booted = true;
  let r = await call("ready", "main");
  for (let i = 0; i < 20 && r && r.error; i++) { await new Promise(res => setTimeout(res, 250)); r = await call("ready", "main"); }
  if (r && !r.error) {
    S.version = r.version; S.theme = r.theme || "system"; S.hotkey = r.hotkey; S.consentMode = r.consent_mode;
    if (r.state && !r.state.error) S.state = r.state;
    applyTheme();
    const [route, page] = String(r.route || "home").split("/");
    await go(route || "home", { page: page || undefined });
    for (const d of r.dialogs || []) HANDLERS.dialog(d);
  } else {
    render();
    showError((r && r.error) || "Die Verbindung zum Programm fehlt.");
  }
  setInterval(async () => { // fallback when no state events arrive (e.g. after a sleep)
    if (document.hidden) return;
    const st = await call("get_state"); if (st && !st.error) HANDLERS.state(st);
  }, 5000);
}
applyTheme();
render();
window.addEventListener("pywebviewready", () => { if (window.pywebview) window.pywebview.__ready = true; boot(); });
if (api()) boot();
