"""기능 5종의 실제 답변을 받아 JSON으로 떨군다.

api.py가 부르는 것과 같은 진입점(ask_auto)을 그대로 쓴다 — HTTP만 안 거칠 뿐
라우팅·검색·요약·에이전트 경로는 전부 동일하다.
"""
import asyncio, json, time
from pathlib import Path

from screenlog.config import USE_LANGGRAPH
from screenlog_langgraph.agent import ask_auto
from screenlog.router import route

OUT = Path(__file__).parent / "answers.json"

QUESTIONS = [
    ("검색",   "8월 4일에 유튜브에서 뭐 봤어?"),
    ("정리",   "8월 5일 하루 정리해줘"),
    ("비교",   "8월 3일이랑 8월 4일 중 언제가 더 바빴어?"),
    ("인수인계", "8월 3일부터 8월 5일까지 한 작업 인수인계 문서로 정리해줘"),
    ("슬랙",   "방금 그거 슬랙에 올릴 초안으로 만들어줘"),
]


async def main():
    print(f"USE_LANGGRAPH={USE_LANGGRAPH}")
    results, history = [], []
    for kind, q in QUESTIONS:
        t0 = time.perf_counter()
        try:
            plan = await route(q, history=history)
        except Exception as e:
            plan = f"(route 실패: {e})"
        answer = await ask_auto(q, history=history)
        dt = time.perf_counter() - t0

        print(f"\n{'='*70}\n[{kind}] {q}   ({dt:.1f}s)\n{'-'*70}")
        print(f"라우팅: {plan}")
        print(f"{'-'*70}\n{answer}")

        results.append({"kind": kind, "question": q, "plan": str(plan),
                        "answer": answer, "seconds": round(dt, 1)})
        history.append({"question": q, "answer": answer})

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n\n✓ {OUT}")


asyncio.run(main())
