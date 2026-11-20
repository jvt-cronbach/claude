"""Plain, user-editable tuning files in %APPDATA%\\CallRecorder.

- ``glossary.txt``      names, company and technical terms (-> Whisper hotwords)
- ``replacements.txt``  post-processing corrections for recurring mis-recognitions
- ``prompts/*.txt``     templates for the summary LLM
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from callrec import paths

log = logging.getLogger(__name__)

GLOSSARY_TEMPLATE = """\
# Glossar für die Transkription: ein Begriff pro Zeile (Namen, Firmen, Produkte, Fachbegriffe).
# Die Begriffe werden Whisper als "Hotwords" mitgegeben und verbessern die Schreibweise.
# Zeilen mit # werden ignoriert. Nicht zu viele Begriffe (max. ca. 50-100 Wörter wirken).
#
# Beispiel:
# Jour fixe
# Kubernetes
"""

REPLACEMENTS_TEMPLATE = """\
# Korrekturen, die nach der Transkription angewendet werden (eine Regel pro Zeile).
#   falsch => richtig              ganzes Wort/Wortgruppe, Groß-/Kleinschreibung egal
#   re: regulärer Ausdruck => Ersatz   (Python-Syntax, \\1 für Gruppen)
# Zeilen mit # werden ignoriert. Die Rohdaten bleiben unverändert, nur das Transkript wird korrigiert.
#
# Beispiele:
# Kuba Netties => Kubernetes
# re: \\bSchur fix\\b => Jour fixe
"""

SUMMARY_SYSTEM_TEMPLATE = """\
Du bist ein sorgfältiger Protokollführer. Du erstellst aus dem Transkript eines Gesprächs ein sachliches Ergebnisprotokoll auf Deutsch.
Das Protokoll wird später automatisch weiterverarbeitet (Aufgabenlisten, Projektstände). Halte deshalb die vorgegebene Gliederung exakt ein.
Regeln:
- Verwende ausschließlich Informationen aus dem Transkript. Erfinde nichts.
- Nenne Personen mit den Namen aus dem Transkript. "Sprecher N" bleibt "Sprecher N".
- Verantwortliche und Fristen nur, wenn sie im Gespräch ausdrücklich genannt wurden. Sonst schreibe genau "offen". Rate keine Namen und keine Daten.
- Übernimm Fristen und Termine so, wie sie genannt wurden (z. B. "bis Freitag"); rechne sie nicht in Kalenderdaten um.
- Verwende genau die vorgegebenen Überschriften in der vorgegebenen Reihenfolge, auch wenn ein Abschnitt leer bleibt (dann "Keine").
- Die Zusammenfassung konzentriert sich ausschließlich auf geschäftsrelevante Inhalte. Smalltalk, Tratsch und Witze werden nicht berücksichtigt - ebenso wenig Begrüßung und Verabschiedung, private Gespräche, persönliche Neuigkeiten (Gesundheit, Familie, Urlaub), Geplänkel und Gerede über Dritte, auch nicht andeutungsweise.
- Ein Witz, eine scherzhafte Bemerkung oder eine Randbemerkung ist nie eine Entscheidung, keine Aufgabe, kein Projektstand und keine offene Frage - nimm sie in keinen Abschnitt auf.
- Enthält das Gespräch keine geschäftsrelevanten Inhalte, antworte nur mit genau einer Zeile: "Keine geschäftsrelevanten Inhalte – nichts protokolliert."
- Das Transkript wurde automatisch erstellt und kann Erkennungsfehler enthalten.
- Antworte nur mit dem Protokoll in Markdown, ohne Vorrede und ohne Schlussbemerkung.
"""

SUMMARY_FINAL_TEMPLATE = """\
Erstelle das Ergebnisprotokoll nur über die geschäftsrelevanten Inhalte (Smalltalk, Tratsch, Witze und Privates nicht berücksichtigen; ein Witz oder eine Randbemerkung ist keine Entscheidung, Aufgabe, kein Projektstand und keine offene Frage) mit genau diesen sechs Abschnitten, in dieser Reihenfolge und mit genau diesen Überschriften. Gab es keine geschäftsrelevanten Inhalte, antworte stattdessen nur mit der einen Zeile "Keine geschäftsrelevanten Inhalte – nichts protokolliert."

## Kurzfassung
(3-6 Sätze: Anlass des Gesprächs und wichtigste Ergebnisse)

## Entscheidungen
- (eine Entscheidung pro Zeile; "Keine" wenn es keine gab)

## Aufgaben
| Aufgabe | Verantwortlich | Frist |
|---|---|---|
| (konkrete Aufgabe) | (Name aus dem Transkript oder "offen") | (Frist wie genannt oder "offen") |
(Gab es keine Aufgaben, genau eine Zeile: | Keine | - | - |)

## Projektstand und Updates
### (Name des Projekts oder Themas, wie im Gespräch genannt)
- (Stand, Fortschritt, Probleme, Änderungen)
(Ein Unterabschnitt pro Projekt/Thema; "Keine" wenn keine Projekte besprochen wurden)

## Offene Fragen
- (eine Frage pro Zeile; "Keine" wenn es keine gab)

## Nächste Termine
- (Termin wie genannt: wann, was, mit wem; "Keine genannt" wenn keine)

Gespräch: {title}
Datum: {date}
Teilnehmende: {participants}

{material_label}:
{material}
"""

SUMMARY_CHUNK_TEMPLATE = """\
Das folgende ist Teil {i} von {n} eines Gesprächstranskripts.
Fasse diesen Teil in Stichpunkten zusammen, gegliedert nach: besprochene Themen, Entscheidungen, Aufgaben (mit verantwortlicher Person und Frist nur, wenn genannt, sonst "offen"), Projektstand und Updates (je Projekt/Thema), offene Fragen, genannte nächste Termine. Nur geschäftsrelevante Inhalte aus diesem Teil, nichts erfinden; Smalltalk, Tratsch, Witze und Privates (Begrüßung, persönliche Neuigkeiten, Gesundheit, Familie, Geplänkel über Dritte) nicht berücksichtigen - ein Witz oder eine Randbemerkung ist keine Entscheidung, Aufgabe oder offene Frage. Enthält der Teil nichts Geschäftsrelevantes, schreibe nur "(nichts Geschäftsrelevantes)".

{chunk}
"""

# Earlier shipped defaults: an unchanged copy on disk is upgraded to the new default automatically;
# a customised file is never touched (the new default is written next to it as *.default.txt).
_PREVIOUS_DEFAULTS = {
    "summary_system.txt": [
        'Du bist ein sorgfältiger Protokollführer. Du erstellst aus dem Transkript eines Gesprächs ein sachliches Ergebnisprotokoll auf Deutsch.\nDas Protokoll wird später automatisch weiterverarbeitet (Aufgabenlisten, Projektstände). Halte deshalb die vorgegebene Gliederung exakt ein.\nRegeln:\n- Verwende ausschließlich Informationen aus dem Transkript. Erfinde nichts.\n- Nenne Personen mit den Namen aus dem Transkript. "Sprecher N" bleibt "Sprecher N".\n- Verantwortliche und Fristen nur, wenn sie im Gespräch ausdrücklich genannt wurden. Sonst schreibe genau "offen". Rate keine Namen und keine Daten.\n- Übernimm Fristen und Termine so, wie sie genannt wurden (z. B. "bis Freitag"); rechne sie nicht in Kalenderdaten um.\n- Verwende genau die vorgegebenen Überschriften in der vorgegebenen Reihenfolge, auch wenn ein Abschnitt leer bleibt (dann "Keine").\n- Protokolliere nur geschäftliche Inhalte. Lass Smalltalk und Privates bewusst weg: Begrüßung und Verabschiedung, private Gespräche, persönliche Neuigkeiten, Gesundheit, Familie, Urlaub und Gerede über Dritte - auch nicht andeutungsweise.\n- Ist das ganze Gespräch privat (keine geschäftlichen Inhalte), antworte nur mit genau einer Zeile: "Privates Gespräch – keine geschäftlichen Inhalte protokolliert."\n- Das Transkript wurde automatisch erstellt und kann Erkennungsfehler enthalten.\n- Antworte nur mit dem Protokoll in Markdown, ohne Vorrede und ohne Schlussbemerkung.\n',  # 0.3.0
        'Du bist ein sorgfältiger Protokollführer. Du erstellst aus dem Transkript eines Gesprächs ein sachliches Ergebnisprotokoll auf Deutsch.\nDas Protokoll wird später automatisch weiterverarbeitet (Aufgabenlisten, Projektstände). Halte deshalb die vorgegebene Gliederung exakt ein.\nRegeln:\n- Verwende ausschließlich Informationen aus dem Transkript. Erfinde nichts.\n- Nenne Personen mit den Namen aus dem Transkript. "Sprecher N" bleibt "Sprecher N".\n- Verantwortliche und Fristen nur, wenn sie im Gespräch ausdrücklich genannt wurden. Sonst schreibe genau "offen". Rate keine Namen und keine Daten.\n- Übernimm Fristen und Termine so, wie sie genannt wurden (z. B. "bis Freitag"); rechne sie nicht in Kalenderdaten um.\n- Verwende genau die vorgegebenen Überschriften in der vorgegebenen Reihenfolge, auch wenn ein Abschnitt leer bleibt (dann "Keine").\n- Das Transkript wurde automatisch erstellt und kann Erkennungsfehler enthalten.\n- Antworte nur mit dem Protokoll in Markdown, ohne Vorrede und ohne Schlussbemerkung.\n',
        "Du bist ein sorgfältiger Protokollführer. Du erstellst aus dem Transkript eines Gesprächs ein kurzes, sachliches Ergebnisprotokoll auf Deutsch.\n"
        "Regeln:\n"
        "- Verwende ausschließlich Informationen aus dem Transkript. Erfinde nichts.\n"
        "- Nenne Personen mit den Namen aus dem Transkript. \"Sprecher N\" bleibt \"Sprecher N\".\n"
        "- Wenn für eine Aufgabe niemand verantwortlich ist, schreibe \"offen\". Wenn keine Frist genannt wurde, schreibe \"-\".\n"
        "- Das Transkript wurde automatisch erstellt und kann Erkennungsfehler enthalten.\n"
        "- Antworte nur mit dem Protokoll in Markdown, ohne Vorrede.\n",
    ],
    "summary_final.txt": [
        'Erstelle das Ergebnisprotokoll nur über die geschäftlichen Inhalte (Smalltalk und Privates weglassen) mit genau diesen sechs Abschnitten, in dieser Reihenfolge und mit genau diesen Überschriften. War das Gespräch ausschließlich privat, antworte stattdessen nur mit der einen Zeile "Privates Gespräch – keine geschäftlichen Inhalte protokolliert."\n\n## Kurzfassung\n(3-6 Sätze: Anlass des Gesprächs und wichtigste Ergebnisse)\n\n## Entscheidungen\n- (eine Entscheidung pro Zeile; "Keine" wenn es keine gab)\n\n## Aufgaben\n| Aufgabe | Verantwortlich | Frist |\n|---|---|---|\n| (konkrete Aufgabe) | (Name aus dem Transkript oder "offen") | (Frist wie genannt oder "offen") |\n(Gab es keine Aufgaben, genau eine Zeile: | Keine | - | - |)\n\n## Projektstand und Updates\n### (Name des Projekts oder Themas, wie im Gespräch genannt)\n- (Stand, Fortschritt, Probleme, Änderungen)\n(Ein Unterabschnitt pro Projekt/Thema; "Keine" wenn keine Projekte besprochen wurden)\n\n## Offene Fragen\n- (eine Frage pro Zeile; "Keine" wenn es keine gab)\n\n## Nächste Termine\n- (Termin wie genannt: wann, was, mit wem; "Keine genannt" wenn keine)\n\nGespräch: {title}\nDatum: {date}\nTeilnehmende: {participants}\n\n{material_label}:\n{material}\n',  # 0.3.0
        'Erstelle das Ergebnisprotokoll mit genau diesen sechs Abschnitten, in dieser Reihenfolge und mit genau diesen Überschriften:\n\n## Kurzfassung\n(3-6 Sätze: Anlass des Gesprächs und wichtigste Ergebnisse)\n\n## Entscheidungen\n- (eine Entscheidung pro Zeile; "Keine" wenn es keine gab)\n\n## Aufgaben\n| Aufgabe | Verantwortlich | Frist |\n|---|---|---|\n| (konkrete Aufgabe) | (Name aus dem Transkript oder "offen") | (Frist wie genannt oder "offen") |\n(Gab es keine Aufgaben, genau eine Zeile: | Keine | - | - |)\n\n## Projektstand und Updates\n### (Name des Projekts oder Themas, wie im Gespräch genannt)\n- (Stand, Fortschritt, Probleme, Änderungen)\n(Ein Unterabschnitt pro Projekt/Thema; "Keine" wenn keine Projekte besprochen wurden)\n\n## Offene Fragen\n- (eine Frage pro Zeile; "Keine" wenn es keine gab)\n\n## Nächste Termine\n- (Termin wie genannt: wann, was, mit wem; "Keine genannt" wenn keine)\n\nGespräch: {title}\nDatum: {date}\nTeilnehmende: {participants}\n\n{material_label}:\n{material}\n',
        "Erstelle das Ergebnisprotokoll mit genau diesen Abschnitten:\n\n## Kurzfassung\n(3-6 Sätze)\n\n"
        "## Entscheidungen\n(Aufzählung; \"Keine\" wenn es keine gab)\n\n## Aufgaben\n"
        "| Aufgabe | Verantwortlich | Frist |\n|---|---|---|\n\n## Offene Punkte\n"
        "(Aufzählung; \"Keine\" wenn es keine gab)\n\nGespräch: {title}\nDatum: {date}\n"
        "Teilnehmende: {participants}\n\n{material_label}:\n{material}\n",
    ],
    "summary_chunk.txt": [
        'Das folgende ist Teil {i} von {n} eines Gesprächstranskripts.\nFasse diesen Teil in Stichpunkten zusammen, gegliedert nach: besprochene Themen, Entscheidungen, Aufgaben (mit verantwortlicher Person und Frist nur, wenn genannt, sonst "offen"), Projektstand und Updates (je Projekt/Thema), offene Fragen, genannte nächste Termine. Nur geschäftliche Inhalte aus diesem Teil, nichts erfinden; Smalltalk und Privates (Begrüßung, persönliche Neuigkeiten, Gesundheit, Familie, Gerede über Dritte) weglassen. Ist der Teil ganz privat, schreibe nur "(privat)".\n\n{chunk}\n',  # 0.3.0
        'Das folgende ist Teil {i} von {n} eines Gesprächstranskripts.\nFasse diesen Teil in Stichpunkten zusammen, gegliedert nach: besprochene Themen, Entscheidungen, Aufgaben (mit verantwortlicher Person und Frist nur, wenn genannt, sonst "offen"), Projektstand und Updates (je Projekt/Thema), offene Fragen, genannte nächste Termine. Nur Inhalte aus diesem Teil, nichts erfinden.\n\n{chunk}\n',
        "Das folgende ist Teil {i} von {n} eines Gesprächstranskripts.\n"
        "Fasse diesen Teil in Stichpunkten zusammen: besprochene Themen, Entscheidungen, Aufgaben (mit verantwortlicher "
        "Person und Frist, falls genannt) und offene Fragen. Nur Inhalte aus diesem Teil.\n\n{chunk}\n",
    ],
}

# The one line a summary of an entirely private call consists of (no sections expected then)
NO_BUSINESS_LINE = "Keine geschäftsrelevanten Inhalte – nichts protokolliert."
PRIVATE_CALL_LINE = "Privates Gespräch – keine geschäftlichen Inhalte protokolliert."  # the line of 0.3.0

# Section headings the summary is expected to contain (checked tolerantly, see summary.missing_sections)
SUMMARY_SECTIONS = ("Kurzfassung", "Entscheidungen", "Aufgaben", "Projektstand und Updates", "Offene Fragen", "Nächste Termine")

PROMPT_FILES = {
    "system": ("summary_system.txt", SUMMARY_SYSTEM_TEMPLATE),
    "final": ("summary_final.txt", SUMMARY_FINAL_TEMPLATE),
    "chunk": ("summary_chunk.txt", SUMMARY_CHUNK_TEMPLATE),
}
# placeholder that must be present, otherwise the material is appended
REQUIRED_PLACEHOLDER = {"final": "material", "chunk": "chunk"}


def _ensure(path: Path, template: str) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template, encoding="utf-8")
    return path


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _ensure_prompt(directory: Path, name: str, template: str) -> Path:
    """Create a prompt file; upgrade it only if it is an unchanged earlier default.

    A customised file is never modified. The current default is always available next to it as
    ``<name>.default.txt`` (to compare or adopt: copy it over the customised file, or delete the
    customised file and it is recreated from the new default).
    """
    path = directory / name
    directory.mkdir(parents=True, exist_ok=True)
    default_copy = path.with_name(path.stem + ".default.txt")
    if not default_copy.exists() or default_copy.read_text(encoding="utf-8") != template:
        default_copy.write_text(template, encoding="utf-8")
    if not path.exists():
        path.write_text(template, encoding="utf-8")
        return path
    current = _norm(path.read_text(encoding="utf-8"))
    if current != _norm(template) and current in {_norm(t) for t in _PREVIOUS_DEFAULTS.get(name, [])}:
        path.write_text(template, encoding="utf-8")
        log.info("Vorlage %s war unverändert und wurde auf die neue Standardvorlage aktualisiert", path)
    elif current != _norm(template) and name in _PREVIOUS_DEFAULTS:
        log.info("Vorlage %s ist angepasst und bleibt unverändert (neue Standardvorlage: %s)", path, default_copy)
    return path


def prompt_is_customised(name: str, directory: Path | None = None) -> bool:
    path = (directory or paths.prompts_dir()) / name
    template = dict(PROMPT_FILES.values()).get(name, "")
    return path.exists() and _norm(path.read_text(encoding="utf-8")) != _norm(template)


def ensure_user_files() -> list[Path]:
    """Create the editable files with commented defaults (never overwrites customised files)."""
    out = [_ensure(paths.glossary_file(), GLOSSARY_TEMPLATE), _ensure(paths.replacements_file(), REPLACEMENTS_TEMPLATE)]
    for name, template in PROMPT_FILES.values():
        out.append(_ensure_prompt(paths.prompts_dir(), name, template))
    paths.presets_dir().mkdir(parents=True, exist_ok=True)
    return out


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


# -- glossary ------------------------------------------------------------------
def parse_glossary(text: str) -> list[str]:
    return list(dict.fromkeys(_lines(text)))


def load_glossary(path: Path | None = None) -> list[str]:
    path = path or paths.glossary_file()
    try:
        return parse_glossary(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


# -- replacements -----------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    source: str
    pattern: re.Pattern[str]
    replacement: str


def parse_replacements(text: str) -> tuple[list[Rule], list[str]]:
    rules: list[Rule] = []
    errors: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=>" not in s:
            errors.append(f"Zeile {n}: '=>' fehlt")
            continue
        left, right = (x.strip() for x in s.split("=>", 1))
        try:
            if left.lower().startswith("re:"):
                pat = re.compile(left[3:].strip())
                repl = right
            else:
                if not left:
                    raise re.error("leerer Suchtext")
                pat = re.compile(r"(?<!\w)" + re.escape(left) + r"(?!\w)", re.IGNORECASE)
                repl = right.replace("\\", "\\\\")  # literal replacement
            rules.append(Rule(s, pat, repl))
        except re.error as exc:
            errors.append(f"Zeile {n}: ungültiger Ausdruck ({exc})")
    return rules, errors


def load_replacements(path: Path | None = None) -> list[Rule]:
    path = path or paths.replacements_file()
    try:
        rules, errors = parse_replacements(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    for e in errors:
        log.warning("replacements.txt %s", e)
    return rules


def apply_replacements(text: str, rules: list[Rule]) -> str:
    for r in rules:
        text = r.pattern.sub(r.replacement, text)
    return text


# -- prompt templates -----------------------------------------------------------------
@dataclass(frozen=True)
class PromptTemplates:
    system: str
    final: str
    chunk: str

    def fingerprint(self) -> str:
        return hashlib.sha256("\x00".join((self.system, self.final, self.chunk)).encode()).hexdigest()[:12]


DEFAULT_PROMPTS = PromptTemplates(SUMMARY_SYSTEM_TEMPLATE, SUMMARY_FINAL_TEMPLATE, SUMMARY_CHUNK_TEMPLATE)


def load_prompts(directory: Path | None = None) -> PromptTemplates:
    d = directory or paths.prompts_dir()
    values = {}
    for key, (name, default) in PROMPT_FILES.items():
        try:
            text = (d / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            text = default
        values[key] = text if text.strip() else default
    return PromptTemplates(**values)


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render_template(template: str, values: dict[str, str], required: str | None = None) -> str:
    """Replace ``{name}`` for known names only (braces elsewhere stay untouched)."""
    out = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)
    if required and "{" + required + "}" not in template:
        log.warning("Vorlage ohne {%s} - Inhalt wird angehängt", required)
        out = out.rstrip() + "\n\n" + values.get(required, "")
    return out


PROMPT_PLACEHOLDERS = {
    "system": {},
    "final": {"title": "Titel des Gesprächs", "date": "Datum und Uhrzeit", "participants": "Teilnehmende/Sprechende",
              "material_label": "„Transkript“ bzw. „Teilzusammenfassungen“", "material": "das Transkript (Pflicht)"},
    "chunk": {"i": "Nummer des Teils", "n": "Anzahl der Teile", "chunk": "der Transkriptteil (Pflicht)"},
}


def validate_prompt(key: str, text: str) -> list[str]:
    """German warnings for a prompt template (empty list = fine)."""
    out: list[str] = []
    if not text.strip():
        return ["Die Vorlage ist leer – es wird die Standardvorlage verwendet."]
    req = REQUIRED_PLACEHOLDER.get(key)
    if req and "{" + req + "}" not in text:
        out.append(f"Der Platzhalter {{{req}}} fehlt – ohne ihn sieht das Modell das Gespräch nicht (es würde nur "
                   "angehängt).")
    if key == "final":
        gone = [h for h in SUMMARY_SECTIONS
                if not re.search(rf"^\s{{0,3}}#{{1,4}}\s*{re.escape(h)}\b", text, re.MULTILINE | re.IGNORECASE)]
        if gone:
            out.append("Diese Überschriften fehlen in der Vorlage: " + ", ".join(gone) + " – die Weiterverarbeitung "
                       "erwartet sie; jede Zusammenfassung wird dann als unvollständig markiert.")
    known = PROMPT_PLACEHOLDERS.get(key, {})
    unknown = sorted({m.group(1) for m in _PLACEHOLDER.finditer(text)} - set(known))
    if unknown:
        out.append("Unbekannte Platzhalter (bleiben wörtlich stehen): " + ", ".join("{" + u + "}" for u in unknown))
    return out


def default_prompt(key: str) -> str:
    return PROMPT_FILES[key][1]
