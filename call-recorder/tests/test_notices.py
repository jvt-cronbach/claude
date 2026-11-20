"""Notices after an update are shown exactly once, in whichever UI starts next (0.3.1)."""

from __future__ import annotations

from callrec import paths
from callrec.config import CONSENT_ASK_NOTE, LEGACY_LANGUAGE_NOTE, load_config
from callrec.notices import TITLE, startup_notices


def _old_config() -> None:
    p = paths.config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[general]\nuser_name = "Ich"\n\n[transcription]\nlanguage = "de"\n', encoding="utf-8")


def test_tray_with_ui_does_not_swallow_the_migration_notices():
    from callrec.cli import set_frontend

    _old_config()
    set_frontend("web")  # what `callrec tray --ui web` does before the UI starts
    cfg = load_config()  # the UI's own load: nothing to migrate any more
    shown = startup_notices(cfg)
    texts = [t for title, t in shown if title == TITLE]
    assert CONSENT_ASK_NOTE in texts and LEGACY_LANGUAGE_NOTE in texts
    assert startup_notices(load_config()) == []  # exactly once
    assert 'mode = "ask"' in paths.config_file().read_text(encoding="utf-8")


def test_a_ui_loading_the_old_file_itself_shows_each_notice_once():
    _old_config()
    cfg = load_config()
    shown = startup_notices(cfg)
    assert [t for _title, t in shown].count(CONSENT_ASK_NOTE) == 1
    assert all(title == TITLE for title, t in shown if t == CONSENT_ASK_NOTE)


def test_fresh_install_has_no_consent_notice():
    cfg = load_config()  # writes the template (with [consent])
    assert startup_notices(cfg) == []
