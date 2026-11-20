# Port-Plan: von Tk zu pywebview (HTML/CSS/JS)

Ziel: Die Oberfläche aus `design/prototype.html` / `design/design-system.md` wird die echte Oberfläche –
lokales HTML/CSS/JS in pywebview (Edge WebView2, Windows 11), offline, ohne Build-Werkzeuge. Die
Fachlogik bleibt in Python und wird nicht neu geschrieben.

> **Stand 20.11.2026 (Version 0.3.0, Vorschau):** Schritte 0–5 sind umgesetzt, Schritt 6 (Standard umstellen, Tk
> entfernen) bewusst nicht. Abweichungen vom Plan: eigener Loopback-Server (`ui/webserver.py`, Token je Sitzung,
> Whitelist, CSP) statt pywebviews Bottle-Server; Hörproben im Browser (`<audio>`, Clips nur unter zufälligem
> Namen) statt `winsound`; `ui/app.py` bleibt die Tk-Verdrahtung, die Web-Verdrahtung steht in `ui/webapp.py`
> (gemeinsam genutzt: `MeProvider`, `detection_loop`, Update-Prüfung, `AppActions`, `DialogPort`);
> Dialoge laufen über `ui/dialogport.py` (Tk) bzw. `ui/webdialogs.py` (Broker). Einwilligung in zwei Modi
> (`[consent] mode`, gemeinsamer Baustein `ui/startflow.py` für beide Oberflächen).

Grundsatz: **Python entscheidet, JS zeigt an.** Einwilligung, „darf übergeben werden“, Cloud-Bestätigung,
„darf gelöscht werden“ werden weiterhin in Python geprüft (`consent.confirm_consent`, `handoff_ready`,
`cloud_ok`, `controller.is_busy`). Das Frontend kann nichts, was die Python-Seite nicht erlaubt.

---

## 1. Was bleibt, was ändert sich

| Modul | Zukunft |
|---|---|
| `controller.py`, `jobs.py`, `pipeline.py`, `postcall.py`, `autostop.py`, `detect.py`, `hotkey.py`, `library.py`, `setupcheck.py`, `hints.py`, `handoff.py`, `review.py`, `consent.py`, `updater.py`, `autostart.py`, `calendar/*`, `speakers/*`, `asr/*`, `audio/*`, `summary.py`, `llm*.py`, `config.py` | **bleiben unverändert** (Fachlogik, UI-frei, getestet) |
| `ui/viewmodel.py` | bleibt (UI-frei). Ergänzt um `markdown_blocks()` (Block-Struktur statt Tk-Spans, inkl. echter Tabellen) und DTO-Funktionen (siehe 3) |
| `ui/actions.py` (`AppActions`) | bleibt die **eine** Aktionsschicht für Fenster und Tray. Blockierende Tk-Dialoge (`dialogs.*`, `messagebox`) werden durch einen Dialog-Broker ersetzt (siehe 4); die Logik der Methoden bleibt |
| `ui/app.py` | wird zu `ui/core.py` (Verdrahtung ohne Tk: Controller, Queue, Actions, AutoStop, Tray, Hotkey, Update-Check) + `ui/webapp.py` (pywebview-Start) |
| `ui/tkui.py` (`TkUi`: `post`/`call`/`notify`/Overlay) | ersetzt durch `ui/apploop.py` (`AppLoop`: eigener App-Thread mit `post`/`call`) und `ui/webui.py` (`WebUi` implementiert `controller.UiPort`) |
| `ui/tray.py` (pystray) | bleibt, nur `post` kommt vom `AppLoop` |
| `ui/mainwindow.py`, `ui/dialogs.py`, `ui/settings.py` | entfallen nach der Umstellung; ihre UI-freien Teile wandern vorher in `viewmodel.py`/`bridge.py` |
| neu `ui/web/` | `index.html`, `app.css` (Tokens 1:1 aus dem Prototyp), `app.js` (+ ES-Module je Bildschirm), `indicator.html`, `toast.html`, `icons.svg` |
| neu `ui/bridge.py` | `Bridge`-Klasse = `js_api` von pywebview; dünn, ruft nur `AppActions`/DTO-Funktionen |

---

## 2. Threading unter Windows

```
Hauptthread ── pywebview (webview.start) ── WebView2-Fenster: Hauptfenster, Aufnahme-Anzeige, Toast
App-Thread  ── AppLoop: Controller, AppActions, Postcall-Kette (ersetzt den Tk-Thread; alles Bisherige,
               das „auf dem Tk-Thread“ lief, läuft hier – gleiche Einzel-Thread-Garantie)
Queue-Thread ── ProcessingQueue (unverändert), Whisper im Kindprozess (unverändert)
AutoStop-Thread ── AutoStopRunner (unverändert, postet an AppLoop)
Tray-Thread ── pystray (`Icon.run()` in eigenem Thread wie bisher, Callbacks → AppLoop.post)
Hotkey-Thread ── GlobalHotkey (RegisterHotKey + eigene Nachrichtenschleife, unverändert → AppLoop.post)
Detect-Thread ── detection_loop (unverändert)
pywebview-API-Threads ── jeder JS-Aufruf von `window.pywebview.api.x()` läuft in einem eigenen Thread
```

Regeln:

1. **pywebview besitzt den Hauptthread** (`webview.start()` muss dort laufen; ohne Fenster endet die
   Schleife → das Hauptfenster wird beim Start versteckt erzeugt, `hidden=True`, auch bei „nur im Infobereich“).
2. **Alles, was heute `ui.post`/`ui.call` nutzt, geht an `AppLoop`** – eine Queue + ein Thread, Schnittstelle
   identisch zu `TkUi.post/call`. Dadurch ändern sich `controller.py`, `autostop.py`, `postcall.py` nicht.
3. **Bridge-Methoden blockieren nie den Hauptthread.** Sie laufen in pywebviews API-Threads, reichen per
   `AppLoop.call(fn)` an den App-Thread weiter und geben ein JSON-fähiges Ergebnis zurück (JS bekommt ein Promise).
   Lange Arbeit (Kalender, Modell-Download, Verbindungstest, Update-Prüfung) läuft wie bisher in Worker-Threads und
   meldet sich per Ereignis.
4. **Python → JS nur über `Events.emit(name, payload)`**: serialisiert JSON, ruft
   `window.evaluate_js(f"window.cr.emit({json})")` für das jeweilige Fenster. Gedrosselt: Pegel 10/s nur solange
   das Hauptfenster sichtbar ist, Fortschritt höchstens 4/s, Laufzeit 1/s (oder JS zählt selbst ab `started_at`).
5. **Begleitfenster sind eigene pywebview-Fenster** (gleicher Prozess, gleicher Hauptthread):
   - *Aufnahme-Anzeige* (ersetzt `tkui.RecordingOverlay`): `create_window("…", "web/indicator.html",
     width=380, height=64, frameless=True, on_top=True, easy_drag=True, focus=False, hidden=True)`;
     Position oben rechts aus `webview.screens[0]` (Arbeitsbereich), Position wird gemerkt. Zeigt Laufzeit +
     Stopp, beim Countdown erweitert (Höhe 150) mit „Jetzt beenden“ / „Weiter aufnehmen“.
   - *„Call erkannt“-Toast* (ersetzt `dialogs.ask_record_prompt`): 400×240, unten rechts über der Taskleiste,
     `frameless`, `on_top`, `focus=False`, schließt nach 90 s. „Aufnahme vorbereiten …“ holt das Hauptfenster
     nach vorn und öffnet die Einwilligung (dort, nicht im Toast, wird bestätigt).
   - Beide werden **beim Start versteckt erzeugt und nur ein-/ausgeblendet** (WebView2-Kaltstart ≈ 0,3–1 s
     wäre beim Call-Beginn zu spät).
   - Beide bekommen ein **eigenes, kleines `js_api`** (nur `stop`, `keep_recording`, `prepare_recording`,
     `dismiss`), nicht die volle Bridge.
6. **Tray-Symbol bleibt nativ** (pystray). Menü wie heute (README Abschnitt 7). Klick → `window.show()` +
   `restore()`; Schließen des Hauptfensters = verstecken (`closing`-Ereignis gibt `False` zurück),
   Beenden nur über Tray/Menü (`AppCore.quit` → `webview.windows[*].destroy()`).
7. **Einzelinstanz** wie bisher (`acquire_single_instance`); ein zweiter Start zeigt das Fenster der ersten Instanz.
8. Beenden: `quit_app` bleibt (AutoStop aus, Aufnahme mit `app_quit` stoppen, Queue herunterfahren, Downloads
   abbrechen, Hotkey/Tray stoppen, Fensterzustand sichern), danach Fenster zerstören.

---

## 3. Bridge-API (JS → Python)

Aus `ui/actions.py` (AppActions) und `ui/viewmodel.py` abgeleitet. Alle IDs sind Ordnernamen
(`RecordingInfo.id`). Rückgaben sind JSON; Fehler kommen als `{"error": "Text auf Deutsch", "hint": {...}}`
(aus `hints.py`), nie als Exception-Text im UI.
Methoden, die eine zusätzliche Bestätigung brauchen, antworten `{"confirm": {"kind": "...", …}}`; JS zeigt
den Dialog und ruft dieselbe Methode mit `confirmed=["kind"]` erneut (z. B. Cloud-Bestätigung für ältere Aufnahmen).

### Zustand und Übersicht

| Methode | Quelle | Ergebnis |
|---|---|---|
| `get_app_state()` | Controller, Queue-Snapshot, `setupcheck.summary_line`, `version_text`, `MeProvider` | `{recording:{on,title,started_at,event}, autostop:{…}, queue:{current:{id,title,step,fraction,message,eta}, pending}, setup:{ok, warnings}, me, version, theme}` |
| `get_home()` | `calendar.fetch_day()`/`upcoming_online_meetings`, `library` + `next_action`, Queue | `{next_event:{subject,start,end,online,participants[]}, running_events[], attention[{id,title,meta,reason,action:{key,label}}], work, today[{time,subject,kind,recording_state}], recent[]}` |
| `get_events()` / `refresh_calendar(force)` | `selected_suggestion`, `refresh_calendar` | Termine für „Anderer Termin“ |

### Aufnahme

| Methode | Quelle |
|---|---|
| `prepare_recording(event_id or null)` | liefert die Vorbelegung des Einwilligungsdialogs (`Suggestion`, `consent_statement(provider)`, Cloud-Hinweis) |
| `import_ics()` | nativer Dateidialog (`create_file_dialog(OPEN_DIALOG, file_types=("Kalenderdatei (*.ics)",))`) → `calendar.suggest_from_ics` |
| `start_recording({title, participants[], event_id, consent_checked})` | **Python** baut `confirm_consent(...)` (wirft ohne Häkchen) und gibt `StartDecision` an den wartenden `Controller.start` zurück (Broker, siehe 4) |
| `stop_recording()`, `toggle()` | `AppActions.stop_recording/toggle` |
| `autostop_keep()`, `autostop_stop()`, `autostop_dismiss()` | `MainWindow.autostop_*` → AutoStopRunner |
| `answer_after_recording(id, choice, plan{transcript,speakers,summary,handoff})` | `ask_after_recording` (full/later/discard; `cloud_ok` vor `chain.start`) |
| `process_now(id)` | `AppActions.process_now` |

### Aufnahmen

| Methode | Quelle |
|---|---|
| `list_recordings(filter, query)` | `RecordingIndex`/`library` + `next_action` + Filter wie `MainWindow.apply_filter` → `[{id,title,started_at,duration_s,language,participants,status:{key,text},action:{key,label},busy,fraction}]` |
| `get_recording(id)` | `RecordingInfo` + Übergabe (`handoff`, `handoff_current`, Ziel, Version), Hinweise (`summary_problem`, `capture_warnings`, `old_identification`), `version_choices(…)` für beide Arten |
| `get_text(id, kind, version)` | Datei lesen → `markdown_blocks()` (neu, getestet). **JS setzt nur `textContent`** – kein HTML aus Dateien |
| `cancel_processing(job_id or null)` | `AppActions.cancel_processing` |
| `resummarize(id, confirmed=[])` | `AppActions.resummarize` (+ `cloud_ok`) |
| `retranscribe(id, language, preset)` / `get_retranscribe_choices(id)` | `ask_retranscribe` (Sprache auto/de/en, `list_presets()`) |
| `reprocess(id)`, `rerender(id)` | `AppActions.reprocess/rerender` |
| `handoff_preview(id)` → `{ready, why, target, document, sha256}` | `handoff_ready` + Vorschau aus `dialogs.ask_handoff` |
| `handoff_confirm(id, sha256)` | `perform_handoff(cfg, rec, sha)` – bricht ab, wenn sich die Zusammenfassung seit der Vorschau geändert hat |
| `assign_event_candidates(id)` / `assign_event(id, event_id, take_title)` | `AppActions.assign_event/_choose_event` |
| `get_participants(id)` / `set_participants(id, people[])` | `edit_participants` → `review.set_participants` |
| `recheck_preview(id)` / `recheck_apply(id)` | `recheck_speakers` |
| `allow_more_speakers(id, allow)` | `AppActions.allow_more_speakers` |
| `rename(id, title)`, `delete(id, permanent=false)` | `AppActions.rename/delete` (gesperrt während Aufnahme/Verarbeitung) |
| `play(id)`, `open_folder(id or null)` | `AppActions.play/open_folder` (Windows-Standardprogramm) |

### Sprecher

| Methode | Quelle |
|---|---|
| `get_speaker_review(id)` | `speaker_overview`, `self_overview`, `row_choices` (nur Teilnehmende; Nicht-Teilnehmende als `suggestion`), Warnungen `trackcheck`, `in_chain` |
| `play_sample(id, speaker_id)` / `stop_playback()` | `write_clip` + `winsound` (wie heute; kein Audio im Browser nötig) |
| `apply_speakers(id, assignments[{id,name,keep}], resummarize)` | `review.apply_assignments` + `rerender`; in der Kette: Antwort an den wartenden `review_in_chain` |
| `skip_review(id)` | Kette: „Überspringen“ |

### Einrichtung, Einstellungen, Updates

| Methode | Quelle |
|---|---|
| `run_checks(force_audio)` | `setupcheck.run_all` → `CheckItem` (key, title, state, detail, required, actions) |
| `setup_action(key)` | `MainWindow.setup_action` (Geräte, Download, Test, Client-ID, Login, Autostart …) |
| `test_summary()` / `download_whisper()` | `probe_summary_provider` / `AppActions.download_whisper` (Fortschritt als Ereignis) |
| `login()` / `logout()` / `set_autostart(on)` | `AppActions.login/set_autostart` |
| `get_settings()` | `load_config(preset="")` + Auswahllisten (Geräte über `WasapiBackend`, Modelle, Presets) |
| `save_settings(patch, confirmed=[])` | `SettingsDialog.collect/do_save` ohne Tk: Validierung, `confirm_cloud`, `confirm_auto_handoff`, `save_config`, `reload_config_in_place`, Kalenderquelle neu |
| `save_api_key(key)`, `delete_api_key()`, `test_anthropic()` | `settings.py` (DPAPI) – der Schlüssel wird nie an JS zurückgegeben |
| `list_llm_models()`, `pull_llm_model(name)` | `settings.refresh_models/pull_model` |
| `get_prompts()`, `save_prompt(name, text)`, `restore_prompt_default(name)`, `test_prompt(id)` | `userfiles` + `edit_prompts` (Test = neue Version der Zusammenfassung) |
| `get_text_file(kind)`, `save_text_file(kind, text)` | Glossar, Korrekturliste |
| `list_profiles()`, `profile_origins(pid)`, `delete_profile(pid)`, `reset_profile(pid)`, `rebuild_profile(pid)`, `delete_all_profiles()` | `dialogs.manage_profiles` |
| `check_updates()`, `start_update(mode, wait)`, `restore_previous_version()` | `AppActions.check_updates/start_update/restore_previous_version`, `busy_reason` |
| `open_log()`, `open_readme()`, `pick_folder(purpose)` | `open_path`, nativer Ordnerdialog (`FOLDER_DIALOG`) für Aufnahmeordner und Übergabeordner |
| `save_window_state(state)`, `set_theme(mode)` | `WindowState` (Größe, Position, maximiert, Listenbreite, Filter, Reiter, Thema) |
| `quit()`, `hide_window()` | `AppActions.quit`, Fenster verstecken |

### Ereignisse (Python → JS, `window.cr.emit(name, payload)`)

| Ereignis | Auslöser |
|---|---|
| `recording` | `set_recording_state` (an/aus, Titel, Start) |
| `levels` | Controller-Pegel (10/s, nur sichtbar) |
| `autostop` | Countdown/Grund/„Weiter aufnehmen“ (`AutoStopRunner.info`) |
| `queue` | Job-Listener (Schritt, Bruchteil, Meldung, Wartende) |
| `recording_changed` | `AppActions.on_changed` (ID oder alle) |
| `config_changed` | `AppActions.on_config_changed` |
| `notify` | `ui.notify` (Titel, Text, optional `hint{action,label}` aus `hints.py`) – im Fenster als Meldung, sonst Tray-Sprechblase |
| `dialog` | Broker-Anfrage (siehe 4) |
| `calendar` | neue Termine |
| `checks` | Ergebnis von `run_checks` |
| `update` | Update-Prüfung/-Status |

---

## 4. Dialoge: von blockierendem Tk zu Anfrage/Antwort

Heute blockieren `Controller.start → ui.ask_start`, `ask_after_recording`, `review_in_chain`,
`confirm_cloud_summary`, `messagebox.askyesno` den Tk-Thread mit einer verschachtelten Ereignisschleife.

Neu: `DialogBroker.ask(kind, data) -> Future`:

1. Python erzeugt eine Anfrage-ID, sendet `dialog {id, kind, data}` an das Hauptfenster (zeigt es ggf. an,
   ohne Fokus zu stehlen, außer bei Einwilligung) und wartet auf das Future – **nur auf dem App-Thread oder
   einem Worker, nie auf dem Hauptthread**.
2. JS zeigt den HTML-Dialog aus dem Prototyp und ruft `answer(id, result)`; Schließen/Esc = Abbrechen bzw.
   „Später“ (wie heute: Fenster schließen = „Später“).
3. Damit der App-Thread während eines offenen Dialogs nicht steht (heute erledigt das Tks verschachtelte
   Schleife), wartet der Broker **mit Pumpen**: `AppLoop.wait(future)` arbeitet weitere gepostete Aufrufe ab, bis
   das Future erfüllt ist (gleiches Verhalten wie `TkUi._pump` + `wait_window`).
4. Ist die App beim Start einer Anfrage beendet worden, wird sie wie heute beim nächsten Start erneut gestellt
   (`processing.chain` in `meta.json`).

`UiPort` (Controller) wird so implementiert: `ask_start` → Broker `consent`; `ask_record_prompt` → Toast-Fenster
(Broker mit 90-s-Zeitlimit); `notify` → Ereignis/Tray; `set_recording_state` → Ereignis + Anzeige-Fenster;
`set_status` → Tray-Tooltip + Ereignis; `open_review` → Broker `review`.

**Native Dialoge bleiben nur für Dateien/Ordner** (`create_file_dialog`); alle Rückfragen sind HTML-Dialoge im
Hauptfenster (einheitlich, per Tastatur bedienbar, testbar). Kein `alert()/confirm()` in JS.

---

## 5. Frontend-Aufbau (`ui/web/`)

- `index.html` (lädt `app.css`, `app.js` als ES-Modul); **CSP** per `<meta http-equiv="Content-Security-Policy"
  content="default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'">` – daher kein
  Inline-Script/-Style wie im Prototyp (dort nur, damit es eine Datei bleibt).
- Laden über pywebviews eingebauten lokalen Server (relativer Pfad `web/index.html`), damit ES-Module
  funktionieren; keine externen URLs, Navigation zu fremden Seiten wird blockiert und Links öffnen im Browser.
- Rendering wie im Prototyp (eine `render()`-Funktion je Bildschirm), aber **ohne `innerHTML` mit Daten**: Texte
  aus Aufnahmen, Kalender und Transkripten sind fremder Inhalt und kommen nur über `textContent`/ein
  `esc()`-Hilfsmittel in die Seite.
- Zustand im JS nur als Ansicht (aktuelle Route, Auswahl, Filter); Wahrheit liegt in Python.
- Thema: `prefers-color-scheme` (folgt Windows live) + gespeicherte Wahl; pywebview-Fensterhintergrund passend
  setzen, damit beim Start kein weißer Blitz im Dunkelmodus erscheint.
- Die Prototyp-Zustandsliste, `?static`, `?clean` und die Simulationen entfallen.

---

## 6. Tests

| Ebene | Wie | Läuft wo |
|---|---|---|
| Fachlogik | bestehende `pytest`-Suite unverändert | Linux/Windows |
| Bridge/DTO | neue Tests für `bridge.py`-Funktionen mit den vorhandenen Fakes (Controller, Queue, Recording-Ordner in `tmp_path`): `list_recordings`/`next_action`-Abbildung, Filter, `markdown_blocks`, Bestätigungs-Rückgaben (`confirm`), Einwilligung ohne Häkchen wird abgelehnt, Handoff mit veralteter Prüfsumme bricht ab | Linux/Windows |
| Broker/AppLoop | Anfrage/Antwort, Zeitlimit, Abbrechen, Pumpen während Warten, Reihenfolge der Posts | Linux/Windows |
| Vertrag Python ↔ JS | ein Test schreibt Beispiel-DTOs nach `tests/fixtures/ui/*.json`; das Frontend-Test-Gerüst lädt genau diese Dateien → DTO-Änderungen brechen die UI-Tests sichtbar | Linux/Windows |
| Frontend | Playwright (vorhandenes Chromium) gegen `ui/web/index.html` mit `fake-bridge.js` (implementiert `window.pywebview.api` aus den Fixtures, zeichnet Aufrufe auf): Navigation, Einwilligung sperrt „Aufnahme starten“, Countdown-Knöpfe rufen `autostop_keep/stop`, Nachfrage-Dialoge, Tastatur (Tab, Esc, Strg+F, Pfeile, Fokusfalle), kein waagerechter Überlauf bei 1366×768, 1093×614 (125 %) und 911×512 (150 %), hell/dunkel; Screenshots als Artefakt | Linux (CI) |
| Kontrast | Token-Kontrast-Test (Skript wie für `design-system.md`) gegen die Werte in `app.css` | Linux |
| Windows-Handtest (Checkliste) | WebView2-Start, Hotkey bei verstecktem Fenster, Toast stiehlt keinen Fokus aus Teams, Anzeige bleibt über Teams im Vollbild, Countdown → automatischer Stopp, Tray-Menü, 125/150 % auf 1366×768, Wechsel hell/dunkel zur Laufzeit, Neustart mit offener Kette, Update + Wiederherstellen | Rechner des Nutzers |

Bestehende Tk-Tests (`test_ui_mainwindow.py`, `test_ui_smoke.py`) bleiben, solange Tk existiert, und werden mit
dem Tk-Frontend entfernt; `test_mainwindow_logic.py` (UI-frei) bleibt.

---

## 7. Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| **pywebview-Abhängigkeiten unter Windows**: WebView2-Backend über `pythonnet`/.NET; Wheels für Python 3.12/3.13 und Versions-Pins in `requirements.txt`/`constraints-win.txt` | vor Schritt 3 auf dem Zielrechner prüfen (Schritt 0); WebView2-Runtime ist auf Windows 11 vorinstalliert; `setup.ps1` prüft sie |
| **`focus=False`, `on_top`, `frameless` für Begleitfenster** verhalten sich je nach pywebview-Version unterschiedlich | in Schritt 0 mit der gepinnten Version prüfen; Rückfall: Aufnahme-Anzeige als kleines Win32-Layered-Window über `ctypes` (kein zweites UI-Toolkit) |
| Speicher: 3 WebView2-Fenster ≈ 150–250 MB | gemeinsamer Benutzerdatenordner, Begleitfenster minimal (kein `app.js`), versteckt statt zerstört |
| **Blockierende Dialoge → asynchron**: größter Umbau in `actions.py` | Broker mit Pumpen bildet das Tk-Verhalten nach; Einwilligungs- und Übergabeprüfungen bleiben in Python; je Dialog ein Test |
| Doppelte Ereignisschleifen während der Übergangszeit | Tk und pywebview nie im selben Prozess gleichzeitig (siehe Reihenfolge: Umschalten per Einstellung, nicht Mischbetrieb) |
| Sicherheit: `js_api` ist für die Seite voll zugänglich | nur lokale Seiten, CSP, keine fremde Navigation, keine Daten-HTML-Injektion, API-Schlüssel nie an JS, destruktive Methoden prüfen `is_busy` in Python |
| Startzeit beim Autostart | Hauptfenster versteckt erzeugen, Daten erst bei `show` laden; Tray/Hotkey sind sofort bereit |
| Barrierefreiheit in WebView2 | semantisches HTML (siehe design-system.md), Handtest mit Sprachausgabe |
| Ungleichstand Prototyp ↔ echte Daten (lange Titel, viele Teilnehmende, 300 Aufnahmen) | Playwright-Fixtures mit Extremfällen; Liste ab ~200 Einträgen virtualisieren oder nach Monaten nachladen |

---

## 8. Reihenfolge (die App bleibt nach jedem Schritt benutzbar)

0. **Machbarkeit auf Windows (½ Tag):** pywebview + WebView2 im `.venv` installieren, drei Fenster (Haupt,
   rahmenlos-oben, Toast ohne Fokus), `evaluate_js`-Ereignisse, `create_file_dialog`, `closing` abfangen,
   pystray + Hotkey parallel. Ergebnis: gepinnte Versionen. *Tk-App unverändert im Einsatz.*
1. **`AppLoop` + `ui/core.py` herausziehen:** Verdrahtung aus `ui/app.py` ohne Tk-Abhängigkeit; Tk läuft weiter
   und nutzt `AppLoop` als Post-Ziel (Tk-Thread = App-Thread). *Kein sichtbarer Unterschied.*
2. **DTOs, `markdown_blocks`, Broker, `bridge.py` + Tests + Fixture-Export:** noch ohne Fenster. *Kein sichtbarer
   Unterschied.*
3. **Web-Frontend v1 hinter `[ui] frontend = "web"`** (Standard bleibt `"tk"`): Übersicht, Einwilligung,
   Aufnahme + Countdown, Aufnahme-Anzeige, Toast, Nach-dem-Call-Frage, Fortschritt, Aufnahmen (Liste, Detail,
   Zusammenfassung/Transkript, Versionen), Übergabe mit Vorschau, Sprecher zuordnen, Tray, Hotkey. Das ist der
   tägliche Ablauf; der Nutzer kann umschalten und jederzeit zurück (Einstellung bzw. `callrec tray --ui tk`).
4. **v2: Einrichtung, Einstellungen, Anweisungs-Editor, Updates**, restliche „Weitere“-Aktionen (neu
   transkribieren, Termin zuordnen, Teilnehmende, neu prüfen, weitere Sprecher, umbenennen, löschen).
5. **v3: Seltenes:** Stimmprofile verwalten, Presets, Glossar/Korrekturliste, Ollama-Modelle laden,
   Protokoll/Anleitung öffnen. Danach Gleichstand mit Tk (Checkliste aus README 7a).
6. **Standard umstellen** auf `"web"`, eine Version lang Tk als Rückfall behalten, dann `mainwindow.py`,
   `dialogs.py`, `settings.py`, `tkui.py` und die Tk-Tests entfernen; README Abschnitt 7 neu schreiben,
   Screenshots aus `design/screenshots` ersetzen.

Jeder Schritt ist für sich testbar (Schritte 1–2 ändern nichts Sichtbares, ab 3 entscheidet der Nutzer per
Einstellung), und ein Update auf eine Zwischenversion lässt die gewohnte Tk-Oberfläche funktionsfähig.
