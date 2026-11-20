# CallRecorder – Design-System

Stand: 20.11.2026 · gilt für die neue Oberfläche (HTML/CSS/JS in pywebview, Edge WebView2, Windows 11).
Referenz-Implementierung: `design/prototype.html` (alle Tokens als CSS-Variablen in Abschnitt 1 dort).
Bilder: `design/screenshots/` (`<screen>_<Breite>x<Höhe>[_z125|_z150]_<light|dark>.png`).

---

## 1. Richtung

**Eine ruhige Windows-11-Arbeitsfläche, auf der man immer sieht, was gerade passiert und was als Nächstes zu tun ist.**

- **Natives Gefühl statt Web-Look.** Segoe UI Variable, neutraler Mica-ähnlicher Hintergrund, 4/8-px-Radien,
  flache Karten mit Haarlinie, keine Verläufe, keine Glas-Effekte, keine Illustrationen. Das Programm soll neben
  Outlook und Teams wie ein Teil von Windows wirken.
- **Eine Akzentfarbe (Petrol) für alles Normale, Rot nur für „Aufnahme“.** Rot erscheint ausschließlich am
  Aufnahme-Knopf, am laufenden Aufnahmezustand und beim Löschen. So ist eine laufende Aufnahme unverwechselbar,
  ohne dass die übrige Oberfläche alarmiert.
- **Aufgaben vor Daten.** Die Startseite beantwortet drei Fragen: *Was kommt jetzt?* (nächster Termin mit
  Teilnehmenden und einem Klick zum Aufnehmen), *Was wartet auf mich?* (je Aufnahme genau eine Entscheidung mit
  genau einem Knopf), *Was läuft gerade?* (Verarbeitung als sichtbare Kette). Die Liste aller Aufnahmen ist die
  zweite Ebene.
- **Lesen ist die Hauptarbeit.** Zusammenfassung und Transkript werden wie ein Dokument gesetzt (16 px, 26 px
  Zeilenabstand, max. 74 Zeichen Zeilenlänge, Sprecherfarben, Zeitstempel in eigener Spalte).
- **Erklären statt Fachbegriffe.** Jede Einstellung hat einen Satz Klartext; technische Stellschrauben stehen
  eingeklappt unter „Für Fortgeschrittene“ bzw. „Feineinstellung“.

---

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

Kontrast nach WCAG 2.1, berechnet (Skript im Arbeitsverzeichnis, Formel relative Luminanz).
AA verlangt 4,5:1 für Text, 3:1 für Bedienelement-Grenzen und große Schrift.

**Neutrale Flächen und Text**

| Token | Hell | Dunkel | Verwendung | Kontrast hell / dunkel |
|---|---|---|---|---|
| `--bg` | `#F3F3F3` | `#1F1F1F` | Fensterhintergrund, Navigation, Liste | – |
| `--surface` | `#FFFFFF` | `#2A2A2A` | Karten, Detailbereich, Dialoge | – |
| `--surface-2` | `#F7F7F7` | `#303030` | Flächen in Karten (Hinweise, Kopf von Transkripten) | – |
| `--surface-hover` / `-pressed` | `#EFEFEF` / `#E7E7E7` | `#353535` / `#3B3B3B` | Hover/Druck | – |
| `--border` | `#E3E3E3` | `#3A3A3A` | dekorative Trennlinien | (dekorativ) |
| `--border-control` | `#858585` | `#8C8C8C` | Rahmen von Eingabefeldern, Checkboxen, Schaltern | 3,69 / 4,27 auf surface |
| `--text` | `#1B1B1B` | `#F2F2F2` | Fließtext, Titel | 17,2 / 12,8 (auf bg 15,5 / 14,7) |
| `--text-2` | `#565656` | `#C4C4C4` | Sekundärtext, Beschreibungen | 7,34 / 8,23 (auf bg 6,61) |
| `--text-3` | `#6B6B6B` | `#A3A3A3` | Metadaten, Zeitstempel, Hinweise | 5,33 / 5,69 (auf bg 4,80; dunkel auf surface-2 5,23) |

**Akzent (Petrol) – alles Normale: Primärknöpfe, Auswahl, Links, Fortschritt**

| Token | Hell | Dunkel | Kontrast |
|---|---|---|---|
| `--accent` | `#0F6E68` | `#5CCFC2` | als Text auf surface 6,09 / 7,63; auf bg 5,48 |
| `--text-on-accent` | `#FFFFFF` | `#062523` | Text auf Primärknopf 6,09 / 8,61 |
| `--accent-text` | `#0B5753` | `#7EDDD2` | auf `--accent-subtle` 7,28 / 7,56 |
| `--accent-subtle` | `#E3F2F0` | `#1E3B39` | ausgewählte Zeilen/Optionen, Chips |

**Aufnahme (Rot) – nur Aufnahme-Knopf, laufende Aufnahme, Löschen bestätigen**

| Token | Hell | Dunkel | Kontrast |
|---|---|---|---|
| `--rec` | `#C42B1C` | `#C42B1C` | Weiß auf Rot 5,66 (beide Modi gleich, damit der Knopf überall gleich aussieht); als Text hell 5,66 |
| `--rec-text` | `#A1251A` | `#FF9C94` | auf `--rec-subtle` 6,57 / 8,34; dunkel auf surface 7,13 |
| `--rec-subtle` | `#FDECEA` | `#46201D` | Aufnahme-Leiste, Status-Chip |

**Status (immer zusammen mit Icon und Text, nie nur Farbe)**

| Rolle | Text hell | Fläche hell | Text dunkel | Fläche dunkel | Kontrast Text auf Fläche hell / Text auf surface dunkel |
|---|---|---|---|---|---|
| ok (`--ok…`) | `#0E6B0E` | `#E6F4E3` | `#8EDB82` | `#1F3A1C` | 5,89 / 7,58 |
| Hinweis (`--warn…`) | `#7A4800` | `#FFF4D6` | `#F7CD6A` | `#3D3216` | 6,96 / 8,34 (auf Fläche) |
| Fehler (`--err…`) | `#9E2119` | `#FDECEA` | `#FFB4AD` | `#46201D` | 6,84 / 7,13 |
| Info (`--info…`) | `#00539A` | `#E5F0FA` | `#9AD2FF` | `#1A3247` | 6,72 / 7,65 |
| Markierung `--warn-mark` | `#C77A00` | `#F5C451` | – | – | nur für Icons/Ringe/Rahmen (≥ 3:1 nicht-textlich) |

**Sprecherfarben** (Namen im Transkript, Stimmkarten; Sie selbst = `--accent-text`)

| Token | Hell | Kontrast | Dunkel | Kontrast |
|---|---|---|---|---|
| `--sp-1` Blau | `#1F5FA8` | 6,44 | `#8CC2FF` | 7,71 |
| `--sp-2` Violett | `#7A3FB8` | 6,50 | `#C9A6F2` | 6,99 |
| `--sp-3` Magenta | `#A3266E` | 6,86 | `#F29AC9` | 7,00 |
| `--sp-4` Orange | `#9A4A00` | 6,26 | `#F5AE6B` | 7,62 |
| `--sp-5` Grün | `#2E7031` | 6,04 | `#8CD38F` | 8,09 |
| `--sp-6` Schiefer | `#4A5B6E` | 6,97 | `#AFC0D3` | 7,73 |

Farbe ist bei Sprechern nie das einzige Merkmal: Name steht immer daneben, „Sie“ trägt zusätzlich ein Etikett.

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
| Body | 14 / 20 | 400, stark 600 | gesamte Bedienung |
| Lesen | 16 / 26 | 400 | Zusammenfassung, Transkript (Kurzfassung 17 / 28) |
| Subtitle | 18 / 24 | 600 | Kartentitel, Abschnitte in Einstellungen |
| Title | 22 / 28 | 600 | Titel in Liste/Detail |
| Display | 28 / 36 | 600 | Seitentitel |
| Timer | 40 / 1 | 600 | Laufzeit der Aufnahme |

Lesetext: max. 74 Zeichen (`max-width: 74ch`), Transkript 80 Zeichen. Datum/Uhrzeit mit geschütztem Leerzeichen
(„2.&nbsp;Dezember“, „63&nbsp;%“, „30&nbsp;s“).

### 3.3 Abstände, Radien, Höhen

- Raster 4 px: `--s-1` 4 · `--s-2` 8 · `--s-3` 12 · `--s-4` 16 · `--s-5` 20 · `--s-6` 24 · `--s-8` 32 · `--s-10` 40.
- Seitenrand `--page-pad` 28 px (≤ 1279 px: 24, ≤ 1099 px: 20). Kartenabstand 20 px. Karte innen 20 px.
- Radien: `--r-sm` 4 (Knöpfe, Felder) · `--r-md` 6 (Navigationseinträge, Listenzeilen) · `--r-lg` 8 (Karten,
  Hinweise) · `--r-xl` 12 (Dialoge) · `--r-pill` (Chips, Avatare, Filter).
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

- **App-Rahmen:** Navigationsleiste links (232 px; ≤ 1099 px nur Symbole, 64 px) mit Übersicht, Aufnahmen,
  Einrichtung, Einstellungen; unten Aufnahme-Knopf (außer auf der Übersicht, dort ist er groß im Termin), Darstellung,
  Begleitfenster, Hilfe, Bereitschaft/Version. Rechts der Inhalt.
- **Aufnahme-Leiste:** Läuft eine Aufnahme, steht auf jeder Seite außer der Übersicht oben eine rote Leiste
  (Punkt, „Aufnahme läuft“, Laufzeit, Titel, „Beenden“); beim Countdown wird sie gelb mit Countdown und beiden Knöpfen.
- **Übersicht:** Spalten werden per JS befüllt (keine Lücken durch Zeilenhöhen):
  ≥ 1560 px drei Spalten (*Termin + In Arbeit* | *Wartet auf Sie* | *Heute + Zuletzt*),
  1100–1559 px zwei Spalten (*Termin, In Arbeit, Zuletzt* | *Wartet auf Sie, Heute*), darunter eine Spalte.
- **Aufnahmen:** Liste (300–380 px) + Detail. Die Aktionsleiste des Details bleibt beim Scrollen oben stehen; ab
  1080 px Detailbreite (Container-Query) erscheint rechts „Auf einen Blick“ (Sprechzeit, Zahl der Entscheidungen/
  Aufgaben, Dateien). Unter 1000 px: nur Liste *oder* Detail mit „‹ Alle Aufnahmen“.
- **Einstellungen:** Bereichsliste links (220 px), Inhalt max. 880 px; unter 900 px Bereiche als Kachelraster oben.
- **Prüfgrößen:** 1366×768 und 1920×1080 bei 100 %, 1366×768 bei 125 % (1093×614 CSS-px) und 150 % (911×512).
  Kein waagerechtes Scrollen, Dialoge scrollen innen (`max-height: 100vh − 48px`).

---

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
| Aufgabenliste | `.att` | Icon-Ton warn/ok/err/info | „Wartet auf Sie“: Titel, Zeit, ein Satz, ein Knopf |
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

1. **Sie-Form, kurze Hauptsätze, Aktiv.** „Ordnen Sie jede Stimme einer Person zu.“ – nicht „Die Zuordnung der
   Stimmen ist vorzunehmen.“
2. **Knöpfe sagen, was passiert** (Verb, Infinitiv): „Aufnahme starten …“, „Übergeben …“, „Jetzt beenden“,
   „Weiter aufnehmen“. Kein „OK“, kein „Ja/Nein“ ohne Inhalt.
3. **„…“ nur, wenn danach noch eine Rückfrage oder ein Dialog kommt** (Windows-Konvention).
4. **Fehler = was passiert ist + was trotzdem fertig ist + was Sie jetzt tun**, mit Knopf:
   „Zusammenfassung nicht erstellt: keine Verbindung zu Claude. Das Transkript ist fertig. Häufigste Ursache ist
   ein aktives VPN …“ – nie Fehlercodes oder Stacktraces in der Oberfläche (die stehen im Protokoll).
5. **Datenschutz offen benennen, ohne Angst zu machen:** „Transkript verlässt den Rechner“, „bleibt auf diesem
   Rechner“, „nur die Zusammenfassung – nie Audio oder Transkript“.
6. **Feste Begriffe** (nicht mischen): Aufnahme · Transkript · Zusammenfassung · Sprecher/Stimme · Teilnehmende ·
   Übergabe / übergeben (an SharePoint) · Einwilligung · Einrichtung · Einstellungen · Version.
   Nicht: Recording, Meeting, Protokoll (für die Zusammenfassung), Upload, Speaker, Prompt (in der Oberfläche:
   „Anweisungen“).
7. **Zahlen und Zeiten:** „52 Min“, „1 Std 3 Min“, „63 %“, „heute, 14:00“, „Fr 13.11.“; Laufzeit `00:23:41`.
8. **Keine Ausrufezeichen, keine Emojis, kein Marketington** („Super!“, „Ups“). Erfolg: kurze Bestätigung
   („Übergeben – SharePoint › Geschäftsführung › Protokolle“).
9. **Gendergerecht über neutrale Formen** („Teilnehmende“), keine Sternchen.
10. **Technik nur, wo nötig, dann erklärt:** „Split-Tunneling (python.exe und pythonw.exe im Programmordner)“.

---

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
| `#home` | Übersicht: nächster Termin + Aufnehmen, Wartet auf Sie, In Arbeit, Heute, Zuletzt |
| `#toast` | „Call erkannt: … – aufnehmen?“ als Begleitfenster unten rechts |
| `#consent` | Aufnahme vorbereiten: Titel, Teilnehmende, .ics, Einwilligung (§ 201 StGB, Claude-Hinweis) |
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
