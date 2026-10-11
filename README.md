# Embedding Flight

Animierte Visualisierung für kontextuelles Targeting im True-Relevance-Look.

Die Seite zeigt in fünf Schritten, wie ein Briefing- oder Produkttext zu passendem Content findet:

1. Der Text wird in ein Embedding übersetzt (ein Vektor mit über 1.000 Dimensionen).
2. Der Vektor fliegt als Trajektorie durch den Embedding-Raum.
3. Er erreicht seinen Zielpunkt.
4. Um den Zielpunkt erscheinen nach und nach die Embeddings der digitalen Inhalte als Wolke. Je näher, desto ähnlicher.
5. Die nächsten Inhalte sind das Ergebnis.

## Zweite Seite: Acht Welten (`themenwelten.html`)

Dieselbe Gestaltung, ein anderes Bild: Acht Themenwelten stehen als transparente Quader nebeneinander, wie
die parallelen Dimensionen aus Darstellungen der Stringtheorie. Startpunkt ist eine Marke (Beispiel: Magnum
Gold Billionaire Caramel). Ihr Bedeutungsvektor durchstößt jeden Quader, und um den Durchstoßpunkt erscheinen
die Inhalte dieser Welt mit ähnlichem psychologischem Profil: Desserts, Online-Artikel, Automodelle,
Bekleidungsmarken, Arbeitgebermarken, Arbeitgeber-Benefits, Fernsehshows und Sportarten. Weitere Welten
lassen sich in der JSON anhängen; Titel, Zählungen und Ablauf passen sich an.

- Das Profil ist ein 3×3-Motivraster: Spalten Macht, Leistung, Anschluss; Zeilen Emotional, Rational, Handlung.
  Die neun Werte in `profile` sind in der Reihenfolge Motiv × Haltung abgelegt (Macht-Emotional, Macht-Rational, …).
  Die Ähnlichkeit ist die Kosinus-Ähnlichkeit der Profile, als Winkelmaß gespreizt und auf 0,55 bis 0,97 abgebildet.
- Klick auf eine Scheibe (oder einen Punkt, eine Zeile der Weltenliste, die Tasten 1 bis 7 oder die Pfeile)
  stellt die Welt frontal dar; „Alle Welten“, Escape oder 0 führen zur Übersicht zurück.
- Hover zeigt Name, Ähnlichkeit, Begründung und das Mini-Raster des Inhalts.
- Jede Dimension ist ein Quader mit Tiefe; die Inhalte verteilen sich im Volumen. Der runde Schalter rechts in der
  Weltenliste blendet eine Dimension aus, „Alle einschalten“ holt sie zurück.
- Die Daten liegen in `data/themenwelten.json` (Beispieldaten mit echten Marken- und Titelnamen).
  Nach einer Änderung bettet `python3 tools/inline-data.py` sie wieder in die Seite ein.
- `themenwelten.html#canvas` zeigt nur die Animation. Für ein Video:
  `node tools/render-video.js --page themenwelten.html --api siebenWelten --seconds 16 --no-zoom`.

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
