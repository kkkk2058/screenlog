"""6. 웹 서비스 — 지금까지 만든 걸 브라우저에서 쓴다

로직은 새로 짜지 않는다. ask_auto()를 얇게 감싸기만 한다.

    GET  /          웹 페이지
    POST /api/ask   질문 -> 답변 + 라우팅 계획 + 근거

개인정보: 화면 기록엔 메신저 대화와 로그인 화면이 섞여 있다. 그래서 근거의
본문 전체를 내보내지 않고 짧은 발췌만 준다. 외부에 공개할 때는 127.0.0.1
바인딩과 인증을 먼저 붙여야 한다.

실행: uv run uvicorn screenlog.api:app --reload
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from screenlog.ask import ask_auto
from screenlog.config import AI_APPS
from screenlog.stats import build_stats, build_timeline
import json
from fastapi.responses import StreamingResponse

from screenlog.ask import stream_ask_auto   # ask_auto 옆에 추가



app = FastAPI(title="screenlog")

STATIC_DIR = Path(__file__).parent / "static"

# 팀원용 녹화 프로그램(.dmg) 배포 페이지. 여기서 받아야 실제 브라우저
# 다운로드로 격리(quarantine) 속성이 붙어서, Gatekeeper 경고까지 포함한
# 진짜 최초 설치 경험을 그대로 재현한다.


DOWNLOAD_DIR = Path(__file__).parent.parent.parent / "distribution" / "mac"
if DOWNLOAD_DIR.exists():
    app.mount("/download", StaticFiles(directory=DOWNLOAD_DIR, html=True), name="download")

EXCERPT = 200   # 근거 본문은 이만큼만 내보낸다


class AskRequest(BaseModel):
    question: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api/stats")
def api_stats():
    """대시보드용 집계. 본문 없이 숫자와 앱 이름만 나간다."""
    return build_stats()


@app.get("/api/timeline/{date}")
def api_timeline(date: str):
    """하루치 리본 타임라인. date는 "YYYY-MM-DD"."""
    return build_timeline(date)


@app.post("/api/ask")
def api_ask(req: AskRequest):
    """질문 -> ask_auto()를 그대로 호출하고 결과를 JSON으로 돌려준다."""
    answer, plan, hits = ask_auto(req.question)

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
def api_ask_stream(req: AskRequest):
    def event_generator():
        try:
            # stream_ask_auto()가 route()로 라우팅하고 intent(검색/정리/비교/집계)에
            # 따라 ask_auto()와 같은 함수로 답을 만든 뒤 plan/hits/token/done
            # 이벤트로 쪼개서 넘겨준다 — 여기서는 그걸 SSE로 포장하기만 한다.
            for item in stream_ask_auto(req.question):
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
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as e:
            error_payload = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")