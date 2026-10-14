# Embedding Flight

Animierte Visualisierung für kontextuelles Targeting im True-Relevance-Look.

Die Seite zeigt in fünf Schritten, wie ein Briefing- oder Produkttext zu passendem Content findet:

1. Der Text wird in ein Embedding übersetzt (ein Vektor mit über 1.000 Dimensionen).
2. Der Vektor fliegt als Trajektorie durch den Embedding-Raum.
3. Er erreicht seinen Zielpunkt.
4. Um den Zielpunkt erscheinen nach und nach die Embeddings der digitalen Inhalte als Wolke. Je näher, desto ähnlicher.
5. Die nächsten Inhalte sind das Ergebnis.

## Zweite Seite: Marken-Universen (`themenwelten.html`)

Dieselbe Gestaltung, ein anderes Bild. Startpunkt ist eine Marke (Beispiel: Magnum Gold Billionaire Caramel).
Ihr Bedeutungsvektor durchsticht zuerst die **Psychografie**: einen Stapel transparenter Heatmaps (3×3-Raster,
Spalten Macht, Leistung, Anschluss; Zeilen Emotional, Rational, Handlung), die aufrecht hintereinander stehen.
Danach folgen die Themenwelten als transparente Quader mit Tiefe, in Gruppen mit Abständen und
Schriftzügen im Raum:

1. **Situation**, direkt hinter der Psychografie: Category Entry Points, also die Situationen, in denen Menschen an
   die Kategorie denken (Anlass, Zeitpunkt, Ort, Begleitung, Stimmung).
2. In-Category Produkte (ohne Gruppennamen, Abstand zwei Dimensionen).
3. **Paid Media** (Abstand 1,5 Dimensionen): Websites, Streaming / CTV, YouTube Creator Videos (einzelne Videos,
   keine Kanäle), Podcast, Kinofilme, DOOH (Orte, Nähe über die Bedeutung des Ortes), Influencer,
   Markenkooperation Automobil, Markenkooperation Fashion, Markenkooperation Sport, Markenkooperation Veranstaltungen.
4. **Owned Media & 1st Party** (Abstand zwei Dimensionen): Brand-App-Features, Next Best Offer,
   Retail-Features online, Retail-Features offline.

- Jede Welt hat 21 benannte Inhalte (Beispieldaten mit echten Namen) und 16 unbenannte Füllpunkte im Quader.
  Position und Ähnlichkeit kommen aus der Kosinus-Ähnlichkeit der 3×3-Profile (als Winkelmaß gespreizt auf 0,55 bis 0,97).
- Die Reihenfolge, die Gruppen (`groups` mit `gapBefore` als Vielfaches des Quaderabstands; bei der ersten Gruppe der
  Abstand zum Psychografie-Stapel) und die Heatmaps des Psychografie-Stapels (`psychografie.layers`, je ein Profil aus neun Werten) stehen in `data/themenwelten.json`.
  Eine Heatmap kann statt des Profils auch ein Bild zeigen: `"image"` mit einer Data-URL, oder `"imageFile"` mit einem
  Pfad unter `assets/`, den `tools/inline-data.py` beim Einbetten in eine Data-URL umwandelt. Eine Welt kann mit
  `"color"` (`pink`, `blue`, `yellow`, `green`) eine feste Farbe bekommen; die übrigen Welten nehmen die Farben reihum.
- Klick auf eine Dimension (oder einen Punkt, eine Listenzeile, die Ziffern 1 bis 9 für die ersten neun Welten oder
  die Pfeile) stellt die Welt frontal dar; „Alle Welten“, Escape oder 0 führen zur Übersicht zurück. Die Listenzeilen
  sind auch per Tastatur erreichbar (Tab, Enter).
- In der Nahansicht sind alle 21 benannten Inhalte beschriftet; dünne Linien führen vom Markenpunkt zu den nahen
  davon (Ähnlichkeit ab 0,85, `LINE_MIN_SIM`). Dazu erscheinen 400 weitere Inhalte vom Durchstoßpunkt nach außen,
  innerhalb und außerhalb des Quaders (`N_EXTRA`), und ein Zähler läuft mit.
- **Navigation entlang des Vektors** (oben links auf der Canvas): die Chips „Übersicht“, „Psychografie“, „Situation“,
  „Paid Media“ und „Owned Media & 1st Party“ rahmen den jeweiligen Abschnitt, der Regler fährt die Kamera die Kette entlang,
  ebenso Shift + Scrollen (oder waagerechtes Scrollen) und Bild-auf/Bild-ab beziehungsweise Shift + Pfeiltasten
  von Dimension zu Dimension. Drehen und Zoomen bleiben wie gehabt.
- Die Gruppennamen schweben als Schriftzüge mittig unter ihren Dimensionen und bleiben so aus jedem Blickwinkel bei
  ihnen. „Psychografie“ und „Situation“ stehen in einer gemeinsamen Zeile kleinerer Schrift nebeneinander; würden sie
  sich berühren, rücken beide gleich weit auseinander (`WORD_ROW_H`, `WORD_GAP`).
- Der runde Schalter rechts in der Weltenliste blendet eine Dimension aus. Die Gruppenzeilen „Psychografie“,
  „Situation“, „Paid Media“ und „Owned Media & 1st Party“ haben ebenfalls einen Schalter: Er blendet die ganze Gruppe aus, mit
  ihrem Schriftzug im Raum (bei Psychografie den Heatmap-Stapel) und ihrem Chip in der Navigation. Sind nur einige
  Dimensionen einer Gruppe aktiv, zeigt der Gruppenschalter das als Zwischenzustand. „Alle einschalten“ holt alles zurück.
  Der Vektor endet immer bei der letzten eingeschalteten Dimension: Ohne Owned Media & 1st Party hört er nach der
  letzten Paid-Media-Dimension auf, Übersicht, Regler und Flugdauer rahmen dann nur diesen Teil der Kette.
  Hover zeigt Name, Ähnlichkeit, Begründung und das Mini-Raster des Inhalts.
- „Menü ausblenden“ unter der Weltenliste (oder die Taste M) klappt die linke Spalte weg, die Canvas nimmt dann die
  ganze Breite. „Menü einblenden“ oben links auf der Canvas bringt sie zurück; der Browser merkt sich die Wahl.
- Nach einer Änderung der Daten bettet `python3 tools/inline-data.py` sie wieder in die Seite ein.
- `themenwelten.html#canvas` zeigt nur die Animation. Für ein Video:
  `node tools/render-video.js --page themenwelten.html --api siebenWelten --seconds 20 --no-zoom`.

## Öffnen

`index.html` ist eine einzelne Datei ohne Build-Schritt. Im Browser öffnen, fertig.
Die Schrift Gabarito wird von Google Fonts geladen; ohne Netz greift der Fallback (Nunito Sans, Systemschrift).

- **Neu abspielen** startet die Animation von vorn.
- Die fünf Kreise über der Erklärung springen direkt zu einem Schritt.
- Ziehen mit Maus oder Finger dreht den Raum.
- Sobald der Vektor angekommen ist, zoomt das Mausrad oder ein Zwei-Finger-Pinch am Zielpunkt hinein und heraus.
  Unten rechts liegen dazu die Kreise „−“ und „+“; ein Klick auf die Zoomanzeige setzt auf 1,0× zurück.
  Tastatur: `+`, `-` und `0`.
- **Ebenen:** Ein Klick auf einen Punkt der Wolke öffnet die nächste Ebene. Die Kamera fliegt zu diesem Inhalt,
  und sein eigenes Umfeld erscheint als neue Wolke (die nächsten Inhalte zu diesem Inhalt). Das geht beliebig tief.
  Zurück führen der Pfad oben links auf der Canvas, der Knopf „← Zurück“ oder die Escape-Taste.
  Mit der Maus zeigt ein Hinweis Titel und Ähnlichkeit des Punkts unter dem Zeiger.
- **Kontextfilter** (oben links auf der Canvas): „Alle“, „Direkter Kontext“ (Ähnlichkeit ab 0,85) oder
  „Verwandter Kontext“ (0,75 bis 0,85). Zähler, Trefferliste und Markierungen folgen dem Filter, auch auf tieferen Ebenen.
- Bei aktivem `prefers-reduced-motion` startet die Seite im Endzustand.

## In PowerPoint, Keynote oder Google Slides einbauen

PowerPoint spielt kein HTML ab. Für die Folie gibt es deshalb zwei Wege:

**1. Als Video (empfohlen für die Animation).** Im Ordner `export/` liegen fertige MP4-Dateien
(H.264, 1920 × 1080, 30 fps, 26 s, ohne Ton):

- `embedding-flight-full-1920x1080.mp4` – ganze Seite mit Textspalte und Logo
- `embedding-flight-canvas-1920x1080.mp4` – nur die Animation, für eigene Folientexte daneben

In PowerPoint: *Einfügen → Video → Dieses Gerät*, dann unter *Wiedergabe* „Automatisch starten“
und bei Bedarf „Endlosschleife“ wählen. Am Ende des Videos fährt die Kamera einmal an den Zielpunkt
heran und wieder heraus.

Neu rendern (etwa zwei Minuten):

```
npm install playwright && npx playwright install chromium
node tools/render-video.js --mode full
node tools/render-video.js --mode canvas
```

Optionen: `--seconds`, `--fps`, `--width`, `--height`, `--no-zoom` (ohne Zoomfahrt), `--out`.
Voraussetzung ist ein `ffmpeg` mit libx264 im PATH oder als `FFMPEG=/pfad/zu/ffmpeg`.

**2. Live und interaktiv (Drehen und Zoomen im Vortrag).** Ein Bild der Animation auf die Folie legen
und mit `index.html` verknüpfen (*Einfügen → Link*), entweder als Datei neben der Präsentation oder
als gehostete Adresse. Im Vortrag öffnet der Klick den Browser (F11 für Vollbild), zurück geht es mit
Alt + Tab. Alternativ zeigt das Microsoft-Add-in „Web Viewer“ eine per HTTPS gehostete Seite direkt auf
der Folie; das setzt Internet im Vortragsraum voraus und ist in manchen Firmenumgebungen gesperrt.

`index.html#canvas` öffnet die Seite ohne Textspalte und Logo.

## Anpassen

Alle Inhalte und Zeiten stehen am Anfang des Skripts in `index.html`:

| Konstante | Bedeutung |
| --- | --- |
| `DIMENSIONS_LABEL` | Text hinter „Embedding ·“ (Standard: „über 1.000 Dimensionen“) |
| `THRESHOLD_LABEL` | Angezeigte Ähnlichkeitsschwelle im Zählerpanel |
| `CLOUD_COUNT` | Anzahl der Punkte in der Wolke um das Briefing |
| `SUB_COUNT`, `LEVEL_SCALE` | Punkte pro tieferer Ebene und wie viel kleiner jede Ebene ist |
| `THRESHOLD`, `DIRECT_THRESHOLD` | Untergrenze der Wolke und Grenze zwischen direktem und verwandtem Kontext |
| `TITLE_POOL` | Titel für die übrigen Punkte und für tiefere Ebenen |
| `TL` | Zeitpunkte der Phasen in Sekunden (Flugstart, Ankunft, Wolke, Ergebnis) |
| `CAPTIONS` | Erklärtexte der fünf Schritte |
| `TYPES`, `TYPE_WEIGHTS` | Content-Typen mit Markenfarbe und Anteil in der Wolke |
| `TOP` | Die fünf nächsten Inhalte mit Titel, Typ und Ähnlichkeit |

Für den Videoexport stellt die Seite `window.embeddingFlight.seek(sekunden, {zoom})` bereit;
`tools/render-video.js` zeichnet damit Bild für Bild und übergibt die Bilder an ffmpeg.
Dieselbe Schnittstelle bietet `dive(index)`, `up()`, `filter('all'|'direct'|'related')` und `points()`,
etwa um eine Ebenen-Fahrt zu skripten.

Das Beispiel-Briefing steht im HTML im Abschnitt `callout`.

## Gestaltung

Farben, Schrift und Layout folgen den True-Relevance-Brand-Guidelines: weißer Grund, Text in Ink,
die vier Kernfarben nur als transparente Flächen (20–50 %), Callout auf `callout-cream`,
Statistikpanel auf `neutral-20`, Logo oben rechts. Die Trajektorie ist eine Linie in Ink,
die Wolke besteht aus überlappenden transparenten Kreisen.
