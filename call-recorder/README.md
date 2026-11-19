# CallRecorder – lokale Aufnahme, Transkription und Zusammenfassung von Teams-Gesprächen

CallRecorder ist ein kleines Windows-11-Programm im Infobereich (Tray). Es nimmt Gespräche
– typischerweise Microsoft-Teams-Anrufe über ein Headset – **mit Einwilligung aller
Teilnehmenden** auf, ohne die Aufzeichnungsfunktion von Teams zu nutzen, und erstellt
anschließend **lokal** (die Zusammenfassung wahlweise auch mit Claude über die Anthropic-API,
siehe unten):

- ein Transkript mit Sprechernamen und Zeitstempeln (`transcript.md`),
- ein kurzes Ergebnisprotokoll mit Entscheidungen und Aufgaben (`summary.md`),
- Metadaten inkl. Einwilligungsbestätigung (`meta.json`),
- die Audiospuren (FLAC oder Opus).

Audio und Stimmprofile verlassen den Rechner nie. Transkripte bleiben lokal – **außer** Sie
wählen ausdrücklich Claude (Anthropic) als Anbieter für die Zusammenfassung (Abschnitt 6a): Dann
wird für jede Zusammenfassung der Transkripttext an Anthropic übertragen. Das ist eine bewusste,
gesondert bestätigte Einstellung (nie per Preset), die Einwilligungsabfrage nennt sie dann
ausdrücklich. Weitere Netzwerkzugriffe gibt es nur für den Kalender (Microsoft Graph, nur
Termin-Metadaten), für das Herunterladen der Modelle – und für **einen bewusst ausgelösten
Schritt**: Auf Klick kann die **Zusammenfassung** (nur sie) an einen SharePoint-Ordner übergeben
werden, damit z. B. ein Microsoft-365-Assistent sie lesen kann (Abschnitt 10). Dieser Schritt
geschieht nie automatisch.

---

## Inhalt

1. [Rechtlicher Hinweis](#1-rechtlicher-hinweis)
2. [So funktioniert es](#2-so-funktioniert-es)
3. [Voraussetzungen](#3-voraussetzungen)
4. [Installation](#4-installation)
5. [Entra-App-Registrierung (Kalender, optional SharePoint-Upload)](#5-entra-app-registrierung-kalender-optional-sharepoint-upload)
6. [Sprachmodell für die Zusammenfassung](#6-sprachmodell-für-die-zusammenfassung)
   (6a. [Claude über die Anthropic-API](#6a-claude-über-die-anthropic-api-cloud))
7. [Erster Start und Bedienung](#7-erster-start-und-bedienung)
8. [Sprecher zuordnen und Stimmprofile](#8-sprecher-zuordnen-und-stimmprofile)
9. [Transkript schrittweise verbessern](#9-transkript-schrittweise-verbessern)
10. [Zusammenfassung an SharePoint übergeben](#10-zusammenfassung-an-sharepoint-übergeben)
11. [Ergebnisse einer Aufnahme](#11-ergebnisse-einer-aufnahme)
12. [Kommandozeile](#12-kommandozeile)
13. [Konfiguration](#13-konfiguration)
14. [Wo liegen welche Daten?](#14-wo-liegen-welche-daten)
15. [Fehlerbehebung](#15-fehlerbehebung)
16. [Für Entwickler](#16-für-entwickler)

---

## 1. Rechtlicher Hinweis

**Einwilligung (§ 201 StGB).** Wer das nichtöffentlich gesprochene Wort eines anderen
unbefugt aufnimmt, macht sich strafbar. Nehmen Sie deshalb nur auf, wenn **alle**
Teilnehmenden vorher informiert wurden und **ausdrücklich zugestimmt** haben – am besten
zu Beginn des Gesprächs, sodass die Zustimmung auch in der Aufnahme enthalten ist. Wenn Sie
Zusammenfassungen an SharePoint übergeben (Abschnitt 10), sollten die Teilnehmenden auch
darüber informiert sein.
CallRecorder fragt vor **jeder** Aufnahme nach dieser Bestätigung. Ohne Häkchen startet
keine Aufnahme; die Abfrage lässt sich bewusst nicht per Konfiguration abschalten.
Die Bestätigung wird mit Zeitstempel, Ihrem Namen, der Teilnehmerliste und dem zu diesem Zeitpunkt
eingestellten Zusammenfassungs-Anbieter (`summary_provider`) in `meta.json` gespeichert. Die
Verantwortung, dass die Zustimmung tatsächlich vorliegt, bleibt bei Ihnen.

**Zusammenfassung durch Claude (Anthropic).** Ist dieser Anbieter gewählt (Abschnitt 6a), lautet
der Einwilligungstext vor jeder Aufnahme (Dialog und Konsole) ausdrücklich, dass das Transkript zur
Zusammenfassung an Anthropic übertragen wird, und die Aufnahme startet nur mit diesem Text. Für
ältere Aufnahmen, deren Einwilligung den Hinweis nicht enthält, wird nichts übertragen, bis Sie
die Zustimmung nachträglich gesondert bestätigen (wird in `meta.json` unter
`consent.additional_confirmations` vermerkt). Klären Sie vorab, ob die Übertragung an einen
Cloud-Anbieter mit Ihren Datenschutzpflichten vereinbar ist (Auftragsverarbeitung, Verarbeitung
außerhalb der EU, Vertraulichkeit; maßgeblich sind die kommerziellen Bedingungen von Anthropic).

**Datenschutz (DSGVO).** Aufnahmen, Transkripte und Protokolle sind personenbezogene
Daten. Informieren Sie die Teilnehmenden über Zweck, Speicherdauer und – bei Claude – über die
Übertragung an Anthropic und löschen Sie Aufnahmen, wenn sie nicht mehr benötigt werden.

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
   FLAC/Opus erzeugen → Transkription mit faster-whisper (NVIDIA-GPU, sonst direkt CPU) →
   Sprechertrennung der Gegenseite (sherpa-onnx) → Abgleich mit gespeicherten Stimmprofilen →
   `transcript.md` → Zusammenfassung lokal über Ollama/OpenAI-kompatiblen Server **oder** – nur
   wenn ausdrücklich gewählt – über Claude (Anthropic-API; dafür verlässt der Transkripttext den
   Rechner) → `summary.md`.
6. **Unbekannte Stimmen** erscheinen als „Sprecher 1 … n“. Der Zuordnungsdialog öffnet sich
   automatisch; einmal zugeordnete Stimmen werden beim nächsten Gespräch erkannt.

Die Mikrofonspur ist immer **Sie** (Name aus dem Microsoft-Konto bzw. den Einstellungen).

---

## 3. Voraussetzungen

- Windows 11, ein Benutzerkonto mit Administratorrechten (nur für die Installation von
  Python/Treibern nötig)
- **Python 3.12 oder 3.13** (64 Bit) von python.org oder `winget install Python.Python.3.12`
  (mit „tcl/tk“, ist Standard). Python 3.11 geht nicht (numpy 2.5 gibt es dafür nicht).
- Visual C++ Runtime 2015–2022 x64 (`msvcp140.dll`, für die Transkription; meist schon
  vorhanden, sonst `winget install Microsoft.VCRedist.2015+.x64` – `setup.ps1` warnt)
- Optional: NVIDIA-Grafikkarte mit aktuellem Treiber (CUDA 12 kompatibel, Treiber ≥ 525).
  Für `large-v3` werden ca. 5 GB Grafikspeicher benötigt. **Ohne NVIDIA-GPU** (z. B. nur
  AMD Radeon oder Intel-Grafik) erkennt CallRecorder das einmalig und transkribiert direkt auf der
  CPU; mit `transcription.model = "auto"` (Standard) dann mit `large-v3-turbo` (int8). Zum
  Vergleich: Auf einem Rechner ohne NVIDIA-GPU brauchte `large-v3` ca. 45 s für 19 s Audio.
- Optional: [Ollama](https://ollama.com) für die lokale Zusammenfassung **oder** ein
  Anthropic-API-Konto für Claude (Abschnitt 6a)
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
   - NVIDIA-Bibliotheken (ca. 1,5 GB) werden **nur installiert, wenn eine NVIDIA-Grafikkarte
     erkannt wird** (Grafikadapter bzw. `nvidia-smi`).
     `-NoCuda` – nie installieren; `-WithCuda` – trotzdem installieren
   - `-PythonVersion 3.13` – andere Python-Version (nur 3.12 oder 3.13)

   Das Skript legt `.venv` an, installiert alle Pakete in exakt gepinnten Versionen
   (`requirements.txt`, `requirements-cuda.txt`, `constraints-win.txt`), prüft die Importe
   und die GPU (Ergebnis wird gemerkt, `callrec gpu`), legt die Konfiguration an, lädt die
   Sprechertrennungsmodelle und erstellt eine Startmenü-Verknüpfung „CallRecorder“.

   **Bestehende Installation ohne NVIDIA-GPU:** In einer älteren `config.toml` steht
   `model = "large-v3"` – das gilt als bewusste Wahl und bleibt (im Protokoll steht dann ein
   Hinweis). Für die automatische Wahl `model = "auto"` eintragen bzw. in den Einstellungen
   *Transkription → Whisper-Modell* „auto“ wählen.

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

## 5. Entra-App-Registrierung (Kalender, optional SharePoint-Upload)

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

### Optional: Berechtigung für den direkten SharePoint-Upload

Nur nötig, wenn Sie die Zusammenfassung **per Graph-Upload** übergeben wollen
(`[handoff] mode = "graph"`). Für das empfohlene Standardverfahren – Kopieren in einen von
OneDrive synchronisierten Ordner – braucht die App **keine** zusätzliche Berechtigung.

CallRecorder fordert die Datei-Berechtigung **erst beim ersten Upload** an (inkrementelle
Zustimmung); die normale Anmeldung fragt weiterhin nur `Calendars.Read` und `User.Read` an.

Empfohlen (am engsten): **`Sites.Selected` (delegiert)**. Damit darf die App nur in
SharePoint-Websites schreiben, die ihr ein Administrator ausdrücklich freigegeben hat – und
auch dort nur, soweit Ihr eigenes Benutzerkonto Zugriff hat.

1. App-Registrierung → **API-Berechtigungen → Berechtigung hinzufügen → Microsoft Graph →
   Delegierte Berechtigungen** → `Sites.Selected` → Administratorzustimmung erteilen.
2. Website-ID und Bibliotheks-(Laufwerks-)ID ermitteln, z. B. im
   [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer) (als Administrator angemeldet):
   - `GET https://graph.microsoft.com/v1.0/sites/firma.sharepoint.com:/sites/Team` → `id`
   - `GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives` → `id` der Bibliothek
     (z. B. „Dokumente“; beginnt meist mit `b!`)
3. Der App Schreibrecht **auf genau diese Website** geben (benötigt im Graph Explorer
   `Sites.FullControl.All` und eine SharePoint-Administratorrolle):

   ```http
   POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
   Content-Type: application/json

   { "roles": ["write"],
     "grantedToIdentities": [{ "application": { "id": "<client_id>", "displayName": "CallRecorder (lokal)" } }] }
   ```

4. In `config.toml`:

   ```toml
   [handoff]
   mode = "graph"
   graph_drive_id = "b!...."
   graph_folder = "Gesprächsprotokolle"
   graph_scope = "Sites.Selected"
   ```

Einfachere, aber **breitere** Alternative: `graph_scope = "Files.ReadWrite.All"` (delegiert;
die App kann dann alle Dateien lesen und schreiben, auf die Sie Zugriff haben). Schritt 3
entfällt. Hochgeladen wird mit `PUT /drives/{drive-id}/root:/{Ordner}/{Datei}:/content`
(eine vorhandene Datei gleichen Namens wird ersetzt).

**Terminauswahl:** Berücksichtigt werden Termine, die gerade laufen, in den nächsten
10 Minuten beginnen oder vor höchstens 10 Minuten endeten (einstellbar). Abgesagte,
abgelehnte, ganztägige und „frei“-Termine werden ignoriert. Bei Überschneidungen gewinnt:
Online-Besprechung → läuft tatsächlich → Beginn am nächsten an „jetzt“ → kürzerer Termin →
mehr Teilnehmende. Ist der Kalender offline, wird der zuletzt abgerufene Stand verwendet;
sonst Eingabe von Hand oder `.ics`-Import.

---

## 6. Sprachmodell für die Zusammenfassung

Die Zusammenfassung erzeugt standardmäßig ein **lokal laufendes** Sprachmodell. Drei Anbieter
sind möglich (`[summary] provider`):

| Anbieter | Typische Adresse | Hinweis |
|---|---|---|
| `ollama` (Standard) | `http://127.0.0.1:11434` | Modelle direkt aus CallRecorder laden/aktualisieren |
| `openai` – beliebiger OpenAI-kompatibler Server | LM Studio: `http://127.0.0.1:1234/v1`, llama.cpp `llama-server`: `http://127.0.0.1:9931/v1` (Standardport laut aktueller llama.cpp-Doku) | Modelle in der Oberfläche des Servers laden |
| `anthropic` – Claude (Cloud) | fest `https://api.anthropic.com` | **Transkript verlässt den Rechner**, nur mit Bestätigung, Abschnitt 6a |

Ohne NVIDIA-GPU sind lokale Sprachmodelle sehr langsam; dann ist Claude (6a) die schnellere
Alternative – mit den genannten Folgen für den Datenschutz.

**Ollama einrichten**

1. Installieren: <https://ollama.com/download> oder `winget install Ollama.Ollama`.
2. Modell wählen und laden – entweder in CallRecorder unter **Einstellungen →
   Zusammenfassung** („Installierte Modelle abfragen“, Modell eintragen, „Modell laden /
   aktualisieren“ mit Fortschrittsanzeige) oder in PowerShell:

   ```powershell
   ollama pull gemma3:12b          # Standard, ca. 8 GB
   .venv\Scripts\python -m callrec llm models
   .venv\Scripts\python -m callrec llm pull gemma3:4b   # kleiner, für wenig Grafikspeicher
   ```

   Ein erneutes „Laden“ eines vorhandenen Modells aktualisiert es auf den neuesten Stand.
3. **Ollama selbst aktualisieren:** CallRecorder zeigt die installierte Version an
   (`callrec llm version`), aktualisiert aber keine Software von sich aus. Update:

   ```powershell
   winget upgrade Ollama.Ollama
   ```

Läuft der Server nicht oder fehlt das Modell, wird die Zusammenfassung **übersprungen** (mit
Hinweis im Tray und Vermerk in `meta.json`). Später nachholen: Tray-Menü
**„Letzte Aufnahme: Zusammenfassung neu erstellen“** oder `callrec summarize <Ordner>`.

**Nur lokal (Ollama/OpenAI-kompatibel):** CallRecorder akzeptiert für diese beiden Anbieter nur Adressen auf diesem Rechner (`localhost`,
`127.x.x.x`, `::1`) und ignoriert dafür Proxy-Einstellungen. Weiterleitungen (HTTP 3xx)
werden nie befolgt; mehrdeutige Adressen (mit `@`, `\` oder Leerzeichen) werden abgelehnt.
Adresse und Freigabe lassen sich nur in `config.toml` ändern, nicht über ein Preset. Ein Server auf einem anderen
Rechner lässt sich nur mit der absichtlich deutlich benannten Einstellung
`allow_remote_endpoint_data_leaves_this_computer = true` nutzen – dann **verlassen
Gesprächsinhalte diesen Rechner**; das Protokoll und die Einstellungen weisen darauf hin, und
`summary.md` vermerkt es.

**Anweisungen anpassen:** Die Texte, die das Modell bekommt, liegen als bearbeitbare Dateien in
`%APPDATA%\CallRecorder\prompts\` (siehe Abschnitt 9).

**Fortschritt:** Während das Modell arbeitet, zeigen Tray-Tooltip und Konsole die verstrichene
Zeit („Protokoll: warte auf ollama (gemma3:12b) – 95 s“), bei Claude zusätzlich die bereits
empfangenen Zeichen.

### 6a. Claude über die Anthropic-API (Cloud)

> **Achtung:** Mit diesem Anbieter wird für jede Zusammenfassung der **vollständige
> Transkripttext** (mit Sprechernamen und Zeitstempeln) sowie Titel, Datum und die Namen der
> Teilnehmenden an Anthropic übertragen. **Nie** übertragen werden Audio, Stimmprofile,
> Sprecher-Embeddings, E-Mail-Adressen und `meta.json`.

1. **Konto und Schlüssel:** Unter <https://console.anthropic.com> anmelden, unter *Billing*
   Guthaben aufladen und unter *API Keys* einen Schlüssel erstellen (beginnt mit `sk-ant-`).
   Abgerechnet wird **pro Token** (Eingabe und Ausgabe) über Ihr Anthropic-Konto; Preise siehe
   Anthropic-Website. Zur Orientierung (Stand 11/2026, je 1 Mio. Token Eingabe/Ausgabe):
   Claude Opus 5.5 4 $/20 $, Sonnet 5.5 2 $/10 $, Haiku 5.5 0,10 $/0,50 $. Ein einstündiges
   Gespräch hat grob 15 000–25 000 Token Transkript. Die Token je Zusammenfassung stehen in
   `meta.json` unter `processing.summary.usage`.
2. **Aktivieren:** *Einstellungen → Zusammenfassung*: Anbieter `anthropic` wählen, Schlüssel
   eintragen, **„Schlüssel speichern“**, **„Verbindung testen“** (sendet nur „Antworte nur mit: OK“
   und prüft Schlüssel, Modell, Netz und Guthaben – kostet einen Bruchteil eines Cents), dann
   **„Speichern“** und die Rückfrage „Transkripte an Anthropic senden?“ mit *Ja* bestätigen.
   Oder in PowerShell:

   ```powershell
   .venv\Scripts\python -m callrec anthropic set-key     # Eingabe unsichtbar
   .venv\Scripts\python -m callrec anthropic enable      # zeigt den Hinweis, Bestätigung mit "ja"
   .venv\Scripts\python -m callrec anthropic test
   .venv\Scripts\python -m callrec anthropic status
   .venv\Scripts\python -m callrec anthropic disable     # zurück zu Ollama, Freigabe widerrufen
   ```

   Ein Preset kann diesen Anbieter **nie** auswählen oder freigeben (wird ignoriert und gemeldet).
   Wechseln Sie in den Einstellungen auf einen lokalen Anbieter, wird die Freigabe widerrufen.
3. **Wo liegt der Schlüssel?** Nicht in `config.toml`, nicht in Presets, nicht im Protokoll,
   sondern in `%LOCALAPPDATA%\CallRecorder\anthropic_api_key.bin`, mit Windows-DPAPI
   verschlüsselt (nur Ihr Windows-Konto auf diesem Rechner kann ihn lesen). Alternativ wird die
   Umgebungsvariable `ANTHROPIC_API_KEY` verwendet (der gespeicherte Schlüssel hat Vorrang).
   Löschen: `callrec anthropic delete-key` bzw. „Schlüssel löschen“.
4. **Modell:** Standard ist `claude-opus-5-5` (beste Qualität); in den Einstellungen sind
   `claude-sonnet-5-5` und `claude-haiku-5-5` (günstiger) auswählbar, `anthropic_effort` steuert
   die Denktiefe (`medium` = Standard). Bei einer Ablehnung durch die Sicherheitsfilter von
   Anthropic darf die API automatisch ein Ersatzmodell nutzen (`anthropic_refusal_fallback`); das
   tatsächlich verwendete Modell steht in `summary.md` und `meta.json` (`served_model`).
5. **Technik:** Verbindungen nur zu `https://api.anthropic.com` (fest eingestellt, keine
   konfigurierbare Adresse, `ANTHROPIC_BASE_URL` wird ignoriert, Weiterleitungen werden nicht
   befolgt). Bei Überlastung (529), Ratenlimit (429) oder Netzproblemen wird mit Wartezeit
   mehrfach wiederholt; danach wird die Zusammenfassung übersprungen und kann später neu erstellt
   werden. Sehr lange Transkripte werden automatisch in Teile zerlegt
   (`anthropic_max_chunk_chars`). Fehlermeldungen (Schlüssel ungültig, kein Guthaben,
   Ausgabenlimit, keine Verbindung) erscheinen auf Deutsch im Tray und in `meta.json`.
6. **Ältere Aufnahmen** (Einwilligung ohne Anthropic-Hinweis) werden erst nach einer gesonderten
   Bestätigung übertragen: Tray „Zusammenfassung neu erstellen“ fragt nach, auf der Konsole
   `callrec summarize <Ordner> --confirm-anthropic`.

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
- *Laufende Verarbeitung abbrechen* (Transkription bzw. Modell-Download)
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

## 9. Transkript schrittweise verbessern

Alle Stellschrauben gibt es in **Einstellungen** (Reiter *Transkription*, *Sprecher*,
*Darstellung*, *Zusammenfassung*) und als einfache Textdateien:

| Datei (`%APPDATA%\CallRecorder\…`) | Inhalt |
|---|---|
| `config.toml` | alle Einstellungen, siehe Abschnitt 13 |
| `glossary.txt` | Glossar: ein Begriff pro Zeile (Namen, Firma, Produkte, Fachwörter) – wird Whisper als „Hotwords“ mitgegeben |
| `replacements.txt` | Korrekturliste für wiederkehrende Fehler: `falsch => richtig` (ganzes Wort, Groß/Klein egal) oder `re: Ausdruck => Ersatz` |
| `prompts\summary_system.txt`, `summary_final.txt`, `summary_chunk.txt` | Anweisungen für die Zusammenfassung; Platzhalter `{title}`, `{date}`, `{participants}`, `{material}` bzw. `{i}`, `{n}`, `{chunk}`. Daneben liegt jeweils `*.default.txt` = aktuelle Standardvorlage |
| `presets\<Name>.toml` | benannte Presets |

**Gliederung der Zusammenfassung** (Standardvorlage, für alle Anbieter gleich – damit ein
Assistent daraus Aufgaben und Projektstände übernehmen kann):

```
## Kurzfassung
## Entscheidungen
## Aufgaben            Tabelle | Aufgabe | Verantwortlich | Frist | – „offen“, wenn nicht genannt
## Projektstand und Updates   je Projekt/Thema ein Unterabschnitt (###)
## Offene Fragen
## Nächste Termine
```

Das Modell soll keine Verantwortlichen oder Fristen erfinden („offen“) und Fristen so übernehmen,
wie sie genannt wurden. Fehlt einer dieser Abschnitte in einer erzeugten Zusammenfassung, wird sie
trotzdem gespeichert; Protokoll und `meta.json` (`processing.summary.missing_sections`) nennen die
fehlenden Abschnitte.

**Neue Standardvorlage übernehmen:** Unveränderte Vorlagen früherer Versionen werden automatisch
auf die neue Gliederung aktualisiert. **Selbst angepasste Vorlagen bleiben unverändert.** Die
neue Standardvorlage liegt daneben als `summary_final.default.txt` (bzw. `summary_system…`,
`summary_chunk…`). Übernehmen: eigene Datei löschen (wird beim nächsten Start aus der Standardvorlage
neu angelegt) oder Inhalt der `.default.txt` hineinkopieren und eigene Ergänzungen wieder einfügen.

**Was wirkt worauf?**

- *Whisper-Modell, Sprache, Beam-Größe*: Genauigkeit vs. Geschwindigkeit. Der Echtzeitfaktor
  (Rechenzeit ÷ Audiodauer, < 1 = schneller als Echtzeit) jeder Transkription steht im Kopf von
  `transcript.md`, im Protokoll und in `meta.json` (`processing.transcription.mic.timing`) – zum
  Vergleich von Modellen. Die Einstellungen zeigen das effektiv verwendete Gerät und Modell.
- *Zusatz-Prompt* (`initial_prompt`) und *Teilnehmernamen als Prompt*: Kontext für Whisper.
- *Glossar*: richtige Schreibweise von Eigennamen und Fachbegriffen.
- *VAD-Schwelle / Mindestpause / Polster*: wie Sprache und Stille erkannt werden (abgeschnittene
  Satzanfänge → Polster erhöhen; Störgeräusche als Text → Schwelle erhöhen).
- *Halluzinationsschutz*: unterdrückt erfundene Sätze in langen Pausen.
- *Clusterschwelle / feste Sprecheranzahl*: Sprechertrennung der Gegenseite;
  *Schwelle Wiedererkennung / Bonus*: Abgleich mit Stimmprofilen.
- *Darstellung*: Zeitstempel (`hh:mm:ss`, `mm:ss`, `none`), Zusammenfassen aufeinanderfolgender
  Beiträge und maximale Pause, Absatzlänge, Korrekturliste an/aus.

**Presets:** Einstellungen → „Als Preset speichern …“ (oder `callrec preset save Genau`).
Ein Preset enthält nur diese Abstimmungs-Einstellungen (nie Geräte, Kalender oder
SharePoint, nie Adresse/Schlüssel des KI-Servers, die Freigabe entfernter Server oder den Anbieter
Claude/Anthropic samt Freigabe). Aktivieren unter *Allgemein → Aktives Preset* oder einmalig:
`callrec retranscribe <Ordner> --preset Genau`.

**Vorher/Nachher vergleichen:** Wird eine Aufnahme mit geänderten Einstellungen neu verarbeitet
(Tray: „Letzte Aufnahme: mit aktuellen Einstellungen neu transkribieren“ oder
`callrec retranscribe <Ordner>`), entsteht eine **neue Version**: `transcript.v2.md`,
`summary.v2.md` … Die bisherigen Versionen bleiben erhalten, `transcript.md`/`summary.md`
zeigen immer die neueste. In `meta.json` → `versions` steht zu jeder Version, mit welchen
Einstellungen (Modell, Prompt, Glossar, VAD, Korrekturen, Preset, Sprachmodell …) sie erstellt
wurde; `callrec versions <Ordner>` listet sie auf. Das vorherige Whisper-Rohergebnis wird in
`data\archive\` aufbewahrt. Ein reines Neu-Zeichnen nach einer Sprecherzuordnung erzeugt
keine neue Version.

**Sprecherzuordnungen bleiben erhalten:** Beim Neu-Transkribieren bleibt die Sprechertrennung
unverändert, manuelle Zuordnungen gelten weiter. Wird die Sprechertrennung neu berechnet
(`--steps rediarize`), werden manuelle Zuordnungen auf die neuen Sprecher übertragen, wenn
deren Redezeiten überwiegend übereinstimmen; sonst erscheinen sie wieder als „Sprecher N“.

---

## 10. Zusammenfassung an SharePoint übergeben

> **Bewusste Ausnahme von „nur lokal“.** Dieser Schritt überträgt Daten vom Rechner nach
> Microsoft 365. Er geschieht **nur auf Ihren Klick** und überträgt **ausschließlich die
> Zusammenfassung** – nie Transkript, Audio, Stimmprofile, Sprecherdaten oder `meta.json`.

Zweck: Ihr Claude-Assistent (oder jede andere Anwendung) kann die Zusammenfassungen über den
Microsoft-365-Connector in SharePoint lesen.

Übergeben wird **eine Markdown-Datei**, benannt wie die Aufnahme
(`2026-11-19_1400_Projekt-Jour-fixe.md`), mit einem YAML-Block am Anfang, kurzem Kopf (Titel,
Datum/Uhrzeit, Teilnehmende – Namen, E-Mails nur bei `include_participant_emails = true` –,
Hinweis auf die bestätigte Einwilligung, Version und Modell) und dem Inhalt von `summary.md`.
Der YAML-Block dient einem Assistenten zum Erkennen und Zusammenführen von Gesprächen (gleiche
`recording_id` = dasselbe Gespräch, höhere `summary_version` = neuere Fassung):

```yaml
---
callrec_summary: 1
recording_id: "2026-11-19_1400_Projekt-Jour-fixe"
title: "Projekt Jour fixe"
date: "2026-11-19"
start_time: "14:00"
started_at: "2026-11-19T14:00:00+01:00"
duration_min: 47
participants:
  - "Anna Beispiel"
summary_provider: "anthropic"
summary_model: "claude-opus-5-5"
summary_version: 2
---
``` Technisch kann
die Übergabefunktion nur ein „Zusammenfassungs-Dokument“ annehmen, das ausschließlich aus
`summary.md` gebaut wird; das ist durch Tests abgesichert. Übertragen wird genau die Fassung,
die die Vorschau zeigt – hat sich die Zusammenfassung inzwischen geändert (z. B. weil eine
Neuerstellung fertig wurde), bricht die Übergabe ab. Achtung: Die Zusammenfassung ist ein Text
des Sprachmodells und kann einzelne Aussagen wörtlich zitieren – deshalb die Vorschau lesen.

**Auslösen:** Tray-Menü **„Letzte Aufnahme: Zusammenfassung an SharePoint übergeben …“**,
Schaltfläche im Sprecher-Zuordnungsdialog oder `callrec handoff <Ordner>`. Es erscheint
immer eine Vorschau der Datei; erst „An SharePoint übergeben“ (bzw. Eingabe von `ja`) startet
die Übertragung. Wurde die Zusammenfassung neu erstellt, kann sie erneut übergeben werden –
die Datei in SharePoint wird dann ersetzt. Jede Übergabe (Zeit, Ziel, Version, Prüfsumme)
wird in `meta.json` unter `handoffs` vermerkt.

**Verfahren (Einstellungen → SharePoint):**

1. **`folder` (Standard, empfohlen):** Die Datei wird in einen lokalen Ordner kopiert, den
   OneDrive mit einer SharePoint-Bibliothek synchronisiert. Einrichtung: In SharePoint die
   Bibliothek/den Ordner öffnen → **Synchronisieren** → im Explorer erscheint z. B. `C:\Users\<Sie>\Firma\Team - Dokumente\Protokolle`.
   Diesen Pfad als *OneDrive-synchronisierter Ordner* eintragen. Keine zusätzliche
   App-Berechtigung nötig. (Den **Aufnahme**ordner bitte weiterhin nicht in OneDrive legen.)
2. **`graph`:** direkter Upload über Microsoft Graph – siehe Abschnitt 5 „Optional:
   Berechtigung für den direkten SharePoint-Upload“. Beim ersten Upload öffnet sich einmalig
   der Browser für die zusätzliche Zustimmung.

---

## 11. Ergebnisse einer Aufnahme

```
%USERPROFILE%\Anrufaufnahmen\2026-11-19_1400_Projekt-Jour-fixe\
├── audio\
│   ├── mic.flac          Ihre Spur
│   ├── remote.flac       Gegenseite (Loopback)
│   └── mix.flac          beide Spuren gemischt (zum Anhören)
├── transcript.md         Transkript mit Namen und Zeitstempeln (neueste Version)
├── transcript.v1.md …    alle Versionen (Abschnitt 9)
├── summary.md            Kurzfassung, Entscheidungen, Aufgaben, Projektstand, offene Fragen, nächste Termine
├── summary.v1.md …       alle Versionen
├── meta.json             Termin, Teilnehmende, Einwilligung, Geräte, Modelle, Versionen mit Einstellungen, Übergaben
└── data\                 Zwischenergebnisse für die Nachbearbeitung
    ├── asr_mic.json / asr_remote.json   Whisper-Ergebnis mit Wort-Zeitstempeln
    ├── diarization.json                 Sprecherabschnitte der Gegenseite
    ├── speakers.json                    Zuordnung Sprecher → Name
    ├── utterances.json                  zusammengeführte Äußerungen
    ├── archive\                         frühere Whisper-Ergebnisse
    └── speaker_embeddings.npz           Stimm-Embeddings dieses Gesprächs (biometrisch!)
```

Während der Aufnahme liegen dort `audio\mic.part.wav` und `audio\remote.part.wav`; sie
werden nach erfolgreicher Umwandlung gelöscht (`audio.keep_wav = true` behält sie).

---

## 12. Kommandozeile

Alle Befehle mit `.venv\Scripts\python -m callrec …` (bzw. `callrec …` in der aktivierten
Umgebung):

| Befehl | Zweck |
|---|---|
| `tray` (oder ohne Argument) | Tray-Anwendung starten |
| `record [--title T] [--participants "A <a@x>; B"] [--ics F] [--no-calendar] [--no-process]` | Aufnahme in der Konsole (Einwilligung durch Eintippen von `ja` im Konsolenfenster; umgeleitete Eingabe wird nicht akzeptiert) |
| `process <Ordner> [--steps …] [--model M] [--device cuda\|cpu] [--language de\|auto] [--preset P]` | (Neu-)Verarbeitung; Schritte: `finalize,transcribe,diarize,identify,render,summarize` oder Presets `all`, `retranscribe`, `rediarize`, `resummarize`, `render` |
| `retranscribe <Ordner> [--preset P]` | mit aktuellen Einstellungen neu transkribieren (neue Version, alte bleibt) |
| `versions <Ordner>` | Versionen von Transkript und Zusammenfassung mit Einstellungen |
| `handoff <Ordner> [--yes]` | **nur** die Zusammenfassung an SharePoint übergeben (mit Vorschau/Rückfrage) |
| `llm models \| version \| pull [Modell]` | Sprachmodelle anzeigen, Ollama-Version, Modell laden/aktualisieren |
| `preset list \| save <Name> \| show <Name>` | Presets verwalten |
| `summarize <Ordner> [--confirm-anthropic]` | Zusammenfassung neu erstellen (Fortschritt mit verstrichener Zeit); `--confirm-anthropic`: ältere Aufnahme nachträglich für Claude freigeben |
| `anthropic set-key \| delete-key \| status \| test \| enable \| disable [--model M]` | Claude (Anthropic-API) einrichten, Abschnitt 6a |
| `gpu [--recheck]` | ob eine NVIDIA-GPU nutzbar ist und welches Gerät/Modell die Transkription verwendet |
| `repair <Ordner>` | unterbrochene Aufnahme reparieren und Audio abschließen |
| `speakers <Ordner>` | erkannte Sprecher anzeigen |
| `assign <Ordner> S1="Anna Beispiel" S2="Bernd" [--no-profile] [--resummarize]` | Sprecher zuordnen (ohne Dialog) |
| `review <Ordner>` | Zuordnungsdialog öffnen |
| `profiles list \| delete <Name/E-Mail/ID> \| delete-all` | Stimmprofile verwalten |
| `purge-voiceprints <Ordner> \| --all` | Stimm-Embeddings in Aufnahmeordnern löschen |
| `login`, `logout`, `whoami` | Microsoft-Anmeldung |
| `event [--ics F]` | zeigt den Termin, der jetzt gewählt würde |
| `devices` | Audiogeräte und aktuelle Auswahl |
| `models [download] [--whisper] [--model M]` | Modelle vorab laden; Whisper mit Fortschritt (MB/%), Abbruch mit Strg+C, erneuter Aufruf setzt fort |
| `list` | Aufnahmen und Status |
| `config path \| check \| files` | Konfigurationsdatei anzeigen/prüfen; `files` legt Glossar, Korrekturliste und Vorlagen an |

---

## 13. Konfiguration

Datei: `%APPDATA%\CallRecorder\config.toml` (wird beim ersten Start mit Kommentaren angelegt).
Wichtige Einstellungen:

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `general.user_name` | leer | Ihr Name im Transkript (leer = aus Microsoft-Konto) |
| `general.recordings_dir` | leer | Ablage (leer = `%USERPROFILE%\Anrufaufnahmen`; bitte nicht in OneDrive) |
| `audio.mic_device` | `auto` | `auto` = Gerät, das Teams gerade nutzt, sonst Windows-Standard für Kommunikation; oder (Teil des) Gerätenamens |
| `audio.loopback_device` | `auto` | dasselbe für das Ausgabegerät (Headset) |
| `audio.format` | `flac` | `flac` oder `opus` |
| `transcription.model` | `auto` | `auto` = `large-v3` mit NVIDIA-GPU, sonst `large-v3-turbo`; oder fest z. B. `large-v3`, `medium` |
| `transcription.device` | `auto` | `auto` (NVIDIA-GPU nur wenn nutzbar, sonst direkt CPU), `cuda` (mit CPU-Rückfall) oder `cpu` |
| `transcription.language` | `de` | oder `auto` für automatische Erkennung |
| `diarization.cluster_threshold` | `0.4` | kleiner = Stimmen werden feiner getrennt |
| `diarization.num_speakers` | `0` | feste Sprecheranzahl der Gegenseite (0 = automatisch) |
| `speakers.match_threshold` | `0.55` | Schwelle für die Wiedererkennung |
| `general.preset` | leer | aktives Preset |
| `transcription.initial_prompt` | leer | Zusatz-Kontext für Whisper |
| `transcription.use_glossary` | `true` | Glossar als Hotwords |
| `transcription.vad_threshold` / `vad_min_silence_ms` / `vad_speech_pad_ms` | `0.5` / `2000` / `400` | Sprachaktivitätserkennung |
| `transcription.beam_size` | `5` | Suchbreite |
| `transcript.timestamp_format` | `hh:mm:ss` | `hh:mm:ss`, `mm:ss`, `none` |
| `transcript.merge_same_speaker` / `merge_max_gap_s` | `true` / `1.5` | Beiträge zusammenfassen |
| `transcript.apply_replacements` | `true` | Korrekturliste anwenden |
| `summary.provider` | `ollama` | `ollama`, `openai` (OpenAI-kompatibel) oder `anthropic` (Claude, Cloud – Abschnitt 6a) |
| `summary.model` | `gemma3:12b` | Modellname (Ollama/OpenAI-kompatibel) |
| `summary.anthropic_model` / `anthropic_effort` | `claude-opus-5-5` / `medium` | Claude-Modell und Denktiefe |
| `summary.anthropic_transcripts_leave_this_computer_confirmed` | `false` | wird durch die Bestätigung gesetzt; nie per Preset |
| `summary.allow_remote_endpoint_data_leaves_this_computer` | `false` | nur `true`, wenn Inhalte den Rechner verlassen dürfen |
| `handoff.mode` | `folder` | `folder` (OneDrive-Ordner) oder `graph` |
| `handoff.folder` | leer | synchronisierter SharePoint-Ordner |
| `detection.teams_processes` | `["ms-teams.exe", "teams.exe"]` | Prozessnamen für die Erinnerung |
| `hotkey.toggle` | `ctrl+alt+shift+r` | z. B. `ctrl+shift+f9`, `win+alt+r` |

Eine Einstellung zum Abschalten der Einwilligungsabfrage gibt es nicht; entsprechende
Einträge werden ignoriert und gemeldet.

---

## 14. Wo liegen welche Daten?

| Pfad | Inhalt |
|---|---|
| `%APPDATA%\CallRecorder\config.toml` | Konfiguration |
| `%APPDATA%\CallRecorder\glossary.txt`, `replacements.txt`, `prompts\`, `presets\` | Abstimmungsdateien (Abschnitt 9) |
| `%LOCALAPPDATA%\CallRecorder\logs\callrec.log` | Protokoll (rotierend, 5 × 5 MB; UTF-8 mit BOM, damit `Get-Content` Umlaute richtig zeigt) |
| `%LOCALAPPDATA%\CallRecorder\anthropic_api_key.bin` | Anthropic-API-Schlüssel (DPAPI-verschlüsselt, nur bei Claude) |
| `%LOCALAPPDATA%\CallRecorder\gpu_status.json` | Ergebnis der GPU-Prüfung (erneut: Einstellungen „GPU erneut prüfen“ / `callrec gpu --recheck`) |
| `%LOCALAPPDATA%\CallRecorder\msal_token_cache.bin` | Microsoft-Anmeldung (DPAPI-verschlüsselt) |
| `%LOCALAPPDATA%\CallRecorder\voiceprints.bin` | **Stimmprofile** (DPAPI-verschlüsselt) |
| `%LOCALAPPDATA%\CallRecorder\models\` | Whisper- und Sprechertrennungsmodelle |
| `%LOCALAPPDATA%\CallRecorder\calendar_cache.json`, `me.json` | zuletzt abgerufene Termine, Ihr Name |
| `%USERPROFILE%\Anrufaufnahmen\` | Aufnahmen (siehe Abschnitt 11) |

**Was verlässt den Rechner?**
- Anfragen an `login.microsoftonline.com` und `graph.microsoft.com` (Anmeldung,
  Termin-Metadaten, Ihr Name),
- Modell-Downloads von `huggingface.co` (Whisper; nur solange das Modell noch nicht lokal
  liegt), `github.com` (Sprechertrennung,
  SHA-256-geprüft) und – auf Ihren Klick – die Ollama-Modellbibliothek (Sprachmodelle laden/aktualisieren),
- **nur auf Ihren ausdrücklichen Klick:** die Zusammenfassung einer Aufnahme an SharePoint
  (Abschnitt 10),
- nur wenn Sie es ausdrücklich erlauben (`allow_remote_endpoint_data_leaves_this_computer`):
  Transkripttext an einen entfernten KI-Server,
- nur wenn Sie Claude ausdrücklich gewählt und bestätigt haben (Abschnitt 6a): für jede
  Zusammenfassung Transkripttext, Titel, Datum und Teilnehmernamen samt Anweisungstexten an
  `api.anthropic.com` (beim Verbindungstest nur „Antworte nur mit: OK“).

Audio, Stimmprofile, Sprecher-Embeddings, E-Mail-Adressen und `meta.json` werden nie an einen
KI-Anbieter übertragen; Transkripte nur im zuvor genannten Fall.

**Alles löschen:** CallRecorder beenden, die Ordner `%APPDATA%\CallRecorder`,
`%LOCALAPPDATA%\CallRecorder` und den Aufnahmeordner löschen.

---

## 15. Fehlerbehebung

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

**Kein NVIDIA-Grafikchip (z. B. AMD Radeon).** Normal: CallRecorder prüft einmal und
transkribiert dann direkt auf der CPU, mit `model = "auto"` mit `large-v3-turbo`. Der
Echtzeitfaktor steht im Transkriptkopf; ist er deutlich > 1, ein kleineres Modell (`medium`)
probieren. `callrec gpu` zeigt den Stand.

**Modell-Download scheint zu hängen.** Der erste Start lädt das Whisper-Modell (1,5–3 GB). Tray
und Konsole zeigen „Modell wird heruntergeladen: … MB von … GB (… %)“, danach „Modell wird
geladen“. Vorab laden: `callrec models --whisper` (Abbruch mit Strg+C, im Tray „Laufende
Verarbeitung abbrechen“). Ein erneuter Start lädt nur fehlende Dateien; eine abgebrochene große
Datei beginnt neu, wobei der Xet-Zwischenspeicher von Hugging Face bereits geladene Teile
wiederverwenden kann (`%USERPROFILE%\.cache\huggingface\xet`). Der Modellordner wächst erst,
wenn eine Datei vollständig ist – das ist kein Zeichen für einen Stillstand.

**Umlaute im Protokoll falsch.** Seit dieser Version ist `callrec.log` UTF-8 mit BOM; eine ältere
Datei ohne BOM wird beim Start einmalig zu `callrec.log.1` rotiert.

**Zusammenfassung mit Claude fehlt.** Meldung in Tray/`meta.json` lesen: Schlüssel ungültig
→ neu eintragen; kein Guthaben / Ausgabenlimit → in der Anthropic-Konsole aufladen bzw. Limit
erhöhen; keine Verbindung → Internet/Proxy/Firewall (`api.anthropic.com:443`). Danach
„Zusammenfassung neu erstellen“. `callrec anthropic test` prüft alles mit einer Mini-Anfrage.

**Transkription läuft nur auf der CPU, obwohl eine NVIDIA-GPU vorhanden ist.** Im Protokoll steht dann
„Transkription auf CUDA fehlgeschlagen“; das Ergebnis wird gemerkt (danach direkt CPU), bis Sie
`callrec gpu --recheck` ausführen oder in den Einstellungen „GPU erneut prüfen“ klicken. Prüfen: `nvidia-smi` funktioniert, Setup wurde
ohne `-NoCuda` ausgeführt, Treiber aktuell. Test:
`.venv\Scripts\python -m callrec retranscribe <Ordner>` und die Datei `data\asr_mic.log`
lesen (z. B. „cudnn_ops64_9.dll not found“ → `setup.ps1` erneut ausführen).
Zu wenig Grafikspeicher → `transcription.model = "large-v3-turbo"` oder
`compute_type = "int8_float16"`.

**Zusammenfassung fehlt.** Der KI-Server läuft nicht oder das Modell fehlt (`callrec llm
models`). Danach `callrec summarize <Ordner>`. Bei OpenAI-kompatiblen Servern muss der
Modellname exakt dem dort angezeigten entsprechen.

**SharePoint-Übergabe schlägt fehl.** *folder*: Ordner existiert nicht / OneDrive pausiert
(OneDrive-Symbol prüfen). *graph*: `403` → bei `Sites.Selected` fehlt die Freigabe der
Website für die App (Abschnitt 5, Schritt 3) oder Ihr Konto hat dort kein Schreibrecht;
`404` → `graph_drive_id` oder Ordnerpfad prüfen.

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

## 16. Für Entwickler

**Prüfstand der verwendeten Fremd-APIs** (Stand dieser Version): Ollama `/api/tags`,
`/api/chat`, `/api/pull` (NDJSON mit `status`/`total`/`completed`, Fehler als `{"error": …}`),
`/api/version` – geprüft gegen `docs/api.md` und `docs/api/errors.mdx` im Ollama-Repository.
OpenAI-kompatibel `/v1/models` (`data[].id`) und `/v1/chat/completions`
(`choices[0].message.content`) – geprüft gegen die llama.cpp-Server-Doku. LM-Studio-Doku war
nicht erreichbar (Port 1234 = bekannter Standard, nicht erneut geprüft). Graph
`PUT …/root:/{pfad}:/content` (Content-Type `text/plain`, bis 250 MB) und das
`Sites.Selected`-Modell – geprüft gegen die microsoft-graph-docs.
Anthropic: Python-SDK `anthropic` 2.10.0 – `client.beta.messages.stream(...)` +
`get_final_message()`, Modell-IDs, `output_config.effort`, adaptives Denken (Standard, daher
kein `thinking`-Parameter), kein `temperature`, Refusal-Fallback (`server-side-fallback-2026-07-01`,
`fallbacks="default"`), Fehlerklassen und Fehlercodes – geprüft gegen die claude-api-Referenz und
den installierten SDK-Quelltext (dort u. a.: API-Schlüssel wird als `Authorization: Bearer`
gesendet, `anthropic-version: 2026-10-21`, Standard-HTTP-Client folgt Weiterleitungen – daher
eigener Client mit `follow_redirects=False`). Hugging Face: Fortschritt über `tqdm_class` von
`snapshot_download` – geprüft gegen den Quelltext von huggingface_hub 2.4.0.

Aufbau (`src/callrec/`):

| Modul | Aufgabe |
|---|---|
| `audio/capture.py` | plattformunabhängiger Zwei-Spur-Rekorder, Lückenfüllung, Zeitbasis |
| `audio/wavwriter.py` | absturzsichere WAV-Datei mit periodischem Header-Update, Reparatur |
| `audio/wasapi.py` | **Windows:** PyAudioWPatch (WASAPI/Loopback), pycaw (Standardgeräte, Teams-Sitzungen) |
| `audio/finalize.py` | FLAC/Opus, Mix, Laden/Resampling |
| `calendar/` | Graph (MSAL, DPAPI-Cache), `.ics`, Terminauswahl, Offline-Cache |
| `consent.py` | Einwilligungs-Gate |
| `asr/` | faster-whisper im Kindprozess, CUDA-DLL-Pfade, GPU-Prüfung und Modellwahl (`gpu.py`), Download mit Fortschritt (`download.py`), CPU-Rückfall |
| `speakers/` | sherpa-onnx-Diarisierung, Embeddings, Stimmprofile, Wort→Sprecher |
| `transcript.py`, `summary.py`, `llm.py`, `llm_anthropic.py`, `apikey.py` | Zusammenführen/Markdown; Zusammenfassung über austauschbare Anbieter (Ollama, OpenAI-kompatibel, Claude); DPAPI-geschützter API-Schlüssel |
| `userfiles.py`, `versions.py` | Glossar, Korrekturliste, Prompt-Vorlagen; versionierte Ausgaben |
| `handoff.py` | Übergabe **nur** der Zusammenfassung (Ordner oder Graph) |
| `pipeline.py`, `jobs.py` | Verarbeitungsschritte, Hintergrund-Warteschlange |
| `controller.py` | Ablaufsteuerung der App (UI-unabhängig) |
| `ui/` | Tk-Dialoge (inkl. Einstellungen mit Reitern, Übergabe-Vorschau), Overlay, pystray-Tray, App-Verdrahtung |
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
