"""The questions AppActions asks the user - one interface, two implementations.

:class:`TkDialogPort` calls the existing Tk dialogs (``callrec.ui.dialogs`` / ``settings``), looked
up at call time so tests can patch them. The web UI implements the same methods with in-page
dialogs (``callrec.ui.webdialogs.WebDialogPort``). AppActions keeps all the logic around the
questions (what is checked before, what happens with the answer).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from callrec.models import Person
from callrec.recording import Recording

AFTER_FULL, AFTER_LATER, AFTER_DISCARD = "full", "later", "discard"


class DialogPort(Protocol):
    def ask_after_recording(self, rec: Recording, plan: Any, handoff_problem: str = "",
                            summary_problem: str = "") -> tuple[str, Any]: ...

    def review_speakers(self, rec: Recording, profiles: Any, rerender: Callable[[], object],
                        resummarize: Callable[[], object], handoff: Callable[[], object] | None = None,
                        in_chain: bool = False, speaker_cfg: Any = None,
                        allow_more: Callable[[], object] | None = None) -> bool: ...

    def edit_participants(self, rec: Recording) -> list[Person] | None: ...

    def confirm_reset_foreign(self, rec: Recording, foreign: list[tuple[str, str, str]]) -> bool: ...

    def confirm_recheck(self, rec: Recording, changes: list[tuple[str, str, str]]) -> bool: ...

    def confirm_cloud_summary(self, rec: Recording, me: Person) -> bool: ...

    def ask_handoff(self, rec: Recording, cfg: Any, perform: Callable[[str], dict[str, Any]]) -> bool: ...

    def ask_event_for_recording(self, rec: Recording, events: list[Any]) -> tuple[Any, bool] | None: ...

    def ask_retranscribe(self, rec: Recording, presets: list[str]) -> tuple[str, str | None, str | None] | None: ...

    def ask_update(self, res: Any, busy: str, install_info: dict[str, Any]) -> str | None: ...

    def manage_profiles(self, profiles: Any, rebuild: Callable[[str], str] | None = None,
                        recordings_root: Any = None, reidentify: Callable[[], object] | None = None,
                        repair: Callable[[], object] | None = None) -> None: ...

    def edit_settings(self, base: Any, mics: list[str], outs: list[str], save: Callable[[Any], None],
                      initial_tab: str = "", autostart: Any = None) -> bool: ...

    def edit_summary_prompts(self, test: Callable[[], object] | None = None) -> bool: ...

    def ask_yes_no(self, title: str, message: str, warning: bool = False) -> bool: ...

    def show_text(self, title: str, text: str) -> None: ...

    def ask_retention(self, kind: str, text: str, items: list[tuple[str, str]], rt: Any) -> str: ...


class TkDialogPort:
    """The Tk dialogs of the classic UI (all calls on the Tk thread)."""

    def __init__(self, root: Callable[[], Any]) -> None:
        self._root = root

    def ask_after_recording(self, rec: Recording, plan: Any, handoff_problem: str = "",
                            summary_problem: str = "") -> tuple[str, Any]:
        from callrec.ui import dialogs

        return dialogs.ask_after_recording(self._root(), rec, plan, handoff_problem, summary_problem)

    def review_speakers(self, rec: Recording, profiles: Any, rerender: Callable[[], object],
                        resummarize: Callable[[], object], handoff: Callable[[], object] | None = None,
                        in_chain: bool = False, speaker_cfg: Any = None,
                        allow_more: Callable[[], object] | None = None) -> bool:
        from callrec.ui import dialogs

        kwargs: dict[str, Any] = {"rerender": rerender, "resummarize": resummarize, "speaker_cfg": speaker_cfg}
        if in_chain:
            kwargs["in_chain"] = True
        else:
            kwargs["handoff"] = handoff
            kwargs["allow_more"] = allow_more
        return bool(dialogs.review_speakers(self._root(), rec, profiles, **kwargs))

    def edit_participants(self, rec: Recording) -> list[Person] | None:
        from callrec.ui import dialogs

        return dialogs.edit_participants(self._root(), rec)

    def confirm_reset_foreign(self, rec: Recording, foreign: list[tuple[str, str, str]]) -> bool:
        from callrec.ui import dialogs

        return bool(dialogs.confirm_reset_foreign(self._root(), rec, foreign))

    def confirm_recheck(self, rec: Recording, changes: list[tuple[str, str, str]]) -> bool:
        from callrec.ui import dialogs

        return bool(dialogs.confirm_recheck(self._root(), rec, changes))

    def confirm_cloud_summary(self, rec: Recording, me: Person) -> bool:
        from callrec.ui import dialogs

        return bool(dialogs.confirm_cloud_summary(self._root(), rec, me))

    def ask_handoff(self, rec: Recording, cfg: Any, perform: Callable[[str], dict[str, Any]]) -> bool:
        from callrec.ui import dialogs

        return bool(dialogs.ask_handoff(self._root(), rec, cfg, perform))

    def ask_event_for_recording(self, rec: Recording, events: list[Any]) -> tuple[Any, bool] | None:
        from callrec.ui import dialogs

        return dialogs.ask_event_for_recording(self._root(), rec, events)

    def ask_retranscribe(self, rec: Recording, presets: list[str]) -> tuple[str, str | None, str | None] | None:
        from callrec.ui import dialogs

        return dialogs.ask_retranscribe(self._root(), rec, presets)

    def ask_update(self, res: Any, busy: str, install_info: dict[str, Any]) -> str | None:
        from callrec.ui import dialogs

        return dialogs.ask_update(self._root(), res, busy, install_info)

    def manage_profiles(self, profiles: Any, rebuild: Callable[[str], str] | None = None,
                        recordings_root: Any = None, reidentify: Callable[[], object] | None = None,
                        repair: Callable[[], object] | None = None) -> None:
        from callrec.ui import dialogs

        dialogs.manage_profiles(self._root(), profiles, rebuild=rebuild, recordings_root=recordings_root,
                                reidentify=reidentify, repair=repair)

    def edit_settings(self, base: Any, mics: list[str], outs: list[str], save: Callable[[Any], None],
                      initial_tab: str = "", autostart: Any = None) -> bool:
        from callrec.ui.settings import edit_settings

        return bool(edit_settings(self._root(), base, mics, outs, save, initial_tab=initial_tab, autostart=autostart))

    def edit_summary_prompts(self, test: Callable[[], object] | None = None) -> bool:
        from callrec.ui.settings import edit_summary_prompts

        return bool(edit_summary_prompts(self._root(), test=test))

    def ask_yes_no(self, title: str, message: str, warning: bool = False) -> bool:
        from tkinter import messagebox

        kwargs: dict[str, Any] = {"parent": self._root()}
        if warning:
            kwargs["icon"] = "warning"
        return bool(messagebox.askyesno(title, message, **kwargs))

    def show_text(self, title: str, text: str) -> None:
        from callrec.ui import dialogs

        dialogs.show_text(self._root(), title, text)

    def ask_retention(self, kind: str, text: str, items: list[tuple[str, str]], rt: Any) -> str:
        from callrec.retention import RETENTION_CHOICES, question_text
        from callrec.ui import dialogs

        title, message = question_text(kind, text, items, rt)
        return dialogs.choose(self._root(), title, message, list(RETENTION_CHOICES))
