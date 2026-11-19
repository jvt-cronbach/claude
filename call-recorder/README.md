# CallRecorder – lokale Aufnahme, Transkription und Zusammenfassung von Teams-Gesprächen

CallRecorder ist ein kleines Windows-11-Programm im Infobereich (Tray). Es nimmt Gespräche
– typischerweise Microsoft-Teams-Anrufe über ein Headset – **mit Einwilligung aller
Teilnehmenden** auf, ohne die Aufzeichnungsfunktion von Teams zu nutzen, und erstellt
anschließend **vollständig lokal**:

- ein Transkript mit Sprechernamen und Zeitstempeln (`transcript.md`),
- ein kurzes Ergebnisprotokoll mit Entscheidungen und Aufgaben (`summary.md`),
- Metadaten inkl. Einwilligungsbestätigung (`meta.json`),
- die Audiospuren (FLAC oder Opus).

Audio, Transkripte und Zusammenfassungen verlassen den Rechner nicht. Netzwerkzugriffe gibt
es nur für den Kalender (Microsoft Graph, nur Termin-Metadaten) und für das einmalige
Herunterladen der Modelle.

---

## Inhalt

1. [Rechtlicher Hinweis](#1-rechtlicher-hinweis)
2. [So funktioniert es](#2-so-funktioniert-es)
3. [Voraussetzungen](#3-voraussetzungen)
4. [Installation](#4-installation)
5. [Entra-App-Registrierung (Kalender)](#5-entra-app-registrierung-kalender)
6. [Ollama für die Zusammenfassung](#6-ollama-für-die-zusammenfassung)
7. [Erster Start und Bedienung](#7-erster-start-und-bedienung)
8. [Sprecher zuordnen und Stimmprofile](#8-sprecher-zuordnen-und-stimmprofile)
9. [Ergebnisse einer Aufnahme](#9-ergebnisse-einer-aufnahme)
10. [Kommandozeile](#10-kommandozeile)
11. [Konfiguration](#11-konfiguration)
12. [Wo liegen welche Daten?](#12-wo-liegen-welche-daten)
13. [Fehlerbehebung](#13-fehlerbehebung)
14. [Für Entwickler](#14-für-entwickler)

---

## 1. Rechtlicher Hinweis

**Einwilligung (§ 201 StGB).** Wer das nichtöffentlich gesprochene Wort eines anderen
unbefugt aufnimmt, macht sich strafbar. Nehmen Sie deshalb nur auf, wenn **alle**
Teilnehmenden vorher informiert wurden und **ausdrücklich zugestimmt** haben – am besten
zu Beginn des Gesprächs, sodass die Zustimmung auch in der Aufnahme enthalten ist.
CallRecorder fragt vor **jeder** Aufnahme nach dieser Bestätigung. Ohne Häkchen startet
keine Aufnahme; die Abfrage lässt sich bewusst nicht per Konfiguration abschalten.
Die Bestätigung wird mit Zeitstempel, Ihrem Namen und der Teilnehmerliste in `meta.json`
gespeichert. Die Verantwortung, dass die Zustimmung tatsächlich vorliegt, bleibt bei Ihnen.

**Datenschutz (DSGVO).** Aufnahmen, Transkripte und Protokolle sind personenbezogene
Daten. Informieren Sie die Teilnehmenden über Zweck und Speicherdauer und löschen Sie
Aufnahmen, wenn sie nicht mehr benötigt werden.

**Stimmprofile sind biometrische Daten (Art. 9 DSGVO).** Für die automatische
Wiedererkennung speichert CallRecorder pro Person einen „Stimmabdruck“ (Sprecher-Embedding).
Diese Profile

- werden **nur lokal** gespeichert und unter Windows mit DPAPI verschlüsselt (nur Ihr
  Windows-Konto auf diesem Rechner kann sie lesen),
- werden nur angelegt, wenn Sie eine Stimme ausdrücklich einer Person zuordnen
  (Häkchen „Stimme merken“),
- lassen sich jederzeit einzeln oder komplett löschen (Tray-Menü „Stimmprofile verwalten“
  oder `callrec profiles delete "Name"`).

Holen Sie für das Anlegen eines Stimmprofils am besten eine gesonderte Einwilligung der
Person ein. Zusätzlich liegen in jedem Aufnahmeordner unter `data\speaker_embeddings.npz`
die Embeddings der Sprecher dieses Gesprächs (für die spätere Zuordnung); löschbar mit
`callrec purge-voiceprints <Ordner>` bzw. `--all`.

---

## 2. So funktioniert es

1. **Start** per Tastenkürzel (Standard **Strg+Alt+Umschalt+R**) oder Tray-Menü.
   Wenn Teams das Mikrofon öffnet oder ein Online-Termin beginnt, fragt CallRecorder
   zusätzlich nach, ob aufgenommen werden soll – es startet **nie** von selbst.
2. **Kalender:** Der aktuell laufende Termin wird aus Outlook (Microsoft Graph) gesucht.
   Betreff → Aufnahmename (z. B. `2026-11-19_1400_Projekt-Jour-fixe`), Organisator und
   Teilnehmende → mögliche Sprecher. Ohne Kalender: Titel und Teilnehmende eintippen oder
   eine `.ics`-Einladung importieren.
3. **Einwilligung bestätigen** → Aufnahme läuft. Ein roter Balken oben rechts und das rote
   Tray-Symbol zeigen das deutlich an.
4. **Aufnahme:** Zwei getrennte Spuren über WASAPI, ohne virtuellen Audiotreiber:
   - Ihr Mikrofon (Standard: Windows-Standardgerät für *Kommunikation*),
   - ein Loopback-Mitschnitt des Ausgabegeräts (Headset) = die Gegenseite.
   Beide Spuren werden fortlaufend auf die Festplatte geschrieben (absturzsicher; bei einem
   Absturz geht höchstens die letzte ~2 Sekunden verloren).
5. **Nach dem Stopp** (im Hintergrund, Fortschritt im Tray-Tooltip):
   FLAC/Opus erzeugen → Transkription mit faster-whisper (GPU, sonst CPU) →
   Sprechertrennung der Gegenseite (sherpa-onnx) → Abgleich mit gespeicherten Stimmprofilen →
   `transcript.md` → Zusammenfassung über Ollama → `summary.md`.
6. **Unbekannte Stimmen** erscheinen als „Sprecher 1 … n“. Der Zuordnungsdialog öffnet sich
   automatisch; einmal zugeordnete Stimmen werden beim nächsten Gespräch erkannt.

Die Mikrofonspur ist immer **Sie** (Name aus dem Microsoft-Konto bzw. den Einstellungen).

---

## 3. Voraussetzungen

- Windows 11, ein Benutzerkonto mit Administratorrechten (nur für die Installation von
  Python/Treibern nötig)
- **Python 3.12** (64 Bit) von python.org oder `winget install Python.Python.3.12`
  (mit „tcl/tk“, ist Standard)
- NVIDIA-Grafikkarte mit aktuellem Treiber (CUDA 12 kompatibel, Treiber ≥ 525).
  Für `large-v3` werden ca. 5 GB Grafikspeicher benötigt; sonst `large-v3-turbo` oder
  `medium` einstellen. Ohne GPU läuft alles auf der CPU (deutlich langsamer).
- Optional: [Ollama](https://ollama.com) für die Zusammenfassung
- Freier Speicher: ~4 GB (Modelle + NVIDIA-Bibliotheken) plus Aufnahmen
  (Spuren werden in der Geräte-Abtastrate, meist 48 kHz mono, gespeichert: FLAC grob
  100–200 MB pro Stunde und Spur, Opus etwa ein Zehntel davon; während der Aufnahme
  belegt die WAV-Zwischendatei ca. 350 MB pro Stunde und Spur)

---

## 4. Installation

1. Ordner `call-recorder` an einen festen Ort kopieren, z. B. `C:\Tools\call-recorder`.
2. PowerShell in diesem Ordner öffnen und ausführen:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   Optionen:
   - `-Autostart` – CallRecorder bei der Anmeldung starten
   - `-WithWhisperModel` – das Whisper-Modell (≈ 3 GB) gleich mitladen
   - `-NoCuda` – keine NVIDIA-Bibliotheken installieren (nur CPU)
   - `-PythonVersion 3.13` – andere Python-Version

   Das Skript legt `.venv` an, installiert alle Pakete in exakt gepinnten Versionen
   (`requirements.txt`, `requirements-cuda.txt`, `constraints-win.txt`), prüft die Importe
   und die GPU, legt die Konfiguration an, lädt die Sprechertrennungsmodelle und erstellt
   eine Startmenü-Verknüpfung „CallRecorder“.

3. Konfiguration öffnen und mindestens die Kalenderdaten eintragen (Abschnitt 5):

   ```powershell
   notepad "$env:APPDATA\CallRecorder\config.toml"
   ```

4. Audiogeräte prüfen:

   ```powershell
   .venv\Scripts\python -m callrec devices
   ```

5. Testaufnahme in der Konsole (nimmt ein paar Sekunden auf, Enter beendet):

   ```powershell
   .venv\Scripts\python -m callrec record --title "Test" --no-calendar
   ```

---

## 5. Entra-App-Registrierung (Kalender)

CallRecorder liest Ihren Kalender mit **delegierten** Rechten (nur Ihr eigener Kalender,
nur lesend). Es wird **kein** geheimer Schlüssel benötigt („öffentlicher Client“).

1. <https://entra.microsoft.com> öffnen → **Identität → Anwendungen → App-Registrierungen**
   → **Neue Registrierung**.
2. **Name:** z. B. `CallRecorder (lokal)`.
   **Unterstützte Kontotypen:** *Nur Konten in diesem Organisationsverzeichnis (Einzelner
   Mandant)*.
   **Umleitungs-URI:** Plattform **Öffentlicher Client/nativ (mobil und Desktop)**, Wert
   `http://localhost` → **Registrieren**.
3. Auf der **Übersicht** notieren:
   - **Anwendungs-ID (Client)** → `client_id`
   - **Verzeichnis-ID (Mandant)** → `tenant_id`
4. **Authentifizierung:** Prüfen, dass unter *Mobilgerät- und Desktopanwendungen* die URI
   `http://localhost` steht. Unter *Erweiterte Einstellungen* **Öffentliche Clientflows
   zulassen** auf **Ja** stellen und speichern.
5. **API-Berechtigungen → Berechtigung hinzufügen → Microsoft Graph → Delegierte
   Berechtigungen**: `Calendars.Read` auswählen. `User.Read` ist meist schon vorhanden
   (sonst ebenfalls hinzufügen). Keine Anwendungsberechtigungen!
6. Als Mandantenadministrator **„Administratorzustimmung für <Mandant> erteilen“** klicken
   (sonst erscheint die Zustimmung beim ersten Anmelden).
7. In `%APPDATA%\CallRecorder\config.toml` eintragen:

   ```toml
   [calendar]
   client_id = "00000000-0000-0000-0000-000000000000"
   tenant_id = "11111111-1111-1111-1111-111111111111"
   ```

8. Anmelden (öffnet den Browser; danach bleibt die Anmeldung im DPAPI-verschlüsselten
   Token-Cache gespeichert):

   ```powershell
   .venv\Scripts\python -m callrec login
   .venv\Scripts\python -m callrec event     # zeigt, welcher Termin jetzt gewählt würde
   ```

   Alternativ Tray-Menü → **Bei Microsoft anmelden …**

**Terminauswahl:** Berücksichtigt werden Termine, die gerade laufen, in den nächsten
10 Minuten beginnen oder vor höchstens 10 Minuten endeten (einstellbar). Abgesagte,
abgelehnte, ganztägige und „frei“-Termine werden ignoriert. Bei Überschneidungen gewinnt:
Online-Besprechung → läuft tatsächlich → Beginn am nächsten an „jetzt“ → kürzerer Termin →
mehr Teilnehmende. Ist der Kalender offline, wird der zuletzt abgerufene Stand verwendet;
sonst Eingabe von Hand oder `.ics`-Import.

---

## 6. Ollama für die Zusammenfassung

1. Ollama für Windows installieren: <https://ollama.com/download> (oder
   `winget install Ollama.Ollama`). Ollama läuft danach im Hintergrund auf
   `http://127.0.0.1:11434`.
2. Modell laden (Standard in der Konfiguration: `gemma3:12b`, ca. 8 GB):

   ```powershell
   ollama pull gemma3:12b
   ```

   Bei weniger Grafikspeicher z. B. `gemma3:4b` oder `qwen2.5:7b` laden und in
   `config.toml` unter `[summary] model = "..."` eintragen.
3. Läuft Ollama nicht oder fehlt das Modell, wird die Zusammenfassung **übersprungen** (mit
   Hinweis im Tray und Vermerk in `meta.json`). Später nachholen:
   Tray-Menü **„Letzte Aufnahme: Zusammenfassung neu erstellen“** oder

   ```powershell
   .venv\Scripts\python -m callrec summarize "C:\Users\<Sie>\Anrufaufnahmen\2026-11-19_1400_Projekt-Jour-fixe"
   ```

Aus Datenschutzgründen akzeptiert CallRecorder nur Ollama-Adressen auf diesem Rechner
(`localhost`, `127.0.0.1`, `::1`); Proxy-Einstellungen werden dafür ignoriert.

---

## 7. Erster Start und Bedienung

Start über **Startmenü → CallRecorder**. Das Symbol erscheint im Infobereich (ggf. über
„^“ einblenden und in die Taskleiste ziehen).

| Symbol | Bedeutung |
|---|---|
| grau mit Mikrofon | bereit |
| **rot mit weißem Punkt** + roter Balken oben rechts | **Aufnahme läuft** |
| orange | Verarbeitung läuft (Fortschritt im Tooltip) |

**Tastenkürzel:** **Strg+Alt+Umschalt+R** startet bzw. beendet die Aufnahme
(änderbar unter *Einstellungen* oder `[hotkey] toggle`).

**Tray-Menü:**

- *Aufnahme starten … / Aufnahme beenden*
- *Letzte Aufnahme: Sprecher zuordnen …*
- *Letzte Aufnahme: Zusammenfassung neu erstellen*
- *Letzte Aufnahme: komplett neu verarbeiten*
- *Aufnahmeordner öffnen*
- *Stimmprofile verwalten …*
- *Einstellungen …* (Mikrofon, Ausgabegerät, Name, Ordner, Sprache, Modelle, Tastenkürzel)
- *Bei Microsoft anmelden …*, *Protokolldatei öffnen*, *Beenden*

**Aufnahmedialog:** Titel und Teilnehmende sind aus dem Kalender vorausgefüllt und können
geändert werden (eine Person pro Zeile, optional `Name <mail@firma.de>`). Mit
**„.ics-Einladung importieren …“** lässt sich eine Einladung aus Outlook laden
(Termin öffnen → Datei → Speichern unter → `.ics`). Die Schaltfläche **„Aufnahme
starten“** wird erst aktiv, wenn die Einwilligung bestätigt ist.

**Erinnerung:** Öffnet Teams (`ms-teams.exe` bzw. klassisch `Teams.exe`) eine aktive
Mikrofon-Sitzung oder beginnt ein Online-Termin, erscheint unten rechts die Frage
„Gespräch aufnehmen?“. „Aufnahme vorbereiten …“ öffnet den normalen Dialog mit
Einwilligungsabfrage; nach „Nein“ ist 10 Minuten Ruhe.

**Absturz / Stromausfall:** Beim nächsten Start werden unterbrochene Aufnahmen automatisch
repariert und verarbeitet (oder `callrec repair <Ordner>`).

---

## 8. Sprecher zuordnen und Stimmprofile

Nach der Verarbeitung öffnet sich – falls es unbekannte Stimmen gibt – der Dialog
**„Sprecher zuordnen“**:

- je Stimme: Redezeit, ein Beispielsatz und **„▶ Anhören“** (spielt einen kurzen Ausschnitt),
- Auswahlliste mit den Eingeladenen und bekannten Profilen; freie Eingabe ist möglich,
- **„Stimme merken“**: legt das Stimmprofil an bzw. verbessert ein bestehendes,
- **„Übernehmen“** aktualisiert `transcript.md` (und auf Wunsch `summary.md`).

Werden zwei „Sprecher“ derselben Person zugeordnet (die Sprechertrennung teilt eine Stimme
gelegentlich auf), werden sie im Transkript zusammengeführt.

Bei späteren Gesprächen wird jede Stimme mit den Profilen verglichen (Kosinus-Ähnlichkeit,
Schwelle `speakers.match_threshold`, Standard 0,55). Eingeladene Personen erhalten einen
kleinen Bonus (`invitee_bonus`). Bei sicherer Erkennung (≥ `auto_update_threshold`) wird das
Profil automatisch nachgeschärft. Ihre eigene Stimme wird nie einem Sprecher der Gegenseite
zugeordnet.

Profile ansehen/löschen: Tray-Menü **„Stimmprofile verwalten …“** oder

```powershell
.venv\Scripts\python -m callrec profiles list
.venv\Scripts\python -m callrec profiles delete "Anna Beispiel"
.venv\Scripts\python -m callrec profiles delete-all
```

---

## 9. Ergebnisse einer Aufnahme

```
%USERPROFILE%\Anrufaufnahmen\2026-11-19_1400_Projekt-Jour-fixe\
├── audio\
│   ├── mic.flac          Ihre Spur
│   ├── remote.flac       Gegenseite (Loopback)
│   └── mix.flac          beide Spuren gemischt (zum Anhören)
├── transcript.md         Transkript mit Namen und Zeitstempeln
├── summary.md            Kurzfassung, Entscheidungen, Aufgaben (mit Verantwortlichen), offene Punkte
├── meta.json             Termin, Teilnehmende, Einwilligung, Geräte, Modelle, Verarbeitungsstatus
└── data\                 Zwischenergebnisse für die Nachbearbeitung
    ├── asr_mic.json / asr_remote.json   Whisper-Ergebnis mit Wort-Zeitstempeln
    ├── diarization.json                 Sprecherabschnitte der Gegenseite
    ├── speakers.json                    Zuordnung Sprecher → Name
    ├── utterances.json                  zusammengeführte Äußerungen
    └── speaker_embeddings.npz           Stimm-Embeddings dieses Gesprächs (biometrisch!)
```

Während der Aufnahme liegen dort `audio\mic.part.wav` und `audio\remote.part.wav`; sie
werden nach erfolgreicher Umwandlung gelöscht (`audio.keep_wav = true` behält sie).

---

## 10. Kommandozeile

Alle Befehle mit `.venv\Scripts\python -m callrec …` (bzw. `callrec …` in der aktivierten
Umgebung):

| Befehl | Zweck |
|---|---|
| `tray` (oder ohne Argument) | Tray-Anwendung starten |
| `record [--title T] [--participants "A <a@x>; B"] [--ics F] [--no-calendar]` | Aufnahme in der Konsole (Einwilligung durch Eingabe von `ja`) |
| `process <Ordner> [--steps …] [--model M] [--device cuda\|cpu] [--language de\|auto]` | (Neu-)Verarbeitung; Schritte: `finalize,transcribe,diarize,identify,render,summarize` oder Presets `all`, `retranscribe`, `rediarize`, `resummarize`, `render` |
| `retranscribe <Ordner>` | neu transkribieren (inkl. Transkript und Zusammenfassung) |
| `summarize <Ordner>` | Zusammenfassung neu erstellen |
| `repair <Ordner>` | unterbrochene Aufnahme reparieren und Audio abschließen |
| `speakers <Ordner>` | erkannte Sprecher anzeigen |
| `assign <Ordner> S1="Anna Beispiel" S2="Bernd" [--no-profile] [--resummarize]` | Sprecher zuordnen (ohne Dialog) |
| `review <Ordner>` | Zuordnungsdialog öffnen |
| `profiles list \| delete <Name/E-Mail/ID> \| delete-all` | Stimmprofile verwalten |
| `purge-voiceprints <Ordner> \| --all` | Stimm-Embeddings in Aufnahmeordnern löschen |
| `login`, `logout`, `whoami` | Microsoft-Anmeldung |
| `event [--ics F]` | zeigt den Termin, der jetzt gewählt würde |
| `devices` | Audiogeräte und aktuelle Auswahl |
| `models download [--whisper]` | Modelle vorab laden |
| `list` | Aufnahmen und Status |
| `config path \| check` | Konfigurationsdatei anzeigen/prüfen |

---

## 11. Konfiguration

Datei: `%APPDATA%\CallRecorder\config.toml` (wird beim ersten Start mit Kommentaren angelegt).
Wichtige Einstellungen:

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `general.user_name` | leer | Ihr Name im Transkript (leer = aus Microsoft-Konto) |
| `general.recordings_dir` | leer | Ablage (leer = `%USERPROFILE%\Anrufaufnahmen`; bitte nicht in OneDrive) |
| `audio.mic_device` | `auto` | `auto` = Gerät, das Teams gerade nutzt, sonst Windows-Standard für Kommunikation; oder (Teil des) Gerätenamens |
| `audio.loopback_device` | `auto` | dasselbe für das Ausgabegerät (Headset) |
| `audio.format` | `flac` | `flac` oder `opus` |
| `transcription.model` | `large-v3` | z. B. `large-v3-turbo`, `medium` |
| `transcription.device` | `auto` | `auto`/`cuda` (mit CPU-Rückfall) oder `cpu` |
| `transcription.language` | `de` | oder `auto` für automatische Erkennung |
| `diarization.cluster_threshold` | `0.4` | kleiner = Stimmen werden feiner getrennt |
| `diarization.num_speakers` | `0` | feste Sprecheranzahl der Gegenseite (0 = automatisch) |
| `speakers.match_threshold` | `0.55` | Schwelle für die Wiedererkennung |
| `summary.model` | `gemma3:12b` | Ollama-Modell |
| `detection.teams_processes` | `["ms-teams.exe", "teams.exe"]` | Prozessnamen für die Erinnerung |
| `hotkey.toggle` | `ctrl+alt+shift+r` | z. B. `ctrl+shift+f9`, `win+alt+r` |

Eine Einstellung zum Abschalten der Einwilligungsabfrage gibt es nicht; entsprechende
Einträge werden ignoriert und gemeldet.

---

## 12. Wo liegen welche Daten?

| Pfad | Inhalt |
|---|---|
| `%APPDATA%\CallRecorder\config.toml` | Konfiguration |
| `%LOCALAPPDATA%\CallRecorder\logs\callrec.log` | Protokoll (rotierend, 5 × 5 MB) |
| `%LOCALAPPDATA%\CallRecorder\msal_token_cache.bin` | Microsoft-Anmeldung (DPAPI-verschlüsselt) |
| `%LOCALAPPDATA%\CallRecorder\voiceprints.bin` | **Stimmprofile** (DPAPI-verschlüsselt) |
| `%LOCALAPPDATA%\CallRecorder\models\` | Whisper- und Sprechertrennungsmodelle |
| `%LOCALAPPDATA%\CallRecorder\calendar_cache.json`, `me.json` | zuletzt abgerufene Termine, Ihr Name |
| `%USERPROFILE%\Anrufaufnahmen\` | Aufnahmen (siehe Abschnitt 9) |

**Was verlässt den Rechner?** Nur Anfragen an `login.microsoftonline.com` und
`graph.microsoft.com` (Anmeldung, Termin-Metadaten, Ihr Name) sowie einmalige
Modell-Downloads von `huggingface.co` (Whisper) und `github.com` (Sprechertrennung,
SHA-256-geprüft). Audio, Transkripte, Zusammenfassungen und Stimmprofile nicht.

**Alles löschen:** CallRecorder beenden, die Ordner `%APPDATA%\CallRecorder`,
`%LOCALAPPDATA%\CallRecorder` und den Aufnahmeordner löschen.

---

## 13. Fehlerbehebung

Zuerst ins Protokoll schauen: Tray-Menü **„Protokolldatei öffnen“**. Für die Transkription
gibt es zusätzlich `data\asr_mic.log` / `data\asr_remote.log` im Aufnahmeordner.

**Gegenseite ist stumm / falsches Gerät.** CallRecorder schneidet das Ausgabegerät mit, auf
dem Teams aktiv spielt, sonst das Windows-Standardgerät für *Kommunikation*. Stellt Teams
ein anderes Gerät ein als Windows, das Gerät unter *Einstellungen → Ausgabegerät* fest
wählen. `callrec devices` zeigt die erkannten Geräte und die Auswahl.
Bluetooth-Headsets haben zwei Ausgabegeräte („Kopfhörer“ und „Headset … Hands-Free“); im
Gespräch nutzt Teams meist das *Hands-Free*-Gerät.

**Mikrofon nimmt nichts auf.** *Windows-Einstellungen → Datenschutz und Sicherheit →
Mikrofon*: „Desktop-Apps den Zugriff auf das Mikrofon erlauben“ aktivieren. Wenn ein
Gerät „exklusiv“ belegt ist: *Systemsteuerung → Sound → Gerät → Eigenschaften → Erweitert*
→ „Anwendungen haben alleinige Kontrolle …“ deaktivieren.

**Meldung „Stream ist nicht mehr aktiv (Gerät getrennt?)“.** Das Headset wurde während der
Aufnahme getrennt oder gewechselt. Die Aufnahme bis dahin bleibt erhalten; Aufnahme
beenden und neu starten.

**Transkription läuft nur auf der CPU / sehr langsam.** Im Protokoll steht dann
„Transkription auf CUDA fehlgeschlagen“. Prüfen: `nvidia-smi` funktioniert, Setup wurde
ohne `-NoCuda` ausgeführt, Treiber aktuell. Test:
`.venv\Scripts\python -m callrec retranscribe <Ordner>` und die Datei `data\asr_mic.log`
lesen (z. B. „cudnn_ops64_9.dll not found“ → `setup.ps1` erneut ausführen).
Zu wenig Grafikspeicher → `transcription.model = "large-v3-turbo"` oder
`compute_type = "int8_float16"`.

**Zusammenfassung fehlt.** Ollama läuft nicht oder das Modell fehlt (`ollama list`).
Danach `callrec summarize <Ordner>`.

**Anmeldung schlägt fehl.**
- `AADSTS50011` (Redirect-URI) → in der App-Registrierung `http://localhost` unter
  *Mobilgerät- und Desktopanwendungen* eintragen.
- `AADSTS65001` (Zustimmung fehlt) → Administratorzustimmung erteilen.
- `AADSTS7000218` → *Öffentliche Clientflows zulassen* auf *Ja*.
- `AADSTS700016` (App nicht gefunden) → `client_id`/`tenant_id` prüfen.
Abmelden und neu anmelden: `callrec logout`, dann `callrec login`.

**Tastenkürzel funktioniert nicht.** Ein anderes Programm nutzt die Kombination (Hinweis
beim Start). Anderes Kürzel wählen und CallRecorder neu starten.

**Keine Teams-Erinnerung.** Das neue Teams heißt `ms-teams.exe`. Im Task-Manager prüfen, wie
der Prozess bei Ihnen heißt, und `detection.teams_processes` anpassen. Die Erinnerung ist
nur ein Komfort – Start per Tastenkürzel funktioniert immer.

**Falsche oder zu viele Sprecher.** `diarization.cluster_threshold` anpassen (größer =
weniger Sprecher) oder bei bekannter Anzahl `diarization.num_speakers` setzen, dann
`callrec process <Ordner> --steps rediarize`.

**„CallRecorder läuft bereits“.** Es darf nur eine Instanz laufen; ggf. im Task-Manager
`pythonw.exe` beenden.

**PowerShell blockiert das Setup.** Mit `-ExecutionPolicy Bypass` aufrufen (siehe oben).

---

## 14. Für Entwickler

Aufbau (`src/callrec/`):

| Modul | Aufgabe |
|---|---|
| `audio/capture.py` | plattformunabhängiger Zwei-Spur-Rekorder, Lückenfüllung, Zeitbasis |
| `audio/wavwriter.py` | absturzsichere WAV-Datei mit periodischem Header-Update, Reparatur |
| `audio/wasapi.py` | **Windows:** PyAudioWPatch (WASAPI/Loopback), pycaw (Standardgeräte, Teams-Sitzungen) |
| `audio/finalize.py` | FLAC/Opus, Mix, Laden/Resampling |
| `calendar/` | Graph (MSAL, DPAPI-Cache), `.ics`, Terminauswahl, Offline-Cache |
| `consent.py` | Einwilligungs-Gate |
| `asr/` | faster-whisper im Kindprozess, CUDA-DLL-Pfade, CPU-Rückfall |
| `speakers/` | sherpa-onnx-Diarisierung, Embeddings, Stimmprofile, Wort→Sprecher |
| `transcript.py`, `summary.py` | Zusammenführen/Markdown, Ollama |
| `pipeline.py`, `jobs.py` | Verarbeitungsschritte, Hintergrund-Warteschlange |
| `controller.py` | Ablaufsteuerung der App (UI-unabhängig) |
| `ui/` | Tk-Dialoge, Overlay, pystray-Tray, App-Verdrahtung |
| `detect.py`, `hotkey.py` | Erinnerungslogik, globales Tastenkürzel (RegisterHotKey) |

Tests (laufen auch unter Linux/macOS; Windows-spezifisches ist gekapselt):

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy
```

`tests/test_integration_sherpa.py` prüft die echte Sprechertrennung und Wiedererkennung mit
synthetischer Sprache; es läuft nur, wenn `CALLREC_INTEGRATION_MODELS` auf einen Ordner mit
den Modellen und vorbereiteten Sprachdateien zeigt (siehe Docstring).
