# kakaocli-auto

Windows 10/11용 비공식 카카오톡 CLI입니다. 카카오 공식 API나 프로토콜을 사용하지 않고, 실행 중인 PC 카카오톡 창을 Win32 API로 조작합니다.

[Lee-SiHyeon/kakaocli-win](https://github.com/Lee-SiHyeon/kakaocli-win)에서 갈라져 나온 저장소로, 닫힌 방 찾기, 메시지 검색, 메시지 분류, 일정 비교를 추가했습니다. 카카오나 원본 저장소와 제휴 관계가 없습니다.

Windows용 PC 카카오톡 26.9에서 확인했습니다. 다른 버전이나 화면 배율에서는 동작이 다를 수 있습니다.

## 현재 지원 범위

- 카카오톡 설치·실행 상태 점검
- 현재 독립 창으로 열린 채팅방 목록
- 이미 독립 창으로 열린 채팅방 포커스
- 닫힌 채팅방을 채팅 탭에서 검색해 열기
- 통합검색으로 메시지를 찾아 결과 창을 이미지로 저장
- 대화 텍스트 읽기(스크롤로 과거 대화 포함)
- 확인 후 메시지 전송
- 메시지 분류와 일정 비교(`kakao_triage.py`)
- 자동화용 JSON 출력
- 카카오톡 업데이트 호환성 진단용 컨트롤 트리 출력
- 실행 중인 `KakaoTalk.exe` 메모리에서 SQLCipher 4 raw key 후보 복구
- 검증된 키를 Windows DPAPI로 보호해 로컬 저장
- 저장된 키로 `TalkUserDB.edb`를 임시 복호화해 친구 목록 조회
- 저장된 별도 키로 `chatListInfo.edb`의 채팅방 메타데이터 조회(메시지 본문 제외)

`read`는 카카오톡 화면에 현재 로드된 대화만 대상으로 합니다. `friends`와 `chat-rooms`는 각각 친구 및 채팅방 메타데이터 DB를 읽지만, 메시지 본문은 읽지 않습니다. 카카오톡 UI가 변경되면 `inspect` 결과를 바탕으로 선택자를 조정해야 할 수 있습니다.

## 설치

요구 사항:

- Windows 10 또는 11
- Python 3.10 이상
- PC 카카오톡 설치 및 로그인

PowerShell에서 이 폴더로 이동한 뒤 실행합니다.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
.\kakaocli.cmd doctor
```

GitHub에서 바로 설치하려면 다음처럼 복제할 수 있습니다.

```powershell
git clone https://github.com/lsungwookl-art/kakaocli-auto.git
cd kakaocli-auto
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

## 사용법

```powershell
# 카카오톡 실행
.\kakaocli.cmd start

# 준비 상태 점검
.\kakaocli.cmd doctor

# 열린 채팅방 확인
.\kakaocli.cmd rooms

# 이미 독립 창으로 열린 채팅방 포커스
.\kakaocli.cmd open "홍길동"

# 닫힌 방을 채팅 탭에서 검색해 열기 (제목이 정확히 같을 때만 유지)
.\kakaocli.cmd open "홍길동" --search

# 닫힌 방을 검색해 연 뒤, 확인 질문을 거쳐 전송
.\kakaocli.cmd send "홍길동" "테스트입니다" --open

# 화면에 로드된 대화 읽기
.\kakaocli.cmd read "홍길동"

# 위로 40회 스크롤해 과거 대화까지 읽기
.\kakaocli.cmd read "홍길동" --scroll 40

# 통합검색으로 메시지를 찾아 결과 창을 이미지로 저장 (결과 방은 열지 않음)
# 결과가 많으면 result-2.png처럼 이어서 저장하고, complete 값으로 끝까지 봤는지 알려 줌
.\kakaocli.cmd --json search "11월 25일" --output ".\result.png" --pages 10

# 실제 전송 전 점검
.\kakaocli.cmd send "홍길동" "테스트입니다" --dry-run

# 확인 질문을 거쳐 전송
.\kakaocli.cmd send "홍길동" "테스트입니다"

# 스크립트에서 사용: 정확한 방 이름 + 확인 질문 생략 + JSON
.\kakaocli.cmd --json send "홍길동" "테스트입니다" --exact --yes

# 최신 TalkUserDB.edb 키 복구 및 DPAPI 저장
.\kakaocli.cmd --json recover-key

# 특정 DB 키 복구
.\kakaocli.cmd --json recover-key --db "$env:LOCALAPPDATA\Kakao\KakaoTalk\users\사용자폴더\chat_data\chatListInfo.edb"

# 키 원문 없이 저장 상태와 지문 확인
.\kakaocli.cmd --json key-status

# GUI 자동화 없이 친구 DB 검색
.\kakaocli.cmd friends --contains "vs"

# 자동화용 JSON
.\kakaocli.cmd --json friends --contains "vs"

# GUI 자동화 없이 DB에서 채팅방 제목 검색
.\kakaocli.cmd chat-rooms --contains "바다"

# 종류·개수 필터와 메타데이터 JSON 출력
.\kakaocli.cmd --json chat-rooms --type MultiChat --limit 20

# DB에 캐시된 참여자 이름·전화번호를 로컬 CSV로 저장
.\kakaocli.cmd --json export-room-contacts "모임방" --exact --output ".\contacts.csv"

# 방 표시 인원보다 캐시가 적다는 경고를 확인한 뒤 부분 목록 저장
.\kakaocli.cmd export-room-contacts "모임방" --exact --output ".\contacts.csv" --allow-partial

# DB에서 나와의 채팅(MemoChat)을 식별한 뒤 전송 전 점검
.\kakaocli.cmd --json send-self "나에게 남길 메모" --dry-run

# 확인 질문을 거쳐 나와의 채팅으로 전송
.\kakaocli.cmd send-self "나에게 남길 메모"

# 데이터 행을 노출하지 않고 DB 테이블·열·행 수만 확인
.\kakaocli.cmd --json db-schema
```

동일한 문자열이 들어간 방이 여러 개면 전송을 중단합니다. 자동화에서는 완전한 채팅방 이름과 `--exact` 사용을 권장합니다.

## 메시지 분류와 일정 비교

`kakao_triage.py`는 독립 창으로 열어 둔 방을 읽어 메시지를 분류합니다. 메시지를 보내는 기능은 확인을 거치는 `--report-room`뿐입니다. 분류에는 [TypeSafe](https://docs.typesafe.ai)의 Jev를 쓰므로 `TYPESAFE_API_KEY` 환경 변수가 필요합니다.

```powershell
# 외부 전송 없이 읽기만 확인
python .\kakao_triage.py "방이름" --dry-run

# 내 마지막 메시지 이후에서 답장이 필요한 메시지 고르기
python .\kakao_triage.py "방이름" --me "내 대화 이름"

# 전체 분류 보기 (잡담, 정보, 요청, 문제, 결정, 일정, 질문)
python .\kakao_triage.py "방이름" --category 전체

# 과거 대화까지 읽어 일정 메시지만 보기
python .\kakao_triage.py "방이름" --scroll 40 --limit 200 --category 일정

# 일정 메시지를 캘린더 일정 JSON과 비교해 수정안 만들기 (캘린더는 바꾸지 않음)
python .\kakao_triage.py "방이름" --events ".\events.json"

# 날짜를 여러 표현으로 통합검색해 결과 이미지 경로 출력
python .\kakao_triage.py --date 2026-11-25

# 세금계산서 요청을 찾아 검토용 초안 만들기 (발행하지 않음)
python .\kakao_triage.py "방이름" --invoices

# 오늘 메시지를 문제, 결정, 요청, 질문, 일정, 세금계산서로 정리
python .\kakao_triage.py "방이름1" "방이름2" --briefing --me "내 대화 이름"

# 열어 둔 방 전체를 최근 3일 범위로 정리하고, 일정은 캘린더와 비교
python .\kakao_triage.py --all-open --briefing --days 3 --scroll 20 --limit 100 --events ".\events.json"
```

브리핑은 독립 창으로 열어 둔 방만 읽습니다. 읽지 못한 방, 잘렸을 수 있는 범위, 판단에 실패한 메시지는 결과에 따로 표시되므로, 항목이 적을 때는 이 표시를 함께 확인하세요.

### 설정

```powershell
# %LOCALAPPDATA%\kakaocli-auto\config.json 틀 만들기
python .\kakao_triage.py --init-config
```

```json
{
  "rooms": ["방이름1", "방이름2"],
  "me": "내 대화 이름",
  "threshold": 0.5,
  "limit": 30,
  "days": 1,
  "scroll": 0,
  "overdue_hours": 24,
  "report_room": "",
  "rules": ["거래처의 납기 변경은 항상 문제로 분류한다"],
  "aliases": {"A사": "A상사"}
}
```

`rooms`를 넣으면 방 이름 없이 실행할 수 있고, 명령줄 옵션이 설정보다 우선합니다. `rules`는 Jev 판단에 함께 전달됩니다. `aliases`는 일정과 세금계산서 항목을 찾을 때만 쓰며, 화면에 보이는 메시지는 바꾸지 않습니다. 설정 파일은 저장소 밖에 있으니 회사 기준을 커밋할 걱정은 없습니다.

### 중복 방지와 놓친 답장

```powershell
# 이미 본 메시지는 건너뛰고, 답장이나 조치가 필요한 항목을 추적
python .\kakao_triage.py --briefing --track --me "내 대화 이름"

# 미처리 항목 보기, 처리한 항목 닫기
python .\kakao_triage.py --open-items
python .\kakao_triage.py --done 항목ID
```

`--me`를 지정하면 내가 그 방에 답한 시각 이전의 항목은 자동으로 닫힙니다. 주제와 상관없이 닫히므로 한 방에서 여러 건이 오갈 때는 놓칠 수 있습니다. 기록은 `%LOCALAPPDATA%\kakaocli-auto\state.json`에 저장됩니다. 본 메시지는 해시로만 남지만, 미처리 항목은 찾아보기 쉽도록 보낸 사람과 메시지 앞부분 80자를 그대로 저장합니다.

### 정확도 측정

```powershell
# 열린 방의 최근 메시지로 정답 입력 파일 만들기 (외부 전송 없음)
python .\kakao_triage.py "방이름" --make-labels .\labels.json --limit 50

# category, needs_reply, invoice를 채운 뒤 측정 (메시지가 TypeSafe로 전송됨)
python .\kakao_triage.py --evaluate .\labels.json
```

분류별로 찾은 비율과 맞은 비율, 기준값별로 놓친 수와 잘못 잡은 수를 보여 줍니다. `labels.json`에는 대화 원문이 들어 있으니 저장소에 커밋하지 마세요.

### 캘린더 직접 조회

`GOOGLE_CALENDAR_ACCESS_TOKEN` 환경 변수에 읽기 권한이 있는 Google OAuth 액세스 토큰을 넣으면 `--events` 대신 `--calendar`로 기본 캘린더를 읽어 비교합니다. 읽기만 하고 수정은 하지 않습니다. 토큰 발급과 갱신은 직접 해야 합니다.

### 답장 초안 목록과 보고 전송

```powershell
# 답장이 필요한 메시지와 앞뒤 맥락을 JSON으로 저장 (전송 없음)
python .\kakao_triage.py "방이름" --reply-queue .\replies.json

# 브리핑 요약을 열어 둔 방으로 보내기. 내용을 보여 주고 y를 입력해야 전송됨
python .\kakao_triage.py --briefing --report-room "나와의 채팅 제목"
```

`replies.json`의 `draft`는 비어 있습니다. 직접 쓰거나 AI에게 맡긴 뒤 내용을 확인하고 `kakaocli send`로 보내세요. 이 도구가 답장을 자동으로 보내지는 않습니다. 보고에는 다른 방의 메시지 일부가 들어가므로 나와의 채팅으로 보내는 것을 권합니다.

세금계산서 초안은 상호, 사업자번호, 공급가액, 세액, 합계, 작성일, 품목을 규칙으로 뽑고 사업자번호 검증 숫자와 세액 10%를 확인합니다. 빠지거나 불확실한 값은 추측하지 않고 `확인 필요`로 표시합니다. **이 도구는 세금계산서를 발행하지 않습니다.** 초안의 값을 확인한 뒤 쓰시는 서비스에서 직접 발행하세요. 공급자 정보는 다루지 않으며, 초안의 항목만으로 법적 필수 기재 사항이 모두 갖춰졌다고 보장하지 않습니다. 사업자번호와 금액이 든 메시지도 TypeSafe로 전송됩니다.

일정 비교 결과는 `수정안`, `일치`, `캘린더에 없음`, `확인 필요` 중 하나입니다. "쯤", "아직", 질문처럼 확정되지 않은 표현과 오전/오후가 없는 시간, 반복 일정, 종일 일정은 추측하지 않고 `확인 필요`로 표시합니다. `events.json`은 `{"events": [...]}` 형식이고, 일정마다 `id`, `summary`, `start.dateTime`, `end.dateTime`이 필요합니다.

주의할 점:

- 대화 내용과 보낸 사람 이름이 TypeSafe로 전송됩니다. `--dry-run`과 `--date`는 전송하지 않습니다.
- 정확도는 실제 대화로 충분히 검증하지 않았습니다. 수정안은 사람이 확인한 뒤 적용하세요.
- `--date` 결과 이미지에는 메시지가 그대로 들어 있고 임시 폴더에 남습니다. 확인 후 직접 삭제하세요.
- 통합검색은 적힌 글자를 찾으므로 "다음 주 수요일"처럼 돌려 말한 날짜는 찾기 어렵습니다.

## 문제 해결

`doctor`가 메인 창을 찾지 못하면 카카오톡에 로그인하고 메인 창을 열어 둡니다. 관리자 권한으로 카카오톡을 실행했다면 CLI도 같은 권한 수준으로 실행해야 Windows가 입력을 허용합니다.

카카오톡은 버전에 따라 내부 컨트롤 클래스가 달라질 수 있습니다. 아래 결과를 파일로 저장하면 선택자 조정에 사용할 수 있습니다.

현재 확인된 카카오톡 버전에서는 친구 탭의 `Ctrl+F`가 검색이 아니라 친구 추가를 열 수 있어, 닫힌 채팅방 자동 검색은 `open --search`와 `send --open`을 지정할 때만 동작합니다. 메인 창을 채팅 탭으로 둬야 합니다.

검색 결과 목록은 읽을 수 없어 첫 줄을 엽니다. 제목이 정확히 일치하지 않으면 창을 닫고 중단하지만, 그 사이 다른 방의 메시지가 읽음으로 표시될 수 있습니다. 제목이 완전히 같은 방은 구별하지 못하므로, 민감한 방은 직접 독립 창으로 열어 두세요. 같은 이유로 `send --open`은 `--yes`와 함께 쓸 수 없습니다.

```powershell
.\kakaocli.cmd --json inspect > main-controls.json
.\kakaocli.cmd --json inspect --room "홍길동" > room-controls.json
```

## 안전 및 제한

- 개인적인 테스트 및 자동화 용도로만 사용하세요.
- 카카오톡 서비스 약관과 조직 정책을 확인하세요.
- 대량·반복 전송 기능은 제공하지 않습니다.
- `send`는 기본적으로 사람의 확인을 요구합니다.
- `read` 명령은 대화 내용을 복사하므로 실행 후 Windows 클립보드가 해당 텍스트로 바뀝니다.
- 채팅방을 맨 앞으로 가져오지 못하면 다른 앱에 키가 입력되지 않도록 `open`, `read`, `send`를 중단합니다.
- UI 자동화 특성상 전송 후 실제 채팅창에서 결과를 확인하는 것이 좋습니다.
- 저장소에는 실제 카카오톡 DB, 대화 내용, 방 이름, 복구 키 또는 로컬 진단 결과를 커밋하지 마세요.
- 연락처 CSV에는 제3자의 개인정보가 포함될 수 있으므로 저장·공유·삭제를 직접 관리하세요. CLI는 연락처 CSV를 GitHub로 업로드하지 않습니다.

### DB 키 복구 안전성

- `recover-key`는 지정한 PID가 실제 `KakaoTalk.exe`인지 확인합니다.
- `MEM_COMMIT` 상태인 읽기 가능한 `MEM_PRIVATE` 영역만 읽습니다.
- 후보 키는 대상 DB 첫 페이지의 SQLCipher 4 HMAC-SHA512로 검증합니다.
- DB 원본과 카카오톡 프로세스 메모리를 수정하지 않습니다.
- 키 원문은 콘솔이나 JSON에 출력하지 않습니다.
- 검증 키는 `%LOCALAPPDATA%\kakaocli-win\keys.json`에 DPAPI 암호문으로 저장되어 같은 Windows 사용자만 해제할 수 있습니다.
- DB 조회 시 평문 SQLite 파일은 Windows 임시 폴더에 만들고 조회 종료 시 즉시 삭제합니다.
- DB가 실제로 사용되는 화면을 최근에 열지 않았다면 키가 메모리에 없을 수 있습니다. 이 경우 해당 화면을 연 뒤 다시 실행하세요.
- 다른 사람의 PC, 계정 또는 접근 권한이 없는 데이터에는 사용하지 마세요.

구현은 `TalkUserDB.edb` 키 복구·재검증과 DPAPI 저장을 지원합니다. 다른 `.edb` 파일은 별도 키를 사용할 수 있으며, 대상 데이터가 메모리에 로드되지 않았다면 제한 시간 안에 키를 찾지 못할 수 있습니다.

## Codex 스킬과 유지관리 스크립트

저장소에는 다른 사용자가 Codex에서 재사용할 수 있는 두 개의 스킬이 포함됩니다.

- `skills/kakaotalk-windows-cli`: 설치, 개인정보를 가린 진단, 안전한 UI 자동화
- `skills/kakaotalk-db-key-recovery`: 소유자 승인 범위의 로컬 DB 키 복구와 검증

스킬을 사용하려면 원하는 폴더를 Codex 스킬 디렉터리에 복사하세요. 각 스킬의 `scripts` 폴더에는 개인정보를 덜 노출하는 진단 및 키 복구 도우미가 들어 있습니다.

공개 전 검증은 다음 명령으로 실행합니다.

```powershell
.\scripts\verify.ps1
```

`privacy-audit.ps1`은 로컬 사용자 경로, 흔한 토큰 형식, 비밀키와 DB/키 저장 파일을 검사합니다. 이는 보조 검사이므로 커밋 전 `git diff --cached`도 직접 확인하세요.

## 출처

이 저장소는 [Lee-SiHyeon/kakaocli-win](https://github.com/Lee-SiHyeon/kakaocli-win)을 기반으로 합니다.

Windows의 `EVA_Window_Dblclk`/`RICHEDIT50W` 탐색 방식은 [rime10221/kakao_MCP](https://github.com/rime10221/kakao_MCP)의 공개 구현을 참고해 안전 확인과 진단 기능을 추가했습니다. 이 프로젝트는 해당 저장소나 카카오와 제휴되어 있지 않습니다.

SQLCipher 키 검증은 [SQLCipher 공식 설계 문서](https://www.zetetic.net/sqlcipher/design/)와 [공식 verify.c](https://github.com/sqlcipher/sqlcipher-tools/blob/master/verify.c)의 SQLCipher 4 페이지 HMAC 방식을 따릅니다.
