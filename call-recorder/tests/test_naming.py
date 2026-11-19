from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from callrec.naming import create_recording_folder, recording_basename, slugify_title, unique_folder


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Projekt Jour fixe", "Projekt-Jour-fixe"),
        ("AW: WG: Abstimmung Q4/2026", "Abstimmung-Q4-2026"),
        ('Review "Release" <v2>: final?', "Review-Release-v2-final"),
        ("Größenänderung für Übersicht", "Größenänderung-für-Übersicht"),
        ("   ", "Anruf"),
        ("CON", "CON_"),
        ("nul.txt", "nul.txt_"),
        ("Ende...", "Ende"),
        ("Team | Daily * Standup", "Team-Daily-Standup"),
        ("Tab\tund\nZeilenumbruch", "Tab-und-Zeilenumbruch"),
    ],
)
def test_slugify(title, expected):
    assert slugify_title(title) == expected


def test_slug_is_windows_safe_and_short():
    s = slugify_title("Sehr langer Titel mit vielen Wörtern " * 10)
    assert len(s) <= 60
    assert not any(c in s for c in '<>:"/\\|?*')
    assert not s.endswith((".", " ", "-"))


def test_basename():
    assert recording_basename(datetime(2026, 11, 19, 14, 0), "Projekt Jour fixe") == "2026-11-19_1400_Projekt-Jour-fixe"


def test_collisions(tmp_path: Path):
    start = datetime(2026, 11, 19, 14, 0)
    a = create_recording_folder(tmp_path, start, "Jour fixe")
    b = create_recording_folder(tmp_path, start, "Jour fixe")
    c = create_recording_folder(tmp_path, start, "Jour fixe")
    assert a.name == "2026-11-19_1400_Jour-fixe"
    assert b.name == "2026-11-19_1400_Jour-fixe_2"
    assert c.name == "2026-11-19_1400_Jour-fixe_3"
    assert unique_folder(tmp_path, a.name).name == "2026-11-19_1400_Jour-fixe_4"
