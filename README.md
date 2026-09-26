# Embedding Flight

Animierte Visualisierung für kontextuelles Targeting im True-Relevance-Look.

Die Seite zeigt in fünf Schritten, wie ein Briefing- oder Produkttext zu passendem Content findet:

1. Der Text wird in ein Embedding übersetzt (ein Vektor mit über 1.000 Dimensionen).
2. Der Vektor fliegt als Trajektorie durch den Embedding-Raum.
3. Er erreicht seinen Zielpunkt.
4. Um den Zielpunkt erscheinen nach und nach die Embeddings der digitalen Inhalte als Wolke. Je näher, desto ähnlicher.
5. Die nächsten Inhalte sind das Ergebnis.

## Öffnen

`index.html` ist eine einzelne Datei ohne Build-Schritt. Im Browser öffnen, fertig.
Die Schrift Gabarito wird von Google Fonts geladen; ohne Netz greift der Fallback (Nunito Sans, Systemschrift).

- **Neu abspielen** startet die Animation von vorn.
- Die fünf Kreise über der Erklärung springen direkt zu einem Schritt.
- Ziehen mit Maus oder Finger dreht den Raum.
- Sobald der Vektor angekommen ist, zoomt das Mausrad oder ein Zwei-Finger-Pinch am Zielpunkt hinein und heraus.
  Unten rechts liegen dazu die Kreise „−“ und „+“; ein Klick auf die Zoomanzeige setzt auf 1,0× zurück.
  Tastatur: `+`, `-` und `0`.
- Bei aktivem `prefers-reduced-motion` startet die Seite im Endzustand.

## Anpassen

Alle Inhalte und Zeiten stehen am Anfang des Skripts in `index.html`:

| Konstante | Bedeutung |
| --- | --- |
| `DIMENSIONS_LABEL` | Text hinter „Embedding ·“ (Standard: „über 1.000 Dimensionen“) |
| `THRESHOLD_LABEL` | Angezeigte Ähnlichkeitsschwelle im Zählerpanel |
| `CLOUD_COUNT` | Anzahl der Punkte in der Wolke |
| `TL` | Zeitpunkte der Phasen in Sekunden (Flugstart, Ankunft, Wolke, Ergebnis) |
| `CAPTIONS` | Erklärtexte der fünf Schritte |
| `TYPES`, `TYPE_WEIGHTS` | Content-Typen mit Markenfarbe und Anteil in der Wolke |
| `TOP` | Die fünf nächsten Inhalte mit Titel, Typ und Ähnlichkeit |

Das Beispiel-Briefing steht im HTML im Abschnitt `callout`.

## Gestaltung

Farben, Schrift und Layout folgen den True-Relevance-Brand-Guidelines: weißer Grund, Text in Ink,
die vier Kernfarben nur als transparente Flächen (20–50 %), Callout auf `callout-cream`,
Statistikpanel auf `neutral-20`, Logo oben rechts. Die Trajektorie ist eine Linie in Ink,
die Wolke besteht aus überlappenden transparenten Kreisen.
