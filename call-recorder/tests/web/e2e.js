// End-to-end tests of the real frontend (src/callrec/ui/web) in Chromium against the fake bridge.
// Usage: node e2e.js <base-url> <fixtures.json> <screenshot-dir|-> [scenario,...]
// Prints one JSON line {results: [{name, ok, errors[]}]}; the pytest wrapper asserts on it.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [BASE, FIXFILE, SHOTS, ONLY] = process.argv.slice(2);
const FIX = fs.readFileSync(FIXFILE, "utf8");
const FAKE = path.join(__dirname, "fake_bridge.js");
const results = [];

async function page(browser, { w = 1366, h = 768, zoom = 1, theme = "light", mode = "ask", route = "home", extra = "" } = {}) {
  const ctx = await browser.newContext({ viewport: { width: Math.round(w / zoom), height: Math.round(h / zoom) }, deviceScaleFactor: zoom, colorScheme: theme, reducedMotion: "reduce" });
  const p = await ctx.newPage();
  p.__errors = [];
  p.on("pageerror", e => p.__errors.push(`pageerror: ${e.message}`));
  p.on("console", m => { if (m.type() === "error") p.__errors.push(`console: ${m.text()}`); });
  await p.addInitScript({ content: `window.__FIX = ${FIX}; window.__MODE = ${JSON.stringify(mode)}; window.__ROUTE = ${JSON.stringify(route)}; window.__THEME = ${JSON.stringify(theme)}; ${extra}` });
  await p.addInitScript({ path: FAKE });
  await p.goto(`${BASE}?theme=${theme}`);
  await p.waitForSelector("#main h1");
  await p.waitForTimeout(250);
  return p;
}
async function scenario(name, fn) {
  if (ONLY && !ONLY.split(",").includes(name)) return;
  const errors = [];
  const check = (cond, msg) => { if (!cond) errors.push(msg); };
  try { await fn(check); } catch (e) { errors.push(`exception: ${e.message}`); }
  results.push({ name, ok: errors.length === 0, errors });
}
const shot = async (p, name) => { if (SHOTS && SHOTS !== "-") await p.screenshot({ path: path.join(SHOTS, `${name}.png`) }); };
const calls = p => p.evaluate(() => window.__calls);
const answers = p => p.evaluate(() => window.__answers);
async function overflow(p) {
  return p.evaluate(() => {
    const out = [];
    const de = document.documentElement;
    if (de.scrollWidth > de.clientWidth + 1) out.push(`page ${de.scrollWidth}>${de.clientWidth}`);
    for (const el of document.querySelectorAll("#main *, #overlay *, #topbar *, #recbar *")) {
      if (["svg", "use", "path", "circle", "rect"].includes(el.tagName.toLowerCase())) continue;
      const cs = getComputedStyle(el);
      // .pipe-step: its connector line (::after) reaches into the gap on purpose
      if (cs.overflowX !== "visible" || el.closest(".ellipsis") || el.clientWidth === 0 || el.classList.contains("pipe-step")) continue;
      if (el.scrollWidth > el.clientWidth + 2) out.push(`${el.tagName}.${String(el.className).slice(0, 40)} "${(el.textContent || "").trim().slice(0, 30)}" ${el.scrollWidth}>${el.clientWidth}`);
    }
    for (const el of document.querySelectorAll(".modal")) { const r = el.getBoundingClientRect(); if (r.right > innerWidth + 1 || r.left < -1) out.push(`modal outside ${r.left}-${r.right}`); }
    return out.slice(0, 6);
  });
}

(async () => {
  const browser = await chromium.launch();

  await scenario("start_ask_record_stop_postcall_progress", async check => {
    const p = await page(browser, { mode: "ask" });
    await p.click(".hero [data-act=start]");
    await p.waitForSelector(".modal[role=dialog]");
    check(await p.$("#st-check") === null, "Modus ask: kein Häkchen im Startdialog");
    const go = await p.$("#st-go");
    check(go && !(await go.isDisabled()), "Modus ask: „Aufnehmen“ sofort aktiv");
    check((await go.textContent()).includes("Aufnehmen"), "Knopf heißt „Aufnehmen“");
    const stmt = await p.textContent("#st-statement");
    const fix = JSON.parse(FIX);
    check(stmt.trim() === fix.start_ask.statement, "Hinweistext = reminder_statement (exakt)");
    check((await p.textContent(".modal-foot")).includes("Nicht aufnehmen"), "„Nicht aufnehmen“ vorhanden");
    await shot(p, "consent_ask_1366x768_light");
    await p.fill("#st-title", "Jour fixe <b>Test</b>");
    await p.click("#st-go");
    await p.waitForSelector(".hero.recording");
    const a = (await answers(p)).find(x => x.result && x.result.action);
    check(a && a.result.action === "start" && a.result.title === "Jour fixe <b>Test</b>", "Antwort start mit Titel");
    check(await p.$("#overlay .modal") === null, "Dialog geschlossen");
    await p.waitForTimeout(700);
    const t = await p.textContent(".timer");
    check(/00:00:0[1-9]/.test(t), `Laufzeit läuft (${t})`);
    check((await p.textContent(".hero h2")).includes("Jour fixe <b>Test</b>"), "Titel als Text");
    check(await p.$(".rec-pill") !== null, "Aufnahme-Anzeige in der Titelleiste");
    await shot(p, "recording_1366x768_light");
    await p.evaluate(() => window.__countdown(24));
    await p.waitForSelector(".countdown");
    check((await p.textContent(".countdown")).includes("Weiter aufnehmen"), "Countdown mit „Weiter aufnehmen“");
    await shot(p, "countdown_1366x768_light");
    await p.click(".countdown [data-act=stopnow]");
    await p.waitForSelector(".modal .pipe");
    const handoff = await p.$("input[data-plan=handoff]");
    check(handoff && await handoff.isChecked(), "Übergabe-Häkchen standardmäßig gesetzt (Entscheidung 4)");
    // the window may come up while the user types elsewhere: Enter/Space must not answer the question
    check(await p.evaluate(() => document.activeElement.tagName !== "BUTTON" && !!document.activeElement.closest(".modal")), "Fokus im Dialog, nicht auf einer Schaltfläche");
    await p.keyboard.press("Enter");
    await p.keyboard.press(" ");
    check(await p.$(".modal .pipe") !== null && !(await answers(p)).some(a => a && a.choice === "full"), "Enter/Leertaste verarbeiten nicht");
    await shot(p, "postcall_1366x768_light");
    await p.click(".modal-foot [data-c=full]");
    await p.waitForSelector(".work .bar", { timeout: 3000 });
    await shot(p, "pipeline_1366x768_light");
    const pc = (await answers(p)).find(x => x.result && x.result.choice === "full");
    check(pc && pc.result.plan.handoff === true && pc.result.plan.transcript === true, "Plan mit Übergabe gesendet");
    await p.waitForSelector(".snack", { timeout: 4000 });
    check((await p.evaluate(() => window.__jsErrors || [])).length === 0, "keine JS-Fehler gemeldet");
    check(p.__errors.length === 0, `Konsole: ${p.__errors.join("; ")}`);
  });

  await scenario("start_ask_dismiss_starts_nothing", async check => {
    const p = await page(browser, { mode: "ask" });
    await p.click("#topbar [data-act=start]");
    await p.waitForSelector(".modal[role=dialog]");
    await p.click(".modal-foot [data-act=dlg-cancel]");
    await p.waitForTimeout(200);
    const a = await answers(p);
    check(a.length === 1 && a[0].result.action === "cancel", "„Nicht aufnehmen“ = cancel");
    check(await p.$(".hero.recording") === null, "keine Aufnahme");
  });

  await scenario("start_confirm_needs_checkbox", async check => {
    const p = await page(browser, { mode: "confirm" });
    await p.click("#topbar [data-act=start]");
    await p.waitForSelector("#st-check");
    check(await p.isDisabled("#st-go"), "Start gesperrt ohne Häkchen");
    check(!(await p.isChecked("#st-check")), "Häkchen nie vorausgewählt");
    await shot(p, "consent_confirm_1366x768_light");
    await p.keyboard.press("Escape");
    await p.waitForTimeout(150);
    check((await answers(p))[0].result.action === "cancel", "Esc = abbrechen");
    await p.click("#topbar [data-act=start]");
    await p.waitForSelector("#st-check");
    await p.check("#st-check");
    check(!(await p.isDisabled("#st-go")), "Start nach Häkchen aktiv");
    await p.click("#st-go");
    await p.waitForSelector(".hero.recording");
    const a = (await answers(p)).pop();
    check(a.result.checked === true, "Häkchen wird übermittelt");
  });

  await scenario("start_ask_cloud_variant", async check => {
    const p = await page(browser, { mode: "ask", extra: "window.__CLOUD = true;" });
    await p.click("#topbar [data-act=start]");
    await p.waitForSelector("#st-statement");
    const fix = JSON.parse(FIX);
    check((await p.textContent("#st-statement")).trim() === fix.start_ask_cloud.statement, "Anthropic-Hinweistext exakt");
    check((await p.textContent(".dlg-statement")).includes("Anthropic"), "Cloud-Hinweis sichtbar");
  });

  await scenario("library_search_filters_hostile", async check => {
    const p = await page(browser, { route: "library" });
    await p.waitForSelector(".rec-item");
    const n = (await p.$$(".rec-item")).length;
    check(n === 5, `5 Aufnahmen (${n})`);
    await p.fill("#q", "Kessler");
    await p.waitForTimeout(450);
    check((await p.$$(".rec-item")).length === 1, "Suche filtert");
    await p.fill("#q", "");
    await p.waitForTimeout(450);
    await p.click("[data-act=filter][data-f=speakers_open]");
    await p.waitForTimeout(250);
    check((await p.$$(".rec-item")).length === 1, "Filter „Sprecher offen“");
    await p.click("[data-act=filter][data-f=all]");
    await p.waitForTimeout(250);
    await p.click(".rec-item[data-id='2026-11-13_1400_Jour-fixe']");
    await p.waitForSelector(".reader h2");
    check((await p.$$(".reader table tr")).length >= 3, "Tabelle gerendert");
    await shot(p, "library_1366x768_light");
    await p.click("[data-act=tab][data-tab=transcript]");
    await p.waitForSelector(".utt");
    const t0 = Date.now();
    const shown = (await p.$$(".utt")).length;
    check(shown > 250 && shown <= 300, `Transkript seitenweise (${shown})`);
    await p.evaluate(() => { const d = document.querySelector(".lib-detail"); d.scrollTop = d.scrollHeight; });
    await p.waitForTimeout(400);
    const more = (await p.$$(".utt")).length;
    check(more > 300, `Nachladen beim Scrollen (${more})`);
    check(Date.now() - t0 < 4000, "2-Std-Transkript ohne Hänger");
    await shot(p, "transcript_1366x768_light");
    // hostile strings: shown as text, nothing executed
    await p.click(".rec-item[data-id='2026-11-18_1000_Hostile']");
    await p.waitForSelector("[data-act=tab][data-tab=summary]");
    await p.click("[data-act=tab][data-tab=summary]");
    await p.waitForSelector(".reader p");
    await p.evaluate(() => { const d = document.querySelector(".lib-detail"); d.scrollTop = 0; });
    const xss = await p.evaluate(() => window.__xss);
    check(xss === undefined, `kein Skript ausgeführt (${xss})`);
    const injected = await p.evaluate(() => document.querySelectorAll("#main img, #main script, #main [onerror], #main [onload], #main [onmouseover], #main a[href^='javascript']").length);
    check(injected === 0, `keine eingeschleusten Elemente (${injected})`);
    check((await p.textContent("#det-title")).includes('<img src=x onerror="window.__xss=1">'), "Titel wörtlich als Text");
    check((await p.textContent(".reader")).includes("<script>window.__xss=2</script>"), "Markdown-HTML wörtlich");
    await shot(p, "hostile_1366x768_light");
  });

  await scenario("speaker_review_assign", async check => {
    const p = await page(browser, { route: "library" });
    await p.waitForSelector(".rec-item");
    await p.click(".rec-item[data-id='2026-11-20_1100_Kundengespraech']");
    await p.waitForSelector(".det-actions [data-key=review]");
    await p.click(".det-actions [data-key=review]");
    await p.waitForSelector(".modal.review");
    await shot(p, "speakers_1366x768_light");
    const radios = await p.$$(".modal.review input[type=radio]");
    check(radios.length >= 4, `Auswahl je Stimme (${radios.length})`);
    const firstName = await p.evaluate(() => document.querySelector(".modal.review input[type=radio]").value);
    await p.click(".modal.review .choice >> nth=0");
    await p.waitForTimeout(100);
    check(await p.isChecked(".modal.review input[type=radio] >> nth=0"), "Auswahl übernommen");
    await p.fill("[data-other] >> nth=1", "Karl Kunde");
    await p.click(".modal.review [data-a=sample]").catch(() => {});
    await p.click(".modal-foot [data-a=apply]");
    await p.waitForTimeout(200);
    const a = (await answers(p)).pop();
    check(a && a.result.action === "apply", "Übernehmen");
    const asg = a.result.assignments;
    check(asg[0].name === firstName && asg[1].name === "Karl Kunde", `Zuordnungen ${JSON.stringify(asg)}`);
    const fix = JSON.parse(FIX);
    check(fix.review.rows.some(r => r.choices.some(c => c.kind === "self" && c.label.startsWith("Das bin ich"))), "„Das bin ich“ angeboten");
    check(await p.evaluate(() => window.__xss) === undefined, "Hörprobe/Text sicher");
  });

  await scenario("settings_roundtrip", async check => {
    const p = await page(browser, { route: "settings" });
    await p.waitForSelector(".subnav");
    check((await p.textContent(".set-col")).includes("Neue Oberfläche (Vorschau)"), "Schalter für die neue Oberfläche");
    check((await p.textContent(".set-col")).includes("Windows-App-Modus"), "Hinweis: Darstellung folgt Windows");
    await p.check("input[data-field='ui.frontend'][value=web]");
    await p.selectOption("select[data-field='ui.theme']", "dark");
    check(await p.evaluate(() => document.documentElement.dataset.theme) === "dark", "Dunkel sofort sichtbar");
    await p.fill("input[data-field='hotkey.toggle']", "kaputt");
    await p.click("[data-act=save-settings]");
    await p.waitForSelector("#notices .banner-err");
    check((await p.textContent("#notices")).includes("kaputt"), "Fehler bei ungültigem Tastenkürzel");
    await p.fill("input[data-field='hotkey.toggle']", "ctrl+alt+shift+r");
    await p.click("[data-act=setpage][data-page=summary]");
    await p.check("input[data-field='summary.provider'][value=anthropic]");
    await p.click("[data-act=save-settings]");
    await p.waitForSelector(".modal [data-act=ask-ok]");
    await shot(p, "settings_confirm_1366x768_dark");
    await p.click(".modal [data-act=ask-ok]");
    await p.waitForTimeout(300);
    const saved = await p.evaluate(() => window.__saved);
    check(saved && saved.confirmed.includes("cloud") && saved.patch["ui.frontend"] === "web" && saved.patch["ui.theme"] === "dark", `gespeichert ${JSON.stringify(saved)}`);
    await p.click("[data-act=setpage][data-page=recording]");
    check((await p.textContent(".set-col")).includes("Eine Frage vor der Aufnahme"), "Einwilligungsmodus einstellbar");
    check((await p.textContent(".set-col")).includes("Verantwortung"), "Hinweis auf die Verantwortung");
    await shot(p, "settings_recording_1366x768_dark");
  });

  await scenario("errors_and_notices", async check => {
    const p = await page(browser);
    await p.evaluate(() => window.cr.emit("notify", { title: "Verarbeitung fehlgeschlagen", message: "<b>x</b> keine Verbindung", level: "error", hint: { next: "VPN pausieren", action: "open_log", label: "Protokoll öffnen" } }));
    await p.waitForSelector("#notices .banner-err");
    check((await p.textContent("#notices")).includes("<b>x</b>"), "Fehlertext als Text");
    check(await p.$("#notices [data-act=hint]") !== null, "Knopf für den nächsten Schritt");
    await p.click("#notices [data-act=hint]");
    check((await calls(p)).some(c => c[0] === "open_path" && c[1] === "log"), "Protokoll öffnen");
    await p.evaluate(() => window.cr.emit("dialog", { id: "zz", kind: "info", data: { title: "Keine Übergabe möglich", message: "Für diese Aufnahme gibt es noch keine Zusammenfassung." } }));
    await p.waitForSelector(".modal");
    await p.keyboard.press("Escape");
    await p.waitForTimeout(150);
    check(await p.$(".modal") === null, "Info-Dialog per Esc");
    // the page recovers after a reload with an open Python dialog
    const p2 = await page(browser, { extra: "window.__REOPEN = true;" });
    await p2.evaluate(() => window.cr.emit("dialog", { id: "re1", kind: "after_recording", data: JSON.parse(JSON.stringify(window.__FIX.after_recording)) }));
    await p2.waitForSelector(".modal .pipe");
    check(true, "");
  });

  await scenario("keyboard", async check => {
    const p = await page(browser, { route: "library" });
    await p.waitForSelector(".rec-item");
    await p.keyboard.press("Control+f");
    check(await p.evaluate(() => document.activeElement.id) === "q", "Strg+F fokussiert die Suche");
    await p.keyboard.press("ArrowDown");
    await p.waitForTimeout(200);
    check(await p.evaluate(() => document.activeElement.classList.contains("rec-item")), "Pfeil nach unten in die Liste");
    await p.keyboard.press("ArrowDown");
    await p.waitForTimeout(300);
    const sel = await p.$$eval(".rec-item[aria-selected=true]", e => e.length);
    check(sel === 1, "Auswahl mit Pfeiltasten");
    await p.click("[data-act=menu][data-menu=more]");
    await p.waitForSelector(".menu");
    await p.keyboard.press("ArrowDown");
    check(await p.evaluate(() => document.activeElement.getAttribute("role")) === "menuitem", "Menü per Pfeiltaste");
    await p.keyboard.press("Escape");
    check(await p.$(".menu") === null, "Esc schließt Menü");
    await p.click("#topbar [data-act=start]");
    await p.waitForSelector(".modal");
    for (let i = 0; i < 12; i++) await p.keyboard.press("Tab");
    check(await p.evaluate(() => !!document.activeElement.closest(".modal")), "Fokusfalle im Dialog");
    const focusVisible = await p.evaluate(() => getComputedStyle(document.activeElement).outlineStyle !== "none" || getComputedStyle(document.activeElement).boxShadow !== "none");
    check(focusVisible, "Fokus sichtbar");
  });

  // layout matrix: no horizontal overflow, light/dark, 100/125/150 %
  const sizes = [[1366, 768], [1920, 1080]], zooms = [1, 1.25, 1.5];
  for (const [w, hgt] of sizes) for (const zoom of zooms) for (const theme of ["light", "dark"]) {
    if (w === 1920 && zoom !== 1) continue;
    await scenario(`layout_${w}x${hgt}_z${Math.round(zoom * 100)}_${theme}`, async check => {
      for (const route of ["home", "library", "setup", "settings"]) {
        const p = await page(browser, { w, h: hgt, zoom, theme, route, extra: route === "home" && theme === "dark" ? "window.__NOCAL = true;" : "" });
        await p.waitForTimeout(route === "library" ? 500 : 250);
        const o = await overflow(p);
        check(o.length === 0, `${route}: ${o.join(" | ")}`);
        const tag = `${w}x${hgt}${zoom !== 1 ? `_z${Math.round(zoom * 100)}` : ""}_${theme}`;
        if ((w === 1920 && route !== "setup") || (zoom === 1.5 && route !== "settings") || (zoom === 1.25 && route === "home") || (theme === "dark" && w === 1366 && zoom === 1)) await shot(p, `${route}_${tag}`);
        if (route === "home") {
          for (const kind of ["start", "after_recording", "review", "handoff"]) {
            const data = kind === "start" ? "start_ask" : kind;
            await p.evaluate(([k, d]) => window.cr.emit("dialog", { id: `x-${k}`, kind: k, data: JSON.parse(JSON.stringify(window.__FIX[d])) }), [kind, data]);
            await p.waitForSelector(".modal");
            await p.waitForTimeout(120);
            const o2 = await overflow(p);
            check(o2.length === 0, `Dialog ${kind}: ${o2.join(" | ")}`);
            if (zoom === 1.5 || (w === 1920 && kind === "review")) await shot(p, `dlg-${kind}_${tag}`);
            await p.evaluate(k => window.cr.emit("dialog_close", { id: `x-${k}` }), kind);
          }
        }
        check(p.__errors.length === 0, `${route} Konsole: ${p.__errors.join("; ")}`);
        await p.context().close();
      }
    });
  }

  await browser.close();
  console.log(JSON.stringify({ results }));
})();
