import struct
import zlib

import pytest

import kakaocli_win.backend as backend
import kakaocli_win.cli as cli
from kakaocli_win.backend import KakaoError, WindowInfo
from kakaocli_win.cli import build_parser


def test_send_dry_run_arguments():
    args = build_parser().parse_args(["--json", "send", "친구", "안녕", "--dry-run"])
    assert args.json is True
    assert args.command == "send"
    assert args.room == "친구"
    assert args.message == "안녕"
    assert args.dry_run is True


def test_send_requires_message():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["send", "친구"])


def test_send_self_dry_run_arguments():
    args = build_parser().parse_args(["--json", "send-self", "메모", "--dry-run"])
    assert args.command == "send-self"
    assert args.message == "메모"
    assert args.dry_run is True
    assert args.yes is False


def test_inspect_room_argument():
    args = build_parser().parse_args(["inspect", "--room", "테스트방"])
    assert args.room == "테스트방"


def test_friends_filter_arguments():
    args = build_parser().parse_args(["--json", "friends", "--contains", "vs"])
    assert args.command == "friends"
    assert args.contains == "vs"
    assert args.include_hidden is False


def test_chat_rooms_filter_arguments():
    args = build_parser().parse_args(
        ["--json", "chat-rooms", "--contains", "바다", "--type", "MultiChat", "--limit", "5"]
    )
    assert args.command == "chat-rooms"
    assert args.contains == "바다"
    assert args.room_type == "MultiChat"
    assert args.limit == 5
    assert args.include_ids is False


def test_export_room_contacts_arguments():
    args = build_parser().parse_args(
        [
            "--json",
            "export-room-contacts",
            "동창방",
            "--output",
            "contacts.csv",
            "--exact",
        ]
    )
    assert args.command == "export-room-contacts"
    assert args.room == "동창방"
    assert args.output == "contacts.csv"
    assert args.exact is True
    assert args.allow_partial is False


def make_window(title: str, hwnd: int) -> WindowInfo:
    return WindowInfo(hwnd, title, backend.CHAT_CLASS, 380, 640, True, "chat")


def test_find_room_rejects_ambiguous_partial_match(monkeypatch):
    monkeypatch.setattr(
        backend,
        "list_rooms",
        lambda: [make_window("개발팀", 1), make_window("개발팀 공지", 2)],
    )
    with pytest.raises(KakaoError, match="여러 개"):
        backend.find_room("개발팀")


def test_find_room_exact_match(monkeypatch):
    expected = make_window("개발팀", 1)
    monkeypatch.setattr(
        backend,
        "list_rooms",
        lambda: [expected, make_window("개발팀 공지", 2)],
    )
    assert backend.find_room("개발팀", exact=True) == expected


def test_open_room_requires_manual_open_when_not_found(monkeypatch):
    monkeypatch.setattr(backend, "list_rooms", lambda: [])
    with pytest.raises(KakaoError, match="직접 독립 창으로"):
        backend.open_room("개발팀", exact=True)


@pytest.fixture
def focus_env(monkeypatch):
    """Fake window APIs so no real key reaches the desktop; returns the keys that _press sent."""
    pressed = []
    monkeypatch.setattr(backend, "FOCUS_TIMEOUT", 0)
    monkeypatch.setattr(backend, "find_room", lambda *_a, **_k: make_window("개발팀", 111))
    monkeypatch.setattr(backend, "_press", lambda vk, modifiers=(): pressed.append(vk))
    monkeypatch.setattr(backend, "_input_handles", lambda _hwnd: [111])
    monkeypatch.setattr(backend, "enum_children", lambda _hwnd: [111])
    monkeypatch.setattr(backend, "_set_text", lambda _hwnd, _text: True)
    monkeypatch.setattr(backend.win32gui, "GetClassName", lambda _hwnd: "EVA_VH_ListControl_Dblclk")
    monkeypatch.setattr(backend.win32gui, "GetWindowRect", lambda _hwnd: (0, 0, 10, 10))
    monkeypatch.setattr(backend.win32gui, "GetAncestor", lambda hwnd, _flag: hwnd)
    monkeypatch.setattr(backend.win32gui, "ShowWindow", lambda *_a: None)
    monkeypatch.setattr(backend.win32api, "keybd_event", lambda *_a: None)
    monkeypatch.setattr(
        backend.win32process,
        "GetWindowThreadProcessId",
        lambda _hwnd: (backend.win32api.GetCurrentThreadId(), 0),
    )
    return pressed


@pytest.mark.parametrize(
    "action",
    [
        lambda: backend.send_message("개발팀", "테스트"),
        lambda: backend.read_visible("개발팀"),
    ],
)
def test_no_keys_are_pressed_when_room_is_not_in_front(focus_env, monkeypatch, action):
    monkeypatch.setattr(backend.win32gui, "GetForegroundWindow", lambda: 999)
    with pytest.raises(KakaoError, match="맨 앞"):
        action()
    assert focus_env == []


def test_foreground_lost_before_enter_does_not_send(focus_env, monkeypatch):
    front = {"hwnd": 111}

    def lose_focus_while_typing(_hwnd, _text):
        front["hwnd"] = 999
        return True

    monkeypatch.setattr(backend.win32gui, "GetForegroundWindow", lambda: front["hwnd"])
    monkeypatch.setattr(backend, "_set_text", lose_focus_while_typing)
    with pytest.raises(KakaoError, match="맨 앞"):
        backend.send_message("개발팀", "테스트")
    assert backend.win32con.VK_RETURN not in focus_env


def test_png_bytes_encodes_pixels():
    png = backend._png_bytes(2, 1, bytes([1, 2, 3, 255, 4, 5, 6, 255]))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (2, 1)
    compressed = png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8]
    assert zlib.decompress(compressed) == bytes([0, 3, 2, 1, 6, 5, 4])


def test_search_messages_saves_pages_then_closes_without_opening_a_result(search_env, monkeypatch, tmp_path):
    dialog = WindowInfo(900, backend.SEARCH_ALL_TITLE, backend.CHAT_CLASS, 580, 640, True, "popup")
    monkeypatch.setattr(backend, "SEARCH_ALL_SETTLE", 0)
    monkeypatch.setattr(backend, "_save_result_pages", lambda hwnd, path, pages: ([path], True))
    monkeypatch.setattr(backend, "_click_search_all", lambda _m, _b: search_env["windows"].append(dialog))
    image = tmp_path / "result.png"
    assert backend.search_messages("11월 25일", image, timeout=0.2) == ([image], True)
    assert search_env["closed"] == [900]
    assert search_env["texts"] == ["11월 25일", ""]
    assert search_env["keys"] == []


@pytest.fixture
def pages_env(monkeypatch):
    """Fake capture and result list; `shots` is the sequence of captured images."""
    state = {"shots": [], "scrolled": 0}
    monkeypatch.setattr(backend, "SEARCH_SCROLL_SETTLE", 0)
    monkeypatch.setattr(backend, "SCROLL_SETTLE", 0)
    monkeypatch.setattr(backend, "_visible_children", lambda _hwnd: [11, 12])
    monkeypatch.setattr(backend.win32gui, "GetClassName", lambda _hwnd: backend.SEARCH_RESULT_CLASS)
    monkeypatch.setattr(
        backend.win32gui, "GetWindowRect", lambda hwnd: (0, 0, 100, 100) if hwnd == 12 else (0, 0, 10, 10)
    )
    monkeypatch.setattr(backend, "_capture_window", lambda _hwnd: state["shots"].pop(0))

    def post(hwnd, _message, wparam, _position):
        assert hwnd == 12, "the largest result list must receive the scroll"
        assert wparam >> 16 == (-backend.win32con.WHEEL_DELTA & 0xFFFF)
        state["scrolled"] += 1

    monkeypatch.setattr(backend.win32gui, "PostMessage", post)
    return state


def test_result_pages_stop_when_scrolling_changes_nothing(pages_env, tmp_path):
    pages_env["shots"] = [b"a", b"b", b"b"]
    image = tmp_path / "p.png"
    saved, complete = backend._save_result_pages(900, image, 10)
    assert complete is True
    assert saved == [image, tmp_path / "p-2.png"]
    assert [path.read_bytes() for path in saved] == [b"a", b"b"]


def test_result_pages_report_incomplete_at_page_limit(pages_env, tmp_path):
    pages_env["shots"] = [b"a", b"b", b"c"]
    saved, complete = backend._save_result_pages(900, tmp_path / "p.png", 3)
    assert complete is False and len(saved) == 3


def test_single_result_page_is_complete(pages_env, tmp_path):
    pages_env["shots"] = [b"a", b"a"]
    saved, complete = backend._save_result_pages(900, tmp_path / "p.png", 10)
    assert complete is True and len(saved) == 1


def test_search_messages_keep_open_leaves_window(search_env, monkeypatch, tmp_path):
    dialog = WindowInfo(900, backend.SEARCH_ALL_TITLE, backend.CHAT_CLASS, 580, 640, True, "popup")
    monkeypatch.setattr(backend, "SEARCH_ALL_SETTLE", 0)
    monkeypatch.setattr(backend, "_save_result_pages", lambda _h, path, _p: ([path], True))
    monkeypatch.setattr(backend, "_click_search_all", lambda _m, _b: search_env["windows"].append(dialog))
    backend.search_messages("11월 25일", tmp_path / "r.png", keep_open=True, timeout=0.2)
    assert search_env["closed"] == []


def test_search_messages_closes_unexpected_window(search_env, monkeypatch, tmp_path):
    other = WindowInfo(900, "친구 추가", backend.CHAT_CLASS, 300, 460, True, "popup")
    monkeypatch.setattr(backend, "_click_search_all", lambda _m, _b: search_env["windows"].append(other))
    image = tmp_path / "r.png"
    with pytest.raises(KakaoError, match="통합검색"):
        backend.search_messages("11월 25일", image, timeout=0.2)
    assert search_env["closed"] == [900] and not image.exists()


def test_search_messages_rejects_blank_query(tmp_path):
    with pytest.raises(KakaoError, match="검색어"):
        backend.search_messages("  ", tmp_path / "r.png")


def test_scroll_up_posts_wheel_messages_to_the_list(monkeypatch):
    posted = []
    monkeypatch.setattr(backend, "SCROLL_SETTLE", 0)
    monkeypatch.setattr(backend.win32gui, "GetWindowRect", lambda _hwnd: (100, 200, 300, 400))
    monkeypatch.setattr(backend.win32gui, "PostMessage", lambda *args: posted.append(args))
    backend._scroll_up(55, 3)
    delta = backend.win32con.WHEEL_DELTA << 16
    assert posted == [(55, backend.win32con.WM_MOUSEWHEEL, delta, (300 << 16) | 200)] * 3


def test_read_scroll_argument():
    assert build_parser().parse_args(["read", "방", "--scroll", "5"]).scroll == 5
    assert build_parser().parse_args(["read", "방"]).scroll == 0


def test_focus_never_taps_bare_alt(focus_env, monkeypatch):
    events = []
    fronts = iter([999, 111])
    monkeypatch.setattr(backend.win32gui, "GetForegroundWindow", lambda: next(fronts))
    monkeypatch.setattr(backend.win32api, "keybd_event", lambda vk, _s, flags, _e: events.append((vk, flags)))
    backend._focus(111)
    up = backend.win32con.KEYEVENTF_KEYUP
    assert events == [
        (backend.win32con.VK_MENU, 0),
        (backend.MENU_MASK_KEY, 0),
        (backend.MENU_MASK_KEY, up),
        (backend.win32con.VK_MENU, up),
    ]


def test_focus_sends_no_alt_when_already_in_front(focus_env, monkeypatch):
    events = []
    monkeypatch.setattr(backend.win32gui, "GetForegroundWindow", lambda: 111)
    monkeypatch.setattr(backend.win32api, "keybd_event", lambda *a: events.append(a))
    backend._focus(111)
    assert events == []


def test_require_foreground_passes_for_child_of_room(focus_env, monkeypatch):
    monkeypatch.setattr(backend.win32gui, "GetForegroundWindow", lambda: 222)
    monkeypatch.setattr(backend.win32gui, "GetAncestor", lambda _hwnd, _flag: 111)
    backend._require_foreground(333)


@pytest.fixture
def search_env(monkeypatch):
    """Fake a KakaoTalk main window; `windows` is the mutable list of top-level windows."""
    main = WindowInfo(500, "카카오톡", backend.CHAT_CLASS, 393, 502, True, "main")
    state = {
        "windows": [main],
        "keys": [],
        "texts": [],
        "closed": [],
        "children": {
            600: ("ChatRoomListView_0x1", "EVA_Window"),
            601: ("", "Edit"),
            602: ("ChatRoomListCtrl_0x1", "EVA_VH_ListControl_Dblclk"),
        },
        "on_open": lambda: None,
        "on_ctrl_f": lambda: None,
    }

    def press(vk, modifiers=()):
        state["keys"].append(vk)
        if vk == ord("F"):
            state["on_ctrl_f"]()

    def post(hwnd, message, _w, _l):
        if message == backend.win32con.WM_CLOSE:
            state["closed"].append(hwnd)
        elif message == backend.win32con.WM_LBUTTONDBLCLK:
            state["on_open"]()

    monkeypatch.setattr(backend, "SEARCH_SETTLE", 0)
    monkeypatch.setattr(backend, "list_windows", lambda: list(state["windows"]))
    monkeypatch.setattr(backend, "list_rooms", lambda: [w for w in state["windows"] if w.kind == "chat"])
    monkeypatch.setattr(backend, "_focus", lambda _hwnd: None)
    monkeypatch.setattr(backend, "_require_foreground", lambda _hwnd: None)
    monkeypatch.setattr(backend, "_press", press)
    monkeypatch.setattr(backend, "enum_children", lambda _hwnd: list(state["children"]))
    monkeypatch.setattr(backend.win32gui, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(backend.win32gui, "GetWindowText", lambda hwnd: state["children"][hwnd][0])
    monkeypatch.setattr(backend.win32gui, "GetClassName", lambda hwnd: state["children"][hwnd][1])
    monkeypatch.setattr(backend.win32gui, "GetWindowRect", lambda _hwnd: (0, 0, 300, 300))
    monkeypatch.setattr(backend.win32gui, "SendMessage", lambda _h, _m, _w, text: state["texts"].append(text))
    monkeypatch.setattr(backend.win32gui, "PostMessage", post)
    return state


def test_search_opens_exact_room_and_clears_search_box(search_env):
    room = make_window("개발팀", 700)
    search_env["on_open"] = lambda: search_env["windows"].append(room)
    assert backend.search_and_open("개발팀", timeout=0.2) == room
    assert search_env["texts"] == ["개발팀", ""]
    assert search_env["closed"] == [] and search_env["keys"] == []


def test_search_closes_room_with_different_title(search_env):
    search_env["on_open"] = lambda: search_env["windows"].append(make_window("개발팀 공지", 700))
    with pytest.raises(KakaoError, match="읽음"):
        backend.search_and_open("개발팀", timeout=0.2)
    assert search_env["closed"] == [700]
    assert search_env["texts"][-1] == ""


def test_search_closes_non_chat_result(search_env):
    profile = WindowInfo(700, "개발팀", backend.CHAT_CLASS, 300, 500, True, "popup")
    search_env["on_open"] = lambda: search_env["windows"].append(profile)
    with pytest.raises(KakaoError, match="닫았습니다"):
        backend.search_and_open("개발팀", timeout=0.2)
    assert search_env["closed"] == [700]


def test_search_reports_no_result(search_env):
    with pytest.raises(KakaoError, match="새로 열지 못했습니다"):
        backend.search_and_open("없는방", timeout=0.05)
    assert search_env["closed"] == []


def test_search_uses_open_room_without_keys(search_env):
    room = make_window("개발팀", 700)
    search_env["windows"].append(room)
    assert backend.search_and_open("개발팀") == room
    assert search_env["keys"] == [] and search_env["texts"] == []


def test_search_requires_chat_tab(search_env):
    search_env["children"] = {600: ("ContactListView_0x1", "EVA_Window"), 601: ("", "Edit")}
    with pytest.raises(KakaoError, match="채팅 탭"):
        backend.search_and_open("개발팀")
    assert search_env["keys"] == [] and search_env["texts"] == []


def test_search_closes_add_friend_dialog_without_typing(search_env):
    del search_env["children"][601]
    dialog = WindowInfo(800, "", backend.CHAT_CLASS, 300, 460, True, "main")
    search_env["on_ctrl_f"] = lambda: search_env["windows"].append(dialog)
    with pytest.raises(KakaoError, match="다른 창"):
        backend.search_and_open("개발팀")
    assert search_env["closed"] == [800]
    assert search_env["texts"] == []


def test_search_refuses_when_list_is_ambiguous(search_env):
    search_env["children"][603] = ("OtherListCtrl_0x1", "EVA_VH_ListControl_Dblclk")
    with pytest.raises(KakaoError, match="채팅 목록"):
        backend.search_and_open("개발팀", timeout=0.05)
    assert search_env["texts"] == ["개발팀", ""]


def test_send_open_cannot_skip_confirmation():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["send", "방", "내용", "--open", "--yes"])


def test_send_open_confirms_opened_room_before_sending(monkeypatch):
    events = []
    room = make_window("개발팀", 700)
    monkeypatch.setattr(cli, "search_and_open", lambda name: events.append(("open", name)) or room)
    monkeypatch.setattr(cli, "confirm_send", lambda name, _m, *, skip: events.append(("confirm", name, skip)) or True)
    monkeypatch.setattr(cli, "send_message", lambda name, _m, *, exact: events.append(("send", name, exact)) or room)
    assert cli.main(["send", "개발팀", "내용", "--open"]) == 0
    assert events == [("open", "개발팀"), ("confirm", "개발팀", False), ("send", "개발팀", True)]


def test_send_open_declined_does_not_send(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "search_and_open", lambda _name: make_window("개발팀", 700))
    monkeypatch.setattr(cli, "confirm_send", lambda *_a, **_k: False)
    monkeypatch.setattr(cli, "send_message", lambda *_a, **_k: sent.append(True))
    assert cli.main(["send", "개발팀", "내용", "--open"]) == 2
    assert sent == []


def test_send_open_dry_run_does_not_open(monkeypatch):
    def fail(_name):
        raise AssertionError("dry-run must not open a room")

    monkeypatch.setattr(cli, "search_and_open", fail)
    assert cli.main(["send", "개발팀", "내용", "--open", "--dry-run"]) == 0


def test_set_text_falls_back_to_replace_selection(monkeypatch):
    state = {"text": ""}

    def send_message(_hwnd, message, _wparam, lparam):
        if message == backend.win32con.EM_REPLACESEL:
            state["text"] = lparam

    monkeypatch.setattr(backend.win32gui, "SendMessage", send_message)
    monkeypatch.setattr(backend.win32gui, "GetWindowText", lambda _hwnd: state["text"])
    assert backend._set_text(1, "테스트") is True
    assert state["text"] == "테스트"
