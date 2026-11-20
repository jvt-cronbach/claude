# CallRecorder – Design-System

Stand: 20.11.2026 (Revision 2: Fluent 2 wie in Teams, entpersonalisierte Texte) · gilt für die neue Oberfläche
(HTML/CSS/JS in pywebview, Edge WebView2, Windows 11). Referenz: `design/prototype.html`; die App nutzt dieselben
Tokens und Komponenten in `src/callrec/ui/web/app.css` (Abschnitte 1–6 identisch, Abschnitt 7 = App-Ergänzungen).
Bilder: `design/screenshots/` (`<screen>_<Breite>x<Höhe>[_z125|_z150]_<light|dark>.png`).

---

## 1. Richtung

**Eine ruhige Arbeitsfläche im Fluent-2-Stil, wie Teams ihn verwendet – damit CallRecorder neben Teams vertraut
wirkt, ohne sich als Teams auszugeben.**

- **Teams-Familie, eigene Marke.** Fluent-2-Bausteine so, wie Teams sie einsetzt: Titelleiste mit Suche in der
  Mitte, schmale App-Leiste links (Symbol über kurzer Beschriftung, Auswahl als Akzentbalken und Akzentfarbe),
  Liste/Detail-Bereiche, Pillen-Zähler, Karten mit weichem Schatten, 4-px-Radien für Bedienelemente und 8 px für
  Karten/Dialoge, Segoe UI Variable, Teams-typische Grauwerte in Hell und Dunkel. **Keine** Microsoft- oder
  Teams-Logos, -Namen oder -Grafiken in der Oberfläche; CallRecorder behält seinen Namen und ein eigenes,
  neutrales Symbol (Mikrofon im abgerundeten Quadrat mit rotem Aufnahmepunkt).
- **Indigo für alles Normale, Rot nur für „Aufnahme“.** Der Akzent ist ein Teams-ähnliches Indigo (`#5B5FC7`).
  Rot erscheint ausschließlich am Aufnahme-Knopf, am laufenden Aufnahmezustand und beim Löschen/Verwerfen.
  Zähler (Badges) sind deshalb Indigo bzw. Bernstein – nicht rot wie in Teams.
- **Aufgaben vor Daten.** Die Startseite beantwortet drei Fragen: *Als Nächstes* (Termin mit Teilnehmenden und
  einem Klick zum Aufnehmen bzw. die laufende Aufnahme), *Wartet* (je Aufnahme genau eine Entscheidung mit genau
  einem Knopf), *In Arbeit* (Verarbeitung als sichtbare Kette). Die Liste aller Aufnahmen ist die zweite Ebene.
- **Lesen ist die Hauptarbeit.** Zusammenfassung und Transkript wie ein Dokument (15/24, max. 74 Zeichen,
  Sprecherfarben, Zeitstempel in eigener Spalte).
- **Erklären statt Fachbegriffe** und **keine persönliche Anrede** (Abschnitt 6).

## 2. Woher die Entscheidungen kommen (UI/UX Pro Max)

Die Skill-Datenbank (`nextlevelbuilder/ui-ux-pro-max-skill`, MIT) wurde nur als lokale Wissensbasis genutzt:
`python3 src/ui-ux-pro-max/scripts/search.py …` (reine CSV-Suche, nichts installiert, keine Netzzugriffe).

| Abfrage | Ergebnis (Auszug) | Übernommen / verworfen |
|---|---|---|
| `"productivity desktop tool business call recording transcription calm trustworthy" --design-system --density 6 --variance 3 --motion 2` | Stil *Minimalism & Swiss Style*; Farben Primär `#DC2626` (Aufnahme-Rot) + Akzent `#2563EB`; Typo *Poppins/Open Sans*; Muster „Product Demo + Features“; Effekte „subtle hover 200–250 ms“; Checkliste (keine Emojis, Fokus sichtbar, reduced motion, Kontrast 4,5:1) | **Übernommen:** Stil (klar, rasterbasiert, wenig Dekor), Bewegung „subtil“, Checkliste. **Verworfen:** Rot als Primärfarbe (Rot muss für den Aufnahmezustand reserviert bleiben, sonst ist „Aufnahme läuft“ nicht mehr eindeutig); Poppins/Open Sans (Webfonts sind offline nicht erlaubt, Segoe UI Variable ist auf Windows 11 vorhanden und nativ); Landing-Page-Muster und GSAP-Scroll-Reveal (kein Marketing, keine Scroll-Animationen). |
| `"call recording transcription" --domain product` | *Voice Recorder & Memo*: Minimalism + „AI-Native UI“, „Clean white + recording red + waveform accent“ | **Übernommen:** Rot nur für Aufnahme, Wellenform als Akzent (Stimmproben im Sprecherdialog). **Verworfen:** „AI-Native UI“ (Chat-Optik passt nicht zu einem Werkzeug). |
| `"productivity tool meeting notes" --domain product` | *Productivity Tool*: Flat Design + Micro-interactions, sekundär Minimalism; „Clear hierarchy + functional colors“ | **Übernommen:** flache Flächen, funktionale Statusfarben, Mikro-Feedback (Hover, Druck, Toast). |
| `"minimal professional desktop productivity" --domain style` | Minimalism & Swiss: „high contrast, sans-serif, grid-based, single primary only“, A11y-Risiko niedrig | **Übernommen:** genau eine Akzentfarbe, Raster, keine Schatten außer zur Ebenen-Trennung. **Angepasst:** Radius 0 → 4/8 px (Windows-11-Konvention). |
| `"productivity professional trust calm" --domain color` | *Real Estate* `#0F766E` „Trust teal + professional blue“; *Productivity* `#0D9488` + Orange | **Übernommen:** Petrol/Teal als Vertrauensfarbe, abgedunkelt auf `#0F6E68` (6,09:1 auf Weiß). **Verworfen:** Orange als zweiter Akzent (konkurriert mit Warnung/Aufnahme). |
| `"professional readable corporate long text" --domain typography` | *Corporate Trust*: Lexend + Source Sans 3 | **Verworfen** (Webfonts). Übernommen nur das Prinzip „Lesbarkeit zuerst“ → eigener Lese-Stil 16/26. |
| `--domain ux`: `"focus visible keyboard navigation"`, `"progress indicator long running task"`, `"empty state guidance"`, `"error message recovery next step"`, `"dark mode contrast"`, `"reduced motion"`, `"status color not only"`, `"confirmation destructive action"` | Fokus sichtbar, Tab-Reihenfolge = visuelle Reihenfolge; Schrittanzeige bei mehrstufigen Prozessen; leere Zustände mit Hinweis und Aktion; Fehler mit nächstem Schritt und `aria-live`; 4,5:1; `prefers-reduced-motion`; Farbe nie allein; Bestätigung vor Löschen, kurze Erfolgsmeldung | **Alles übernommen** – siehe Abschnitt 7 (UX-Prinzipien). |
| `--stack html-tailwind`: `"focus ring visible"`, `"dark mode colors"`, `"responsive container zoom"`, `"semantic buttons forms labels"` | `focus-visible`-Ring, Dark-Mode-Tokens, Container-Queries, `aria-label` an Icon-Knöpfen, semantische Tokens statt Rohwerten | **Übernommen** ohne Tailwind (kein Build): `:focus-visible`, Tokens als CSS-Variablen, `@container` für die Detailansicht, `aria-label` an allen Icon-Knöpfen. |
| `--stack winui`: `"dark mode light theme system"`, `"NavigationView"` | Theme-Ressourcen statt Hex, Systemthema respektieren; NavigationView (Left/LeftCompact) als App-Rahmen | **Übernommen:** Navigationsleiste links, die bei wenig Platz auf Symbole einklappt (LeftCompact); Hell/Dunkel folgt Windows, manuell umschaltbar. |
| `"icon button accessible label" --domain icons` | Eine Icon-Familie, Outline, dekorative Icons `aria-hidden`, Icon-Knöpfe mit Namen | **Übernommen:** eigene Outline-Icons 24er Raster, Strich 1,75; Phosphor/Heroicons selbst nicht eingebunden (offline, kein Paket). |
| `references/pro-rules.md` (Pre-Delivery-Checkliste) | keine Emojis, eine Icon-Familie, Pressed-State ohne Layoutsprung, Dark-Kontrast separat prüfen | **Übernommen** außer den mobilen Punkten (Safe Area, 44 pt Touch, Dynamic Type) – Desktop-Ziel; Mindest-Klickfläche hier 32 px (Fluent), Primäraktionen 44 px. |

Basis-Schriftgröße: Die Skill empfiehlt 16 px Fließtext. **Für Bedienelemente gilt hier 14 px** (Windows-Standard,
sonst wirkt das Programm wie eine Webseite und passt nicht auf 1366×768); **Lesetext ist 16 px.**

---

## 3. Tokens

Alle Werte stehen als CSS-Variablen in `:root` bzw. `:root[data-theme="dark"]` (prototype.html, Abschnitt 1).
`data-theme` wird beim Start aus `prefers-color-scheme` (= Windows-Einstellung „Modus auswählen“) gesetzt und
lässt sich in der Navigation umschalten (System → Hell → Dunkel). Komponenten verwenden **nur** Tokens.

### 3.1 Farben

Kontrast nach WCAG 2.1, berechnet mit dem Prüfskript (`tests/test_web_bridge.py::test_token_contrast`, relative
Luminanz) gegen die Werte in `app.css`. AA: 4,5:1 für Text, 3:1 für Bedienelement-Grenzen und Markierungen.

**Neutrale Flächen und Text (Teams-Grauwerte)**

| Token | Hell | Dunkel | Verwendung | Kontrast hell / dunkel |
|---|---|---|---|---|
| `--frame` | `#EBEBEB` | `#141414` | Titelleiste, App-Leiste | – |
| `--bg` | `#F5F5F5` | `#1F1F1F` | Inhaltshintergrund, Listenbereich | – |
| `--surface` | `#FFFFFF` | `#292929` | Karten, Detailbereich, Dialoge | – |
| `--surface-2` | `#FAFAFA` | `#2E2E2E` | Flächen in Karten | – |
| `--surface-hover` / `-pressed` | `#F0F0F0` / `#E0E0E0` | `#333333` / `#3D3D3D` | Hover/Druck | – |
| `--surface-selected` | `#E8EBFA` | `#2F2F4A` | ausgewählte Optionen | Text 13,1 / 12,9 |
| `--border` / `--border-strong` | `#E0E0E0` / `#D1D1D1` | `#3D3D3D` / `#4D4D4D` | dekorative Linien, Knopfrand | (dekorativ) |
| `--border-control` | `#858585` | `#8F8F8F` | Rahmen von Feldern, Checkboxen, Schaltern | 3,69 / 4,50 auf surface |
| `--text` | `#242424` | `#FFFFFF` | Fließtext, Titel | 15,5 / 14,6 (auf bg 14,2 / 16,5) |
| `--text-2` | `#424242` | `#D6D6D6` | Sekundärtext | 10,1 / 10,0 |
| `--text-3` | `#616161` | `#ADADAD` | Metadaten, Zeitstempel | 6,19 / 6,48 (auf frame 5,20 / 8,21) |

**Akzent (Indigo)** – zwei Rollen, damit der Kontrast in beiden Modi stimmt

| Token | Hell | Dunkel | Kontrast |
|---|---|---|---|
| `--accent-fill` | `#5B5FC7` | `#4F52B2` | Primärknopf, gesetzte Checkbox/Schalter; Weiß darauf 5,38 / 6,60 |
| `--accent` | `#5B5FC7` | `#7F85F5` | Markierungen: Auswahlbalken, Fortschritt, Fokus in Feldern; 5,38 / 4,56 auf surface |
| `--accent-text` | `#4F52B2` | `#A6A9F8` | Links, ausgewählte Leisteneinträge; 6,60 / 6,69 auf surface, 5,54 / 8,47 auf frame |
| `--accent-subtle` (+ `-text`) | `#E8EBFA` (`#3D3E78`) | `#2F2F4A` (`#C5C7FA`) | Chips, Auswahlflächen; 8,20 / 7,95 |

**Aufnahme (Rot)** – nur Aufnahme-Knopf, laufende Aufnahme, Löschen/Verwerfen

| Token | Hell | Dunkel | Kontrast |
|---|---|---|---|
| `--rec` | `#C4314B` | `#C4314B` | Weiß darauf 5,40 (in beiden Modi gleich) |
| `--rec-text` | `#A4262C` | `#FF9CA8` | auf `--rec-subtle` 6,15 / 7,41 |
| `--rec-subtle` | `#FDE7E9` | `#3E1F25` | Aufnahme-Leiste, Status-Chip |

**Status (immer mit Symbol und Text, nie nur Farbe)**

| Rolle | Text hell / Fläche | Text dunkel / Fläche | Kontrast hell / dunkel |
|---|---|---|---|
| ok | `#0E700E` / `#F1FAF1` | `#9FD89F` / `#052505` | 5,89 / 10,05 |
| Hinweis | `#7A4A00` / `#FFF4CE` | `#F8D57A` / `#3D3216` | 6,80 / 8,88 |
| Fehler | `#9E1B1B` / `#FDE7E9` | `#FFA8AF` / `#3E1F25` | 6,77 / 8,04 |
| Info | `#115EA3` / `#EBF3FC` | `#96C6FA` / `#0E2A47` | 5,95 / 8,16 |
| `--warn-mark` | `#C77A00` | `#F5C451` | nur Symbole/Ringe/Zähler (3,38 / 8,93) |

**Sprecherfarben** (Namen im Transkript, Stimmkarten; eigene Spur = `--accent-text`)

| Token | Hell | Kontrast | Dunkel | Kontrast |
|---|---|---|---|---|
| `--sp-1` Blau | `#1F5FA8` | 6,44 | `#8CC2FF` | 7,71 |
| `--sp-2` Violett | `#7A3FB8` | 6,50 | `#C9A6F2` | 6,99 |
| `--sp-3` Magenta | `#A3266E` | 6,86 | `#F29AC9` | 7,00 |
| `--sp-4` Orange | `#9A4A00` | 6,26 | `#F5AE6B` | 7,62 |
| `--sp-5` Grün | `#2E7031` | 6,04 | `#8CD38F` | 8,09 |
| `--sp-6` Schiefer | `#4A5B6E` | 6,97 | `#AFC0D3` | 7,73 |

Farbe ist bei Sprechern nie das einzige Merkmal: Name steht immer daneben, die eigene Spur trägt zusätzlich das
Etikett „eigene Spur“.

**Fokus:** `--focus` (`#1B1B1B` / `#FFFFFF`) als 2-px-Rahmen mit 2 px Abstand plus 2-px-Innenring
`--focus-inner` – sichtbar auf jeder Fläche, auch auf farbigen Knöpfen.

### 3.2 Typografie

Schrift: `"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif`; Überschriften
`"Segoe UI Variable Display", …`; Monospace `"Cascadia Mono", Consolas, monospace` (nur Anweisungs-Editor).
Im Prototyp steht zusätzlich `"Inter"` vor `system-ui`, damit die Linux-Screenshots eine Segoe-ähnliche Laufweite
haben – Inter läuft etwas breiter, die Screenshots prüfen also eher zu lange Texte. In der App entfällt es.
Zahlen, die sich ändern (Laufzeit, Prozent, Zeiten), immer `font-variant-numeric: tabular-nums`.

| Stufe | Größe / Zeile | Gewicht | Einsatz |
|---|---|---|---|
| Caption | 12 / 16 | 400–600 | Metadaten, Chips, Eyebrow (600, Versalien, +0,02em) |
| Body | 14 / 20 | 400; Knöpfe 600 (Fluent) | gesamte Bedienung |
| Leiste | 10 / 12 | 400, ausgewählt 600 | Beschriftung der App-Leiste |
| Lesen | 15 / 24 | 400 | Zusammenfassung, Transkript |
| Subtitle | 16 / 22 | 600 | Kartentitel, Abschnitte in Einstellungen |
| Title | 20 / 28 | 600 | Titel in Liste/Detail |
| Display | 24 / 32 | 700 | Seitentitel (wie Teams) |
| Timer | 36 / 1 | 600 | Laufzeit der Aufnahme |

Lesetext: max. 74 Zeichen (`max-width: 74ch`), Transkript 80 Zeichen. Datum/Uhrzeit mit geschütztem Leerzeichen
(„2.&nbsp;Dezember“, „63&nbsp;%“, „30&nbsp;s“).

### 3.3 Abstände, Radien, Höhen

- Raster 4 px: `--s-1` 4 · `--s-2` 8 · `--s-3` 12 · `--s-4` 16 · `--s-5` 20 · `--s-6` 24 · `--s-8` 32 · `--s-10` 40.
- Seitenrand `--page-pad` 24 px (≤ 1099 px: 20). Kartenabstand 20 px. Karte innen 20 px. Titelleiste 48 px,
  App-Leiste 72 px.
- Radien (Fluent 2): `--r-sm`/`--r-md` 4 (Knöpfe, Felder, Leisten- und Listeneinträge) · `--r-lg`/`--r-xl` 8
  (Karten, Hinweise, Dialoge, Inhaltsecke oben links) · `--r-pill` (Chips, Zähler, Avatare, Filter).
- Höhen: Knopf 32 (klein 28, groß 44 für die wichtigste Aktion je Ansicht), Eingabefeld 32, Navigationseintrag 40,
  Aufnahme-Leiste ≥ 44.

### 3.4 Ebenen (Elevation)

| Token | Wert hell | Einsatz |
|---|---|---|
| `--shadow-1` | `0 1px 2px rgba(0,0,0,.05)` | Karten, ausgewählter Navigationseintrag |
| `--shadow-2` | `0 2px 6px …, 0 1px 2px …` | Aufnahme-Karte |
| `--shadow-flyout` | `0 8px 16px rgba(0,0,0,.14)` + 1-px-Kontur | Menüs („Weitere“, „Anderer Termin“) |
| `--shadow-dialog` | `0 32px 64px rgba(0,0,0,.20), 0 2px 21px rgba(0,0,0,.14)` | Dialoge, Begleitfenster |

Im Dunkelmodus sind die Schatten stärker und haben zusätzlich eine 1-px-Aufhellung (sonst verschwinden Ebenen).
Ebenen werden primär über Flächenfarbe (`bg` → `surface`) getrennt, Schatten nur unterstützend.

### 3.5 Bewegung

| Token | Dauer | Einsatz |
|---|---|---|
| `--t-fast` | 83 ms | Hover, Druck |
| `--t-normal` | 167 ms | Schalter, Menüs aufklappen, Scrim |
| `--t-slow` | 250 ms | Dialog erscheint, Toast gleitet ein, Fortschrittsbalken |

Kurve `--ease-out: cubic-bezier(0,0,0,1)` (Fluent „decelerate“) für Eintreten, `--ease-in` für Austreten.
Einzige Dauer-Animation: der pulsierende Aufnahme-Punkt (1,8 s) und der unbestimmte Fortschrittsbalken.
`prefers-reduced-motion: reduce` schaltet alle Animationen auf ≈ 0 ms (Punkt bleibt rot, pulsiert nicht).
Keine Layout-verschiebenden Animationen, kein Scroll-Reveal.

### 3.6 Icons

Eigene Outline-Icons (24er Raster, Strich 1,75, runde Enden) als `<symbol>`-Sprite im HTML, Größen 16 / 20 / 24.
Gefüllt nur: Aufnahme-Punkt, Stopp-Quadrat, Abspielen. Neben Text immer `aria-hidden="true"`; Icon-Knöpfe ohne
Text haben `aria-label` und `title`. Keine Emojis.

---

## 4. Layout

- **App-Rahmen (wie Teams):** Titelleiste (48 px, Farbe `--frame`) mit eigenem Symbol und Namen links, Suche
  „Aufnahmen durchsuchen“ in der Mitte (Strg+F), rechts der rote Knopf **„Aufnehmen“** bzw. während der Aufnahme
  eine rote Pille mit Laufzeit und „Beenden“, dazu Hilfe. Darunter links die App-Leiste (72 px): *Übersicht,
  Aufnahmen, Einrichtung, Einstellungen* (Symbol über Beschriftung, Zähler als Pille), unten *Ausblenden*
  (Fenster schließen, läuft im Infobereich weiter). Rechts der Inhalt mit abgerundeter Ecke oben links.
- **Aufnahme-Leiste:** Läuft eine Aufnahme, steht auf jeder Seite außer der Übersicht oben eine rote Leiste
  (Punkt, „Aufnahme läuft“, Laufzeit, Titel, „Beenden“); beim Countdown wird sie gelb mit beiden Knöpfen.
- **Übersicht:** ≥ 1560 px drei Spalten (*Als Nächstes + In Arbeit* | *Wartet* | *Heute + Zuletzt*), 1100–1559 px
  zwei Spalten, darunter eine.
- **Aufnahmen:** Listenbereich (300–380 px, `--bg`) + Detail (`--surface`) wie Chat-Liste/Unterhaltung in Teams.
  Die Aktionsleiste bleibt oben stehen; ab 1080 px Detailbreite erscheint rechts „Auf einen Blick“. Unter 1000 px
  nur Liste *oder* Detail mit „‹ Alle Aufnahmen“. Lange Transkripte werden in Abschnitten zu 300 Beiträgen
  angezeigt und beim Scrollen nachgeladen.
- **Einstellungen:** Bereichsliste links (220 px), Inhalt max. 880 px, Speichern-Leiste unten; unter 900 px
  Bereiche als Kachelraster.
- **Begleitfenster:** *Aufnahme-Anzeige* (380 × 64, beim Countdown 156 hoch) oben rechts, *Call erkannt*
  (400 × 252) unten rechts – rahmenlos, immer im Vordergrund, ohne Tastaturfokus.
- **Prüfgrößen:** 1366×768 und 1920×1080 bei 100 %, 1366×768 bei 125 % (1093×614 CSS-px) und 150 % (911×512);
  hell und dunkel. Kein waagerechtes Scrollen, Dialoge scrollen innen (automatisch geprüft, `tests/web/e2e.js`).

## 5. Komponenten

| Komponente | Klassen | Varianten / Zustände | Hinweise |
|---|---|---|---|
| Knopf | `.btn` | `-primary` (Petrol), `-record` (Rot), `-subtle`, `-link`, `-danger`; `-sm` 28, `-lg` 44 | Hover/Druck/Disabled (45 % Deckkraft, `cursor: not-allowed`); je Ansicht höchstens **ein** Primärknopf |
| Icon-Knopf | `.icon-btn` | 32×32 | immer `aria-label` |
| Karte | `.card`, `.card-head`, `.card-body`, `.card-foot` | – | Titel 18/24 |
| Status-Chip | `.chip` | `-ok`, `-warn`, `-err`, `-info`, `-accent`, `-rec`, neutral, `-dashed` | Icon + Text; nie umbrechen |
| Badge | `.badge` | Akzent, `-warn` | Zähler in Navigation/Karten |
| Avatar / Person | `.avatar`, `.person`, `.det-people` | Größe 24/28/36 | Initialen in Sprecherfarbe |
| Feld | `.field`, `.label`, `.input`, `.select`, `.textarea`, `.help` | Fokus: 2-px-Akzentrahmen | Label immer sichtbar, Hilfetext darunter |
| Checkbox / Radio / Schalter | `.check`, `.radio`, `.switch` (`role="switch"`) | an/aus/disabled | Schalter für sofort wirksame Einstellungen, Checkbox für Auswahl vor einer Aktion |
| Auswahlkarte | `.rcard` | ausgewählt (Akzentrand + Fläche) | Anbieterwahl, Sprache, Übergabeverfahren |
| Einstellungszeile | `.setting` (`.stack`) | – | Titel + ein Satz Klartext links, Bedienelement rechts |
| Aufklapper | `details.adv` | auf/zu | „Feineinstellung“, „Technische Angaben“ |
| Hinweisbox | `.banner` | `-ok`, `-warn`, `-err`, Info | Titel, Erklärung, **Knopf für den nächsten Schritt**; Fehler mit `role="alert"` |
| Reiter | `.tabs`, `.tab` | `aria-selected` | Zusammenfassung / Transkript |
| Filter | `.seg` | `aria-pressed`, mit Anzahl | Aufnahmeliste |
| Menü | `.menu` | `up`, `left`; Trenner, Gefahr | Pfeiltasten, Esc |
| Fortschritt | `.bar` | bestimmt, unbestimmt (`.indet`) | `role="progressbar"` mit Wert |
| Pegel | `.meter` | – | `role="meter"`; Ich / Gegenseite |
| Kette (Entscheidung) | `.pipe`, `.pipe-step` | an/aus (`.off`) | 4 Schritte mit Nummer und Schalter |
| Kette (Fortschritt) | `.vsteps`, `.vstep` | `done`, `active`, `wait`, `err` | senkrecht, mit Erklärung je Schritt |
| Mini-Kette | `.mini-steps`, `.ms` | `done`, `busy`, `open`, `changed`, `err`, `none` | Stand einer Aufnahme in einer Zeile |
| Aufgabenliste | `.att` | Icon-Ton warn/ok/err/info | „Wartet“: Titel, Zeit, ein Satz, ein Knopf |
| Titelleiste / App-Leiste | `.topbar`, `.top-search`, `.rail`, `.rail-item` | ausgewählt (Akzentbalken links, Akzenttext, gefülltes Symbol), Zähler | Teams-Muster; eigenes Symbol `BRAND` |
| Tagesplan | `.agenda`, `.ag`, `.nowline` | vergangen, jetzt, künftig | „Heute“ |
| Aufnahmeliste | `.rec-item` (`role="option"`) | ausgewählt | Titel, Datum·Dauer, Status-Chip |
| Übergabe-Karte | `.handoff` | aktuell / `.changed` | wann, wohin (voller Pfad), welche Version, ob geändert |
| Leser | `.reader`, `.transcript`, `.utt` | – | siehe Typografie |
| Stimmkarte | `.voice`, `.play`, `.wave`, `.quotes`, `.choice` (`.suggest`, `.me`) | ausgewählt, Vorschlag (gestrichelt) | Teilnehmende als Radios; Nicht-Teilnehmende nur als markierter Vorschlag |
| Prüfliste | `.ck` | `ok`, `warn`, `err`, `opt` | Status-Icon + Text + Knopf |
| Dialog | `.scrim`, `.modal` (`.wide`) | – | `role="dialog" aria-modal`, Fokusfalle, Esc, Anfangsfokus auf erstes Feld bzw. Primärknopf |
| Begleitfenster | `.miniwin`, `.toastwin`, `.pillwin` | – | eigene kleine Fenster (siehe port-plan.md) |
| Meldung | `#snack` | – | kurze Bestätigung unten, `role="status"` |
| Leerer Zustand | `.empty` | – | Symbol, Titel, ein Satz, eine Aktion |

---

## 6. Ton der deutschen Texte

1. **Keine persönliche Anrede.** Kein „Sie“, kein „du“, keine Begrüßung mit Namen. Neutrale Imperative und
   Infinitive („Jede Stimme einer Person zuordnen.“, „VPN kurz pausieren.“), Nominalphrasen („Wartet“ statt
   „Wartet“, „Eigener Name“ statt „Ihr Name“, „eigene Spur“ statt „Sie“). Die Übersicht heißt
   „Übersicht“ – kein „Guten Tag, …“. Ausnahme: die Wahl „Das bin ich“ (eine Aussage der Person selbst).
2. **Kurze Hauptsätze, Aktiv.**
3. **Knöpfe sagen, was passiert** (Verb, Infinitiv): „Aufnehmen“, „Nicht aufnehmen“, „Übergeben …“, „Jetzt
   beenden“, „Weiter aufnehmen“. Kein „OK“, kein „Ja/Nein“ ohne Inhalt (Ausnahme: generische Rückfragen aus
   älteren Programmteilen).
4. **„…“ nur, wenn danach noch eine Rückfrage oder ein Dialog kommt** (Windows-Konvention).
5. **Fehler = was passiert ist + was trotzdem fertig ist + nächster Schritt**, mit Knopf; nie Fehlercodes oder
   Stacktraces (die stehen im Protokoll, auch Fehler der Oberfläche selbst).
6. **Datenschutz offen benennen, ohne Angst zu machen:** „Transkript verlässt den Rechner“, „bleibt auf diesem
   Rechner“, „nur die Zusammenfassung – nie Audio oder Transkript“.
7. **Feste Begriffe:** Aufnahme · Transkript · Zusammenfassung · Sprecher/Stimme · Teilnehmende · Übergabe /
   übergeben (an SharePoint) · Einwilligung · Einrichtung · Einstellungen · Version. Nicht: Recording, Meeting,
   Protokoll (für die Zusammenfassung), Upload, Speaker, Prompt (in der Oberfläche: „Anweisungen“).
8. **Einwilligung wahrheitsgemäß:** Im Modus „Eine Frage vor der Aufnahme“ heißt es „Hinweis angezeigt“ /
   „vom Nutzer gestartet“, nie „bestätigt“; „bestätigt“ nur im Modus „Ausdrückliche Bestätigung“.
9. **Zahlen und Zeiten:** „52 min“, „63 %“, „Heute, 14:00“, „Fr 13.11.“; Laufzeit `00:23:41`.
10. **Keine Ausrufezeichen, keine Emojis, kein Marketington.** Gendergerecht über neutrale Formen
    („Teilnehmende“).

## 7. Angewandte UX-Prinzipien

1. **Ein nächster Schritt je Aufnahme.** Jede Aufnahme hat genau einen Status (Chip) und genau eine empfohlene
   Aktion – dieselbe Logik wie `viewmodel.next_action`. Die Startseite listet nur Aufnahmen, die eine Entscheidung
   brauchen.
2. **Der Weg ist sichtbar.** Nach dem Call wird die Kette (Transkript → Sprecher → Zusammenfassung → Übergabe) vor
   der Entscheidung gezeigt und danach als Fortschritt mit Prozent, Restzeit und „was als Nächstes passiert“.
3. **Nie still, nie automatisch beim Start.** Aufnehmen beginnt nur über die Einwilligung (Checkbox nie
   vorausgewählt, Startknopf erst danach aktiv). Das Ende kündigt sich mit Countdown an; „Weiter aufnehmen“ ist
   gleichwertig sichtbar.
4. **Ruhig, aber eindeutig.** Rot bedeutet überall „es wird aufgenommen“. Laufende Aufnahme: rote Leiste,
   Begleitfenster, rotes Tray-Symbol, pulsierender Punkt + Text „Aufnahme läuft“ (nicht nur Farbe).
5. **Fehler mit Lösung.** Jede Fehlermeldung nennt Ursache, was erhalten ist, und hat einen Knopf (VPN-Fall,
   Kalender fehlt, Autostart veraltet). Die Einrichtung ist eine Prüfliste mit je einem Lösungsknopf.
6. **Leere Zustände führen.** Keine Aufnahmen → Erklärung + „Aufnahme starten“; nichts offen → „Nichts offen“ mit
   Erklärung, wofür der Bereich da ist; kein Kalender → Aufnahme ohne Termin + „Mit Outlook verbinden“.
7. **Fortschreitende Offenlegung.** Einstellungen: Klartext zuerst, Feinwerte eingeklappt, Technikbereich
   „Für Fortgeschrittene“ zuletzt. Seltene Aktionen der Aufnahme im Menü „Weitere“.
8. **Bestätigung vor Unumkehrbarem, Bestätigung nach Erfolg.** Löschen/Verwerfen/Standard wiederherstellen fragen
   nach (und sagen, was erhalten bleibt); erfolgreiche Aktionen melden sich kurz.
9. **Tastatur vollständig.** Logische Tab-Reihenfolge, sichtbarer Fokus, Esc schließt Menü/Dialog/Toast,
   Strg+F Suche, ↑/↓ in der Liste und in Menüs, Strg+Alt+Umschalt+R Aufnahme, Fokusfalle in Dialogen, nach
   Seitenwechsel Fokus auf die Überschrift.
10. **Für Screenreader:** Landmarken (`nav`, `main`), `aria-current` in der Navigation, `role="listbox/option"`
    für die Liste, `role="progressbar"`/`meter` mit Werten, Fehler mit `role="alert"`, Meldungen in einer
    `aria-live`-Region.
11. **Zoom-fest.** 125 % und 150 % sind normale Zustände (Navigation klappt ein, Übersicht wird einspaltig,
    Liste/Detail einzeln). Keine festen Höhen für Text, deutsche Texte dürfen umbrechen, Chips nicht.

---

## 8. Bildschirme (Routen im Prototyp)

| Route | Inhalt |
|---|---|
| `#home` | Übersicht: Als Nächstes + Aufnehmen, Wartet, In Arbeit, Heute, Zuletzt |
| `#toast` | „Call erkannt: … – aufnehmen?“ als Begleitfenster unten rechts |
| `#consent` | Startfrage: Hinweis (§ 201 StGB, Claude-Hinweis), Titel, Teilnehmende, .ics; Modus „ask“ ohne, Modus „confirm“ mit Häkchen |
| `#recording` / `#countdown` | Aufnahme läuft (Laufzeit, Pegel, automatisches Ende) / „Call scheint beendet – Aufnahme endet in 30 s“ |
| `#rec-library` | laufende Aufnahme auf anderer Seite (rote Leiste) |
| `#postcall` | „Aufnahme beendet – jetzt vollständig verarbeiten?“ mit Kette und Schaltern; Verwerfen / Später |
| `#pipeline` | Fortschritt der Kette je Schritt |
| `#library`, `#transcript`, `#actions` | Liste + Detail, Zusammenfassung / Transkript, Menü „Weitere“ |
| `#retranscribe`, `#handoff` | Neu transkribieren (Sprache, Preset); Übergabe mit Vorschau |
| `#speakers` | Sprecher zuordnen |
| `#setup` | Einrichtung (Prüfliste) |
| `#settings`, `#settings-recording`, `#settings-summary`, `#settings-sharepoint`, `#profiles`, `#update`, `#settings-advanced` | Einstellungen |
| `#prompt` | Anweisungen für die Zusammenfassung mit Prüfung und „Mit dieser Aufnahme testen“ |
| `#empty`, `#library-empty`, `#no-calendar`, `#error-vpn` | leere und Fehlerzustände |
| `#mini` | Begleitfenster auf einem schematischen Desktop (Aufnahme-Anzeige, Countdown, Call erkannt, Tray) |

URL-Parameter: `?theme=light|dark`, `?static=1` (keine Live-Animation, für Screenshots), `?clean=1` (ohne
Prototyp-Zustandsmenü).


---

## 9. Teams-Anlehnung – was übernommen ist und was bewusst nicht

| Übernommen (Fluent 2 wie in Teams) | Bewusst anders |
|---|---|
| Titelleiste mit Suche in der Mitte, App-Leiste links mit Symbol über Beschriftung | eigenes Symbol und eigener Name statt Teams-Logo; keine Teams-Bezeichnungen in der Oberfläche |
| Indigo-Akzent `#5B5FC7`, Grauwerte `#EBEBEB/#F5F5F5/#FFFFFF` bzw. `#141414/#1F1F1F/#292929` | Zähler nicht rot (Rot bleibt der Aufnahme vorbehalten) |
| Liste/Detail wie Chats, Pillen-Filter, Pillen-Zähler, Karten mit Fluent-Schatten | – |
| Knöpfe 32 px, Radius 4, halbfett; Dialoge Radius 8 | Hauptaktion je Ansicht weiterhin genau ein Primärknopf |
| Hell/Dunkel | folgt dem Windows-App-Modus (Teams' eigene Einstellung ist von außen nicht verlässlich lesbar); in den Einstellungen System / Hell / Dunkel |
