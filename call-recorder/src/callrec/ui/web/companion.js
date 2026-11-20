// The two small always-on-top windows: recording indicator and "Call erkannt" prompt.
// Own, minimal js_api (stop, autostop, toast_answer, show_main). They never take the keyboard
// focus (shown with SW_SHOWNOACTIVATE by Python); no button reacts to Enter typed elsewhere.
"use strict";
const esc = s => String(s ?? "").replace(/[&<>"'`]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" }[c]));
const I = n => `<svg class="ico ico-sm" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const clock = s => { s = Math.max(0, Math.floor(s || 0)); const p = n => String(n).padStart(2, "0"); return `${p(Math.floor(s / 3600))}:${p(Math.floor(s % 3600 / 60))}:${p(s % 60)}`; };
const kind = document.body.dataset.window;
const root = document.getElementById("root");
const api = () => (window.pywebview && window.pywebview.api && (window.pywebview._jsApiCallback || window.pywebview.__fake)) ? window.pywebview.api : null;
const call = async (n, ...a) => { const x = api(); if (!x || !x[n]) return null; try { return await x[n](...a); } catch (e) { return null; } };
window.addEventListener("error", e => { const x = api(); if (x && x.log_js_error) x.log_js_error(String(e.message), String(e.filename || ""), e.lineno || 0, String(e.error && e.error.stack || "")).catch(() => {}); });
let S = { state: null, toast: null, shape: "" };

function renderIndicator() {
  const st = S.state || {}, r = st.recording || {}, v = (st.autostop || {}).view;
  const shape = JSON.stringify([r.on, r.title, !!v, v && v.keep_label]);
  if (shape !== S.shape) {
    S.shape = shape;
    root.classList.toggle("cd-on", !!v);
    root.innerHTML = `<div class="line drag"><span class="rec-dot" aria-hidden="true"></span><span class="num strong" id="clk">${clock(r.elapsed)}</span><span class="t ellipsis" title="${esc(r.title)}">${esc(r.title)}</span>
        <button class="btn btn-record btn-sm" data-a="stop" tabindex="-1" aria-label="Aufnahme beenden">${I("stop")}Beenden</button></div>
      ${v ? `<div class="cd"><b id="cdh">${esc(v.headline)}</b><div class="bar"><i id="cdb"></i></div>
        <div class="btn-row"><button class="btn btn-record btn-sm" data-a="stopnow" tabindex="-1">${I("stop")}${esc(v.stop_label || "Jetzt beenden")}</button><button class="btn btn-sm" data-a="keep" tabindex="-1">${esc(v.keep_label || "Weiter aufnehmen")}</button></div></div>` : ""}`;
  }
  const c = document.getElementById("clk"); if (c) c.textContent = clock(r.elapsed);
  if (v) { const hd = document.getElementById("cdh"); if (hd) hd.textContent = v.headline; const b = document.getElementById("cdb"); if (b) b.style.width = `${Math.max(0, Math.min(100, v.seconds_left / 30 * 100))}%`; }
}
function renderToast() {
  const t = S.toast || {};
  const ask = t.mode === "ask";
  root.innerHTML = `<div class="th"><span class="app-tag">${I("mic")}CallRecorder</span><span class="spacer"></span>
      ${ask ? `<button class="btn btn-subtle btn-sm" data-a="details" tabindex="-1">Details …</button>` : ""}
      <button class="icon-btn" data-a="dismiss" tabindex="-1" aria-label="Schließen">${I("x")}</button></div>
    <div class="q" id="tq">${esc(t.question || "Call erkannt – aufnehmen?")}</div>
    ${t.people && t.people.length ? `<div class="caption muted toast-people">${esc(t.people.join(", "))}</div>` : ""}
    <p class="caption muted">${esc(t.reminder || "Vor dem Start kommt die Bestätigung der Einwilligung im Hauptfenster.")}</p>
    <div class="btn-row">${ask
      ? `<button class="btn btn-record" data-a="record" tabindex="-1">${I("rec")}Aufnehmen</button><button class="btn" data-a="dismiss" tabindex="-1">Nicht aufnehmen</button>`
      : `<button class="btn btn-primary" data-a="details" tabindex="-1">Aufnahme vorbereiten …</button><button class="btn" data-a="dismiss" tabindex="-1">Nicht aufnehmen</button>`}</div>`;
}
const render = () => (kind === "toast" ? renderToast() : renderIndicator());
document.addEventListener("click", e => {
  const b = e.target.closest("[data-a]"); if (!b) return;
  const a = b.dataset.a;
  if (kind === "toast") { b.disabled = true; call("toast_answer", a); return; }
  if (a === "stop") call("stop"); else if (a === "stopnow") call("autostop", "stop"); else if (a === "keep") call("autostop", "keep");
});
// the companion windows must not answer anything from the keyboard (focus belongs to Teams)
document.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") e.preventDefault(); }, true);
window.cr = { emit(name, p) { if (name === "state") { S.state = p; render(); } else if (name === "toast") { S.toast = p; render(); } } };
const MQ = matchMedia("(prefers-color-scheme: dark)");
function theme(mode) { const dark = mode === "dark" || (mode !== "light" && MQ.matches); document.documentElement.dataset.theme = dark ? "dark" : "light"; }
let booted = false;
async function boot() {
  if (booted || !api()) return; booted = true;
  const r = await call("ready");
  if (r) { S.state = r.state; S.toast = r.toast; theme(r.theme); }
  render();
  if (kind === "indicator") setInterval(async () => { const st = await call("state"); if (st && !st.error) { S.state = st; render(); } }, 1000);
}
MQ.addEventListener("change", () => theme(document.documentElement.dataset.mode));
window.addEventListener("pywebviewready", boot);
if (api()) boot(); else render();
