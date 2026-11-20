"""App identity on Windows: AppUserModelID, window icon, Start-menu shortcut (both UIs).

Without an explicit AppUserModelID, Windows groups the windows of ``pythonw.exe`` under Python: the
taskbar button shows the Python icon and name, and "An Taskleiste anheften" pins Python instead of
CallRecorder. Therefore:

* :func:`set_app_user_model_id` - ``SetCurrentProcessExplicitAppUserModelID`` once, at the start of
  ``run_tray_app``, before any window exists (Windows reads the id when a window is created);
* the window icon: Tk ``iconbitmap(default=…)``; pywebview 6.2.1 uses ``webview.start(icon=…)`` on
  WinForms too (``BrowserView.__init__``: ``self.Icon = Icon(_state['icon'])``, although its docstring
  says GTK/Qt only) - without it the form takes the icon of ``sys.executable`` (Python);
* the Start-menu shortcut (``callrec postinstall``) carries the same AppUserModelID
  (``System.AppUserModel.ID`` via IPropertyStore) and the icon, so a pinned button starts CallRecorder
  and the running window is grouped with it. The autostart entry and the updater start the same
  ``pythonw -m callrec tray``; the id comes from the process itself, so they keep the identity.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_ID = "CallRecorder.CallRecorder.App"  # Company.Product.SubProduct, no spaces, < 128 characters
ASSETS = Path(__file__).resolve().parent / "assets"


def icon_path(recording: bool = False) -> Path:
    return ASSETS / ("callrec-recording.ico" if recording else "callrec.ico")


def png_path(size: int = 64, variant: str = "callrec") -> Path:
    return ASSETS / "png" / f"{variant}-{size}.png"


_applied: list[str] = []


def set_app_user_model_id(shell32: Any = None, app_id: str = APP_ID) -> bool:
    """Must run before the first window of the process is created. True = set."""
    if shell32 is None:
        if sys.platform != "win32":
            return False
        import ctypes

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    try:
        hr = shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        log.warning("AppUserModelID nicht gesetzt", exc_info=True)
        return False
    ok = (hr or 0) == 0
    if ok:
        _applied.append(app_id)
    else:
        log.warning("AppUserModelID nicht gesetzt (HRESULT %s)", hr)
    return ok


def tk_set_icon(root: Any, recording: bool = False) -> None:
    """Default icon of every Tk window (title bar, taskbar, Alt+Tab)."""
    try:
        if sys.platform == "win32":
            root.iconbitmap(default=str(icon_path(recording)))
        else:
            import tkinter as tk

            variant = "callrec-recording" if recording else "callrec"
            img = tk.PhotoImage(file=str(png_path(64, variant)))
            root._callrec_icon = img  # keep a reference
            root.iconphoto(True, img)
    except Exception:
        log.debug("Fenstersymbol nicht gesetzt", exc_info=True)


def winforms_set_icon(windows: list[Any], recording: bool = False) -> None:
    """The window icon of the pywebview forms (red while recording). Runs the change on each form's
    GUI thread (``Control.Invoke``)."""
    try:
        from System import Action  # type: ignore[import-not-found]
        from System.Drawing import Icon  # type: ignore[import-not-found]
    except Exception:
        return
    path = str(icon_path(recording))
    for w in windows:
        form = getattr(w, "native", None)
        if form is None:
            continue
        try:
            form.Invoke(Action(lambda form=form: setattr(form, "Icon", Icon(path))))
        except Exception:
            log.debug("Fenstersymbol nicht geändert", exc_info=True)


def shortcut_script(target: str, args: str, workdir: str, icon: str, app_id: str = APP_ID,
                    name: str = "CallRecorder.lnk") -> str:
    """PowerShell for the Start-menu shortcut: WScript.Shell writes target, arguments and icon; the
    AppUserModelID is a shell property (``System.AppUserModel.ID``) that WScript.Shell cannot set, so
    the same script sets it through IPropertyStore (``SHGetPropertyStoreFromParsingName`` with
    GPS_READWRITE, a small C# helper compiled by Add-Type - no extra module)."""
    from callrec.cli import ps_quote

    cs = r'''
using System; using System.Runtime.InteropServices;
public static class CallRecAumid {
  [StructLayout(LayoutKind.Sequential, Pack = 4)] public struct PROPERTYKEY { public Guid fmtid; public uint pid; }
  [StructLayout(LayoutKind.Explicit)] public struct PROPVARIANT { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPropertyStore { [PreserveSig] int GetCount(out uint c); [PreserveSig] int GetAt(uint i, out PROPERTYKEY k);
    [PreserveSig] int GetValue(ref PROPERTYKEY k, out PROPVARIANT v);
    [PreserveSig] int SetValue(ref PROPERTYKEY k, ref PROPVARIANT v); [PreserveSig] int Commit(); }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bc, int flags, ref Guid riid,
    [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
  public static void Set(string lnk, string id) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"); IPropertyStore s;
    SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2 /* GPS_READWRITE */, ref iid, out s);
    PROPERTYKEY k = new PROPERTYKEY(); k.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"); k.pid = 5;
    PROPVARIANT v = new PROPVARIANT(); v.vt = 31 /* VT_LPWSTR */; v.p = Marshal.StringToCoTaskMemUni(id);
    try { Marshal.ThrowExceptionForHR(s.SetValue(ref k, ref v)); Marshal.ThrowExceptionForHR(s.Commit()); }
    finally { Marshal.FreeCoTaskMem(v.p); Marshal.ReleaseComObject(s); }
  }
}'''
    return (
        f"$p = [IO.Path]::Combine([Environment]::GetFolderPath('Programs'), {ps_quote(name)}); "
        "$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut($p); "
        f"$l.TargetPath = {ps_quote(target)}; $l.Arguments = {ps_quote(args)}; $l.WorkingDirectory = {ps_quote(workdir)}; "
        f"$l.IconLocation = {ps_quote(icon + ',0')}; "
        "$l.Description = 'CallRecorder - lokale Gesprächsaufnahme'; $l.Save(); "
        # the id is optional: without it the shortcut still works (only taskbar grouping/pinning differs)
        f"try {{ Add-Type -TypeDefinition @'\n{cs}\n'@ -Language CSharp -ErrorAction Stop; "
        f"[CallRecAumid]::Set($p, {ps_quote(app_id)}) }} "
        "catch { Write-Output ('Verknüpfung ohne AppUserModelID: ' + $_.Exception.Message) }"
    )
