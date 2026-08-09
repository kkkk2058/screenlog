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
SCREENPIPE_DB = Path(os.environ.get("SCREENPIPE_DB")
                     or Path.home() / ".screenpipe-redacted" / "db.sqlite")

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
# 기본값은 상대 경로 그대로 둔다 — 서버(Docker)는 WORKDIR=/app에 chroma를
# 볼륨으로 붙이므로 이 값이 바뀌면 배포가 깨진다.
#
# 대신 환경변수로 덮어쓸 수 있게 열어둔다. 맥 앱(.dmg)은 저장소를 클론하지
# 않고 실행되므로 "현재 디렉토리 기준 chroma/"라는 게 아예 성립하지 않는다 —
# 그래서 메뉴바 앱이 자기 데이터 디렉토리를 여기에 넣어준다. 예전엔 이
# 경로가 상대 경로라서 앱이 굳이 ~/screenlog로 cd한 뒤 uv run을 해야만
# 했다(그게 저장소 의존의 근본 원인이었다).
CHROMA_DIR = os.environ.get("SCREENLOG_DATA_DIR", "chroma")
COLLECTION = "events"        # 검색 단위는 프레임이 아니라 이벤트다

EMBEDDING_MODEL = "BAAI/bge-m3"
EMBED_BATCH_SIZE = 4       # 기본값 32면 MPS OOM이 난다. 화면 텍스트 한 건이 길다.

# bge-m3의 출력 차원. 클라이언트(맥 앱)가 임베딩해서 보낸 벡터를 서버가
# 그대로 받아 넣기 때문에, 서버가 이 값을 직접 검사해야 한다 — 차원이 다른
# 벡터가 섞여 들어가면 컬렉션 전체의 검색이 깨지고, 그때는 이미 어느 게
# 잘못 들어간 건지 구분할 수 없다. 모델을 바꾸면 이 값도 같이 바꾼다.
EMBED_DIM = 1024

# 인제스트 한 번에 받을 이벤트 개수 상한. 클라이언트가 하루치를 통째로
# 밀어넣으면 요청 본문이 수백 MB가 된다(벡터 하나가 1024 x float).
INGEST_MAX_BATCH = 500

# 하루치를 이 개수만큼 묶어서 임베딩 -> 저장을 반복한다. 하루치를 전부
# 임베딩한 뒤에야 한 번에 저장하면, 중간에 죽었을 때 그 하루 진행분이
# 통째로 날아간다. 청크 단위로 바로바로 저장해야 재실행 시 이어서 할 수 있다.
INDEX_CHECKPOINT_SIZE = 200

# --- 검색 -------------------------------------------------------------
# 근거를 몇 개나 프롬프트에 넣을지. 5였을 때 답에 필요한 이벤트가 top-5
# 밖으로 밀려서 LLM이 놓치는 사례가 있었다. CONTEXT_CHARS_PER_HIT(1500자)
# 기준으로 10이면 근거만으로 최대 15,000자라 아직 감당할 수준이라 10으로 올렸다.
#
# 이후 골든셋(eval/retrieval/retrieval_questions.jsonl, 25문항)으로 recall@k를
# 실측해서 8로 낮췄다 — k=8에서 이미 recall 0.99(24/25 만점)이고, 9·10으로
# 늘려도 0.99→1.00으로 딱 1문항(1%p)만 더 잡힌다. 반면 8로 낮추면 프롬프트에
# 들어가는 근거가 20% 줄어든다(10개 -> 8개). recall 손실이 거의 없는데
# 프롬프트만 커지는 구간(9~10)을 걷어낸 것— eval/retrieval/eval_retrieval.py
# 실행 결과 참고.
RETRIEVE_K = 8

# 검색인데 기간(periods)이 있는 질문("14일치 디스코드 공지 찾아줘")의 상한.
# RETRIEVE_K(8)를 그대로 쓰면 기간이 길어질수록 진짜 관련 있는 이벤트가
# top-8 밖으로 밀려서 통째로 누락된다. 그렇다고 상한을 없애면 "며칠치
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

# 팔로우업 해석("그날"/"더 자세히" 등)에 쓸 최근 턴 수. 클라이언트가 매번
# 재전송하던 걸 서버가 자기 DB(chat_history)에서 직접 읽도록 바꾸면서 생긴
# 값 — 예전엔 이 숫자가 dashboard.html의 HISTORY_SEND_TURNS였다.
# 히스토리가 너무 커져서 할루시네이션이 심해졌다. 5 -> 2 로 변경
HISTORY_TURNS = 2

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

# --- 실행 역할 --------------------------------------------------------
# 같은 패키지가 두 자리에서 돈다: EC2의 서버(질문에 답한다)와 맥 앱(기록을
# 이벤트화·임베딩해서 올린다). 앱은 LLM 키가 없고 필요하지도 않아서, 이
# 값으로 두 역할의 요구사항을 갈라놓는다.
IS_CLIENT = os.environ.get("SCREENLOG_ROLE") == "client"

# 맥 앱이 벡터를 올릴 서버. 앱에서 사용자가 바꿀 수 있어야 하므로 환경변수로
# 받는다(기본값은 현재 배포된 EC2).
SCREENLOG_SERVER_URL = os.environ.get("SCREENLOG_SERVER_URL", "http://3.35.7.225:8000")

# --- LLM --------------------------------------------------------------
# 게이트웨이 크레딧이 떨어지면(402) 막히니, ANTHROPIC_API_KEY가 있으면
# Anthropic의 OpenAI 호환 엔드포인트로 넘어갈 수 있게 해뒀다. OpenAI 클라이언트를
# 그대로 쓰는 이유는 ask.py/router.py/summarize.py가 이미 client.chat.completions
# 인터페이스(structured output 포함)로 짜여 있어서다.


# USE_LOCAL_LLM은 다른 키를 지우지 않고 그 위에 우선순위로 얹는 스위치다 —
# 환경변수 하나만 끄면(줄만 지우면) 바로 기존 게이트웨이/Anthropic 경로로
# 돌아갈 수 있어야 로컬 LLM이 말썽일 때 롤백이 코드 변경 없이 된다.
if os.environ.get("USE_LOCAL_LLM"):
    CHAT_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:7b-instruct")
    BASE_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434/v1")
    API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "ollama")  # Ollama는 인증이 없어 더미값만 채운다
elif os.environ.get("OPENAI_API_KEY"):
    CHAT_MODEL = "gemini-3.1-flash-lite"
    BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"
    API_KEY = os.environ["OPENAI_API_KEY"]
elif os.environ.get("ANTHROPIC_API_KEY"):
    CHAT_MODEL = "claude-haiku-4-5"
    BASE_URL = "https://api.anthropic.com/v1/"
    API_KEY = os.environ["ANTHROPIC_API_KEY"]
elif IS_CLIENT:
    # 맥 앱은 LLM을 한 번도 안 부른다 — 화면 기록을 이벤트로 묶고(clean),
    # 임베딩하고(index), 서버로 올리는 것까지가 전부다. 질문에 답하는 건
    # 서버 몫이라 키도 서버에만 있으면 된다. 그런데 이 검사가 import
    # 시점에 있어서, 키가 없는 사용자 맥에서는 screenlog.index를 불러오는
    # 것만으로 앱이 죽었다. 그렇다고 검사를 그냥 없애면 서버가 키 없이
    # 떴을 때 한참 뒤 엉뚱한 곳에서 터지므로, 클라이언트라고 명시한
    # 경우에만 면제한다.
    CHAT_MODEL = BASE_URL = API_KEY = None
else:
    raise RuntimeError("OPENAI_API_KEY 또는 ANTHROPIC_API_KEY 둘 중 하나는 있어야 한다.")

# screenlog_langgraph.agent의 ReAct(멀티턴 tool-calling) 루프 전용 모델.
# Gemini 3.x 계열은 함수 호출 응답에 thought_signature를 요구하는데, 이 게이트웨이의
# OpenAI 호환 변환 레이어가 그 필드를 응답에서 통째로 누락시켜서(실측 확인됨) 두 번째
# 턴부터 400으로 막힌다 — 우리 쪽에서 재전송할 값 자체가 없으니 코드로 못 고친다.
# route()/graph.py처럼 한 턴짜리 구조화 출력은 문제 없어서 CHAT_MODEL을 그대로 두고,
# 도구를 여러 턴 이어 부르는 agent.py만 OpenAI 계열(포맷 변환이 없어 이 문제가 원천적으로
# 없음)의 가장 싼 모델로 분리했다. 로컬 LLM 경로는 이 게이트웨이를 아예 안 거치니
# 이 문제 자체가 없어서 CHAT_MODEL을 그대로 쓴다.
if os.environ.get("USE_LOCAL_LLM"):
    AGENT_CHAT_MODEL = CHAT_MODEL
elif os.environ.get("OPENAI_API_KEY"):
    AGENT_CHAT_MODEL = "gpt-5.4-nano"
else:
    AGENT_CHAT_MODEL = CHAT_MODEL

# --- 슬랙 연동 -----------------------------------------------------------
# draft_slack_message(agent.py 도구)는 LLM이 자유롭게 부를 수 있지만, 실제
# 전송(execute)은 LLM에 도구로 안 준다 — 에이전트가 "승인된 것 같다"고
# 스스로 판단해서 자동으로 실제 메시지를 보내면 사고가 난다. 전송은 반드시
# 프론트에서 사용자가 초안을 보고 명시적으로 "보내기"를 눌렀을 때만 호출되는
# 별도 API 엔드포인트(/api/slack/send)를 거친다. 토큰이 없으면 그 엔드포인트가
# 501로 막는다(민감정보라 값 자체는 로그에 안 남긴다).
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_DEFAULT_CHANNEL = os.environ.get("SLACK_DEFAULT_CHANNEL")

# --- 실험 -------------------------------------------------------------
# ask_auto()/stream_ask_auto()를 screenlog_langgraph 버전으로 바꿔서 쓸지.
# 기본은 원본(screenlog.ask)이고, 환경변수로만 켠다 — api.py가 이 값 하나로
# 분기하므로 롤백은 환경변수를 지우기만 하면 된다(코드 배포 불필요).
USE_LANGGRAPH = os.environ.get("USE_LANGGRAPH", "").lower() in ("1", "true", "yes")
# --- 시간 -------------------------------------------------------------
# screenpipe는 UTC로 저장하는데 질문은 한국 시간으로 들어온다.
# 읽어 들일 때 한 번만 변환하고, 그 뒤로는 전부 로컬로 생각한다.
TZ_OFFSET_HOURS = 9

# --- 인증 -------------------------------------------------------------
# 화면 기록(메신저 대화·로그인 화면 포함)을 다루는 API라 인터넷에 그대로
# 열어두면 안 된다(api.py 상단 주석 참고). EC2에 이미 인증 없이 배포된 적이
# 있었던 사고를 계기로, 값을 안 채우면 아예 기동하지 않게 강제한다 —
# "일단 띄우고 나중에 잠그자"가 반복될 여지를 없앤다. /download, /static은
# 팀 배포용 설치 페이지라 예외로 공개한다(api.py PUBLIC_PATH_PREFIXES).
SCREENLOG_USER = os.environ.get("SCREENLOG_USER")
SCREENLOG_PASSWORD = os.environ.get("SCREENLOG_PASSWORD")
# 맥 앱에서는 이 값이 "서버를 잠그는 열쇠"가 아니라 "서버에 로그인할 계정"이라
# 역할이 반대다. 그리고 앱은 사용자가 계정을 입력하기 전에도 일단 떠서 설정
# 창을 보여줘야 하므로, 없다고 기동을 막으면 안 된다 — 실제로 필요한 시점
# (동기화 버튼을 눌렀을 때)에 sync.py가 확인하고 안내한다. 서버는 종전대로
# 값이 없으면 아예 안 뜬다.
if not (SCREENLOG_USER and SCREENLOG_PASSWORD) and not IS_CLIENT:
    raise RuntimeError(
        "SCREENLOG_USER/SCREENLOG_PASSWORD가 .env에 없다. "
        "화면 기록을 다루는 API라 인증 없이는 기동하지 않는다."
    )
