"""대시보드가 부르는 API 응답을 그대로 덤프한다.

HTTP를 안 거칠 뿐, api.py의 엔드포인트 함수를 직접 호출하므로 응답 모양은 동일하다.
여기에 capture_answers.py가 받아둔 실제 답변을 합쳐 fixtures.json 하나로 만든다.
"""
import asyncio, json, inspect
from pathlib import Path

from screenlog.api import api_stats, api_digest, api_timeline
from screenlog.ask import search
from screenlog.config import RETRIEVE_K

HERE = Path(__file__).parent
ANSWERS = json.loads((HERE / "answers.json").read_text())


async def main():
    stats = api_stats()
    digest = await api_digest(n=5)
    last_date = stats["dates"][-1]
    timeline = api_timeline(last_date)

    # 검색 질문만 근거가 붙는다 — 정리/비교/에이전트 경로는 원래 hits가 빈 배열이다.
    turns = []
    for a in ANSWERS:
        hits = []
        if a["kind"] == "검색":
            raw = search("유튜브에서 본 영상", k=RETRIEVE_K, dates=["2026-08-04"])
            for h in raw:
                hits.append({k: h.get(k) for k in
                             ("app", "window", "start", "distance", "excerpt", "ai_app", "url")})
        turns.append({**a, "hits": hits})

    out = {"stats": stats, "digest": digest, "timeline": timeline,
           "last_date": last_date, "turns": turns}
    (HERE / "fixtures.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    print("stats keys:", list(stats)[:10])
    print("dates:", stats["dates"][:3], "...", stats["dates"][-2:])
    print("digest[0] keys:", list(digest[0]) if isinstance(digest, list) and digest else type(digest))
    print("hits:", len(turns[0]["hits"]), "→ 첫 건:", json.dumps(turns[0]["hits"][:1], ensure_ascii=False)[:300])


asyncio.run(main())
