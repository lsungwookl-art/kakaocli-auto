"""KakaoTalk triage: read open rooms with kakaocli, sort messages with Jev, and prepare items for review.

The only send path is --report-room, which needs a person to answer y. Message text goes to
the TypeSafe API unless --dry-run is set. Settings and tracking state stay outside the repository.

    python kakao_triage.py "방이름" --me "내 이름"
    python kakao_triage.py --all-open --me "내 이름"
    python kakao_triage.py "방이름" --dry-run
    python kakao_triage.py --briefing --track
    python kakao_triage.py --self-check
"""
import argparse
import concurrent.futures
import copy
import ctypes
import dataclasses
import datetime
import decimal
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

KAKAOCLI = Path(os.environ.get("KAKAOCLI", Path(__file__).resolve().parent / ".venv/Scripts/kakaocli.exe"))
API_URL = "https://api.typesafe.ai/v1/systemone"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DATA_DIR = Path(os.environ.get("KAKAO_TRIAGE_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "kakaocli-auto")
CONFIG_DEFAULTS = {
    "rooms": [],
    "me": "",
    "threshold": 0.5,
    "limit": 30,
    "days": 1,
    "scroll": 0,
    "overdue_hours": 24,
    "report_room": "",
    "rules": [],
    "aliases": {},
}
ARGUMENT_SETTINGS = ("me", "threshold", "limit", "days", "scroll", "overdue_hours", "report_room")
TRACKED_CATEGORIES = ("문제", "결정", "요청", "질문")
SEEN_PER_ROOM = 5000
REPORT_LIMIT = 900
EVALUATION_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7)
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

INVOICE_QUESTION = {
    "type": "noul",
    "instructions": "state의 target 메시지는 세금계산서를 발행하거나 수정해 달라는 요청인가? 담당자를 묻거나 발행 완료를 알리는 말은 해당하지 않는다.",
}
INVOICE_FOLLOWING = 3
MONEY_UNITS = {"억": 10**8, "천만": 10**7, "백만": 10**6, "십만": 10**5, "만": 10**4, "천": 10**3}
MONEY = r"\d[\d,]*(?:\.\d+)?\s*(?:억|천만|백만|십만|만|천)?(?:\s*\d[\d,]*\s*(?:천만|백만|십만|만|천))*"
MONEY_LABELS = {
    "supply": r"공급가액|공급가|공급금액",
    "tax": r"세액|부가세|부가가치세|VAT",
    "total": r"합계금액|합계|총액|총 금액|총금액|총",
}
COMPANY = (
    r"(?:\(주\)|㈜|주식회사\s*)[A-Za-z0-9가-힣]+"
    r"|[A-Za-z0-9가-힣]+(?:\(주\)|㈜|주식회사)"
    r"|[A-Za-z0-9가-힣]*(?:상사|전자|건설|산업|기업|물산|테크|유통|상회|컴퍼니)(?![가-힣])"
)
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
        [str(KAKAOCLI), "--json", *args], capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=120
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


def ask_jev(state, key, questions=QUESTIONS):
    body = json.dumps({"model": "jev-latest", "state": state, "questions": questions}).encode("utf-8")
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


def following_from_sender(messages, index):
    """Details often arrive in the sender's next few messages, so keep them with the request."""
    following = []
    for message in messages[index + 1 : index + 1 + INVOICE_FOLLOWING]:
        if message.sender != messages[index].sender:
            break
        following.append(message.text)
    return following


def judge(room, messages, indexes, key, invoices=False, rules=()):
    questions = with_rules({**QUESTIONS, "invoice": INVOICE_QUESTION} if invoices else QUESTIONS, rules)

    def one(index):
        message = messages[index]
        row = {"room": room, "date": message.date, "time": message.time, "sender": message.sender, "text": message.text}
        try:
            state = build_state(room, messages, index)
            row["context"] = state["recent_messages"]
            answers = ask_jev(state, key, questions)["answers"]
            row["needs_reply"] = answers["needs_reply"]["noul"]
            row["category"] = answers["category"]["choice"]
            if invoices:
                row["invoice"] = answers["invoice"]["noul"]
                row["following"] = following_from_sender(messages, index)
        except (RuntimeError, KeyError, TypeError, ValueError) as error:
            row["error"] = str(error) or type(error).__name__
        return row

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(one, indexes))


def valid_business_number(number):
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) != 10:
        return False
    weights = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    total = sum(d * w for d, w in zip(digits, weights)) + digits[8] * 5 // 10
    return (10 - total % 10) % 10 == digits[9]


def parse_money(text):
    """Convert '1,100,000', '110만', '1억 2천만' to won. Returns None for anything it cannot read exactly."""
    total, last_end = 0, 0
    found = False
    for match in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(억|천만|백만|십만|만|천)?", text):
        if text[last_end : match.start()].strip():
            return None
        value = decimal.Decimal(match[1].replace(",", "")) * MONEY_UNITS.get(match[2], 1)
        if value != int(value):
            return None
        total, last_end, found = total + int(value), match.end(), True
    return total if found and not text[last_end:].strip() else None


def extract_invoice(text, today):
    """Pull invoice fields from a message by rule. A value it cannot read stays empty and is listed as missing."""
    # ponytail: rules cover common phrasing only. A missing value is reported, never guessed.
    fields = {"company": None, "representative": None, "business_number": None, "item": None, "email": None}
    warnings = []

    numbers = sorted(set(re.findall(r"(?<!\d)\d{3}-?\d{2}-?\d{5}(?!\d)", text)))
    if len(numbers) > 1:
        warnings.append("사업자번호가 여러 개")
    elif numbers:
        fields["business_number"] = numbers[0]
        if not valid_business_number(numbers[0]):
            warnings.append("사업자번호 검증 숫자가 맞지 않음")

    company = re.search(COMPANY, text)
    fields["company"] = company[0].strip() if company else None
    person = re.search(r"대표(?:자|자명)?\s*[:：]?\s*([가-힣]{2,4})(?:님|씨)?(?![가-힣])", text)
    fields["representative"] = person[1] if person else None
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    fields["email"] = email[0] if email else None
    item = re.search(r"(?:품목|품명|내역)\s*[:：은는]?\s*([^\n,.]+)", text) or re.search(
        r"(\d{1,2}월분?\s*[가-힣]+|[가-힣]+)\s*(?:건|대금|비용)(?![가-힣])", text
    )
    fields["item"] = item[1].strip() if item else None

    basis_match = re.search(r"(?:부가세|부가가치세|VAT)\s*(별도|포함)", text, re.IGNORECASE)
    basis = basis_match[1] if basis_match else None
    body = re.sub(r"(?:부가세|부가가치세|VAT)\s*(?:별도|포함)", " ", text, flags=re.IGNORECASE)

    labeled = {}
    for label, pattern in MONEY_LABELS.items():
        match = re.search(rf"(?:{pattern})\s*(?:은|는|이|가|:|：)?\s*({MONEY})\s*원?", body)
        if match:
            labeled[label] = parse_money(match[1])
            body = body.replace(match[0], " ")
    loose = [parse_money(m) for m in re.findall(rf"({MONEY})\s*원", body)]

    supply, tax, total = labeled.get("supply"), labeled.get("tax"), labeled.get("total")
    if not labeled and len(loose) > 1:
        warnings.append("금액이 여러 개")
    elif not labeled and len(loose) == 1:
        if basis == "별도":
            supply = loose[0]
        elif basis == "포함":
            total = loose[0]
        else:
            total = None
            warnings.append(f"부가세 포함 여부를 알 수 없음 (금액 {loose[0]:,}원)")
    if total is not None and supply is None:
        supply = round(total / 1.1) if tax is None else total - tax
    if supply is not None and tax is None:
        tax = supply // 10 if total is None else total - supply
    if supply is not None and tax is not None:
        if total is not None and supply + tax != total:
            warnings.append("공급가액과 세액의 합이 합계와 다름")
        if abs(tax - supply // 10) > 1:
            warnings.append("세액이 공급가액의 10%와 다름 (영세율, 면세 등 확인)")
        total = supply + tax if total is None else total

    date = parse_when(text, today)[0] if today else None
    draft = {**fields, "supply": supply, "tax": tax, "total": total, "date": date.isoformat() if date else None}
    required = {"company": "상호", "business_number": "사업자번호", "supply": "공급가액", "date": "작성일"}
    missing = [name for key, name in required.items() if draft[key] is None]
    return draft, missing, warnings


def draft_invoice(row):
    """Build a review draft from one message. Issuing is deliberately not implemented."""
    text = "\n".join([row["text"], *row.get("following", [])])
    draft, missing, warnings = extract_invoice(apply_aliases(text, row.get("aliases")), parse_message_date(row["date"]))
    return {
        "status": "확인 필요" if missing or warnings else "초안",
        "room": row["room"],
        "sender": row["sender"],
        "sent": f"{row['date']} {row['time']}",
        "text": text,
        **draft,
        "missing": missing,
        "warnings": warnings,
    }


def print_invoices(drafts):
    print(f"\n세금계산서 초안 {len(drafts)}건 (발행하지 않음. 값을 확인한 뒤 직접 발행하세요)")
    won = lambda value: f"{value:,}원" if value is not None else "-"
    for item in drafts:
        print(f"  [{item['status']}] [{item['room']}] {item['sent']} {item['sender']}: {' '.join(item['text'].split())[:60]}")
        print(f"      상호 {item['company'] or '-'} | 대표 {item['representative'] or '-'} | 사업자번호 {item['business_number'] or '-'}")
        print(f"      공급가액 {won(item['supply'])} | 세액 {won(item['tax'])} | 합계 {won(item['total'])}")
        print(f"      작성일 {item['date'] or '-'} | 품목 {item['item'] or '-'} | 이메일 {item['email'] or '-'}")
        if item["missing"]:
            print(f"      빠진 항목: {', '.join(item['missing'])}")
        for warning in item["warnings"]:
            print(f"      주의: {warning}")


def load_config(path, required=False):
    """Read settings from JSON. Unknown keys are an error, so a typo cannot silently drop a rule."""
    config = copy.deepcopy(CONFIG_DEFAULTS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"설정 파일이 없습니다: {path}") from None
        return config
    except ValueError as error:
        raise RuntimeError(f"설정 파일을 읽지 못했습니다: {error}") from None
    unknown = set(data) - set(config)
    if unknown:
        raise RuntimeError(f"알 수 없는 설정: {', '.join(sorted(unknown))}")
    for key, value in data.items():
        if type(value) is not type(config[key]) and not (key == "threshold" and isinstance(value, int)):
            raise RuntimeError(f"설정 {key}의 형식이 잘못됐습니다.")
    return {**config, **data}


def with_rules(questions, rules):
    """Add company rules to every Jev question."""
    if not rules:
        return questions
    updated = copy.deepcopy(questions)
    guide = "\n회사 기준:\n" + "\n".join(f"- {rule}" for rule in rules)
    for question in updated.values():
        question["instructions"] += guide
    return updated


def apply_aliases(text, aliases):
    """Replace nicknames with official names, longest first, for matching only. Shown text stays original."""
    if not aliases:
        return text
    # One pass, so a replacement is never replaced again by a shorter alias.
    pattern = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    return re.sub(pattern, lambda match: aliases[match[0]], text)


def message_keys(room, messages):
    """Hash each message, so the state file records what was seen without keeping its text."""
    counts, keys = {}, []
    for message in messages:
        raw = "\0".join([room, message.date, message.time, message.sender, message.text])
        counts[raw] = counts.get(raw, 0) + 1
        keys.append(hashlib.sha256(f"{raw}\0{counts[raw]}".encode()).hexdigest()[:20])
    return keys


def message_datetime(date_header, clock):
    date = parse_message_date(date_header)
    match = re.match(r"(오전|오후) (\d{1,2}):(\d{2})$", clock or "")
    if not date or not match:
        return None
    hour = int(match[2]) % 12 + (12 if match[1] == "오후" else 0)
    return datetime.datetime.combine(date, datetime.time(hour, int(match[3])))


def load_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"seen": {}, "open": []}
    except ValueError as error:
        raise RuntimeError(f"추적 기록을 읽지 못했습니다. 파일을 확인하세요: {path} ({error})") from None
    return {"seen": state.get("seen", {}), "open": state.get("open", [])}


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def close_answered(state, room, messages, me):
    """Close open items in this room older than my latest message. Returns how many closed."""
    if not me:
        return 0
    sent = [message_datetime(m.date, m.time) for m in messages if m.sender == me]
    sent = [value for value in sent if value]
    if not sent:
        return 0
    latest = max(sent).isoformat()
    kept = [i for i in state["open"] if i["room"] != room or i["sent"] > latest]
    closed = len(state["open"]) - len(kept)
    state["open"] = kept
    return closed


def record_judged(state, room, keys, rows, threshold):
    """Mark judged messages as seen and open items that need action. Failed messages stay unseen for a retry."""
    seen = state["seen"].setdefault(room, [])
    for key, row in zip(keys, rows):
        if "error" in row:
            continue
        seen.append(key)
        needs_action = row.get("invoice", 0) >= threshold or (
            row["category"] in TRACKED_CATEGORIES and row["needs_reply"] >= threshold
        )
        sent = message_datetime(row["date"], row["time"])
        if needs_action and sent and not any(i["id"] == key for i in state["open"]):
            excerpt = " ".join(row["text"].split())[:80]
            state["open"].append(
                {"id": key, "room": room, "sender": row["sender"], "sent": sent.isoformat(),
                 "category": row["category"], "text": excerpt}
            )
    state["seen"][room] = seen[-SEEN_PER_ROOM:]


def overdue_items(state, now, hours):
    limit = now - datetime.timedelta(hours=hours)
    return sorted((i for i in state["open"] if datetime.datetime.fromisoformat(i["sent"]) <= limit), key=lambda i: i["sent"])


def print_open_items(state, now, hours, me):
    items = overdue_items(state, now, hours)
    print(f"\n## 미처리 {len(state['open'])}건, 그중 {hours}시간 넘은 항목 {len(items)}건")
    for item in items:
        print(f"  - {item['id']} [{item['room']}] {item['sent']} {item['sender']}: {item['text']}")
    if state["open"] and not me:
        print("  주의: --me가 없어 내가 답해도 자동으로 닫히지 않습니다. 처리한 항목은 --done ID로 닫으세요.")


def fetch_calendar_events(token, start, end, url=CALENDAR_URL):
    """Read events from the primary Google Calendar. This only sends GET requests."""
    events, page = [], None
    while True:
        query = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime"}
        if page:
            query["pageToken"] = page
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(query)}", headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            hint = " 토큰이 만료됐는지 확인하세요." if error.code == 401 else ""
            raise RuntimeError(f"캘린더 조회 실패: HTTP {error.code}.{hint}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"캘린더 조회 실패: {type(error).__name__}") from None
        events += data.get("items", [])
        page = data.get("nextPageToken")
        if not page:
            return events


def score_labels(results):
    """Compare judgments with labels. Each result needs labels and answers; unlabeled fields are skipped."""
    summary = {"total": len(results), "category": {}, "needs_reply": {}, "invoice": None}
    labeled = [r for r in results if r.get("category")]
    if labeled:
        summary["category_accuracy"] = sum(r["category"] == r["got_category"] for r in labeled) / len(labeled)
        for name in sorted({r["category"] for r in labeled} | {r["got_category"] for r in labeled}):
            hits = sum(r["category"] == name == r["got_category"] for r in labeled)
            actual = sum(r["category"] == name for r in labeled)
            chosen = sum(r["got_category"] == name for r in labeled)
            summary["category"][name] = {
                "actual": actual,
                "recall": hits / actual if actual else None,
                "precision": hits / chosen if chosen else None,
            }

    def sweep(field, score):
        rows = [r for r in results if isinstance(r.get(field), bool)]
        table = {}
        for threshold in EVALUATION_THRESHOLDS:
            hits = sum(r[field] and r[score] >= threshold for r in rows)
            actual = sum(r[field] for r in rows)
            chosen = sum(r[score] >= threshold for r in rows)
            table[threshold] = {
                "missed": actual - hits,
                "false_alarm": chosen - hits,
                "recall": hits / actual if actual else None,
                "precision": hits / chosen if chosen else None,
            }
        return table if rows else None

    summary["needs_reply"] = sweep("needs_reply", "got_needs_reply")
    summary["invoice"] = sweep("invoice", "got_invoice")
    return summary


def evaluate_file(path, key, rules):
    """Judge labeled messages and score them. Rows are invented or chosen by the person running this."""
    labels = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    questions = with_rules({**QUESTIONS, "invoice": INVOICE_QUESTION}, rules)

    def one(item):
        state = {
            "room": item.get("room", "평가"),
            "recent_messages": item.get("context", []),
            "target": {"sender": item.get("sender", ""), "time": item.get("time", ""), "text": item["text"]},
        }
        answers = ask_jev(state, key, questions)["answers"]
        return {
            **item,
            "got_category": answers["category"]["choice"],
            "got_needs_reply": answers["needs_reply"]["noul"],
            "got_invoice": answers["invoice"]["noul"],
        }

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(one, labels))


def print_evaluation(results):
    percent = lambda value: "-" if value is None else f"{value * 100:.0f}%"
    summary = score_labels(results)
    print(f"평가 메시지 {summary['total']}개 (적은 표본이면 숫자를 믿기 어렵습니다)")
    if "category_accuracy" in summary:
        print(f"\n분류 정확도 {percent(summary['category_accuracy'])}")
        for name, row in summary["category"].items():
            print(f"  {name}: 실제 {row['actual']}개, 찾은 비율 {percent(row['recall'])}, 맞은 비율 {percent(row['precision'])}")
    for label, table in (("답장 필요", summary["needs_reply"]), ("세금계산서 요청", summary["invoice"])):
        if table:
            print(f"\n{label} 기준값별 결과")
            for threshold, row in table.items():
                print(f"  {threshold}: 놓침 {row['missed']}개, 잘못 잡음 {row['false_alarm']}개, 찾은 비율 {percent(row['recall'])}")
    wrong = [r for r in results if r.get("category") and r["category"] != r["got_category"]]
    if wrong:
        print("\n분류가 다른 메시지")
        for row in wrong:
            print(f"  기대 {row['category']} / 판단 {row['got_category']}: {' '.join(row['text'].split())[:60]}")


def reply_queue(rows, threshold):
    """List messages needing a reply, with context, for drafting. Drafts stay empty until a person or Claude fills them."""
    return [
        {
            "room": r["room"],
            "sender": r["sender"],
            "sent": f"{r['date']} {r['time']}",
            "category": r["category"],
            "needs_reply": r["needs_reply"],
            "context": r.get("context", []),
            "text": r["text"],
            "draft": "",
            "approved": False,
        }
        for r in rows
        if "error" not in r and r["needs_reply"] >= threshold
    ]


def write_new_json(path, data):
    """Refuse to overwrite: these files can hold edited drafts or labels."""
    try:
        with open(path, "x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
    except FileExistsError:
        raise RuntimeError(f"이미 있는 파일입니다: {path}") from None


def format_report(briefing, overdue, title):
    """Build a one-line report. Line breaks are avoided because typed input could send them as Enter."""
    sections = briefing["sections"]
    parts = [f"{name} {len(items)}" for name, items in sections.items() if items]
    if briefing["invoices"]:
        parts.append(f"세금계산서 {len(briefing['invoices'])}")
    if overdue:
        parts.append(f"밀림 {len(overdue)}")
    if briefing["failed"]:
        parts.append(f"판단실패 {len(briefing['failed'])}")
    text = f"[{title}] " + (", ".join(parts) or "확인할 항목 없음")
    for name, items in sections.items():
        for row in items:
            excerpt = " ".join(row["text"].split())[:40]
            text += f" / {name}: {row['room']} {row['sender']} {excerpt}"
    if len(text) > REPORT_LIMIT:
        text = text[: REPORT_LIMIT - 1] + "…"
    return text


def send_report(room, text):
    """Send through kakaocli without --yes, so the person sees the text and must answer y."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    sys.stdout.flush()
    return subprocess.run([str(KAKAOCLI), "send", room, text, "--exact"], env=env).returncode


def recent_indexes(messages, indexes, today, days):
    """Keep messages from the last `days` days. Returns kept indexes and how many had no readable date."""
    cutoff = today - datetime.timedelta(days=days - 1)
    kept, undated = [], 0
    for index in indexes:
        date = parse_message_date(messages[index].date)
        if date is None:
            undated += 1
        elif date >= cutoff:
            kept.append(index)
    return kept, undated


def coverage_notes(messages, today, days, recent, limit, undated):
    """Say what the briefing may have missed, so a short list is not mistaken for a quiet day."""
    notes = []
    oldest = parse_message_date(messages[0].date) if messages else None
    if oldest and oldest >= today - datetime.timedelta(days=days - 1):
        notes.append("읽은 범위가 기간 시작 전까지 닿지 않았을 수 있음. --scroll로 늘려 확인")
    if undated:
        notes.append(f"날짜를 알 수 없는 메시지 {undated}개 제외")
    if recent > limit:
        notes.append(f"메시지 {recent}개 중 최근 {limit}개만 판단. --limit으로 늘려 확인")
    return notes


def build_briefing(rows, threshold, proposals=None):
    """Sort judged messages into action sections. Failed messages stay out of the counts and are listed apart."""
    judged = [r for r in rows if "error" not in r]
    invoices = [draft_invoice(r) for r in judged if r.get("invoice", 0) >= threshold]
    invoice_rows = {id(r) for r in judged if r.get("invoice", 0) >= threshold}
    section = lambda name, needs_reply=False: [
        r
        for r in judged
        if r["category"] == name and id(r) not in invoice_rows and (not needs_reply or r["needs_reply"] >= threshold)
    ]
    sections = {
        "문제": section("문제"),
        "결정": section("결정"),
        "요청": section("요청", needs_reply=True),
        "질문": section("질문", needs_reply=True),
        "일정": section("일정"),
    }
    shown = {id(r) for items in sections.values() for r in items} | invoice_rows
    return {
        "sections": sections,
        "invoices": invoices,
        "proposals": proposals,
        "other": sum(id(r) not in shown for r in judged),
        "failed": [r for r in rows if "error" in r],
    }


def print_briefing(briefing, rooms, skipped, title):
    print(f"# {title}")
    for room, total, checked, notes in rooms:
        print(f"[{room}] 읽음 {total}개, 판단 {checked}개")
        for note in notes:
            print(f"  주의: {note}")
    for room, reason in skipped:
        print(f"[{room}] 읽기 실패, 브리핑에 포함되지 않음: {reason}")
    count = sum(len(items) for items in briefing["sections"].values()) + len(briefing["invoices"])
    print(f"\n확인할 항목 {count}개, 그 밖의 메시지 {briefing['other']}개")
    for name, items in briefing["sections"].items():
        if items:
            print(f"\n## {name} {len(items)}건")
        for row in items:
            print(f"  - [{row['room']}] {row['time']} {row['sender']}: {' '.join(row['text'].split())[:80]}")
    if briefing["proposals"]:
        print_proposals(briefing["proposals"])
    if briefing["invoices"]:
        print_invoices(briefing["invoices"])
    if briefing["failed"]:
        print(f"\n## 판단 실패 {len(briefing['failed'])}건: 직접 확인하세요")
        for row in briefing["failed"]:
            print(f"  - [{row['room']}] {row['time']} {row['sender']} ({row['error']})")


def date_queries(date):
    """Spellings of a date to search for. Relative words like 'next Friday' cannot be searched this way."""
    month, day = date.month, date.day
    return [f"{month}월 {day}일", f"{month}월{day}일", f"{month}/{day}", f"{month}.{day}", f"{day}일"]


def search_date(date):
    """Run integrated search for each spelling. Images can hold private text, so they stay in a temp folder."""
    folder = Path(tempfile.mkdtemp(prefix="kakao-search-"))
    results = []
    for index, query in enumerate(date_queries(date)):
        image = folder / f"{index}.png"
        try:
            found = run_kakaocli("search", query, "--output", str(image))
            results.append({"query": query, "images": found["images"], "complete": found["complete"]})
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            results.append({"query": query, "error": str(error)})
    return results


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
    text = apply_aliases(row["text"], row.get("aliases"))
    if not today:
        return needs_check("메시지 날짜를 알 수 없음")
    if TENTATIVE.search(text):
        return needs_check("확정되지 않은 표현")
    date, clock, ambiguous = parse_when(text, today)
    if ambiguous:
        return needs_check("오전/오후를 알 수 없음")
    if not date and not clock:
        return needs_check("날짜나 시간을 찾지 못함")

    event, tied = find_event(text, events)
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
    for room, total, checked, _notes in rooms:
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

    assert date_queries(datetime.date(2026, 3, 5)) == ["3월 5일", "3월5일", "3/5", "3.5", "5일"]

    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        assert load_config(folder / "none.json") == CONFIG_DEFAULTS
        (folder / "c.json").write_text('{"me": "나", "rules": ["납기 변경은 문제"], "threshold": 1}', encoding="utf-8")
        loaded = load_config(folder / "c.json", required=True)
        assert loaded["me"] == "나" and loaded["rules"] == ["납기 변경은 문제"] and loaded["limit"] == 30
        for bad in ('{"typo": 1}', '{"rooms": "방"}', "{"):
            (folder / "c.json").write_text(bad, encoding="utf-8")
            try:
                load_config(folder / "c.json")
            except RuntimeError:
                pass
            else:
                raise AssertionError(f"bad config accepted: {bad}")
        try:
            load_config(folder / "none.json", required=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("an explicit missing config must fail")
        state_file = folder / "sub" / "state.json"
        assert load_state(state_file) == {"seen": {}, "open": []}
        save_state(state_file, {"seen": {"방": ["a"]}, "open": []})
        assert load_state(state_file)["seen"] == {"방": ["a"]}
        state_file.write_text("{", encoding="utf-8")
        try:
            load_state(state_file)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a corrupt state must not be treated as empty")
        write_new_json(folder / "q.json", [])
        try:
            write_new_json(folder / "q.json", [1])
        except RuntimeError:
            pass
        else:
            raise AssertionError("existing files must not be overwritten")
        assert json.loads((folder / "q.json").read_text(encoding="utf-8")) == []

    ruled = with_rules(QUESTIONS, ["납기 변경은 문제로 본다"])
    assert "납기 변경은" in ruled["category"]["instructions"] and "회사 기준" not in QUESTIONS["category"]["instructions"]
    assert with_rules(QUESTIONS, []) is QUESTIONS
    assert apply_aliases("A사 대금", {"A": "X", "A사": "A상사"}) == "A상사 대금" and apply_aliases("글", None) == "글"
    alias_row = {"room": "방", "sender": "김", "date": "2026년 11월 7일 토요일", "time": "오전 9:00",
                 "text": "납회 오후 5시로 변경", "aliases": {"납회": "납품 회의"}}
    alias_event = {"id": "e", "summary": "납품 회의", "start": {"dateTime": "2026-11-20T15:00:00+09:00"},
                   "end": {"dateTime": "2026-11-20T15:30:00+09:00"}}
    alias_result = propose(alias_row, [alias_event])
    assert alias_result["status"] == "수정안" and alias_result["text"] == "납회 오후 5시로 변경"

    pair = parse("2026년 11월 7일 토요일\n[김] [오전 9:00] 네\n[김] [오전 9:00] 네\n[김] [오후 12:30] 다른 말")
    pair_keys = message_keys("방", pair)
    assert len(set(pair_keys)) == 3 and pair_keys != message_keys("다른방", pair) and pair_keys == message_keys("방", pair)
    assert "네" not in "".join(pair_keys)
    assert message_datetime(pair[2].date, pair[2].time) == datetime.datetime(2026, 11, 7, 12, 30)
    assert message_datetime("2026년 11월 7일", "오전 12:05") == datetime.datetime(2026, 11, 7, 0, 5)
    assert message_datetime("", "오전 9:00") is None

    track = {"seen": {}, "open": []}
    base_row = {"date": "2026년 11월 7일 토요일", "time": "오전 9:00", "sender": "김", "room": "방"}
    tracked_rows = [
        {**base_row, "text": "승인?", "category": "결정", "needs_reply": 0.9},
        {**base_row, "text": "안녕", "category": "잡담", "needs_reply": 0.9},
        {**base_row, "text": "참고", "category": "요청", "needs_reply": 0.1},
        {**base_row, "text": "실패", "error": "HTTP 500"},
    ]
    record_judged(track, "방", ["k1", "k2", "k3", "k4"], tracked_rows, 0.5)
    assert track["seen"]["방"] == ["k1", "k2", "k3"], "a failed message must be retried"
    assert [i["id"] for i in track["open"]] == ["k1"]
    record_judged(track, "방", ["k1"], tracked_rows[:1], 0.5)
    assert len(track["open"]) == 1
    assert overdue_items(track, datetime.datetime(2026, 11, 7, 10), 24) == []
    assert len(overdue_items(track, datetime.datetime(2026, 11, 8, 10), 24)) == 1
    other_room = {"seen": {}, "open": [dict(track["open"][0], room="다른방"), *track["open"]]}
    answered = parse("2026년 11월 7일 토요일\n[나] [오전 9:30] 네 승인합니다")
    assert close_answered(other_room, "방", answered, "") == 0
    assert close_answered(other_room, "방", answered, "모름") == 0
    assert close_answered(other_room, "방", answered, "나") == 1 and other_room["open"][0]["room"] == "다른방"

    results = [
        {"text": "a", "category": "문제", "got_category": "문제", "needs_reply": True, "got_needs_reply": 0.9,
         "invoice": False, "got_invoice": 0.1},
        {"text": "b", "category": "문제", "got_category": "요청", "needs_reply": True, "got_needs_reply": 0.4,
         "invoice": False, "got_invoice": 0.1},
        {"text": "c", "category": "잡담", "got_category": "잡담", "needs_reply": False, "got_needs_reply": 0.6,
         "invoice": True, "got_invoice": 0.8},
        {"text": "d", "category": None, "got_category": "잡담", "needs_reply": None, "got_needs_reply": 0.9,
         "invoice": None, "got_invoice": 0.9},
    ]
    scored = score_labels(results)
    assert scored["category_accuracy"] == 2 / 3 and scored["category"]["문제"]["recall"] == 0.5
    assert scored["needs_reply"][0.5] == {"missed": 1, "false_alarm": 1, "recall": 0.5, "precision": 0.5}
    assert scored["needs_reply"][0.3]["missed"] == 0 and scored["invoice"][0.5]["recall"] == 1.0
    assert score_labels([{"text": "x", "got_category": "잡담"}])["needs_reply"] is None

    queue_rows = [
        {**base_row, "text": "보내 주세요", "category": "요청", "needs_reply": 0.9, "context": [{"sender": "최", "text": "앞말"}]},
        {**base_row, "text": "안녕", "category": "잡담", "needs_reply": 0.1},
        {**base_row, "text": "실패", "error": "HTTP 500"},
    ]
    queued = reply_queue(queue_rows, 0.5)
    assert len(queued) == 1 and queued[0]["draft"] == "" and queued[0]["approved"] is False
    assert queued[0]["context"][0]["text"] == "앞말"

    report_brief = {
        "sections": {"문제": [{**base_row, "text": "장애\n둘째 줄"}], "결정": [], "요청": [], "질문": [], "일정": []},
        "invoices": [{}], "failed": [],
    }
    report_text = format_report(report_brief, [{"id": "o"}], "제목")
    assert "\n" not in report_text and "\r" not in report_text and "문제 1" in report_text and "밀림 1" in report_text
    assert "장애 둘째 줄" in report_text
    empty_brief = {"sections": {"문제": []}, "invoices": [], "failed": []}
    assert "확인할 항목 없음" in format_report(empty_brief, [], "제목")
    long_brief = {"sections": {"문제": [{**base_row, "text": "길다 " * 30}] * 60}, "invoices": [], "failed": []}
    assert len(format_report(long_brief, [], "제목")) == REPORT_LIMIT

    import http.server
    import threading

    pages = {None: {"items": [{"id": "1"}], "nextPageToken": "p2"}, "p2": {"items": [{"id": "2"}]}}
    seen_requests = []

    class Calendar(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            seen_requests.append((self.command, self.headers["Authorization"], query))
            if self.headers["Authorization"] != "Bearer good":
                self.send_error(401)
                return
            body = json.dumps(pages[query.get("pageToken", [None])[0]]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Calendar)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake_url = f"http://127.0.0.1:{server.server_port}/events"
    span = datetime.datetime(2026, 11, 7, tzinfo=datetime.timezone.utc)
    try:
        fetched = fetch_calendar_events("good", span, span + datetime.timedelta(days=1), fake_url)
        assert [e["id"] for e in fetched] == ["1", "2"]
        assert all(r[0] == "GET" for r in seen_requests) and seen_requests[0][2]["singleEvents"] == ["true"]
        try:
            fetch_calendar_events("bad", span, span, fake_url)
        except RuntimeError as error:
            assert "토큰" in str(error)
        else:
            raise AssertionError("an expired token must fail")
    finally:
        server.shutdown()
        server.server_close()

    now = datetime.date(2026, 11, 7)
    dated = parse(
        "[없음] [오전 8:00] 날짜 줄 이전\n2026년 11월 5일 목요일\n[김] [오전 9:00] 그저께\n"
        "2026년 11월 6일 금요일\n[김] [오전 9:00] 어제\n2026년 11월 7일 토요일\n[김] [오전 9:00] 오늘"
    )
    every = list(range(len(dated)))
    assert recent_indexes(dated, every, now, 1) == ([3], 1)
    assert recent_indexes(dated, every, now, 2) == ([2, 3], 1)
    assert coverage_notes(dated[3:], now, 1, 1, 30, 0)[0].startswith("읽은 범위")
    assert coverage_notes(dated[1:], now, 1, 1, 30, 0) == []
    assert "최근 30개" in coverage_notes(dated[1:], now, 1, 40, 30, 0)[0]
    assert "1개 제외" in coverage_notes(dated[1:], now, 1, 1, 30, 1)[0]

    def judged(text, category, reply=0.1, invoice=0.0):
        return {
            "room": "방", "date": "2026년 11월 7일 토요일", "time": "오전 9:00", "sender": "김", "text": text,
            "category": category, "needs_reply": reply, "invoice": invoice,
        }

    brief = build_briefing(
        [
            judged("장애", "문제"),
            judged("승인?", "결정"),
            judged("보내 주세요", "요청", reply=0.9),
            judged("참고로 요청", "요청", reply=0.1),
            judged("몇 시?", "질문", reply=0.8),
            judged("수요일 이동", "일정"),
            judged("안녕", "잡담"),
            judged("공유", "정보"),
            judged("A상사 부가세 별도 100만원 계산서", "요청", reply=0.9, invoice=0.9),
            {"room": "방", "time": "오전 9:00", "sender": "김", "text": "실패", "error": "HTTP 500"},
        ],
        0.5,
    )
    assert {k: [r["text"] for r in v] for k, v in brief["sections"].items()} == {
        "문제": ["장애"], "결정": ["승인?"], "요청": ["보내 주세요"], "질문": ["몇 시?"], "일정": ["수요일 이동"],
    }
    assert len(brief["invoices"]) == 1 and brief["other"] == 3 and len(brief["failed"]) == 1

    assert valid_business_number("123-45-67891") and valid_business_number("1234567891")
    assert not valid_business_number("123-45-67890") and not valid_business_number("123-45-6789")
    for text, won in [
        ("1,100,000", 1100000), ("110만", 1100000), ("1억 2천만", 120000000), ("5천", 5000),
        ("1.5만", 15000), ("1.1만", 11000), ("3백만", 3000000), ("1.23456만", None), ("abc", None), ("", None),
    ]:
        assert parse_money(text) == won, text
    day_of = datetime.date(2026, 11, 7)
    fields, missing, warnings = extract_invoice(
        "A상사 대표 김철수 사업자번호 123-45-67891 품목: 3월 납품 공급가액 1,000,000원 11월 20일 발행 a@b.co.kr", day_of
    )
    assert (fields["supply"], fields["tax"], fields["total"]) == (1000000, 100000, 1100000)
    assert fields["company"] == "A상사" and fields["representative"] == "김철수"
    assert fields["date"] == "2026-11-20" and fields["email"] == "a@b.co.kr"
    assert "3월 납품" in fields["item"] and not missing and not warnings
    for text, supply, tax, total in [
        ("부가세 별도 100만원", 1000000, 100000, 1100000),
        ("부가세 포함 110만원", 1000000, 100000, 1100000),
        ("VAT 포함 33,000원", 30000, 3000, 33000),
        ("공급가액 100만원 세액 10만원 합계 110만원", 1000000, 100000, 1100000),
    ]:
        fields, _missing, warnings = extract_invoice(text, day_of)
        assert (fields["supply"], fields["tax"], fields["total"]) == (supply, tax, total), text
        assert not warnings, (text, warnings)
    for text, warning in [
        ("100만원 발행해 주세요", "부가세 포함 여부"),
        ("100만원이랑 200만원 발행", "금액이 여러 개"),
        ("공급가액 100만원 세액 5만원", "10%와 다름"),
        ("공급가액 100만원 세액 10만원 합계 120만원", "합이 합계와 다름"),
        ("123-45-67890 발행", "검증 숫자"),
        ("123-45-67891 와 220-81-62517", "여러 개"),
    ]:
        assert any(warning in w for w in extract_invoice(text, day_of)[2]), text
    fields, missing, _warnings = extract_invoice("세금계산서 발행 부탁드립니다", day_of)
    assert fields["supply"] is None and set(missing) == {"상호", "사업자번호", "공급가액", "작성일"}
    assert extract_invoice("11월 20일 오후 3시", None)[0]["date"] is None
    invoice_row = {
        "room": "방", "sender": "최", "date": "2026년 11월 7일 토요일", "time": "오전 9:00",
        "text": "A상사 11월 20일 부가세 별도 100만원 발행 부탁드립니다", "following": ["사업자번호 123-45-67891"],
    }
    good = draft_invoice(invoice_row)
    assert good["status"] == "초안" and good["business_number"] == "123-45-67891" and not good["missing"]
    assert draft_invoice({**invoice_row, "following": []})["status"] == "확인 필요"
    chat = parse("[최] [오전 9:00] 발행 부탁\n[최] [오전 9:01] 번호요\n[김] [오전 9:02] 네\n[최] [오전 9:03] 늦은 말")
    assert following_from_sender(chat, 0) == ["번호요"]

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
    parser = argparse.ArgumentParser(description="열어 둔 카카오톡 방의 메시지를 분류하고 확인할 항목을 정리합니다.")
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
    parser.add_argument("--invoices", action="store_true", help="세금계산서 요청을 찾아 검토용 초안 출력. 발행은 하지 않음")
    parser.add_argument("--briefing", action="store_true", help="최근 메시지를 문제, 결정, 요청, 질문, 일정, 세금계산서로 정리")
    parser.add_argument("--days", type=int, default=1, help="브리핑 기간. 1이면 오늘만")
    parser.add_argument("--config", help="설정 파일 경로. 기본은 사용자 폴더의 kakaocli-auto\\config.json")
    parser.add_argument("--init-config", action="store_true", help="설정 파일 틀을 만들고 경로를 출력")
    parser.add_argument("--track", action="store_true", help="이미 본 메시지를 건너뛰고 미처리 항목을 추적")
    parser.add_argument("--overdue-hours", type=float, default=24, help="미처리 항목을 밀린 것으로 볼 시간")
    parser.add_argument("--open-items", action="store_true", help="미처리 항목 목록만 출력. 카카오톡을 읽지 않음")
    parser.add_argument("--done", nargs="+", metavar="ID", help="처리한 미처리 항목 닫기")
    parser.add_argument("--evaluate", help="정답을 적은 JSON으로 정확도 측정. 메시지가 TypeSafe로 전송됨")
    parser.add_argument("--make-labels", help="읽은 메시지로 정답 입력용 JSON 만들기. 외부 전송 없음")
    parser.add_argument("--calendar", action="store_true", help="GOOGLE_CALENDAR_ACCESS_TOKEN으로 기본 캘린더를 읽어 일정 비교")
    parser.add_argument("--calendar-days", type=int, default=30, help="--calendar로 읽을 앞으로의 일수")
    parser.add_argument("--reply-queue", help="답장이 필요한 메시지를 초안 작성용 JSON으로 저장. 전송 없음")
    parser.add_argument("--report-room", default="", help="브리핑 요약을 보낼 열린 방의 정확한 제목. y 입력 후에만 전송")
    parser.add_argument(
        "--date",
        type=datetime.date.fromisoformat,
        help="YYYY-MM-DD 날짜를 표현 여러 개로 통합검색해 결과 이미지 경로를 출력. 외부 전송과 방 열기 없음",
    )
    parser.add_argument("--dry-run", action="store_true", help="외부 전송 없이 읽기와 파싱만 확인")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    parser.add_argument("--self-check", action="store_true", help="네트워크와 카카오톡 없이 로직 점검")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    early, _unknown = parser.parse_known_args()
    if early.self_check:
        return self_check()

    config_path = Path(early.config) if early.config else DATA_DIR / "config.json"
    if early.init_config:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_new_json(config_path, CONFIG_DEFAULTS)
        except RuntimeError as error:
            parser.error(str(error))
        print(config_path)
        return None
    try:
        config = load_config(config_path, required=bool(early.config))
    except RuntimeError as error:
        parser.error(str(error))
    parser.set_defaults(**{name: config[name] for name in ARGUMENT_SETTINGS})
    args = parser.parse_args()

    state_path = DATA_DIR / "state.json"
    now = datetime.datetime.now()
    if args.open_items or args.done:
        try:
            state = load_state(state_path)
        except RuntimeError as error:
            parser.error(str(error))
        if args.done:
            unknown = set(args.done) - {i["id"] for i in state["open"]}
            state["open"] = [i for i in state["open"] if i["id"] not in args.done]
            save_state(state_path, state)
            print(f"닫음 {len(args.done) - len(unknown)}건" + (f", 찾지 못한 ID: {', '.join(sorted(unknown))}" if unknown else ""))
        print_open_items(state, now, args.overdue_hours, args.me)
        return None
    if args.date:
        results = search_date(args.date)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return None
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if args.evaluate:
        if not key:
            parser.error("TYPESAFE_API_KEY 환경 변수가 없습니다.")
        print_evaluation(evaluate_file(args.evaluate, key, config["rules"]))
        return None
    if not args.rooms and not args.all_open:
        args.rooms = config["rooms"]
    if bool(args.rooms) == args.all_open:
        parser.error("방 이름 또는 --all-open 중 하나를 지정하세요. 자주 보는 방은 설정 파일의 rooms에 넣을 수 있습니다.")
    if not args.dry_run and not args.make_labels and not key:
        parser.error("TYPESAFE_API_KEY 환경 변수가 없습니다.")
    if args.days < 1:
        parser.error("--days는 1 이상이어야 합니다.")
    if args.report_room and not args.briefing:
        args.report_room = ""
    if args.events and args.calendar:
        parser.error("--events와 --calendar 중 하나만 지정하세요.")
    events = load_events(args.events) if args.events else None
    if args.calendar and not args.dry_run and not args.make_labels:
        token = os.environ.get("GOOGLE_CALENDAR_ACCESS_TOKEN", "")
        if not token:
            parser.error("GOOGLE_CALENDAR_ACCESS_TOKEN 환경 변수가 없습니다.")
        start = now.astimezone()
        try:
            events = fetch_calendar_events(token, start, start + datetime.timedelta(days=args.calendar_days))
        except RuntimeError as error:
            parser.error(str(error))
    if events is not None and not args.briefing:
        args.category = "일정"
    today = now.date()
    try:
        state = load_state(state_path) if args.track else None
    except RuntimeError as error:
        parser.error(str(error))
    label_rows = []

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
            if args.make_labels:
                label_rows += [
                    {"room": title, "sender": m.sender, "time": m.time, "text": m.text,
                     "category": None, "needs_reply": None, "invoice": None}
                    for m in messages[-args.limit :]
                ]
                continue
            keys = message_keys(title, messages)
            indexes = pending(messages, args.me)
            notes = []
            if args.briefing:
                indexes, undated = recent_indexes(messages, indexes, today, args.days)
                notes = coverage_notes(messages, today, args.days, len(indexes), args.limit, undated)
            if state is not None:
                seen = set(state["seen"].get(title, []))
                indexes = [i for i in indexes if keys[i] not in seen]
                if not args.dry_run:
                    close_answered(state, title, messages, args.me)
            indexes = indexes[-args.limit :]
            rooms.append((title, len(messages), len(indexes), notes))
            if not args.dry_run:
                judged = judge(title, messages, indexes, key, args.invoices or args.briefing, config["rules"])
                for row in judged:
                    row["aliases"] = config["aliases"]
                rows += judged
                if state is not None:
                    record_judged(state, title, [keys[i] for i in indexes], judged, args.threshold)
    finally:
        clear_clipboard()
        if previous:
            bring_to_front(previous)

    if args.make_labels:
        try:
            write_new_json(args.make_labels, label_rows)
        except RuntimeError as error:
            parser.error(str(error))
        print(f"메시지 {len(label_rows)}개를 저장했습니다: {args.make_labels}")
        for room, reason in skipped:
            print(f"[{room}] 읽기 실패: {reason}")
        print("category, needs_reply, invoice 값을 채운 뒤 --evaluate로 측정하세요.")
        return
    if state is not None and not args.dry_run:
        save_state(state_path, state)
    if args.reply_queue and not args.dry_run:
        try:
            queue = reply_queue(rows, args.threshold)
            write_new_json(args.reply_queue, queue)
            print(f"답장 초안 목록 {len(queue)}건을 저장했습니다: {args.reply_queue}")
        except RuntimeError as error:
            print(f"답장 초안 목록을 저장하지 못했습니다: {error}")

    if args.dry_run:
        for room, total, checked, notes in rooms:
            print(f"[{room}] 읽음 {total}개, 판단 대상 {checked}개 (dry-run: 외부 전송 없음)")
            for note in notes:
                print(f"  주의: {note}")
        for room, reason in skipped:
            print(f"[{room}] 읽기 실패: {reason}")
        return

    if args.briefing:
        schedule = [r for r in rows if r.get("category") == "일정"]
        proposals = None if events is None else [propose(row, events) for row in schedule]
        briefing = build_briefing(rows, args.threshold, proposals)
        period = f"{today.isoformat()} 브리핑" if args.days == 1 else f"최근 {args.days}일 브리핑"
        overdue = overdue_items(state, now, args.overdue_hours) if state is not None else []
        if args.json:
            coverage = [{"room": r, "read": t, "judged": c, "notes": n} for r, t, c, n in rooms]
            output = {**briefing, "rooms": coverage, "skipped": skipped, "overdue": overdue}
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print_briefing(briefing, rooms, skipped, period)
            if state is not None:
                print_open_items(state, now, args.overdue_hours, args.me)
        if args.report_room:
            if not any(r["title"] == args.report_room for r in open_rooms):
                print(f"보고를 보내지 않았습니다: '{args.report_room}' 창이 열려 있지 않습니다.")
            else:
                send_report(args.report_room, format_report(briefing, overdue, period))
                if previous:
                    bring_to_front(previous)
        return

    flagged, failed = bucket(rows, args.threshold, args.category or "")
    proposals = None if events is None else [propose(row, events) for row in flagged]
    drafts = None
    if args.invoices:
        drafts = [draft_invoice(r) for r in rows if "error" not in r and r["invoice"] >= args.threshold]
    if args.json:
        output = {"flagged": flagged, "failed": failed, "skipped": skipped}
        if proposals is not None:
            output["proposals"] = proposals
        if drafts is not None:
            output["invoices"] = drafts
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        report(rooms, flagged, failed, skipped, args.threshold, args.category or "")
        if proposals is not None:
            print_proposals(proposals)
        if drafts is not None:
            print_invoices(drafts)


if __name__ == "__main__":
    main()
