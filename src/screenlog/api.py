"""6. 웹 서비스 — 지금까지 만든 걸 브라우저에서 쓴다

로직은 새로 짜지 않는다. ask_auto()를 얇게 감싸기만 한다.

    GET  /          웹 페이지
    POST /api/ask   질문 -> 답변 + 라우팅 계획 + 근거

개인정보: 화면 기록엔 메신저 대화와 로그인 화면이 섞여 있다. 그래서 근거의
본문 전체를 내보내지 않고 짧은 발췌만 준다. 외부에 공개할 때는 127.0.0.1
바인딩과 인증을 먼저 붙여야 한다.

실행: uv run uvicorn screenlog.api:app --reload
"""

import asyncio
import base64
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from screenlog import chat_history, summary_cache
from screenlog.config import AI_APPS, HISTORY_TURNS, SCREENLOG_PASSWORD, SCREENLOG_USER, USE_LANGGRAPH
from screenlog.source import weekday_ko
from screenlog.stats import build_stats, build_timeline
from screenlog.summarize import summarize_day
import json
from fastapi.responses import StreamingResponse

# ask_auto/stream_ask_auto 구현을 USE_LANGGRAPH 환경변수로 고른다. 셋 다
# 시그니처와 반환/이벤트 형태가 동일해서(screenlog_langgraph/graph.py 참고)
# 아래 라우트 코드는 어느 쪽이 켜져 있든 손댈 필요가 없다 — A/B든 롤백이든
# 이 한 줄의 분기로 끝난다.
#
# screenlog_langgraph.graph(고정 경로만) 대신 screenlog_langgraph.agent를
# 쓴다 — agent.py는 고정 경로일 땐 graph.py를 그대로 호출하므로(내부
# _fixed_node) graph.py가 하던 일을 전부 포함하는 상위 호환이고, 그 위에
# route()가 4갈래로 못 답하는 복합 질문(인수인계 문서 등)을 처리하는
# 에이전트 루프가 추가로 있다. graph.py를 계속 쓰면 이 에이전트 경로
# 자체가 실제 서비스에서 한 번도 안 불린다.
if USE_LANGGRAPH:
    from screenlog_langgraph.agent import ask_auto, stream_ask_auto
else:
    from screenlog.ask import ask_auto, stream_ask_auto



app = FastAPI(title="screenlog")

# 팀 배포용 설치 페이지(/download), 정적 리소스(/static, css/이미지뿐이라
# 민감정보 없음), 마케팅 랜딩(정확히 "/") 만 예외로 공개한다. 그 외
# 전부(화면 기록 질의/열람) 는 Basic Auth를 통과해야 한다 — 인증 없이
# EC2에 떠 있던 사고 재발 방지.
#
# "/"는 prefix가 아니라 정확히 일치할 때만 공개해야 한다 — startswith에
# "/"를 그대로 넣으면 모든 경로가 "/"로 시작하므로 인증 자체가 무력화된다.
PUBLIC_PATH_PREFIXES = ("/download", "/static")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/" or path.startswith(PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                user, password = "", ""
            # secrets.compare_digest로 타이밍 공격을 막는다 — user/password
            # 비교를 ==로 하면 앞글자가 맞을수록 응답이 미세하게 느려져서
            # 문자 단위로 비밀번호를 추측당할 수 있다.
            if secrets.compare_digest(user, SCREENLOG_USER) and secrets.compare_digest(password, SCREENLOG_PASSWORD):
                return await call_next(request)

        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="screenlog"'})


app.add_middleware(BasicAuthMiddleware)

STATIC_DIR = Path(__file__).parent / "static"

# 공유 디자인 토큰(tokens.css)을 세 페이지가 같이 불러 쓰려면 정적 파일 경로가
# 필요하다. 페이지 자체는 각각 라우트로 서빙하므로 여기선 css/이미지만 나간다.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 팀원용 녹화 프로그램(.dmg) 배포 페이지. 여기서 받아야 실제 브라우저
# 다운로드로 격리(quarantine) 속성이 붙어서, Gatekeeper 경고까지 포함한
# 진짜 최초 설치 경험을 그대로 재현한다.


DOWNLOAD_DIR = Path(__file__).parent.parent.parent / "distribution" / "mac"
if DOWNLOAD_DIR.exists():
    app.mount("/download", StaticFiles(directory=DOWNLOAD_DIR, html=True), name="download")

EXCERPT = 200   # 근거 본문은 이만큼만 내보낸다


class AskRequest(BaseModel):
    question: str
    conversation_id: str | None = None   # None이면 새 대화로 취급, 서버가 하나 발급한다
    # history 필드는 없다 — 팔로우업 맥락은 서버가 conversation_id로 자기
    # DB(chat_history)를 직접 읽어서 만든다. 예전엔 클라이언트가 최근 대화를
    # 통째로 매번 재전송했는데, 서버가 이미 저장해둔 걸 다시 보내는 중복이었다.


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/explore")
def explore():
    return FileResponse(STATIC_DIR / "explore.html")


@app.get("/api/stats")
def api_stats():
    """대시보드용 집계. 본문 없이 숫자와 앱 이름만 나간다."""
    return build_stats()


@app.get("/api/timeline/{date}")
def api_timeline(date: str):
    """하루치 리본 타임라인. date는 "YYYY-MM-DD"."""
    return build_timeline(date)


# 오늘 날짜는 summary_cache가 원래 캐시 대상이 아니다(하루 종일 자라니까).
# 그래서 /api/digest를 부를 때마다 오늘 몫만 매번 LLM을 새로 불러 4초 가까이
# 걸렸다 — 새로고침할 때마다 "데이터를 다시 로딩하는 것처럼" 느려지던 원인이
# 이거다. 디스크(summary_cache.sqlite)에는 안 넣고 이 프로세스 메모리에만
# 짧게(3분) 붙잡아둔다 — 오후 활동이 반영되기까지 최대 3분 지연되는 정도의
# 트레이드오프로 새로고침 지옥을 없앤다.
_TODAY_DIGEST_TTL = 180
_today_digest_cache = {}   # {date: (계산 시각, 텍스트)} — 자정 넘어가면 통째로 버린다


async def _digest_text(date):
    if summary_cache.is_cacheable_day(date):
        return await summarize_day(date)   # 지난 날은 summary_cache가 이미 빠르다

    now = time.monotonic()
    cached = _today_digest_cache.get(date)
    if cached and now - cached[0] < _TODAY_DIGEST_TTL:
        return cached[1]

    text = await summarize_day(date)
    _today_digest_cache.clear()   # 날짜가 바뀌면(자정) 어제 항목은 의미 없다
    _today_digest_cache[date] = (now, text)
    return text


@app.get("/api/digest")
async def api_digest(n: int = 5):
    """최근 n일의 하루 요약 — 홈 화면 "최근 기록" 피드용.

    summarize_day()를 그대로 불러서 헤더줄("[date(요일)]")만 떼어낸다 —
    지난 날이면 summary_cache가 이미 채워둔 걸 즉시 돌려주고, 오늘은
    _digest_text()의 짧은 메모리 캐시를 거친다.

    날짜 목록은 indexed_dates()로 따로 안 구한다 — 그것도 전체 이벤트를
    훑는 함수라 build_stats()가 이미 하는 스캔을 한 번 더 하게 된다.
    build_stats()는 캐시돼 있으니 그 결과의 dates를 그대로 재사용한다.
    """
    dates = build_stats()["dates"][-n:]

    async def one(date):
        text = await _digest_text(date)
        body = re.sub(r"^\[.*?\]\s*", "", text, count=1)
        return {"date": date, "weekday_kr": weekday_ko(date), "summary": body}

    days = await asyncio.gather(*[one(d) for d in dates])
    return {"days": list(reversed(days))}


@app.post("/api/ask")
async def api_ask(req: AskRequest):
    """질문 -> ask_auto()를 그대로 호출하고 결과를 JSON으로 돌려준다."""
    answer, plan, hits = await ask_auto(req.question)

    # 여러 날짜 경로(정리/비교/집계)는 이벤트 단위 근거가 없어서 hits가 None이다.
    hits_out = []
    for hit in hits or []:
        hits_out.append({
            "start": hit["start"],
            "app": hit["app"],
            "window": hit["window"],
            "distance": round(hit["distance"], 3),
            "excerpt": hit["text"][:EXCERPT],
            # 이 근거가 AI 도구 화면에서 왔는지 표시한다. 재귀 오염(AI가 화면에
            # 출력한 텍스트를 RAG가 사실로 되읽는 것) 때문에, 근거의 출처가
            # AI 앱이면 사용자가 한 번 더 의심할 수 있어야 한다.
            "ai_app": hit["app"] in AI_APPS,
        })

    return {
        "answer": answer,
        "plan": plan,
        "hits": hits_out,
    }



@app.post("/api/ask/stream")
async def api_ask_stream(req: AskRequest):
    # conversation_id가 없으면 이 질문이 새 대화의 시작이다 — 여기서 만들어서
    # 첫 이벤트로 클라이언트에 알려준다(사이드바에 표시될 id가 이거다).
    conv_id = req.conversation_id or chat_history.create_conversation(req.question)
    # 이번 질문을 저장하기 전에 먼저 히스토리를 읽는다 — 순서를 바꾸면
    # 방금 들어온 질문 자신이 "이전 대화"로 잡혀서 중복된다.
    history = chat_history.get_recent_turns(conv_id, limit=HISTORY_TURNS)
    chat_history.add_message(conv_id, "user", req.question)

    async def event_generator():
        yield f"data: {json.dumps({'type': 'conversation', 'id': conv_id}, ensure_ascii=False)}\n\n"
        answer_parts = []
        try:
            # stream_ask_auto()가 route()로 라우팅하고 intent(검색/정리/비교/집계)에
            # 따라 ask_auto()와 같은 함수로 답을 만든 뒤 plan/hits/token/done
            # 이벤트로 쪼개서 넘겨준다 — 여기서는 그걸 SSE로 포장하기만 한다.
            async for item in stream_ask_auto(req.question, history=history):
                if item["type"] == "hits":
                    hits_out = []
                    for hit in item["hits"]:
                        hits_out.append({
                            "start": hit["start"],
                            "app": hit["app"],
                            "window": hit["window"],
                            "distance": round(hit["distance"], 3),
                            "excerpt": hit["text"][:EXCERPT],
                            "ai_app": hit["app"] in AI_APPS,
                        })
                    payload = {"type": "hits", "hits": hits_out}
                else:
                    payload = item   # token이나 done은 그대로
                    if item["type"] == "token":
                        answer_parts.append(item["text"])
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            # 토큰을 이어붙이면 답변 전문이 된다(검색 intent는 여러 조각,
            # 정리/비교/집계는 한 조각 — ask.py 쪽 설계가 이미 그렇게 되어 있음).
            chat_history.add_message(conv_id, "assistant", "".join(answer_parts))
        except Exception as e:
            error_payload = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/conversations")
def api_conversations():
    """왼쪽 사이드바 "이전 질문들 모음" 목록. 최근 대화 순."""
    return {"conversations": chat_history.list_conversations()}


@app.get("/api/conversations/{conversation_id}")
def api_conversation_messages(conversation_id: str):
    """그 대화를 다시 열었을 때 채팅창에 재생할 메시지 전체."""
    return {"messages": chat_history.get_messages(conversation_id)}