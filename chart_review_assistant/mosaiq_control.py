# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Open a patient in the running Mosaiq client without touching the user's mouse or keyboard

- UI Automation only locates Mosaiq's controls
- Actions are window messages to the owning Mosaiq hwnds (as AutoHotkey ControlClick/ControlSend):
  a click on the Select Patient drop-down arrow, WM_PASTE of the MRN into the popup patient field,
  then Return
- UIA Invoke is never used on Mosaiq's windowless toolbar buttons; it falls back to a real mouse
  click
"""

import ctypes
import time
from ctypes import wintypes

import clr

clr.AddReference('UIAutomationClient, Version=4.0.0.0, Culture=neutral, '
                 'PublicKeyToken=31bf3856ad364e35')
clr.AddReference('UIAutomationTypes, Version=4.0.0.0, Culture=neutral, '
                 'PublicKeyToken=31bf3856ad364e35')
from System import IntPtr
from System.Diagnostics import Process
from System.Windows.Automation import (AndCondition, AutomationElement, Condition, ControlType,
                                       PropertyCondition, TreeScope, TreeWalker)

user32 = ctypes.WinDLL('user32')
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.SendMessageW.restype = wintypes.LPARAM
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32 = ctypes.WinDLL('kernel32')
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)


def open_patient(mrn):
    """Open the patient with this MRN in the running Mosaiq client.

    Args:
        mrn (str): Patient MRN (Mosaiq IDA).

    Returns:
        str | None: Why the patient could not be opened, or None once the MRN was submitted.
    """
    pids = {p.Id for p in Process.GetProcessesByName('accwin')}
    if not pids:
        return 'Mosaiq is not running.'
    choose = find(pids, 'buttonChoosePatient')
    if choose is None:
        return 'Mosaiq Select Patient button not found.'

    # Click the drop-down arrow, posted to the Select Patient control's own window
    arrow = choose.FindFirst(TreeScope.Children, AndCondition(
        PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button),
        PropertyCondition(AutomationElement.NameProperty, 'Drop down')))
    hwnd = choose.Current.NativeWindowHandle
    r = arrow.Current.BoundingRectangle
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    x = int(r.X + r.Width / 2) - origin.x
    y = int(r.Y + r.Height / 2) - origin.y
    lparam = (y << 16) | (x & 0xFFFF)

    # WM_MOUSEMOVE, WM_LBUTTONDOWN (MK_LBUTTON), WM_LBUTTONUP
    for msg, wparam in ((0x0200, 0), (0x0201, 0x1), (0x0202, 0)):
        user32.PostMessageW(hwnd, msg, wparam, lparam)

    # Wait for the popup patient field
    field = None
    deadline = time.monotonic() + 5
    while field is None and time.monotonic() < deadline:
        time.sleep(0.25)
        field = find(pids, 'inputPatientButtonEdit')
    if field is None:
        return 'Mosaiq patient field did not open.'
    edit = next(e.Current.NativeWindowHandle
                for e in field.FindAll(TreeScope.Subtree, Condition.TrueCondition)
                if e.Current.NativeWindowHandle and 'EDIT' in e.Current.ClassName.upper())

    # Paste over the highlighted current patient, then Return (WM_KEYDOWN / WM_KEYUP VK_RETURN)
    paste(edit, mrn)
    user32.PostMessageW(edit, 0x0100, 0x0D, 0)
    user32.PostMessageW(edit, 0x0101, 0x0D, 0xC0000000)
    return None


def find(pids, aid):
    """First control with this AutomationId in any visible window of the given processes.

    Args:
        pids (set[int]): Mosaiq process ids.
        aid (str): AutomationId to match.

    Returns:
        AutomationElement | None: The control, or None if not found.
    """
    hwnds = []
    pid = wintypes.DWORD()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(h, _):
        """Keep the handle when its window is visible and owned by one of pids."""
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if user32.IsWindowVisible(h) and pid.value in pids:
            hwnds.append(h)
        return True

    user32.EnumWindows(collect, 0)
    for h in hwnds:
        hit = walk(AutomationElement.FromHandle(IntPtr(h)), aid, 10)
        if hit is not None:
            return hit
    return None


def walk(el, aid, maxdepth):
    """Depth-first search under el for this AutomationId, skipping subtrees that raise.

    A full TreeScope.Subtree search of Mosaiq's main window takes minutes, so depth is bounded.

    Args:
        el (AutomationElement): Element to search from.
        aid (str): AutomationId to match.
        maxdepth (int): Levels below el to search.

    Returns:
        AutomationElement | None: The control, or None if not found.
    """
    try:
        if el.Current.AutomationId == aid:
            return el
        if maxdepth == 0:
            return None
        child = TreeWalker.RawViewWalker.GetFirstChild(el)
    except Exception:
        return None
    while child is not None:
        hit = walk(child, aid, maxdepth - 1)
        if hit is not None:
            return hit
        try:
            child = TreeWalker.RawViewWalker.GetNextSibling(child)
        except Exception:
            return None
    return None


def paste(hwnd, text):
    """Paste text into hwnd with WM_PASTE, then restore the previous clipboard text.

    Args:
        hwnd (int): Edit control handle.
        text (str): Text to paste.
    """

    # CF_UNICODETEXT = 13
    user32.OpenClipboard(None)
    h = user32.GetClipboardData(13)
    old = ctypes.wstring_at(kernel32.GlobalLock(h)) if h else None
    if h:
        kernel32.GlobalUnlock(h)
    set_clipboard(text)
    user32.CloseClipboard()

    # WM_PASTE
    user32.SendMessageW(hwnd, 0x0302, 0, 0)

    user32.OpenClipboard(None)
    if old is None:
        user32.EmptyClipboard()
    else:
        set_clipboard(old)
    user32.CloseClipboard()


def set_clipboard(text):
    """Replace the open clipboard's contents with text as CF_UNICODETEXT.

    Args:
        text (str): Text to place on the clipboard.
    """
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)

    # GMEM_MOVEABLE = 0x2
    h = kernel32.GlobalAlloc(0x2, size)
    ctypes.memmove(kernel32.GlobalLock(h), buf, size)
    kernel32.GlobalUnlock(h)
    user32.EmptyClipboard()
    user32.SetClipboardData(13, h)
