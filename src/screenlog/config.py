"""설정 한 곳.

실험하면서 바뀌는 값은 전부 여기 둔다. 코드 여기저기에 숫자가 흩어지면
"어떤 설정으로 낸 결과인지"를 나중에 복원할 수 없다.

바닐라 단계라 값이 몇 개 없다. 기능을 얹을 때마다 늘어난다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- 원본 -------------------------------------------------------------
# screenpipe가 쓰는 DB를 읽기 전용으로 그대로 읽는다. jsonl 사본을 만들지 않는다.
# 사본을 두면 "원본이 자랐는데 사본은 안 자란" 상태가 생기고,
# 그때부터 실험 결과가 어느 시점 데이터인지 알 수 없게 된다.
SCREENPIPE_DB = Path.home() / ".screenpipe-redacted" / "db.sqlite"

# --- 정제 -------------------------------------------------------------
MIN_EVENT_CHARS = 100    # 이보다 짧은 이벤트는 버린다. 알맹이가 없다.
MAX_EVENT_CHARS = 2000   # 이보다 길어지면 끊는다. 긴 답변 스트리밍이 한 덩어리가 되는 걸 막는다.
JACCARD_MIN = 0.3        # 앞 프레임과 겹치는 줄이 이보다 적으면 화면이 통째로 바뀐 것

# 화면에 AI 생성 텍스트가 뜨는 앱들("재귀 오염" — docs/troubleshooting-star.md #8).
# 이 도구 자신이 디버깅하며 터미널/에디터에 출력한 요약문이 화면 캡처로 다시
# 색인되면, LLM이 그걸 진짜 활동 기록으로 믿고 인용해버린다(실측: 존재하지
# 않는 시각/이벤트를 "근거"로 답함). app을 명시적으로 이걸로 물어본 게
# 아니면(예: "코딩 몇 시간 했어?"는 app=Code라 그대로 통과) 검색/조회에서
# 후보군에서 뺀다.
AI_APPS = {"Claude", "Code"}

# --- 색인 -------------------------------------------------------------
CHROMA_DIR = "chroma"
COLLECTION = "events"        # 검색 단위는 프레임이 아니라 이벤트다

EMBEDDING_MODEL = "BAAI/bge-m3"
EMBED_BATCH_SIZE = 4       # 기본값 32면 MPS OOM이 난다. 화면 텍스트 한 건이 길다.

# 하루치를 이 개수만큼 묶어서 임베딩 -> 저장을 반복한다. 하루치를 전부
# 임베딩한 뒤에야 한 번에 저장하면, 중간에 죽었을 때 그 하루 진행분이
# 통째로 날아간다. 청크 단위로 바로바로 저장해야 재실행 시 이어서 할 수 있다.
INDEX_CHECKPOINT_SIZE = 200

# --- 검색 -------------------------------------------------------------
# 근거를 몇 개나 프롬프트에 넣을지. 5였을 때 답에 필요한 이벤트가 top-5
# 밖으로 밀려서 LLM이 놓치는 사례가 있었다. CONTEXT_CHARS_PER_HIT(1500자)
# 기준으로 10이면 근거만으로 최대 15,000자라 아직 감당할 수준이라 10으로 올렸다.
RETRIEVE_K = 10

# 검색인데 기간(periods)이 있는 질문("14일치 디스코드 공지 찾아줘")의 상한.
# RETRIEVE_K(10)를 그대로 쓰면 기간이 길어질수록 진짜 관련 있는 이벤트가
# top-10 밖으로 밀려서 통째로 누락된다. 그렇다고 상한을 없애면 "며칠치
# 공지가 많이 쌓인 경우" 프롬프트가 무한정 커진다 — 그래서 기간 있는
# 검색만 더 넉넉한 별도 상한을 둔다. CONTEXT_CHARS_PER_HIT(1500자) 기준

MAX_PERIOD_SEARCH_K = 50

# "최근 90일 정리해줘" 같은 질문이 와도 90일치를 다 처리하진 않는다. 하루당
# LLM 호출이 1번씩 나가므로, 상한이 없으면 응답 시간과 비용이 날짜 수에 비례해
# 무한정 커진다.
MAX_RANGE_DAYS = 14

# 하루 요약 프롬프트에 넣을 이벤트 개수 상한. 이게 없으면 이벤트가 많은 날
# 하나가(실측: 2,844개 -> 100만 자) 프롬프트를 통째로 터뜨린다.
MAX_EVENTS_PER_DAY_SUMMARY = 60

# --- 대화 기록 (사이드바 "이전 질문들 모음") ---------------------------
# 새로고침하면 날아가던 CONV_HISTORY(브라우저 메모리)를 서버에 영구 저장해서
# 왼쪽 사이드바에서 이전 대화를 다시 열어볼 수 있게 한다.
CHAT_HISTORY_DB = Path(CHROMA_DIR) / "chat_history.sqlite"

# --- 하루 요약 캐시 ---------------------------------------------------
# "이번주 정리해줘"는 하루당 LLM 1번 + browse() 1번이라 7일이면 6초가 넘는다.
# 지난 날의 기록은 더 안 변하므로 요약을 미리 만들어 두고 꺼내 쓴다.
SUMMARY_CACHE_DB = Path(CHROMA_DIR) / "summary_cache.sqlite"

# 요약 분량/난이도를 바꿔달라는 요청이 있으면 캐시(기본 분량으로 생성됨)를
# 쓰면 안 된다 — "자세히"는 캐시된 요약을 재가공해서는 복원할 수 없는
# 정보를 원하는 것이라, 원본 이벤트부터 다시 요약해야 한다.
SUMMARY_DETAIL_KEYWORDS = (
    "자세", "상세", "구체", "풀어서", "낱낱이",
    "쉽게", "간단", "짧게", "요약만", "한줄", "한 줄",
)

# 근거 하나를 프롬프트에 넣을 때의 글자 상한.
# 이벤트 크기는 고르지 않다(중앙값 1,270자인데 최대 36,870자). 상한이 없으면
# 큰 이벤트가 몇 개 걸리는 것만으로 프롬프트가 통째로 부풀어 토큰 비용이 튄다.
CONTEXT_CHARS_PER_HIT = 1500

# --- 대시보드 ---------------------------------------------------------
# 이벤트의 start/end만 쓰면 93%가 폭 0이다(캡처가 1장뿐인 이벤트). 그래서
# "다음 이벤트가 시작될 때까지 그 화면을 보고 있었다"고 본다. 단 이 상한을
# 넘는 공백은 자리를 비운 것으로 보고 늘리지 않는다 — 점심 먹으러 간 90분을
# "그 화면을 봤다"로 셀 수는 없다.
IDLE_GAP_SEC = 120

# --- LLM --------------------------------------------------------------
# 게이트웨이 크레딧이 떨어지면(402) 막히니, ANTHROPIC_API_KEY가 있으면
# Anthropic의 OpenAI 호환 엔드포인트로 넘어갈 수 있게 해뒀다. OpenAI 클라이언트를
# 그대로 쓰는 이유는 ask.py/router.py/summarize.py가 이미 client.chat.completions
# 인터페이스(structured output 포함)로 짜여 있어서다.


if os.environ.get("OPENAI_API_KEY"):
    CHAT_MODEL = "gemini-3.1-flash-lite"
    BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"
    API_KEY = os.environ["OPENAI_API_KEY"]
elif os.environ.get("ANTHROPIC_API_KEY"):
    CHAT_MODEL = "claude-haiku-4-5"
    BASE_URL = "https://api.anthropic.com/v1/"
    API_KEY = os.environ["ANTHROPIC_API_KEY"]
else:
    raise RuntimeError("OPENAI_API_KEY 또는 ANTHROPIC_API_KEY 둘 중 하나는 있어야 한다.")

# --- 실험 -------------------------------------------------------------
# ask_auto()/stream_ask_auto()를 screenlog_langgraph 버전으로 바꿔서 쓸지.
# 기본은 원본(screenlog.ask)이고, 환경변수로만 켠다 — api.py가 이 값 하나로
# 분기하므로 롤백은 환경변수를 지우기만 하면 된다(코드 배포 불필요).
USE_LANGGRAPH = os.environ.get("USE_LANGGRAPH", "").lower() in ("1", "true", "yes")
# USE_LANGGRAPH=True
# --- 시간 -------------------------------------------------------------
# screenpipe는 UTC로 저장하는데 질문은 한국 시간으로 들어온다.
# 읽어 들일 때 한 번만 변환하고, 그 뒤로는 전부 로컬로 생각한다.
TZ_OFFSET_HOURS = 9
