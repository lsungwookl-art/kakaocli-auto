from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import time
import zlib
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path

if os.name == "nt":
    import win32api
    import win32con
    import win32gui
    import win32process

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


CHAT_CLASS = "EVA_Window_Dblclk"
INPUT_CLASSES = {"RICHEDIT50W", "RichEdit50W", "RichEditD2DPT"}
FOCUS_TIMEOUT = 0.5
MENU_MASK_KEY = 0xE8
SEARCH_SETTLE = 0.8
FIRST_ROW_Y = 30
SCROLL_SETTLE = 0.15
SEARCH_ALL_TITLE = "통합검색"
SEARCH_ALL_OFFSET = 25
SEARCH_ALL_SETTLE = 2.5
SEARCH_RESULT_CLASS = "EVA_ChildWindow_Dblclk"
SEARCH_SCROLL_TICKS = 4
SEARCH_SCROLL_SETTLE = 0.5
MESSAGE_TAB_X, MESSAGE_TAB_Y = 178, 148
PW_RENDERFULLCONTENT = 2
CHAT_LIST_VIEW = "ChatRoomListView"


class KakaoError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    width: int
    height: int
    visible: bool
    kind: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["hwnd"] = hex(self.hwnd)
        return data


def require_windows() -> None:
    if os.name != "nt":
        raise KakaoError("kakaocli-win은 Windows에서만 실행할 수 있습니다.")


def enum_children(hwnd: int) -> list[int]:
    children: list[int] = []
    win32gui.EnumChildWindows(hwnd, lambda child, _: children.append(child), None)
    return children


def _input_handles(hwnd: int) -> list[int]:
    return [
        child
        for child in enum_children(hwnd)
        if win32gui.IsWindowVisible(child)
        and (
            win32gui.GetClassName(child) in INPUT_CLASSES
            or "RICHEDIT" in win32gui.GetClassName(child).upper()
        )
    ]


def classify_window(hwnd: int, title: str, class_name: str) -> str:
    if class_name != CHAT_CLASS:
        return "other"
    if _input_handles(hwnd):
        return "chat"
    return "main" if title in {"", "카카오톡", "KakaoTalk"} else "popup"


def list_windows() -> list[WindowInfo]:
    require_windows()
    result: list[WindowInfo] = []

    def callback(hwnd: int, _: object) -> None:
        try:
            class_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if "EVA" not in class_name and "카카오톡" not in title and "KakaoTalk" not in title:
                return
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            result.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    class_name=class_name,
                    width=max(0, right - left),
                    height=max(0, bottom - top),
                    visible=bool(win32gui.IsWindowVisible(hwnd)),
                    kind=classify_window(hwnd, title, class_name),
                )
            )
        except Exception:
            return

    win32gui.EnumWindows(callback, None)
    return sorted(result, key=lambda item: (item.kind, item.title, item.hwnd))


def list_rooms() -> list[WindowInfo]:
    return [
        window
        for window in list_windows()
        if window.kind == "chat" and window.visible and window.title.strip()
    ]


def find_main_window() -> WindowInfo | None:
    candidates = [
        window
        for window in list_windows()
        if window.kind == "main" and window.width > 0 and window.height > 0
    ]
    return max(candidates, key=lambda item: item.width * item.height, default=None)


def find_room(room: str, *, exact: bool = False) -> WindowInfo | None:
    needle = room.casefold().strip()
    matches = []
    for window in list_rooms():
        title = window.title.casefold().strip()
        if (exact and title == needle) or (not exact and needle in title):
            matches.append(window)
    if len(matches) > 1 and not exact:
        names = ", ".join(repr(item.title) for item in matches)
        raise KakaoError(f"채팅방 이름이 여러 개와 일치합니다: {names}. --exact를 사용하세요.")
    return matches[0] if matches else None


def _focus(hwnd: int) -> None:
    root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) or hwnd
    win32gui.ShowWindow(root, win32con.SW_SHOW)
    win32gui.ShowWindow(root, win32con.SW_RESTORE)
    current_tid = win32api.GetCurrentThreadId()
    target_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
    attached = False
    try:
        if current_tid != target_tid:
            attached = bool(ctypes.windll.user32.AttachThreadInput(current_tid, target_tid, True))
        ctypes.windll.user32.BringWindowToTop(root)
        foreground = win32gui.GetForegroundWindow()
        if not (foreground and win32gui.GetAncestor(foreground, win32con.GA_ROOT) == root):
            _set_foreground(root)
        ctypes.windll.user32.SetFocus(hwnd)
    finally:
        if attached:
            ctypes.windll.user32.AttachThreadInput(current_tid, target_tid, False)
    _require_foreground(root, timeout=FOCUS_TIMEOUT)


def _set_foreground(root: int) -> None:
    """Windows ignores SetForegroundWindow from a background process unless Alt is down.

    A bare Alt tap toggles the target's menu mode, which swallows later keys, so
    an unassigned key is pressed in between to keep the menu from activating.
    """
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    try:
        win32api.keybd_event(MENU_MASK_KEY, 0, 0, 0)
        win32api.keybd_event(MENU_MASK_KEY, 0, win32con.KEYEVENTF_KEYUP, 0)
        ctypes.windll.user32.SetForegroundWindow(root)
    finally:
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)


def _require_foreground(hwnd: int, *, timeout: float = 0.0) -> None:
    """Refuse to continue: keyboard events go to whichever window is in front."""
    root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) or hwnd
    deadline = time.monotonic() + timeout
    while True:
        foreground = win32gui.GetForegroundWindow()
        if foreground and win32gui.GetAncestor(foreground, win32con.GA_ROOT) == root:
            return
        if time.monotonic() >= deadline:
            raise KakaoError(
                "카카오톡 창을 맨 앞으로 가져오지 못해 키 입력 없이 중단했습니다. "
                "채팅방 창을 한 번 클릭한 뒤 다시 실행하세요."
            )
        time.sleep(0.05)


def _press(vk: int, modifiers: tuple[int, ...] = ()) -> None:
    for modifier in modifiers:
        win32api.keybd_event(modifier, 0, 0, 0)
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    for modifier in reversed(modifiers):
        win32api.keybd_event(modifier, 0, win32con.KEYEVENTF_KEYUP, 0)


def _set_text(hwnd: int, text: str) -> bool:
    win32gui.SendMessage(hwnd, win32con.WM_SETTEXT, 0, text)
    if win32gui.GetWindowText(hwnd) == text:
        return True

    # Recent KakaoTalk RichEdit controls can reject WM_SETTEXT while still
    # accepting the normal RichEdit replace-selection operation.
    win32gui.SendMessage(hwnd, win32con.EM_SETSEL, 0, -1)
    win32gui.SendMessage(hwnd, win32con.EM_REPLACESEL, True, text)
    if win32gui.GetWindowText(hwnd) == text:
        return True

    _focus(hwnd)
    _press(ord("A"), (win32con.VK_CONTROL,))
    # KakaoTalk intentionally returns an empty value for this control even
    # while typed text is visibly present. SendInput's accepted-event count is
    # therefore the only programmatic confirmation available before Enter.
    return _send_unicode_input(text)


def _send_unicode_input(text: str) -> bool:
    """Type Unicode text through SendInput without touching the clipboard."""
    ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ulong_ptr),
        ]

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ulong_ptr),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [
            ("ki", KeyboardInput),
            ("mi", MouseInput),
            ("hi", HardwareInput),
        ]

    class Input(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", ctypes.c_ulong), ("union", InputUnion)]

    key_events = []
    encoded = text.encode("utf-16-le")
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset : offset + 2], "little")
        key_events.append(Input(1, InputUnion(ki=KeyboardInput(0, unit, 0x0004, 0, 0))))
        key_events.append(Input(1, InputUnion(ki=KeyboardInput(0, unit, 0x0006, 0, 0))))
    if not key_events:
        return True
    inputs = (Input * len(key_events))(*key_events)
    sent = ctypes.windll.user32.SendInput(len(inputs), inputs, ctypes.sizeof(Input))
    return sent == len(inputs)


def open_room(room: str, *, exact: bool = False, timeout: float = 3.0) -> WindowInfo:
    existing = find_room(room, exact=exact)
    if existing:
        _focus(existing.hwnd)
        return existing

    raise KakaoError(
        f"안전한 자동 검색을 지원하지 않는 카카오톡 버전입니다. "
        f"PC 카카오톡에서 {room!r} 방을 직접 독립 창으로 연 뒤 다시 실행하세요."
    )


def _visible_hwnds() -> set[int]:
    return {window.hwnd for window in list_windows() if window.visible}


def _close_windows(hwnds: set[int]) -> None:
    for hwnd in hwnds:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _visible_children(hwnd: int) -> list[int]:
    return [child for child in enum_children(hwnd) if win32gui.IsWindowVisible(child)]


def _visible_edits(hwnd: int) -> list[int]:
    return [child for child in _visible_children(hwnd) if win32gui.GetClassName(child) == "Edit"]


def _chat_search_box(main_hwnd: int) -> int:
    if not any(win32gui.GetWindowText(c).startswith(CHAT_LIST_VIEW) for c in _visible_children(main_hwnd)):
        raise KakaoError("카카오톡 메인 창을 채팅 탭으로 바꾼 뒤 다시 실행하세요.")
    edits = _visible_edits(main_hwnd)
    if not edits:
        before = _visible_hwnds()
        _focus(main_hwnd)
        _press(ord("F"), (win32con.VK_CONTROL,))
        time.sleep(SEARCH_SETTLE)
        # Ctrl+F can open the add-friend dialog instead of the search box.
        unexpected = _visible_hwnds() - before
        if unexpected:
            _close_windows(unexpected)
            raise KakaoError("검색창 대신 다른 창이 열려 닫았습니다. 카카오톡에서 방을 직접 열어 주세요.")
        edits = _visible_edits(main_hwnd)
    if len(edits) != 1:
        raise KakaoError("채팅 검색창을 찾지 못했습니다. 카카오톡에서 방을 직접 열어 주세요.")
    return edits[0]


def _double_click_first_row(main_hwnd: int) -> None:
    lists = [c for c in _visible_children(main_hwnd) if "ListControl" in win32gui.GetClassName(c)]
    if len(lists) != 1:
        raise KakaoError("채팅 목록을 찾지 못했습니다. 카카오톡에서 방을 직접 열어 주세요.")
    left, _top, right, _bottom = win32gui.GetWindowRect(lists[0])
    position = (FIRST_ROW_Y << 16) | ((right - left) // 2)
    for message, buttons in (
        (win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON),
        (win32con.WM_LBUTTONUP, 0),
        (win32con.WM_LBUTTONDBLCLK, win32con.MK_LBUTTON),
        (win32con.WM_LBUTTONUP, 0),
    ):
        win32gui.PostMessage(lists[0], message, buttons, position)


def search_and_open(room: str, *, timeout: float = 3.0) -> WindowInfo:
    """Open the top chat-tab search result, keeping it only if its title equals `room`.

    The result list cannot be read, so a wrong room may open briefly, which can
    mark its messages as read. Rooms sharing the exact title cannot be told apart.
    """
    existing = find_room(room, exact=True)
    if existing:
        _focus(existing.hwnd)
        return existing
    main = find_main_window()
    if not main:
        raise KakaoError("카카오톡 메인 창을 찾지 못했습니다.")

    search_box = _chat_search_box(main.hwnd)
    before = _visible_hwnds()
    # These messages target the controls directly, so nothing can reach another app.
    # Arrow keys and Enter in the search box proved unreliable on KakaoTalk 26.9.
    win32gui.SendMessage(search_box, win32con.WM_SETTEXT, 0, room)
    try:
        time.sleep(SEARCH_SETTLE)
        _double_click_first_row(main.hwnd)
        opened: list[WindowInfo] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            opened = [w for w in list_windows() if w.visible and w.hwnd not in before]
            if opened and all(w.title.strip() for w in opened):
                break
            time.sleep(0.1)
    finally:
        win32gui.SendMessage(search_box, win32con.WM_SETTEXT, 0, "")

    if not opened:
        raise KakaoError(f"{room!r} 방을 새로 열지 못했습니다. 이름을 확인하거나 카카오톡에서 직접 열어 주세요.")
    if len(opened) == 1 and opened[0].kind == "chat" and opened[0].title.strip() == room.strip():
        _focus(opened[0].hwnd)
        return opened[0]
    _close_windows({w.hwnd for w in opened})
    raise KakaoError(
        f"검색 결과의 첫 항목이 {room!r} 방이 아니어서 닫았습니다. "
        "그 방의 메시지가 읽음으로 표시됐을 수 있습니다. 카카오톡에서 직접 열어 주세요."
    )


def send_message(room: str, message: str, *, exact: bool = False) -> WindowInfo:
    if not message:
        raise KakaoError("빈 메시지는 전송할 수 없습니다.")
    window = open_room(room, exact=exact)
    inputs = _input_handles(window.hwnd)
    if not inputs:
        raise KakaoError("메시지 입력창을 찾지 못했습니다.")

    input_hwnd = inputs[-1]
    _focus(input_hwnd)
    if not _set_text(input_hwnd, message):
        raise KakaoError("메시지 입력에 실패했습니다. 전송하지 않았습니다.")
    _require_foreground(window.hwnd)
    _press(win32con.VK_RETURN)
    time.sleep(0.2)
    remaining = win32gui.GetWindowText(input_hwnd)
    if remaining == message:
        raise KakaoError("Enter 입력 후에도 메시지가 남아 있습니다. 전송 여부를 직접 확인하세요.")
    return window


def _png_bytes(width: int, height: int, bgra: bytes) -> bytes:
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    rows = b"".join(b"\0" + bytes(rgb[y * width * 3 : (y + 1) * width * 3]) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _capture_window(hwnd: int) -> bytes:
    """Return only this window as a PNG, so other windows on screen are never included."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    handle = wintypes.HANDLE
    user32.GetWindowDC.restype = handle
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, handle]
    user32.PrintWindow.argtypes = [wintypes.HWND, handle, wintypes.UINT]
    gdi32.CreateCompatibleDC.restype = handle
    gdi32.CreateCompatibleDC.argtypes = [handle]
    gdi32.CreateCompatibleBitmap.restype = handle
    gdi32.CreateCompatibleBitmap.argtypes = [handle, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = handle
    gdi32.SelectObject.argtypes = [handle, handle]
    gdi32.GetDIBits.argtypes = [
        handle, handle, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [handle]
    gdi32.DeleteDC.argtypes = [handle]

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
            raise KakaoError("검색 결과 창을 이미지로 저장하지 못했습니다.")
        pixels = ctypes.create_string_buffer(width * height * 4)
        info = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        if gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, info, 0) != height:
            raise KakaoError("검색 결과 창의 이미지를 읽지 못했습니다.")
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)
    raw = pixels.raw
    if raw == raw[:4] * (width * height):
        raise KakaoError("검색 결과 창이 빈 이미지로 저장됐습니다. 창을 가리지 않게 두고 다시 실행하세요.")
    return _png_bytes(width, height, raw)


def _page_path(image: Path, index: int) -> Path:
    return image if index == 0 else image.with_name(f"{image.stem}-{index + 1}{image.suffix}")


def _save_result_pages(hwnd: int, image: Path, pages: int) -> tuple[list[Path], bool]:
    """Save result pages until scrolling changes nothing. The bool says the end of the list was reached."""
    page = _capture_window(hwnd)
    image.write_bytes(page)
    saved = [image]
    lists = [c for c in _visible_children(hwnd) if win32gui.GetClassName(c) == SEARCH_RESULT_CLASS]
    if not lists:
        return saved, False

    def area(child: int) -> int:
        left, top, right, bottom = win32gui.GetWindowRect(child)
        return (right - left) * (bottom - top)

    results = max(lists, key=area)
    while len(saved) < pages:
        _scroll(results, SEARCH_SCROLL_TICKS, -win32con.WHEEL_DELTA)
        time.sleep(SEARCH_SCROLL_SETTLE)
        following = _capture_window(hwnd)
        if following == page:
            return saved, True
        path = _page_path(image, len(saved))
        path.write_bytes(following)
        saved.append(path)
        page = following
    return saved, False


def _click_search_all(main_hwnd: int, search_box: int) -> None:
    view = next(
        c for c in _visible_children(main_hwnd) if win32gui.GetWindowText(c).startswith(CHAT_LIST_VIEW)
    )
    view_left, view_top, _right, _bottom = win32gui.GetWindowRect(view)
    _left, box_top, box_right, box_bottom = win32gui.GetWindowRect(search_box)
    x, y = box_right + SEARCH_ALL_OFFSET - view_left, (box_top + box_bottom) // 2 - view_top
    position = (y << 16) | x
    win32gui.PostMessage(view, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, position)
    win32gui.PostMessage(view, win32con.WM_LBUTTONUP, 0, position)


def search_messages(
    query: str, image: Path, *, keep_open: bool = False, pages: int = 10, timeout: float = 3.0
) -> tuple[list[Path], bool]:
    """Run KakaoTalk's integrated message search and save its window as images. Opens no result.

    The result list cannot be read as text, so images are the output. Returns the
    saved paths and whether the end of the list was reached within `pages`.
    """
    if not query.strip():
        raise KakaoError("검색어를 입력하세요.")
    main = find_main_window()
    if not main:
        raise KakaoError("카카오톡 메인 창을 찾지 못했습니다.")
    _close_windows({w.hwnd for w in list_windows() if w.visible and w.title == SEARCH_ALL_TITLE})
    time.sleep(0.3)

    search_box = _chat_search_box(main.hwnd)
    before = _visible_hwnds()
    win32gui.SendMessage(search_box, win32con.WM_SETTEXT, 0, query)
    try:
        # An inactive window can use the first click just to activate itself.
        _focus(main.hwnd)
        time.sleep(SEARCH_SETTLE)
        _click_search_all(main.hwnd, search_box)
        opened: list[WindowInfo] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not opened:
            opened = [w for w in list_windows() if w.visible and w.hwnd not in before and w.title.strip()]
            time.sleep(0.1)
    finally:
        win32gui.SendMessage(search_box, win32con.WM_SETTEXT, 0, "")
    if len(opened) != 1 or opened[0].title != SEARCH_ALL_TITLE:
        _close_windows({w.hwnd for w in opened})
        raise KakaoError("통합검색 창을 열지 못했습니다. 메인 창을 가리지 않게 두고 다시 실행하세요.")

    window = opened[0]
    try:
        position = (MESSAGE_TAB_Y << 16) | MESSAGE_TAB_X
        win32gui.PostMessage(window.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, position)
        win32gui.PostMessage(window.hwnd, win32con.WM_LBUTTONUP, 0, position)
        time.sleep(SEARCH_ALL_SETTLE)
        return _save_result_pages(window.hwnd, image, pages)
    finally:
        if not keep_open:
            _close_windows({window.hwnd})


def _scroll(hwnd: int, ticks: int, delta: int) -> None:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    position = (((top + bottom) // 2 & 0xFFFF) << 16) | ((left + right) // 2 & 0xFFFF)
    for _ in range(ticks):
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, position)
        time.sleep(SCROLL_SETTLE)


def _scroll_up(hwnd: int, ticks: int) -> None:
    """Scroll the chat list toward older messages so KakaoTalk loads them."""
    _scroll(hwnd, ticks, win32con.WHEEL_DELTA)


def read_visible(
    room: str, *, exact: bool = False, timeout: float = 1.5, scroll: int = 0
) -> tuple[WindowInfo, str]:
    import win32clipboard

    window = open_room(room, exact=exact)
    list_candidates = [
        child
        for child in enum_children(window.hwnd)
        if "LIST" in win32gui.GetClassName(child).upper()
    ]
    if not list_candidates:
        raise KakaoError("대화 목록 컨트롤을 찾지 못했습니다. `kakaocli inspect --room`을 실행하세요.")

    target = max(
        list_candidates,
        key=lambda hwnd: max(0, win32gui.GetWindowRect(hwnd)[2] - win32gui.GetWindowRect(hwnd)[0])
        * max(0, win32gui.GetWindowRect(hwnd)[3] - win32gui.GetWindowRect(hwnd)[1]),
    )
    _focus(target)
    if scroll > 0:
        _scroll_up(target, scroll)
        _focus(target)
    sequence = ctypes.windll.user32.GetClipboardSequenceNumber()
    _press(ord("A"), (win32con.VK_CONTROL,))
    _press(ord("C"), (win32con.VK_CONTROL,))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctypes.windll.user32.GetClipboardSequenceNumber() != sequence:
            break
        time.sleep(0.05)
    else:
        raise KakaoError("화면의 대화 내용을 복사하지 못했습니다.")

    try:
        win32clipboard.OpenClipboard()
        text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    except Exception as exc:
        raise KakaoError("클립보드에서 대화 텍스트를 읽지 못했습니다.") from exc
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
    return window, text.strip()


def inspect_tree(room: str | None = None) -> list[dict]:
    window = find_room(room) if room else find_main_window()
    if not window:
        raise KakaoError("검사할 카카오톡 창을 찾지 못했습니다.")
    rows = []
    for child in enum_children(window.hwnd):
        left, top, right, bottom = win32gui.GetWindowRect(child)
        rows.append(
            {
                "hwnd": hex(child),
                "class_name": win32gui.GetClassName(child),
                "text": win32gui.GetWindowText(child),
                "visible": bool(win32gui.IsWindowVisible(child)),
                "rect": [left, top, right, bottom],
            }
        )
    return rows


def find_executable() -> Path | None:
    require_windows()
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Kakao" / "KakaoTalk" / "KakaoTalk.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Kakao" / "KakaoTalk" / "KakaoTalk.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Kakao" / "KakaoTalk" / "KakaoTalk.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def start_kakaotalk() -> Path:
    executable = find_executable()
    if not executable:
        raise KakaoError("KakaoTalk.exe를 찾지 못했습니다. PC 카카오톡을 먼저 설치하세요.")
    subprocess.Popen([str(executable)], close_fds=True)
    return executable


def doctor() -> dict:
    require_windows()
    executable = find_executable()
    windows = list_windows()
    main = find_main_window()
    rooms = list_rooms()
    return {
        "ok": bool(main),
        "platform": os.name,
        "executable": str(executable) if executable else None,
        "installed": executable is not None,
        "running": bool(windows),
        "main_window_found": main is not None,
        "open_rooms": [room.title for room in rooms],
        "next": (
            "준비 완료"
            if main
            else "PC 카카오톡을 설치하고 로그인한 뒤 메인 창을 열어 두세요."
        ),
    }
