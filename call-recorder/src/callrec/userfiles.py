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
Du bist ein sorgfältiger Protokollführer. Du erstellst aus dem Transkript eines Gesprächs ein kurzes, sachliches Ergebnisprotokoll auf Deutsch.
Regeln:
- Verwende ausschließlich Informationen aus dem Transkript. Erfinde nichts.
- Nenne Personen mit den Namen aus dem Transkript. "Sprecher N" bleibt "Sprecher N".
- Wenn für eine Aufgabe niemand verantwortlich ist, schreibe "offen". Wenn keine Frist genannt wurde, schreibe "-".
- Das Transkript wurde automatisch erstellt und kann Erkennungsfehler enthalten.
- Antworte nur mit dem Protokoll in Markdown, ohne Vorrede.
"""

SUMMARY_FINAL_TEMPLATE = """\
Erstelle das Ergebnisprotokoll mit genau diesen Abschnitten:

## Kurzfassung
(3-6 Sätze)

## Entscheidungen
(Aufzählung; "Keine" wenn es keine gab)

## Aufgaben
| Aufgabe | Verantwortlich | Frist |
|---|---|---|

## Offene Punkte
(Aufzählung; "Keine" wenn es keine gab)

Gespräch: {title}
Datum: {date}
Teilnehmende: {participants}

{material_label}:
{material}
"""

SUMMARY_CHUNK_TEMPLATE = """\
Das folgende ist Teil {i} von {n} eines Gesprächstranskripts.
Fasse diesen Teil in Stichpunkten zusammen: besprochene Themen, Entscheidungen, Aufgaben (mit verantwortlicher Person und Frist, falls genannt) und offene Fragen. Nur Inhalte aus diesem Teil.

{chunk}
"""

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


def ensure_user_files() -> list[Path]:
    """Create the editable files with commented defaults (never overwrites)."""
    out = [_ensure(paths.glossary_file(), GLOSSARY_TEMPLATE), _ensure(paths.replacements_file(), REPLACEMENTS_TEMPLATE)]
    for name, template in PROMPT_FILES.values():
        out.append(_ensure(paths.prompts_dir() / name, template))
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
