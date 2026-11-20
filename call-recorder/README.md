# CallRecorder – lokale Aufnahme, Transkription und Zusammenfassung von Teams-Gesprächen

CallRecorder ist ein Windows-11-Programm mit Hauptfenster und Symbol im Infobereich (Tray). Es nimmt Gespräche
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
   (7a. [Das Hauptfenster](#7a-das-hauptfenster), 7b. [Automatisches Ende einer Aufnahme](#7b-automatisches-ende-einer-aufnahme),
   7c. [Nach der Aufnahme](#7c-nach-der-aufnahme))
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

**Ende der Aufnahme.** Die Zustimmung gilt für das Gespräch, nicht für das, was danach im Raum gesagt
oder abgespielt wird. CallRecorder erkennt deshalb das Gesprächsende und beendet die Aufnahme nach
einem sichtbaren Countdown (Abschnitt 7b); zusätzlich gibt es eine Höchstdauer. Der **Start** bleibt
immer Ihre Entscheidung – automatisch gestartet wird nie.

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
- lassen sich jederzeit einzeln oder komplett löschen (Hauptfenster: *Extras → Stimmprofile
  verwalten …* oder `callrec profiles delete "Name"`).

Holen Sie für das Anlegen eines Stimmprofils am besten eine gesonderte Einwilligung der
Person ein. Zusätzlich liegen in jedem Aufnahmeordner unter `data\speaker_embeddings.npz`
die Embeddings der Sprecher dieses Gesprächs (für die spätere Zuordnung); löschbar mit
`callrec purge-voiceprints <Ordner>` bzw. `--all`.

---

## 2. So funktioniert es

1. **Start** im Hauptfenster („Aufnahme starten …“), per Tastenkürzel (Standard
   **Strg+Alt+Umschalt+R**) oder im Tray-Menü.
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
   **Ende:** von Hand (Schaltfläche, roter Balken, Tastenkürzel, Tray) oder – wenn das Gespräch vorbei
   ist – nach einem Countdown „Call scheint beendet – Aufnahme endet in 30 s“ (Abschnitt 7b).
5. **Nach dem Stopp** fragt CallRecorder einmal „Aufnahme beendet – jetzt vollständig verarbeiten?“
   (Abschnitt 7c). Dann läuft im Hintergrund (Fortschritt im Hauptfenster und im Tray-Tooltip):
   FLAC/Opus erzeugen → Transkription mit faster-whisper (NVIDIA-GPU, sonst direkt CPU) →
   Sprechertrennung der Gegenseite (sherpa-onnx) → Abgleich mit gespeicherten Stimmprofilen →
   `transcript.md` → Zusammenfassung lokal über Ollama/OpenAI-kompatiblen Server **oder** – nur
   wenn ausdrücklich gewählt – über Claude (Anthropic-API; dafür verlässt der Transkripttext den
   Rechner) → `summary.md`.
6. **Unbekannte Stimmen** erscheinen als „Sprecher 1 … n“. Der Zuordnungsdialog öffnet sich
   automatisch; einmal zugeordnete Stimmen werden bei späteren Gesprächen erkannt – automatisch aber
   nur, wenn die Person an diesem Gespräch teilnimmt (Einladung oder von Ihnen eingetragen).

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
   - `-Autostart` – CallRecorder bei der Anmeldung starten (im Infobereich). Das ist derselbe Eintrag wie
     *Einstellungen → Allgemein → Mit Windows starten* bzw. die Zeile „Mit Windows starten“ im Reiter
     **Einrichtung**: ein Wert `CallRecorder` unter `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
     mit `"<Ordner>\.venv\Scripts\pythonw.exe" -m callrec tray --minimized`. Wird der Programmordner
     verschoben, korrigiert CallRecorder den Eintrag beim nächsten Start selbst (Hinweis im Tray); eine
     Autostart-Verknüpfung älterer Versionen im Autostart-Ordner wird dabei entfernt.
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

   Einfacher: Der Reiter **Einrichtung** führt durch genau diese Schritte (Zeile „Microsoft-Anmeldung“):
   Anwendungs-ID und Verzeichnis-ID über **„Client-ID eintragen …“** speichern, dann **„Bei Microsoft
   anmelden …“** klicken. Danach steht dort zur Kontrolle Ihr aktueller bzw. nächster Termin mit den
   Teilnehmenden („Termine prüfen“ fragt erneut ab).

   **Wofür die Kalenderdaten genutzt werden:** Die Aufnahme heißt wie der Termin (Ordner
   `2026-11-19_1400_<Betreff>`); nur die Teilnehmenden können bei der Stimmerkennung automatisch
   zugeordnet werden (Abschnitt 8) und stehen im Dialog „Sprecher zuordnen“ zur Auswahl.
   **Nachträglich:** Für Aufnahmen von vor der Anmeldung im Hauptfenster die Aufnahme wählen → *Weitere … →
   Termin zuordnen …*. CallRecorder sucht die Termine im Zeitraum der Aufnahme (der am besten passende
   zuerst), übernimmt nach Ihrer Wahl Teilnehmende (und auf Wunsch den Betreff als Titel; der Ordnername
   bleibt) und lässt die Sprechererkennung mit den Teilnehmenden erneut laufen – manuelle Zuordnungen
   bleiben.

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
Hinweis im Hauptfenster und Vermerk in `meta.json`). Später nachholen: im Hauptfenster die Aufnahme
wählen → **„Zusammenfassung neu erstellen“** oder `callrec summarize <Ordner>`.

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

**Fortschritt:** Während das Modell arbeitet, zeigen Hauptfenster, Tray-Tooltip und Konsole die verstrichene
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
   wiederholt (`anthropic_max_retries` = 4 Versuche); ein neuer Versuch startet nur innerhalb von
   `anthropic_retry_budget_s` (300 s) ab dem ersten Versuch. Die gesamte Wartezeit je Anfrage ist
   damit begrenzt auf Budget + Verbindungsaufbau + max. Funkstille (300 + 15 + 180 s ≈ 8 min;
   vorher konnten es bei schlechtem Netz über 10 min werden). Der Status zeigt immer den aktuellen
   Stand („Versuch 1/4 fehlgeschlagen (Zeitüberschreitung), neuer Versuch 2/4 in 20 s – noch 12 s“,
   „Versuch 2/4 läuft – 35 s“). Danach wird die Zusammenfassung übersprungen – **das Transkript ist
   trotzdem fertig**, die Aufnahme gilt nicht als fehlgeschlagen, und die Meldung nennt, wie die
   Zusammenfassung später nachgeholt wird. Sehr lange Transkripte werden automatisch in Teile zerlegt
   (`anthropic_max_chunk_chars`). Fehlermeldungen (Schlüssel ungültig, kein Guthaben,
   Ausgabenlimit, keine Verbindung) erscheinen auf Deutsch im Hauptfenster (mit nächstem Schritt), im
   Tray und in `meta.json`.
6. **Ältere Aufnahmen** (Einwilligung ohne Anthropic-Hinweis) werden erst nach einer gesonderten
   Bestätigung übertragen: „Zusammenfassung neu erstellen“ im Hauptfenster fragt nach, auf der Konsole
   `callrec summarize <Ordner> --confirm-anthropic`.

---

## 7. Erster Start und Bedienung

Start über **Startmenü → CallRecorder** (oder automatisch mit Windows, siehe unten). Es öffnet sich das
**Hauptfenster** (Abschnitt 7a); das
Symbol erscheint zusätzlich im Infobereich (ggf. über „^“ einblenden und in die Taskleiste ziehen).
**Schließen des Fensters beendet CallRecorder nicht** – es läuft im Infobereich weiter (Aufnahme per
Tastenkürzel, Erinnerungen, Verarbeitung). Ein Klick auf das Symbol öffnet das Fenster wieder.
Wer CallRecorder lieber nur im Infobereich starten möchte: *Einstellungen → Allgemein → Beim Start
nur im Infobereich* (`[ui] start_minimized = true`).

**Mit Windows starten:** *Einstellungen → Allgemein → Mit Windows starten* oder im Reiter **Einrichtung**
„Mit Windows starten“. CallRecorder startet dann bei der Anmeldung im Infobereich (ohne Fenster), damit
Erinnerung, Tastenkürzel und das automatische Aufnahme-Ende immer bereit sind. Der Eintrag liegt nur in
Ihrem Benutzerkonto (keine Administratorrechte); Details und Reparatur siehe Abschnitt 4, `-Autostart`.

| Symbol | Bedeutung |
|---|---|
| grau mit Mikrofon | bereit |
| **rot mit weißem Punkt** + roter Balken oben rechts | **Aufnahme läuft** |
| orange | Verarbeitung läuft (Fortschritt im Tooltip) |

**Tastenkürzel:** **Strg+Alt+Umschalt+R** startet bzw. beendet die Aufnahme
(änderbar unter *Einstellungen* oder `[hotkey] toggle`).

**Tray-Menü** (kurz gehalten – alles Weitere im Hauptfenster):

- *CallRecorder öffnen* (auch: Klick auf das Symbol)
- *Aufnahme starten … / Aufnahme beenden*
- *Letzte Aufnahme: Zusammenfassung an SharePoint übergeben …*
- *Laufende Verarbeitung abbrechen* (nur sichtbar, während etwas läuft)
- *Einstellungen …*, *Beenden*

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

### 7a. Das Hauptfenster

Das Fenster hat oben den Bereich **Aufnahme** und unten den Bereich **Verarbeitung**. Dazwischen steht
standardmäßig die **kompakte Ansicht**: die letzten fünf Aufnahmen mit ihrem Stand und je **einer**
Schaltfläche für den wichtigsten nächsten Schritt („Jetzt verarbeiten“, „Sprecher zuordnen“,
„Zusammenfassung erstellen“, „Übergeben“) sowie „Öffnen“. Ist die Einrichtung unvollständig, steht darüber
ein Hinweis mit „Einrichtung öffnen“. **„Mehr …“** (oben rechts im Bereich Aufnahme) bzw. „Alle Aufnahmen …“
schaltet auf die volle Ansicht mit den Reitern **Aufnahmen** und **Einrichtung** um, „Kompakt“ zurück; die
Wahl wird gemerkt.

**Aufnahme (oben)**

- Große Schaltfläche **„● Aufnahme starten …“** bzw. **„■ Aufnahme beenden“**. Während der Aufnahme
  stehen daneben „Aufnahme läuft“, die Laufzeit, der Titel und zwei Pegelanzeigen (*Ich* =
  Mikrofon, *Gegenseite* = Headset-Wiedergabe). Bleibt die Anzeige *Ich* beim Sprechen leer, nimmt
  das Mikrofon nichts auf.
- **Termin:** die gerade laufenden Termine aus dem Outlook-Kalender, der wahrscheinlichste ist
  vorausgewählt. Laufen mehrere, lässt sich hier ein anderer wählen. **„Ohne Termin …“** startet
  mit leerem Titel. Ohne Microsoft-Anmeldung gibt es nur diesen Eintrag.
- Gestartet wird **immer über die Einwilligungsabfrage** (Abschnitt 1): Dort stehen Titel und
  Teilnehmende des gewählten Termins und lassen sich ändern oder von Hand eintippen.
- Darunter eine Zeile **„Automatisches Ende: …“**: was am Gesprächsende passiert (z. B. „Anruf-App
  erkannt: ms-teams.exe (PID 1234) auf Headset Mikrofon, Stille 3 min, höchstens 4 h, Stille seit 0:45“).
  Erkennt CallRecorder das Ende, erscheint hier (und im roten Balken) der gelbe Hinweis mit Countdown und
  den Schaltflächen **„Weiter aufnehmen“** und **„Jetzt beenden“** (Abschnitt 7b).

**Reiter „Aufnahmen“**

- Links die Liste aller Aufnahmen, neueste zuerst: Datum, Titel, Dauer, Sprache (bei schmaler Liste
  ausgeblendet, sie stehen auch rechts) und je Schritt der
  Stand – *Transkript*, *Sprecher* (✓ = alle zugeordnet, „2 offen“), *Zusammenf.* (✓, „–“ = fehlt,
  „Fehler“), *SharePoint* (✓ mit Datum, „geändert“ = seit der Übergabe neu erstellt, „–“ = noch
  nicht übergeben). „…“ = wird gerade verarbeitet.
- **Suchen** findet Titel, Ordnernamen und Teilnehmende. Der **Filter** daneben zeigt z. B. nur
  Aufnahmen „Ohne Zusammenfassung“, „Sprecher noch zuordnen“, „Noch nicht an SharePoint übergeben“,
  „Seit der Übergabe geändert“ oder „Mit Fehlern“.
- Rechts die gewählte Aufnahme: Datum, Dauer, Sprache, Teilnehmende, Hinweise (z. B. warum eine
  Zusammenfassung fehlt) und **wann und wohin sie zuletzt an SharePoint übergeben wurde** und ob sich
  die Zusammenfassung seitdem geändert hat.
- Darunter **Zusammenfassung** und **Transkript** lesbar formatiert (Überschriften, Listen, Tabellen).
  Gibt es mehrere Versionen (Abschnitt 9), wählt **„Version“** eine ältere aus.
- **„Groß anzeigen“** öffnet Zusammenfassung bzw. Transkript in einem eigenen, großen Fenster (praktisch
  auf kleinen Bildschirmen; **Esc** schließt es).
- Schaltflächen: **Jetzt verarbeiten** (nur bei noch nicht verarbeiteten Aufnahmen, z. B. nach „Später“),
  **An SharePoint übergeben …** (übergibt nur die Zusammenfassung, mit Vorschau und
  Rückfrage wie bisher; steht „Erneut an SharePoint übergeben …“ da, hat sich die Zusammenfassung seit
  der letzten Übergabe geändert), **Sprecher zuordnen …**, **Zusammenfassung neu erstellen**,
  **Anhören** (Audio im Standard-Player). Unter **„Weitere …“**: **Ordner öffnen**, **Termin zuordnen …** (Abschnitt 5, nur mit Microsoft-Anmeldung), **Neu transkribieren …** (Sprache automatisch /
  Deutsch / Englisch, optional ein Preset; neue Version, die alte bleibt), **Komplett neu
  verarbeiten …**, **Umbenennen …** und **Löschen …**.
- **Umbenennen** ändert nur den angezeigten Titel. Der Ordnername bleibt, weil SharePoint und der
  Assistent die Aufnahme daran wiedererkennen (`recording_id`).
- **Löschen** verschiebt den ganzen Aufnahmeordner (Audio, Transkript, Zusammenfassung,
  Stimm-Embeddings) nach einer Rückfrage in den **Windows-Papierkorb**. Hat das Laufwerk keinen
  Papierkorb, fragt Windows vorher, ob endgültig gelöscht werden soll. Klappt der Papierkorb nicht,
  bietet CallRecorder ausdrücklich das **endgültige** Löschen an. Bereits an SharePoint übergebene
  Zusammenfassungen bleiben in SharePoint. Während eine Aufnahme läuft oder verarbeitet wird, sind
  Löschen und Umbenennen gesperrt.
- Tastatur: **Strg+F** springt in die Suche, **Pfeil runter** in die Liste, **Eingabe** in den
  Text, **Entf** löscht (mit Rückfrage), **F5** aktualisiert, **Strg+Tab** wechselt den Reiter,
  **Alt** bzw. **F10** öffnet das Menü, **Esc** schließt das Fenster in den Infobereich.

**Reiter „Einrichtung“** – was eingerichtet ist und was fehlt, je mit einer Schaltfläche zur Lösung:

| Punkt | Geprüft wird | Schaltfläche |
|---|---|---|
| Audiogeräte | Mikrofon und Ausgabegerät gefunden (nicht während einer Aufnahme) | *Geräte einstellen …* |
| Whisper-Modell | gewähltes Modell vollständig heruntergeladen | *Jetzt herunterladen* (wie `callrec models --whisper`) |
| Zusammenfassung | Anbieter eingerichtet; **Verbindung testen** (lokal automatisch, Claude nur auf Klick) | *Verbindung testen*, *Einstellungen …* |
| Microsoft-Anmeldung (optional) | Schritt-für-Schritt-Anleitung; Client-ID eingetragen? angemeldet? aktueller/nächster Termin mit Teilnehmenden | *Client-ID eintragen …*, *Bei Microsoft anmelden …*, *Termine prüfen* |
| SharePoint-Übergabe (optional) | Übergabeordner eingestellt, erreichbar, von OneDrive synchronisiert | *Einstellungen …* |
| Automatisches Aufnahme-Ende | an/aus, welche Signale, Countdown, Höchstdauer – also was am Gesprächsende passiert | *Einstellungen …* |
| Mit Windows starten (optional) | Autostart-Eintrag vorhanden und aktuell (veraltet → wird korrigiert) | *Mit Windows starten*, *Jetzt korrigieren*, *Entfernen* |

Microsoft-Anmeldung und SharePoint-Ordner sind **optional**. Ohne sie funktioniert alles andere:
Titel und Teilnehmende tippen Sie beim Start ein, die Zusammenfassung bleibt lokal, bis die Übergabe
eingerichtet ist. Fehlt ein Pflichtpunkt, steht am Reiter „Einrichtung (!)“.

**Verarbeitung (unten)** – die laufende Verarbeitung mit Schritt, Fortschritt und Aufnahme, die Zahl
der wartenden und **„Abbrechen“**. Eine Transkription oder ein Modell-Download wird sofort beendet;
andere Schritte (z. B. eine Zusammenfassung, die gerade bei Claude läuft) werden noch abgeschlossen,
danach stoppt die Verarbeitung. Darunter erscheinen Meldungen in einfachem Deutsch mit dem
**nächsten Schritt** und einer passenden Schaltfläche, z. B. bei einer Zeitüberschreitung zu Claude
der VPN-Hinweis und „Zusammenfassung neu erstellen“.

**Menü:** *Datei* (Aufnahmeordner, Protokolldatei, Beenden), *Extras* (Einstellungen, Stimmprofile
verwalten, Bei Microsoft anmelden, Einrichtung prüfen), *Hilfe* (diese Anleitung).

Fenstergröße, -position, Listenbreite, Reiter und Filter werden gemerkt
(`%LOCALAPPDATA%\CallRecorder\window.json`); war das Fenster auf einem größeren oder inzwischen
abgezogenen Bildschirm, wird es auf den sichtbaren Bereich verkleinert. Das Fenster passt auf
1366×768 und ist für 125–150 % Skalierung ausgelegt (scharfe Schrift durch DPI-Awareness).

### 7b. Automatisches Ende einer Aufnahme

Eine vergessene Aufnahme würde sonst weiter aufnehmen, was nach dem Gespräch im Raum gesagt oder abgespielt
wird. CallRecorder erkennt deshalb das Gesprächsende aus mehreren Signalen zusammen:

1. **Die Anruf-App gibt das Mikrofon frei.** Hatte Teams (`detection.teams_processes`, Standard
   `ms-teams.exe`, `teams.exe`; Groß/Klein und `.exe` egal, auch Hilfsprozesse wie `msedgewebview2.exe`,
   die *von Teams gestartet* wurden) während der Aufnahme eine aktive Mikrofon-Sitzung und ist sie dann
   **20 s** weg (`session_gone_s`) **und** die Gegenseite ebenso lange still, gilt das Gespräch als beendet.
   Spielt danach noch etwas (Video, Musik), reichen **120 s** ohne Teams-Sitzung (`session_gone_max_s`).
   Welche App erkannt wurde, steht im Hauptfenster und im Protokoll („Anruf-App erkannt: …“, beim Start der
   Aufnahme zusätzlich „Mikrofon-Sitzungen der Anruf-App: …“). Fehler der Abfrage (z. B. während ein
   Bluetooth-Headset das Profil wechselt und die Geräte kurz verschwinden) zählen als „unbekannt“, nie als
   „beendet“.
2. **Stille auf beiden Spuren** für **3 min** (`silence_minutes`; still = Spitzenpegel unter −45 dBFS,
   `silence_dbfs`). Das deckt auch Gespräche ohne Teams ab (andere App, Telefon über Softphone). Sind Sie
   stummgeschaltet, während andere reden, oder läuft Warteschleifenmusik, ist die Gegenseite nicht still –
   das zählt also nicht als Ende.
3. **Ende des Kalendertermins** – nur ein schwacher Hinweis: Danach genügen **60 s** Stille
   (`silence_after_event_end_s`). Allein beendet das Terminende nie eine Aufnahme.

**Nichts wird still abgeschnitten.** Im Hauptfenster und im roten Balken erscheint „**Call scheint beendet –
Aufnahme endet in 30 s**“ (`countdown_s`) mit **„Weiter aufnehmen“** und **„Jetzt beenden“**; dazu eine
Tray-Meldung (ist der rote Balken ausgeschaltet, öffnet sich das Fenster, ohne den Fokus zu nehmen). Spricht
jemand wieder bzw. ist Teams wieder aktiv, verschwindet der Hinweis von selbst. Ohne Reaktion wird die
Aufnahme genau wie von Hand beendet und verarbeitet. Vom Ton wird nichts weggeschnitten.
**„Weiter aufnehmen“** unterdrückt weitere Nachfragen für **15 min** (`snooze_minutes`); das Signal „Teams
weg“ zählt danach erst wieder, wenn Teams das Mikrofon erneut aktiv nutzt.

**Sicherheitsnetz** (gilt auch, wenn die Erkennung ausgeschaltet ist): **Höchstdauer 4 h**
(`max_duration_minutes`, 0 = aus) mit Vorwarnung **10 min** vorher; „Weiter aufnehmen“ verlängert um **60 min**
(`max_duration_extend_minutes`). Und eine Nachfrage, wenn **15 min** lang nur Stille aufgenommen wurde
(`silence_safety_minutes`).

Alles ist unter *Einstellungen → Aufnahme-Ende* einstellbar (Abschnitt 13). In `meta.json` steht unter
`recording.stop`, **warum und wann** die Aufnahme endete (`reason`: `manual`, `call_ended`, `silence`,
`max_duration`, `app_quit`; `by`: `user` oder `automatic`; `at`; `detail`), unter `recording.autostop` die
erkannte Anruf-App und jede Nachfrage mit Ihrer Reaktion.

Grenzen: Wird nach dem Gespräch im Raum weitergesprochen und es war **kein** Teams-Gespräch, ist keine Spur
still – dann greift erst die Höchstdauer. Ein Tastenklick o. Ä. über −45 dBFS setzt die Stille zurück
(Schwelle ggf. anpassen).

### 7c. Nach der Aufnahme

Endet eine Aufnahme (von Hand oder automatisch), fragt CallRecorder **einmal**: „**Aufnahme beendet – jetzt
vollständig verarbeiten?**“ mit der Liste, was passiert, und Häkchen (vorbelegt aus *Einstellungen →
Allgemein*, `[processing]`): **Transkript**, **Sprecherzuordnung**, **Zusammenfassung**, **Übergabe der
Zusammenfassung an SharePoint (für den Claude-Assistenten)**.

- **Vollständig verarbeiten** startet eine Kette: Transkript (+ Sprechertrennung und -erkennung) → **sind
  Stimmen mit Text noch offen, öffnet sich „Sprecher zuordnen“** und die Kette wartet (mit „Überspringen“) →
  Zusammenfassung (damit die Namen drinstehen) → Übergabe. Die Übergabe ist durch Ihr Häkchen **für diese
  Aufnahme** freigegeben und läuft deshalb ohne die sonst übliche Vorschau – übergeben wird weiterhin
  ausschließlich die Zusammenfassung, vermerkt in `meta.json` (`handoffs[].authorized_by =
  "postcall_dialog"`, `preview_shown = false`). Ist kein SharePoint-Ordner eingerichtet oder gibt es keine
  Zusammenfassung, wird die Übergabe mit Hinweis übersprungen – die Kette scheitert daran nicht.
- **Später**: Die Aufnahme bleibt unverarbeitet in der Liste mit **„Jetzt verarbeiten“** (ein Klick:
  Transkript, Sprecher, Zusammenfassung – ohne Übergabe; die Übergabe danach wie gewohnt mit Vorschau).
- **Verwerfen …**: nach einer Rückfrage in den Papierkorb.

Der Stand der Kette steht in `meta.json` (`processing.chain`); nach einem Neustart wird sie fortgesetzt
(auch ein offener Zuordnungsdialog erscheint wieder). „Abbrechen“ im Bereich Verarbeitung bricht sie ab.
Wird CallRecorder während einer Aufnahme beendet, kommt die Frage beim nächsten Start.
**Ohne Rückfrage:** `[processing] after_recording = "full"` verarbeitet sofort vollständig; die Übergabe
läuft dann **nur**, wenn zusätzlich `handoff_without_asking = true` gesetzt ist (in den Einstellungen mit
eigener Bestätigung; nie über ein Preset).

---

## 8. Sprecher zuordnen und Stimmprofile

**Ihre eigene Stimme** wird nicht erkannt, sondern ist die Mikrofonspur: Alles, was dort gesprochen wird,
steht unter Ihrem Namen im Transkript (Vorname aus dem Microsoft-Konto, sonst aus *Ihr Name*, sonst
„Ich“; eigener Name unter *Einstellungen → Darstellung → Ihr Name im Transkript*). Deshalb steht Ihre
Stimme nicht in der Liste der zuzuordnenden Sprecher – der Dialog und der Transkriptkopf zeigen sie als
feste Zeile („Ich – Joost (Mikrofonspur), 12 Beiträge, 04:31 Sprechzeit“, mit „▶ Anhören“). Ein neuer
Name gilt beim nächsten Erstellen des Transkripts: *Weitere … → Transkript neu darstellen* (ohne neue
Transkription), danach „Zusammenfassung neu erstellen“.

Nach der Verarbeitung öffnet sich – falls es unbekannte Stimmen gibt – der Dialog
**„Sprecher zuordnen“**:

- je Stimme: Sprechzeit, ein Beispielsatz und **„▶ Anhören“** (spielt einen kurzen Ausschnitt),
- Auswahlliste mit den **Teilnehmenden dieses Termins** (noch nicht zugeordnete zuerst); freie Eingabe
  ist möglich – die Person wird dann als Teilnehmende der Aufnahme ergänzt,
- klingt eine Stimme wie jemand, der **nicht** eingeladen war, steht das nur als Vorschlag
  „klingt wie … (kein Teilnehmer dieses Termins)“ in der Liste; erst Ihre Auswahl ordnet zu (und ergänzt
  die Person als Teilnehmende),
- **„Das bin ich (meine Stimme auf der Spur der Gegenseite)“**: falls Ihre Stimme durch Mithören/Sidetone
  auch auf der Gegenseite landet; diese Beiträge werden Ihnen zugeordnet, doppelte Stellen der
  Mikrofonspur entfallen,
- **„Stimme merken“**: legt das Stimmprofil an bzw. verbessert ein bestehendes. Bei automatisch
  erkannten Stimmen ist es aus: eine unveränderte automatische Zuordnung bleibt automatisch (und wird bei
  „neu prüfen“ neu bewertet); erst mit Häkchen gilt sie als von Ihnen bestätigt,
- **„Übernehmen“** aktualisiert `transcript.md` (und auf Wunsch `summary.md`).

Werden zwei „Sprecher“ derselben Person zugeordnet (die Sprechertrennung teilt eine Stimme
gelegentlich auf), werden sie im Transkript zusammengeführt.

**Automatische Erkennung – nur Teilnehmende.** Eine Stimme wird automatisch nur einer Person
zugeordnet, die an *dieser* Aufnahme teilnimmt: aus der Kalendereinladung, beim Start eingetragen, über
*Weitere … → Teilnehmende bearbeiten …* oder *Termin zuordnen …* (beides vor oder nach der Verarbeitung;
die Erkennung läuft dann erneut, Ihre manuellen Zuordnungen bleiben immer) oder von Ihnen im Dialog
zugeordnet. Ohne Teilnehmerliste wird nichts automatisch zugeordnet („Sprecher 1 … n“). Zusätzlich gilt:

- Ähnlichkeit (Kosinus) mindestens `speakers.match_threshold` (0,55) **und** mindestens
  `match_margin` (0,08) besser als jedes andere Profil – auch als Profile von Nicht-Teilnehmenden; sonst
  „nicht eindeutig“ bzw. nur ein Vorschlag,
- jedes Profil höchstens einmal pro Aufnahme (ein zweiter, ähnlicher Sprecher bekommt nur einen
  Vorschlag),
- mindestens `min_match_speech_s` (8 s) Sprache ohne Überlappung; Stimmmerkmale werden nur aus
  Abschnitten ohne gleichzeitiges Sprechen anderer gebildet,
- Ihre eigene Identität (Name/E-Mail, auch Varianten wie „Nachname, Vorname“) wird nie einem Sprecher
  der Gegenseite zugeordnet.

**Anzahl der Sprecher.** Die Sprechertrennung teilt eine Stimme gelegentlich in mehrere „Sprecher“
auf. Die Teilnehmerliste begrenzt deshalb die Zahl der Sprecher der Gegenseite auf die Teilnehmenden
ohne Sie (`speakers.limit_to_participants`, plus `extra_remote_speakers` nicht eingeladene Gäste,
Standard 0): ähnliche Stimmen werden zusammengeführt – anhand der gespeicherten Stimmmerkmale, ohne das
Audio neu auszuwerten; Stimmen, die Sie verschiedenen Personen zugeordnet haben, nie. Bei einem Gespräch
mit **einer** eingeladenen Person ist die ganze Gegenseite diese Person – auch ohne Stimmprofil; dabei
lernt deren Profil (ab 20 s Sprache) mit. Bleibt genau eine Stimme und eine Teilnehmende offen, ist sie
im Dialog vorausgewählt. Saßen nicht eingeladene Personen mit am Gerät: im Dialog bzw. unter
*Weitere … → Weitere Sprecher zulassen / begrenzen*. Ohne Teilnehmerliste bleibt die automatische
Sprecherzahl.

Ändern sich Sprechernamen, nachdem die Zusammenfassung erstellt wurde, zeigt das Hauptfenster
„Die Zusammenfassung beruht auf alten Sprechernamen“ – dann „Zusammenfassung neu erstellen“ und ggf.
erneut an SharePoint übergeben.

Namen werden robust verglichen: per E-Mail, wenn beide Seiten eine haben, sonst nach Namen ohne Titel
(„Dr.“, „Herr“ …), Klammerzusatz, mit umgestelltem „Nachname, Vorname“ und Umlauten (ä = ae).
`speakers.match_scope = "all_profiles"` stellt das frühere Verhalten (alle bekannten Profile,
`invitee_bonus` für Eingeladene) wieder her – nur in `config.toml` bzw. den Einstellungen, nie per Preset.

**Was ein Profil lernt.** Ein Profil entsteht nur aus einer Zuordnung, die Sie gemacht oder bestätigt
haben (mit mindestens `min_learn_speech_s` = 20 s Sprache), und wird automatisch nur bei sehr sicherer
Erkennung (≥ `auto_update_threshold` = 0,75) nachgeschärft – mit halbem Gewicht. Jeder Beitrag ist mit
Aufnahme und Sprecher gespeichert: Korrigieren Sie eine Zuordnung, wird der frühere Beitrag dieses
Sprechers zurückgenommen; erneutes Erkennen ersetzt den automatischen Beitrag einer Aufnahme, statt ihn
zu wiederholen. *Extras → Stimmprofile verwalten …* zeigt je Profil „Gelernt aus“ (Aufnahme, Sprecher,
Herkunft) mit „Eintrag entfernen“, „Nur Bestätigtes behalten“ und „Aus Ihren Zuordnungen neu aufbauen“
(baut das Profil nur aus Ihren manuellen Zuordnungen in den Aufnahmen neu auf – auch für Profile älterer
Versionen, deren Herkunft unbekannt ist).

**Ältere Aufnahmen.** Aufnahmen, die nach dem früheren Verfahren automatisch zugeordnet wurden, zeigen
im Hauptfenster einen Hinweis. *Weitere … → Sprecherzuordnung neu prüfen …* zeigt zuerst, was sich ändern
würde (inklusive Zusammenführung zu vieler Sprecher auf die Teilnehmenden), und ändert nach Bestätigung nur
automatische Zuordnungen. Bei Aufnahmen früherer Versionen werden zusätzlich Zuordnungen zu Personen, die
nicht eingeladen waren, zum Zurücksetzen angeboten – frühere Versionen haben vorausgefüllte automatische
Namen beim „Übernehmen“ als bestätigt gespeichert; deren Profile werden dann aus Ihren übrigen
Zuordnungen neu aufgebaut. Die bisherige `speakers.json` wird unter `data\archive` gesichert;
manuelle Zuordnungen bleiben sonst unverändert. Ohne diesen Klick wird kein altes Transkript verändert.

**Mikrofonspur prüfen.** Nach der Sprechertrennung prüft CallRecorder, ob die Mikrofonspur plausibel
Ihre Stimme enthält, und warnt im Transkriptkopf, im Hauptfenster und im Dialog, wenn

- die Mikrofonspur kaum Sprache enthält, die Gegenseite aber viel (falsches/stummes Mikrofon),
- die Mikrofonspur im Wesentlichen ein Echo der Gegenseite ist oder Teile davon enthält (Lautsprecher
  statt Headset, Bluetooth-Freisprechprofil, „Abhören“) – erkannt an gleichen Wörtern zur gleichen Zeit
  auf beiden Spuren und am Lautstärkeverlauf,
- Ihre Stimme auf der Spur der Gegenseite als „Sprecher n“ auftaucht (Mithören/Sidetone) – erkannt an
  den gleichen Wörtern bzw. am Stimmvergleich mit der Mikrofonspur; dieser Sprecher wird nie automatisch
  jemandem zugeordnet.

Die Meldung nennt die wahrscheinliche Ursache und wo das Mikrofon geändert wird.

**Geschwindigkeit.** Die Sprechertrennung nutzt automatisch so viele Rechen-Threads, wie physische
Kerne vorhanden sind (minus einen, höchstens 8; während einer laufenden Aufnahme etwa die Hälfte).
Fest einstellen: `diarization.num_threads`. Ein `num_threads = 2` aus einer älteren `config.toml` (der
frühere Standardwert) gilt als „automatisch“. Dauer der Sprechertrennung (`processing.diarization.timing`)
und der Erkennung (`processing.identification.timing`) stehen in `meta.json`. Neu zuordnen, Teilnehmende
ändern und „neu prüfen“ verwenden die gespeicherten Stimmmerkmale der Aufnahme – das Audio wird dafür
nicht erneut ausgewertet.

Profile ansehen/löschen: Hauptfenster *Extras → Stimmprofile verwalten …* oder

```powershell
.venv\Scripts\python -m callrec profiles list
.venv\Scripts\python -m callrec profiles show "Anna Beispiel"      # gelernt aus welchen Aufnahmen
.venv\Scripts\python -m callrec profiles reset "Anna Beispiel"     # nur Bestätigtes behalten
.venv\Scripts\python -m callrec profiles rebuild "Anna Beispiel"   # aus Ihren Zuordnungen neu aufbauen
.venv\Scripts\python -m callrec profiles delete "Anna Beispiel"
.venv\Scripts\python -m callrec profiles delete-all
.venv\Scripts\python -m callrec recheck "<Ordner>"                  # Sprecherzuordnung neu prüfen
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

### Sprache des Gesprächs

Standard ist `transcription.language = "auto"`: CallRecorder misst die Sprache **je Aufnahme** –
an bis zu sechs 30-Sekunden-Abschnitten **Sprache** (nach der Pausenerkennung) je Spur, verteilt
über das ganze Gespräch, nicht nur am Anfang – und wählt nur unter `language_candidates`
(Standard `["de", "en"]`). Die erste Sprache der Liste gilt bei Zweifel: Eine andere wird nur
gewählt, wenn sie über beide Spuren gewichtet mindestens `language_switch_threshold` (0,75) erreicht
und mindestens `language_min_speech_s` (20 s) Sprache gemessen wurden. Ein kurzer, leiser oder
englischer Gruß am Anfang macht also kein deutsches Gespräch zu einem englischen. Beide Spuren
werden gemeinsam bewertet (ein Gespräch hat normalerweise eine Sprache); nur wenn **jede** Spur für
sich eindeutig eine andere Sprache zeigt (z. B. Sie Deutsch, Gegenseite Englisch), wird jede Spur in
ihrer eigenen Sprache transkribiert. Ergebnis, Wahrscheinlichkeiten und Begründung stehen im
Transkriptkopf („**Sprache:** Englisch (automatisch erkannt: de 0,04 / en 0,96; 9 Abschnitte, 270 s
Sprache)“), im Protokoll und in `meta.json` (`processing.transcription.language_detection`, Rohwerte
in `data\language_detect.json`). Die Messung kostet einmal Laden des Modells plus wenige Sekunden je
Abschnitt.

**Fest einstellen:** `language = "de"` (oder `"en"` …) erzwingt die Sprache. **Ältere
Konfigurationen:** Frühere Versionen schrieben `language = "de"` als Standardwert in jede
`config.toml` (auch beim Speichern der Einstellungen) – das war keine bewusste Wahl. Eine Datei mit
`language = "de"` **ohne** `language_candidates` wird deshalb beim ersten Start einmalig auf
`"auto"` mit `["de", "en"]` umgestellt (nur diese Zeile; Kommentare bleiben; einmaliger Hinweis im
Tray). Wer immer Deutsch will, stellt danach `language = "de"` wieder ein – jetzt mit
`language_candidates` in der Datei gilt das als bewusste Wahl. Andere Werte (`"en"` …) werden nie
umgestellt. Ältere Presets mit `language = "de"` schalten die Erkennung ebenfalls nicht mehr ab.

**Falsche Sprache erwischt?** Hauptfenster: Aufnahme wählen → *Weitere … → Neu transkribieren …* → Englisch
bzw. `callrec retranscribe <Ordner> --language en` (oder `--language auto` = neu erkennen). Es
entsteht eine neue Version, die alte bleibt.

**Sprachwechsel innerhalb einer Spur** (echte Mischgespräche): `multilingual = true`
(*Einstellungen → Sprache & Qualität*, experimentell). Das nutzt die Option `multilingual` von
faster-whisper 1.2.1: Sie erkennt die Sprache je 30-s-Fenster neu, allerdings **unter allen rund 100
Sprachen** (nicht nur den Kandidaten) und ohne die Sprache je Abschnitt auszugeben – deshalb nicht
Standard. Ohne diese Option wird jede Spur in einer Sprache transkribiert; englische Einschübe in
einem deutschen Gespräch werden dann meist trotzdem verstanden, können aber eingedeutscht werden.

**Schutz vor Wiederholungsschleifen** (*Einstellungen → Sprache & Qualität*): Whisper dekodiert einen
Abschnitt mit höherer „Temperatur“ neu, wenn der Text zu gleichförmig ist
(`compression_ratio_threshold`, 2,4) oder zu unsicher (`log_prob_threshold`, −1,0); dazu
`no_speech_threshold`, `temperature_fallback`, `repetition_penalty` und `no_repeat_ngram_size`
(Standardwerte = die von faster-whisper; die beiden letzten sind aus, weil sie auch echte
Wiederholungen unterdrücken können). Zusätzlich prüft CallRecorder nach der Transkription, ob sich eine
Wortfolge auffällig oft hintereinander wiederholt („mit Ihnen, mit Ihnen, mit Ihnen …“, ab
`repetition_warn_min_repeats` = 4 Wiederholungen, einzelne Wörter ab 8). Gefundene Stellen werden
**nicht gelöscht**, sondern im Transkriptkopf („**Warnung:** Gegenseite: mögliche
Wiederholungsschleife – [00:12:34] „mit Ihnen“ ×14 …“), im Protokoll und in `meta.json`
(`processing.transcription.<spur>.quality`) gemeldet. Häufigste Ursache ist eine falsche Sprache.

**Sprache der Zusammenfassung:** `summary.language` (Standard `de`). Die Zusammenfassung wird immer in
dieser Sprache geschrieben, auch bei englischen Gesprächen; CallRecorder teilt dem Sprachmodell dazu
die erkannte Gesprächssprache mit (zusätzlich zu den Vorlagen, gilt also auch für angepasste
Vorlagen). Die Überschriften bleiben die deutschen aus der Vorlage.

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
(Hauptfenster: *Weitere … → Neu transkribieren …* oder
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

**In der Kette nach der Aufnahme** (Abschnitt 7c) läuft die Übergabe ohne Vorschau, weil Sie sie dort
für genau diese Aufnahme angehakt haben; übergeben wird auch dann nur die Zusammenfassung.

**Auslösen:** im Hauptfenster die Aufnahme wählen → **„An SharePoint übergeben …“**,
Tray-Menü **„Letzte Aufnahme: Zusammenfassung an SharePoint übergeben …“**, Schaltfläche im
Sprecher-Zuordnungsdialog oder `callrec handoff <Ordner>`. Das Hauptfenster zeigt je Aufnahme, wann und
wohin zuletzt übergeben wurde und ob sich die Zusammenfassung seitdem geändert hat (Spalte
*SharePoint* „geändert“, Filter „Seit der Übergabe geändert“). Ist noch kein Übergabeordner
eingestellt, bietet CallRecorder an, die Einstellungen zu öffnen. Es erscheint
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
├── meta.json             Termin, Teilnehmende, Einwilligung, Geräte, Ende der Aufnahme (recording.stop), Modelle, Versionen mit Einstellungen, Übergaben, Verarbeitungskette
└── data\                 Zwischenergebnisse für die Nachbearbeitung
    ├── asr_mic.json / asr_remote.json   Whisper-Ergebnis mit Wort-Zeitstempeln
    ├── diarization.json                 Sprecherabschnitte der Gegenseite
    ├── speakers.json                    Zuordnung Sprecher → Name
    ├── utterances.json                  zusammengeführte Äußerungen
    ├── language_detect.json             gemessene Sprachwahrscheinlichkeiten je Spur und Abschnitt
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
| `tray [--minimized]` (oder ohne Argument) | Hauptfenster und Tray-Symbol starten; `--minimized` nur im Infobereich (so startet der Autostart) |
| `record [--title T] [--participants "A <a@x>; B"] [--ics F] [--no-calendar] [--no-process]` | Aufnahme in der Konsole (Einwilligung durch Eintippen von `ja` im Konsolenfenster; umgeleitete Eingabe wird nicht akzeptiert) |
| `process <Ordner> [--steps …] [--model M] [--device cuda\|cpu] [--language de\|en\|auto] [--preset P]` | (Neu-)Verarbeitung; Schritte: `finalize,transcribe,diarize,identify,render,summarize` oder Presets `all`, `retranscribe`, `rediarize`, `resummarize`, `render` |
| `retranscribe <Ordner> [--preset P] [--language en\|de\|auto]` | mit aktuellen Einstellungen neu transkribieren (neue Version, alte bleibt); `--language` erzwingt die Sprache für diesen Lauf (`auto` = neu erkennen) |
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
| `models [download] [--whisper] [--model M]` | Modelle vorab laden; Whisper mit Fortschritt, Abbruch mit Strg+C, erneuter Aufruf setzt fort. „Fertig.“ erscheint nur, wenn das Modell danach vollständig und offline ladbar ist – sonst Fehlermeldung und Exit-Code ≠ 0 |
| `models list` | installierte Whisper-Modelle mit Größe, Vollständigkeit und Resten abgebrochener Downloads; `*` = wird verwendet |
| `models remove <Name> [--yes]` | ein Whisper-Modell löschen (z. B. `large-v3`, wenn nur noch `large-v3-turbo` genutzt wird) |
| `list` | Aufnahmen mit Stand je Schritt (wie die Liste im Hauptfenster) |
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
| `transcription.language` | `auto` | je Aufnahme erkennen; `de`, `en` … erzwingt die Sprache (Abschnitt 9, „Sprache des Gesprächs“) |
| `transcription.language_candidates` | `["de", "en"]` | mögliche Sprachen, die erste gilt bei Zweifel |
| `transcription.language_switch_threshold` / `language_min_speech_s` / `language_detection_windows` | `0.75` / `20.0` / `6` | Erkennung: Schwelle für eine andere Sprache, Mindestsprache, Messabschnitte je Spur |
| `transcription.multilingual` | `false` | Sprachwechsel innerhalb einer Spur (experimentell) |
| `transcription.compression_ratio_threshold` / `log_prob_threshold` / `no_speech_threshold` / `temperature_fallback` | `2.4` / `-1.0` / `0.6` / `true` | Schutz vor Wiederholungsschleifen (0 = aus) |
| `transcription.repetition_penalty` / `no_repeat_ngram_size` / `repetition_warn_min_repeats` | `1.0` / `0` / `4` | Wiederholungs-Strafe, n-Gramm-Sperre, Warnung ab so vielen Wiederholungen |
| `summary.language` | `de` | Sprache der Zusammenfassung, unabhängig von der Gesprächssprache |
| `summary.anthropic_connect_timeout_s` / `anthropic_read_timeout_s` / `anthropic_retry_budget_s` / `anthropic_max_retries` | `15` / `180` / `300` / `4` | Claude: Verbindungsaufbau, max. Funkstille während der Antwort, Zeitbudget für Wiederholungen, Versuche |
| `diarization.cluster_threshold` | `0.4` | kleiner = Stimmen werden feiner getrennt |
| `diarization.num_speakers` | `0` | feste Sprecheranzahl der Gegenseite (0 = automatisch) |
| `diarization.num_threads` | `0` | Rechen-Threads der Sprechertrennung; `0` = automatisch (physische Kerne − 1, höchstens 8; während einer Aufnahme etwa die Hälfte). Der wirksame Wert steht unter *Einstellungen → Sprecher* und in `meta.json` (`processing.diarization.timing`) |
| `speakers.match_scope` | `participants` | `participants` = automatisch nur Teilnehmende dieser Aufnahme; `all_profiles` = alle Profile (früheres Verhalten). Nur in `config.toml`/Einstellungen, nie per Preset |
| `speakers.match_threshold` / `match_margin` | `0.55` / `0.08` | Schwelle für die Wiedererkennung; Mindestabstand zum zweitbesten Profil |
| `speakers.min_match_speech_s` / `min_learn_speech_s` | `8` / `20` | Mindest-Sprechzeit (ohne Überlappung) für die automatische Erkennung bzw. damit ein Profil lernt |
| `speakers.auto_update_on_match` / `auto_update_threshold` | `true` / `0.75` | Profil bei sehr sicherer automatischer Erkennung nachschärfen (rücknehmbar) |
| `transcript.self_display_name` | leer | Ihr Name für die eigenen Beiträge; leer = Vorname aus dem Microsoft-Konto, sonst aus `general.user_name`, sonst „Ich“ |
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
| `detection.teams_processes` | `["ms-teams.exe", "teams.exe"]` | Prozessnamen der Anruf-App für Erinnerung und Gesprächsende (auch von ihnen gestartete Hilfsprozesse) |
| `autostop.enabled` | `true` | Gesprächsende automatisch erkennen (Abschnitt 7b) |
| `autostop.call_app_session` / `session_gone_s` / `session_gone_max_s` | `true` / `20` / `120` | Signal „Anruf-App nutzt das Mikrofon nicht mehr“ (mit stiller Gegenseite / auch wenn etwas spielt) |
| `autostop.silence_minutes` / `silence_dbfs` / `silence_after_event_end_s` | `3` / `-45` / `60` | Stille auf beiden Spuren; Schwelle; kürzere Stille nach Terminende |
| `autostop.countdown_s` / `snooze_minutes` | `30` / `15` | Countdown vor dem Beenden; Ruhe nach „Weiter aufnehmen“ |
| `autostop.max_duration_minutes` / `max_duration_warning_minutes` / `max_duration_extend_minutes` | `240` / `10` / `60` | Höchstdauer (0 = aus), Vorwarnung, Verlängerung |
| `autostop.silence_safety_minutes` | `15` | Nachfrage nach so langer Stille (0 = aus), auch bei `enabled = false` |
| `processing.after_recording` | `ask` | `ask` = nach der Aufnahme fragen, `full` = sofort vollständig verarbeiten (Abschnitt 7c) |
| `processing.default_speakers` / `default_summary` / `default_handoff` | `true` / `true` / `false` | Vorbelegung der Häkchen in der Frage |
| `processing.handoff_without_asking` | `false` | nur mit `full`: Zusammenfassung ohne Rückfrage an SharePoint übergeben (nie per Preset) |
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

Zuerst ins Protokoll schauen: Hauptfenster *Datei → Protokolldatei öffnen*. Für die Transkription
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
geladen“. Meldet die Download-Bibliothek (Xet) keinen Byte-Fortschritt, steht dort ehrlich
„Modell wird heruntergeladen … 3:20 min, bisher 900 MB auf der Festplatte (genauer Fortschritt
nicht verfügbar)“ – gemessen am Modellordner und am Xet-Zwischenspeicher. Nach dem Download wird
das Modell offline geprüft; nur dann erscheint „Modell vollständig heruntergeladen und geprüft“. Vorab laden: `callrec models --whisper` (Abbruch mit Strg+C, im Hauptfenster „Abbrechen“, im Tray „Laufende
Verarbeitung abbrechen“). Ein erneuter Start lädt nur fehlende Dateien; eine abgebrochene große
Datei beginnt neu, wobei der Xet-Zwischenspeicher von Hugging Face bereits geladene Teile
wiederverwenden kann (`%USERPROFILE%\.cache\huggingface\xet`). Die eigentliche Modelldatei
erscheint erst, wenn sie vollständig ist; bis dahin wachsen Zwischendateien und der Xet-Zwischenspeicher.

**Alte Modelle belegen Platz** (z. B. `large-v3` mit 5,75 GB nach einem abgebrochenen Download, obwohl
nur noch `large-v3-turbo` genutzt wird): `callrec models list` zeigt alle Modelle mit Größe und Resten
abgebrochener Downloads, `callrec models remove large-v3` löscht eines; ebenso unter *Einstellungen →
Transkription → Installierte Whisper-Modelle*.

**Transkript in der falschen Sprache / Wiederholungen** („mit Ihnen, mit Ihnen …“): Im Transkriptkopf
stehen erkannte Sprache und ggf. eine Warnung. Neu transkribieren: Hauptfenster *Weitere … →
Neu transkribieren … → Englisch* bzw. `callrec retranscribe <Ordner> --language en`. Steht in der
Konfiguration `language = "de"`, wird immer Deutsch erzwungen – für die automatische Erkennung
`"auto"` eintragen.

**„Ohne Text: Sprecher 6“ im Transkriptkopf.** Die Sprechertrennung hat eine Stimme gefunden, deren
Abschnitte zu kurz für ein erkanntes Wort waren (Räuspern, „mhm“). Sie wird nicht als sprechend und
nicht als „noch nicht zugeordnet“ gezählt, aber genannt, damit die Nummerierung nachvollziehbar bleibt.

**Konsole: Statuszeilen.** Im Konsolenfenster wird die Statuszeile an Ort und Stelle aktualisiert
(auf Fensterbreite gekürzt). Wird die Ausgabe in eine Datei umgeleitet, erscheint eine Zeile nur bei
einem neuen Schritt oder geänderten Prozenten, höchstens alle 30 s.

**Umlaute im Protokoll falsch.** Seit dieser Version ist `callrec.log` UTF-8 mit BOM; eine ältere
Datei ohne BOM wird beim Start einmalig zu `callrec.log.1` rotiert.

**Zusammenfassung mit Claude fehlt.** Meldung im Hauptfenster (Verarbeitung) bzw. `meta.json` lesen: Schlüssel ungültig
→ neu eintragen; kein Guthaben / Ausgabenlimit → in der Anthropic-Konsole aufladen bzw. Limit
erhöhen; keine Verbindung → Internet/Proxy/Firewall (`api.anthropic.com:443`).

**Claude: nur Zeitüberschreitungen / keine Verbindung, oft mit VPN.** Ein aktives VPN (beobachtet mit
NordVPN) kann Anfragen an `api.anthropic.com` bis zur Zeitüberschreitung hängen lassen; mit pausiertem
VPN klappt es sofort. Abhilfe: VPN für die Zusammenfassung kurz pausieren oder CallRecorder per
**Split-Tunneling** vom VPN ausnehmen (in der VPN-App die Programme `python.exe` und `pythonw.exe` aus
`<Installationsordner>\.venv\Scripts` als „nicht über VPN“ eintragen). Prüfen mit `callrec anthropic test`
bzw. *Einstellungen → Verbindung testen* – beide brechen nach etwa 20 s ab, statt zu hängen. Auch in
Zügen/Mobilfunk sind Zeitüberschreitungen häufig; die Zusammenfassung lässt sich jederzeit später
nachholen. Danach
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

**Aufnahme endet nicht automatisch / zu früh.** Im Hauptfenster steht während der Aufnahme
„Automatisches Ende: …“. „Anruf-App (Teams) nicht erkannt“ → im Protokoll nach „Mikrofon-Sitzungen der
Anruf-App“ suchen bzw. im Task-Manager den Prozessnamen prüfen und `detection.teams_processes` ergänzen.
„Stille seit …“ zählt nie hoch → das Mikrofon rauscht über der Schwelle: `autostop.silence_dbfs` z. B. auf
`-40` setzen. Endet es in langen Pausen zu früh → `silence_minutes` erhöhen; „Weiter aufnehmen“ hilft im
Einzelfall. Warum eine Aufnahme endete, steht in `meta.json` unter `recording.stop`.

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
`snapshot_download` – geprüft gegen den Quelltext von huggingface_hub 2.4.0 (Weg `snapshot_download` →
`xet_get` → `XetDownloadProgressReporter` → `_AggregatedTqdm`; ein Test treibt genau diesen Code mit
einer nachgebildeten Xet-Sitzung) und hf_xet 1.8.0 (`progress_callback(GroupProgressReport, dict)`);
Prüfung danach mit `faster_whisper.utils.download_model(local_files_only=True)`.
faster-whisper 1.2.1 (`transcribe.py`): Parameter `compression_ratio_threshold`, `log_prob_threshold`,
`no_speech_threshold`, `temperature` (Liste = Rückfall), `repetition_penalty`, `no_repeat_ngram_size`,
`hallucination_silence_threshold`, `multilingual` (erkennt je 30-s-Fenster mit
`model.detect_language` unter allen Sprachen, ohne Sprache je Segment), `language_detection_segments`
(nur aufeinanderfolgende Fenster ab Beginn der Sprache) – deshalb eigene Messung über
`feature_extractor` → `pad_or_trim` → `encode` → `model.detect_language` an verteilten Stellen.

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
| `controller.py` | Ablaufsteuerung der App (UI-unabhängig); **ein** Stopp-Pfad für Hand, Tastenkürzel, Overlay, Beenden und automatisches Ende |
| `autostop.py` | automatisches Ende: reine Zustandsmaschine (`AutoStopMonitor`) und Abfrage-Thread (`AutoStopRunner`); der Stopp läuft über `ui.post` → `Controller.stop` |
| `postcall.py` | Frage nach der Aufnahme und geführte Verarbeitungskette (fortsetzbar, abbrechbar) |
| `autostart.py` | „Mit Windows starten“ (HKCU-Run-Eintrag, Erkennung und Reparatur veralteter Einträge) |
| `library.py`, `setupcheck.py`, `hints.py` | Aufnahmeliste mit Stand je Schritt (zwischengespeichert), Umbenennen/Löschen; Einrichtungs-Checkliste; Fehlertexte → nächster Schritt (alles ohne Tk, getestet) |
| `ui/` | Hauptfenster (`mainwindow.py`, Hilfsfunktionen ohne Tk in `viewmodel.py`), gemeinsame Aktionen für Fenster und Tray (`actions.py`), Tk-Dialoge (inkl. Einstellungen mit Reitern, Übergabe-Vorschau), Overlay, pystray-Tray, App-Verdrahtung |
| `detect.py`, `hotkey.py` | Erinnerungslogik, großzügiger Abgleich der Anruf-App-Prozesse, globales Tastenkürzel (RegisterHotKey) |

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
