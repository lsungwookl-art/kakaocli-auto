"""Read-only KakaoTalk triage: read open rooms with kakaocli, flag what needs a reply with Jev.

This tool has no send path. Message text goes to the TypeSafe API unless --dry-run is set.

    python kakao_triage.py "방이름" --me "내 이름"
    python kakao_triage.py --all-open --me "내 이름"
    python kakao_triage.py "방이름" --dry-run
    python kakao_triage.py --self-check
"""
import argparse
import concurrent.futures
import ctypes
import dataclasses
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

KAKAOCLI = Path(os.environ.get("KAKAOCLI", Path.home() / "kakaocli-win/.venv/Scripts/kakaocli.exe"))
API_URL = "https://api.typesafe.ai/v1/systemone"
CONTEXT_MESSAGES = 5
WORKERS = 4
RETRIES = 3

QUESTIONS = {
    "needs_reply": {
        "type": "noul",
        "instructions": "state의 target 메시지는 이 채팅방을 읽는 사람의 답장이나 조치가 필요한가? recent_messages는 맥락이다.",
    },
    "category": {
        "type": "choice",
        "instructions": "state의 target 메시지 종류는?",
        "criteria": {
            "잡담": "인사, 축하, 맞장구, 단순 확인 등 업무 내용이 없다",
            "정보": "알림이나 공유이며 받는 사람의 답이나 판단이 필요 없다",
            "요청": "자료 전달, 발행, 처리 등 받는 사람의 행동을 요구한다",
            "문제": "장애, 지연, 오류, 사고 등 해결이 필요한 상황을 보고한다",
            "결정": "승인이나 선택 등 받는 사람의 의사결정을 요청한다",
            "일정": "날짜나 시간을 정하거나 바꾸거나 확인한다",
            "질문": "사실이나 정보를 묻고 답을 받으면 끝난다",
        },
    },
}

ALL_CATEGORIES = "전체"
TENTATIVE = re.compile(r"쯤|아직|미정|확정.{0,3}아|확정 전|것 같|듯합|듯해|잡을까|될까요|할까요|어떨까|어때|괜찮을까|\?")
DATE_LINE = re.compile(r"^\d{4}년 \d{1,2}월 \d{1,2}일")
MESSAGE_LINE = re.compile(r"^\[(.+?)\] \[(오전|오후) (\d{1,2}:\d{2})\] (.*)$")


@dataclasses.dataclass
class Message:
    date: str
    sender: str
    time: str
    text: str


def parse(text):
    messages, date = [], ""
    for line in text.splitlines():
        if DATE_LINE.match(line):
            date = line.strip()
        elif match := MESSAGE_LINE.match(line):
            sender, meridiem, clock, body = match.groups()
            messages.append(Message(date, sender, f"{meridiem} {clock}", body))
        elif messages and line.strip():
            messages[-1].text += "\n" + line  # A multi-line message continues until the next header.
    return messages


def pending(messages, me):
    """Keep only messages after my latest one; anything earlier counts as already answered."""
    # ponytail: treats any reply of mine as answering everything before it. Per-thread tracking would need Jev.
    if not me:
        return list(range(len(messages)))
    last = max((i for i, m in enumerate(messages) if m.sender == me), default=-1)
    return [i for i in range(last + 1, len(messages)) if messages[i].sender != me]


def run_kakaocli(*args):
    env = {**os.environ, "PYTHONUTF8": "1"}
    done = subprocess.run(
        [str(KAKAOCLI), "--json", *args], capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=30
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip()[:300] or f"exit {done.returncode}")
    return json.loads(done.stdout)


def clear_clipboard():
    """kakaocli read copies the chat; do not leave it on the clipboard."""
    user32 = ctypes.windll.user32
    if user32.OpenClipboard(None):
        user32.EmptyClipboard()
        user32.CloseClipboard()


def front_window():
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    foreground = user32.GetForegroundWindow()
    return int(user32.GetAncestor(foreground, 2) or foreground or 0)  # 2 = GA_ROOT


def bring_to_front(hwnd):
    """Windows refuses focus changes from background processes unless Alt is down, and fails silently."""
    user32 = ctypes.windll.user32
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.keybd_event(0x12, 0, 0, 0)  # 0x12 = VK_MENU (Alt)
    try:
        # A bare Alt tap toggles menu mode in the target, which swallows later keys.
        user32.keybd_event(0xE8, 0, 0, 0)
        user32.keybd_event(0xE8, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.keybd_event(0x12, 0, 2, 0)  # 2 = KEYEVENTF_KEYUP
    time.sleep(0.2)
    return front_window() == hwnd


def find_target(rooms, name, exact):
    matches = [r for r in rooms if r["title"] == name] or ([] if exact else [r for r in rooms if name in r["title"]])
    if len(matches) != 1:
        raise RuntimeError(f"열린 방 중 일치하는 창이 {len(matches)}개입니다. 방을 독립 창으로 열고 정확한 이름을 쓰세요.")
    return matches[0]


def read_room(room, scroll=0):
    """kakaocli read presses Ctrl+A/C in whichever app is in front, so confirm the room is in front."""
    hwnd = int(room["hwnd"], 16)
    if not bring_to_front(hwnd):
        raise RuntimeError("채팅방을 맨 앞으로 가져오지 못해 읽지 않았습니다.")
    result = run_kakaocli("read", room["title"], "--exact", "--scroll", str(scroll))
    if front_window() != hwnd:
        raise RuntimeError("읽는 중 다른 창이 앞으로 와서 결과를 버렸습니다. 실행 중에는 다른 창을 누르지 마세요.")
    return checked_messages(result["text"])


def checked_messages(text):
    messages = parse(text)
    if not messages:
        raise RuntimeError("채팅 형식의 메시지를 찾지 못해 결과를 버렸습니다.")
    return messages


def build_state(room, messages, index):
    start = max(0, index - CONTEXT_MESSAGES)
    brief = lambda m: {"sender": m.sender, "time": m.time, "text": m.text}
    return {
        "room": room,
        "recent_messages": [brief(m) for m in messages[start:index]],
        "target": brief(messages[index]),
    }


def ask_jev(state, key):
    body = json.dumps({"model": "jev-latest", "state": state, "questions": QUESTIONS}).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "kakao-triage/0.1"}
    for attempt in range(RETRIES + 1):
        try:
            request = urllib.request.Request(API_URL, data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 529) or attempt == RETRIES:
                raise RuntimeError(f"HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == RETRIES:
                raise RuntimeError(type(error).__name__) from None
        time.sleep(2**attempt)


def judge(room, messages, indexes, key):
    def one(index):
        message = messages[index]
        row = {"room": room, "date": message.date, "time": message.time, "sender": message.sender, "text": message.text}
        try:
            answers = ask_jev(build_state(room, messages, index), key)["answers"]
            row["needs_reply"] = answers["needs_reply"]["noul"]
            row["category"] = answers["category"]["choice"]
        except (RuntimeError, KeyError, TypeError, ValueError) as error:
            row["error"] = str(error) or type(error).__name__
        return row

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(one, indexes))


def parse_message_date(header):
    match = re.match(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일", header or "")
    return datetime.date(*map(int, match.groups())) if match else None


def last_match(pattern, text):
    matches = list(re.finditer(pattern, text))
    return matches[-1] if matches else None


def parse_when(text, today):
    """Read the last date and time named in a Korean message; the last one is the new value in 'A에서 B로'.

    Returns (date or None, time or None, time_is_ambiguous).
    """
    # ponytail: regex covers common phrases only. Anything it cannot read becomes 확인 필요 instead of a guess.
    found = []
    for match in re.finditer(r"(오늘|내일|모레)", text):
        found.append((match.start(), today + datetime.timedelta(days=("오늘", "내일", "모레").index(match[1]))))
    for match in re.finditer(r"(\d{1,2})월\s*(\d{1,2})일", text):
        try:
            value = datetime.date(today.year, int(match[1]), int(match[2]))
        except ValueError:
            continue
        if (today - value).days > 180:
            value = value.replace(year=today.year + 1)
        found.append((match.start(), value))
    for match in re.finditer(r"(?:(이번|다음)\s*주\s*)?([월화수목금토일])요일", text):
        weekday = "월화수목금토일".index(match[2])
        if match[1]:
            monday = today - datetime.timedelta(days=today.weekday())
            value = monday + datetime.timedelta(days=weekday + (7 if match[1] == "다음" else 0))
        else:
            value = today + datetime.timedelta(days=(weekday - today.weekday()) % 7)
        found.append((match.start(), value))
    date = max(found, key=lambda item: item[0])[1] if found else None

    clock = last_match(r"(\d{1,2}):(\d{2})", text)
    korean = last_match(r"(?:(오전|오후|아침|저녁|밤)\s*)?(\d{1,2})시(?:\s*(반|\d{1,2}분))?", text)
    if korean and (not clock or korean.start() > clock.start()):
        meridiem, hour = korean[1], int(korean[2])
        minute = 30 if korean[3] == "반" else int(korean[3][:-1]) if korean[3] else 0
        ambiguous = meridiem is None and hour <= 12
        if meridiem in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        if meridiem in ("오전", "아침") and hour == 12:
            hour = 0
    elif clock:
        hour, minute, ambiguous = int(clock[1]), int(clock[2]), False
    else:
        return date, None, False
    if hour > 23 or minute > 59:
        return date, None, True
    return date, datetime.time(hour, minute), ambiguous


def find_event(text, events):
    """Pick the event sharing the most title words with the message; a tie or no overlap finds nothing."""
    scored = []
    for event in events:
        words = [w for w in re.findall(r"[0-9A-Za-z가-힣]+", event.get("summary", "")) if len(w) >= 2]
        score = sum(word in text for word in words)
        if score:
            scored.append((score, event))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None, False
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, True
    return scored[0][1], False


def propose(row, events):
    """Compare one schedule message with calendar events. Never changes anything; a person approves the result."""
    result = {
        "status": "확인 필요",
        "room": row["room"],
        "sender": row["sender"],
        "sent": f"{row['date']} {row['time']}",
        "text": row["text"],
    }

    def needs_check(reason):
        return {**result, "reason": reason}

    today = parse_message_date(row["date"])
    if not today:
        return needs_check("메시지 날짜를 알 수 없음")
    if TENTATIVE.search(row["text"]):
        return needs_check("확정되지 않은 표현")
    date, clock, ambiguous = parse_when(row["text"], today)
    if ambiguous:
        return needs_check("오전/오후를 알 수 없음")
    if not date and not clock:
        return needs_check("날짜나 시간을 찾지 못함")

    event, tied = find_event(row["text"], events)
    if tied:
        return needs_check("비슷한 일정이 여러 개")
    if not event:
        if date and clock:
            return {**result, "status": "캘린더에 없음", "start": datetime.datetime.combine(date, clock).isoformat()}
        return needs_check("맞는 일정이 없고 날짜나 시간이 빠짐")

    result.update(event_id=event.get("id"), event=event.get("summary"), attendees=len(event.get("attendees", [])))
    start = event.get("start", {}).get("dateTime")
    end = event.get("end", {}).get("dateTime")
    if not start or not end:
        return needs_check("종일 일정")
    if event.get("recurringEventId") or event.get("recurrence"):
        return needs_check("반복 일정")
    current = datetime.datetime.fromisoformat(start).replace(tzinfo=None)
    finish = datetime.datetime.fromisoformat(end).replace(tzinfo=None)
    target = datetime.datetime.combine(date or current.date(), clock or current.time())
    if target == current:
        return {**result, "status": "일치", "start": current.isoformat()}
    return {
        **result,
        "status": "수정안",
        "from": current.isoformat(),
        "start": target.isoformat(),
        "end": (target + (finish - current)).isoformat(),
    }


def load_events(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("events", [])


def print_proposals(proposals):
    print(f"\n일정 비교 {len(proposals)}건")
    for item in proposals:
        print(f"  [{item['status']}] [{item['room']}] {item['sent']} {item['sender']}: {' '.join(item['text'].split())[:60]}")
        if item["status"] == "수정안":
            print(f"      일정 '{item['event']}': {item['from']} -> {item['start']} ~ {item['end']} (참석자 {item['attendees']}명)")
        elif item["status"] == "캘린더에 없음":
            print(f"      추가 후보: {item['start']}")
        elif item["status"] == "일치":
            print(f"      일정 '{item['event']}'과 일치: {item['start']}")
        else:
            print(f"      이유: {item['reason']}")


def bucket(rows, threshold, category=""):
    """Failed judgments stay visible: a message we could not judge must not read as 'no reply needed'.

    A category selects by label alone, since a schedule change can look like it needs no reply.
    """
    failed = [r for r in rows if "error" in r]
    if category == ALL_CATEGORIES:
        keep = lambda r: True
    elif category:
        keep = lambda r: r["category"] == category
    else:
        keep = lambda r: r["needs_reply"] >= threshold
    flagged = sorted(
        (r for r in rows if "error" not in r and keep(r)),
        key=lambda r: r["needs_reply"],
        reverse=True,
    )
    return flagged, failed


def report(rooms, flagged, failed, skipped, threshold, category=""):
    for room, total, checked in rooms:
        print(f"[{room}] 읽음 {total}개, 판단 대상 {checked}개")
    for room, reason in skipped:
        print(f"[{room}] 읽기 실패: {reason}")
    print(f"\n{category} 메시지 {len(flagged)}개" if category else f"\n답장 필요 {len(flagged)}개 (기준 {threshold})")
    for row in flagged:
        excerpt = " ".join(row["text"].split())[:80]
        print(f"  {row['needs_reply']:.2f} {row['category']} [{row['room']}] {row['time']} {row['sender']}: {excerpt}")
    if failed:
        print(f"\n판단 실패 {len(failed)}개: 직접 확인하세요")
        for row in failed:
            print(f"  [{row['room']}] {row['time']} {row['sender']} ({row['error']})")


def self_check():
    sample = (
        "2026년 11월 7일 토요일\n"
        "[김대리] [오전 9:05] 일정 변경 가능할까요?\n"
        "둘째 줄입니다\n"
        "[나] [오전 9:10] 네 가능합니다\n"
        "[김대리] [오후 1:00] 감사합니다\n"
        "[박과장] [오후 1:02] 계산서 부탁드려요\n"
    )
    messages = parse(sample)
    assert [m.sender for m in messages] == ["김대리", "나", "김대리", "박과장"]
    assert messages[0].text == "일정 변경 가능할까요?\n둘째 줄입니다"
    assert messages[0].date.startswith("2026년") and messages[3].time == "오후 1:02"
    assert pending(messages, "나") == [2, 3]
    assert pending(messages, "") == [0, 1, 2, 3]
    assert pending(messages, "없는사람") == [0, 1, 2, 3]
    assert pending(parse("[나] [오전 9:10] 끝"), "나") == []
    assert parse("") == []
    state = build_state("방", messages, 3)
    assert state["target"]["sender"] == "박과장" and len(state["recent_messages"]) == 3
    rows = [
        {"needs_reply": 0.9, "text": "a"},
        {"needs_reply": 0.2, "text": "b"},
        {"needs_reply": 0.5, "text": "c"},
        {"error": "HTTP 500", "text": "d"},
    ]
    flagged, failed = bucket(rows, 0.5)
    assert [r["text"] for r in flagged] == ["a", "c"] and [r["text"] for r in failed] == ["d"]
    labeled = [
        {"needs_reply": 0.1, "category": "일정", "text": "a"},
        {"needs_reply": 0.9, "category": "요청", "text": "b"},
        {"error": "HTTP 500", "text": "c"},
    ]
    flagged, failed = bucket(labeled, 0.5, "일정")
    assert [r["text"] for r in flagged] == ["a"] and [r["text"] for r in failed] == ["c"]
    assert [r["text"] for r in bucket(labeled, 0.5, ALL_CATEGORIES)[0]] == ["b", "a"]

    saturday = datetime.date(2026, 11, 7)
    at = datetime.time
    day = lambda month, date: datetime.date(2026, month, date)
    for text, expected in [
        ("11월 20일 오후 3시에서 오후 5시로 옮겼습니다", (day(11, 20), at(17, 0), False)),
        ("내일 오전 10시 30분 회의", (day(11, 8), at(10, 30), False)),
        ("모레 14:30 점검", (day(11, 9), at(14, 30), False)),
        ("다음 주 금요일 오후 2시 반", (day(11, 13), at(14, 30), False)),
        ("이번 주 일요일 오전 12시", (day(11, 8), at(0, 0), False)),
        ("월요일 저녁 7시", (day(11, 9), at(19, 0), False)),
        ("오늘 3시", (day(11, 7), at(3, 0), True)),
        ("오후 3시로 변경", (None, at(15, 0), False)),
        ("11월 30일로 미룹니다", (day(11, 30), None, False)),
        ("13월 40일 25시", (None, None, True)),
        ("일정 공유합니다", (None, None, False)),
    ]:
        assert parse_when(text, saturday) == expected, text
    assert parse_when("1월 3일 오전 9시", datetime.date(2026, 11, 7))[0] == datetime.date(2027, 1, 3)

    event = {
        "id": "e1",
        "summary": "납품 회의",
        "start": {"dateTime": "2026-11-20T15:00:00+09:00"},
        "end": {"dateTime": "2026-11-20T15:30:00+09:00"},
    }
    other = {**event, "id": "e2", "summary": "현장 점검"}
    base = {"room": "방", "sender": "김", "date": "2026년 11월 7일 토요일", "time": "오전 9:00"}
    check = lambda text, items=(event, other): propose({**base, "text": text}, list(items))
    moved = check("납품 회의를 오후 5시로 옮겼습니다")
    assert moved["status"] == "수정안" and moved["event_id"] == "e1"
    assert (moved["start"], moved["end"]) == ("2026-11-20T17:00:00", "2026-11-20T17:30:00")
    assert check("납품 회의 11월 20일 오후 3시 맞습니다")["status"] == "일치"
    assert check("거래처 방문 11월 25일 오전 10시")["status"] == "캘린더에 없음"
    assert check("납품 회의 금요일쯤 할까 합니다")["reason"] == "확정되지 않은 표현"
    assert check("납품 회의 오후 5시로 옮겨도 될까요?")["reason"] == "확정되지 않은 표현"
    assert check("납품 회의 5시로 옮겼습니다")["reason"] == "오전/오후를 알 수 없음"
    assert check("납품 건 공유합니다", [event])["reason"] == "날짜나 시간을 찾지 못함"
    assert check("거래처 방문 오후 3시")["status"] == "확인 필요"
    assert check("납품 현장 오후 5시로 변경")["reason"] == "비슷한 일정이 여러 개"
    repeating = {**event, "recurringEventId": "r"}
    assert check("납품 회의 오후 5시로 변경", [repeating])["reason"] == "반복 일정"
    all_day = {"id": "e3", "summary": "납품 회의", "start": {"date": "2026-11-20"}, "end": {"date": "2026-11-21"}}
    assert check("납품 회의 오후 5시로 변경", [all_day])["reason"] == "종일 일정"
    assert propose({**base, "date": "", "text": "납품 회의 오후 5시"}, [event])["reason"] == "메시지 날짜를 알 수 없음"

    assert len(checked_messages(sample)) == 4
    for not_chat in ("", "다른 앱에서 복사된 웹페이지 글\n\n둘째 줄"):
        try:
            checked_messages(not_chat)
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-chat text must be rejected")
    rooms = [{"title": "개발팀"}, {"title": "개발팀 공지"}, {"title": "가족"}]
    assert find_target(rooms, "개발팀", False)["title"] == "개발팀"
    assert find_target(rooms, "가", False)["title"] == "가족"
    for name, exact in (("없는방", False), ("개발", False), ("개발", True)):
        try:
            find_target(rooms, name, exact)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"{name!r} must not resolve to one room")
    print("self-check ok")


def main():
    parser = argparse.ArgumentParser(description="열어 둔 카카오톡 방에서 답장이 필요한 메시지를 고릅니다. 전송 기능은 없습니다.")
    parser.add_argument("rooms", nargs="*", help="독립 창으로 열어 둔 방 이름")
    parser.add_argument("--all-open", action="store_true", help="열린 방 전체를 읽음")
    parser.add_argument("--me", default="", help="내 대화 이름. 내 마지막 메시지 이후만 판단")
    parser.add_argument("--threshold", type=float, default=0.5, help="답장 필요로 볼 확률 기준")
    parser.add_argument("--limit", type=int, default=30, help="방마다 판단할 최근 메시지 수")
    parser.add_argument(
        "--category",
        choices=sorted([*QUESTIONS["category"]["criteria"], ALL_CATEGORIES]),
        help="이 분류의 메시지만 출력. 답장 기준은 무시",
    )
    parser.add_argument("--scroll", type=int, default=0, help="읽기 전에 위로 스크롤할 횟수. 과거 대화를 불러옴")
    parser.add_argument("--events", help="일정 메시지를 비교할 캘린더 일정 JSON 파일. 수정은 하지 않음")
    parser.add_argument("--dry-run", action="store_true", help="외부 전송 없이 읽기와 파싱만 확인")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    parser.add_argument("--self-check", action="store_true", help="네트워크와 카카오톡 없이 로직 점검")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.self_check:
        return self_check()
    if bool(args.rooms) == args.all_open:
        parser.error("방 이름 또는 --all-open 중 하나를 지정하세요.")
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not args.dry_run and not key:
        parser.error("TYPESAFE_API_KEY 환경 변수가 없습니다.")
    events = load_events(args.events) if args.events else None
    if events is not None:
        args.category = "일정"

    open_rooms = run_kakaocli("rooms")
    targets = [(r["title"], True) for r in open_rooms] if args.all_open else [(r, False) for r in args.rooms]
    rooms, rows, skipped = [], [], []
    previous = front_window()
    try:
        for name, exact in targets:
            try:
                room = find_target(open_rooms, name, exact)
                messages = read_room(room, args.scroll)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                skipped.append((name, str(error)))
                continue
            title = room["title"]
            indexes = pending(messages, args.me)[-args.limit :]
            rooms.append((title, len(messages), len(indexes)))
            if not args.dry_run:
                rows += judge(title, messages, indexes, key)
    finally:
        clear_clipboard()
        if previous:
            bring_to_front(previous)

    if args.dry_run:
        for room, total, checked in rooms:
            print(f"[{room}] 읽음 {total}개, 판단 대상 {checked}개 (dry-run: 외부 전송 없음)")
        for room, reason in skipped:
            print(f"[{room}] 읽기 실패: {reason}")
        return

    flagged, failed = bucket(rows, args.threshold, args.category or "")
    proposals = None if events is None else [propose(row, events) for row in flagged]
    if args.json:
        output = {"flagged": flagged, "failed": failed, "skipped": skipped}
        if proposals is not None:
            output["proposals"] = proposals
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        report(rooms, flagged, failed, skipped, args.threshold, args.category or "")
        if proposals is not None:
            print_proposals(proposals)


if __name__ == "__main__":
    main()
