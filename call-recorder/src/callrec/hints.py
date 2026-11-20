"""Turn error texts of the pipeline/providers into plain German with a suggested next step.

The texts themselves come from the components (most already explain the cause, e.g. the
Anthropic VPN hint); this module only adds what the user can *do* next and which button of
the main window does it.
"""

from __future__ import annotations

from dataclasses import dataclass

# action keys understood by the main window
RESUMMARIZE, REPROCESS, RETRANSCRIBE = "resummarize", "reprocess", "retranscribe"
SETTINGS_SUMMARY, SETTINGS_GENERAL, SETTINGS_SHAREPOINT = "settings:Zusammenfassung", "settings:Allgemein", "settings:SharePoint"
OPEN_LOG, LOGIN = "open_log", "login"


@dataclass(frozen=True)
class Hint:
    message: str  # what happened (the component's own text)
    next_step: str  # what to do now
    action: str = ""  # main-window action key for a button
    action_label: str = ""


def explain(text: str) -> Hint:
    t = text or ""
    low = t.casefold()
    if "abgebrochen" in low or "cancelled" in low:
        return Hint(t or "Verarbeitung abgebrochen.", "Bei Bedarf die Verarbeitung erneut starten; ein abgebrochener "
                    "Modell-Download setzt dort fort, wo er stand.")
    if "anthropic" in low and ("zeitüberschreitung" in low or "keine verbindung" in low or "verbindungsabbruch" in low
                               or "timeout" in low):
        nxt = "Danach „Zusammenfassung neu erstellen“ – das Transkript ist bereits fertig."
        if "vpn" not in low:
            nxt = ("Ein aktives VPN kann die Verbindung zu Anthropic blockieren: VPN kurz pausieren oder Split-Tunneling "
                   "einrichten. " + nxt)
        return Hint(t, nxt, RESUMMARIZE, "Zusammenfassung neu erstellen")
    if "api-schlüssel" in low or "kein schlüssel" in low or "401" in low:
        return Hint(t, "API-Schlüssel in den Einstellungen prüfen bzw. neu eintragen und „Verbindung testen“.",
                    SETTINGS_SUMMARY, "Einstellungen öffnen")
    if "guthaben" in low or "ausgabenlimit" in low or "credit" in low:
        return Hint(t, "In der Anthropic-Konsole Guthaben aufladen bzw. das Ausgabenlimit erhöhen, dann "
                    "„Zusammenfassung neu erstellen“.", RESUMMARIZE, "Zusammenfassung neu erstellen")
    if "ratenlimit" in low or "überlastet" in low or "529" in low:
        return Hint(t, "Einige Minuten warten, dann „Zusammenfassung neu erstellen“.", RESUMMARIZE,
                    "Zusammenfassung neu erstellen")
    if "erwähnt die übertragung an anthropic nicht" in low:
        return Hint(t, "„Zusammenfassung neu erstellen“ klicken – dann wird die nachträgliche Bestätigung abgefragt.",
                    RESUMMARIZE, "Zusammenfassung neu erstellen")
    if "ollama" in low or "ki-server" in low or "sprachmodell" in low or "nicht installiert (ollama pull" in low:
        return Hint(t, "Prüfen, ob Ollama bzw. der KI-Server läuft und das Modell geladen ist (Einrichtung → "
                    "„Verbindung testen“), dann „Zusammenfassung neu erstellen“.", RESUMMARIZE,
                    "Zusammenfassung neu erstellen")
    if "audiogeräte" in low or "stream ist nicht mehr aktiv" in low or "mikrofon liefert" in low:
        return Hint(t, "Headset prüfen (angeschlossen, in Windows als Standard für Kommunikation?) oder das Gerät in den "
                    "Einstellungen fest wählen.", SETTINGS_GENERAL, "Geräte einstellen")
    if "übergabeordner" in low or "upload fehlgeschlagen" in low or "sharepoint" in low:
        return Hint(t, "Übergabe-Einstellungen prüfen (Ordner vorhanden, OneDrive läuft?).", SETTINGS_SHAREPOINT,
                    "SharePoint-Einstellungen")
    if "keine fertigen audiodateien" in low or "transkription fehlt" in low:
        return Hint(t, "„Komplett neu verarbeiten“ ausführen.", REPROCESS, "Komplett neu verarbeiten")
    if "cuda" in low or "cudnn" in low or "grafikspeicher" in low:
        return Hint(t, "Erneut transkribieren – CallRecorder nutzt dann die CPU. Details im Protokoll.", RETRANSCRIBE,
                    "Neu transkribieren …")
    if "download" in low or "huggingface" in low:
        return Hint(t, "Internetverbindung prüfen und erneut starten; der Download setzt fort.", OPEN_LOG,
                    "Protokoll öffnen")
    if "anmeldung" in low or "aadsts" in low:
        return Hint(t, "Erneut anmelden; Fehlercodes erklärt README Abschnitt 15.", LOGIN, "Anmelden …")
    return Hint(t, "Details stehen im Protokoll. Die Aufnahme selbst bleibt erhalten.", OPEN_LOG, "Protokoll öffnen")
