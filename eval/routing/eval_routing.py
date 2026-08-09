"""라우팅만 평가한다. ask()는 부르지 않는다.

run_eval.py는 검색+생성까지 전부 돌려서 LLM 비용이 질문 수만큼 나간다.
여기서는 route()가 app/hour/date를 제대로 뽑는지만 본다 — 그래서 규칙이
잡은 질문은 LLM 비용이 0이고, 규칙이 못 잡아 LLM 폴백이 걸린 질문만 비용이 든다.

사용: uv run python eval/eval_routing.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # run_eval.py를 같은 폴더에서 import

from run_eval import resolve_expect_date            # noqa: E402
from screenlog.router import get_stats, reset_stats, route  # noqa: E402
from screenlog.source import LOCAL_TZ                # noqa: E402

QUESTIONS = Path(__file__).parent / "questions.jsonl"


def mark(ok):
    return "O" if ok else "X"


def main():
    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]

    # "어제"/"오늘" 같은 상대 날짜 질문의 정답을 지금 시점 기준으로 계산한다.
    today = datetime.now(LOCAL_TZ)
    for q in questions:
        q["expect_date"] = resolve_expect_date(q, today)

    reset_stats()

    print(f"{'qid':4} {'app':7} {'hour':6} {'date':6} 질문")
    print("-" * 70)

    n_app_ok = n_hour_ok = n_date_ok = 0
    for q in questions:
        plan = route(q["question"])

        app_ok = plan["app"] == q["expect_app"]
        hour_ok = plan["hour"] == q["expect_hour"]
        date_ok = plan["date"] == q["expect_date"]
        n_app_ok += app_ok
        n_hour_ok += hour_ok
        n_date_ok += date_ok

        print(f"{q['qid']:4} {mark(app_ok):7} {mark(hour_ok):6} {mark(date_ok):6} {q['question']}")

    n = len(questions)
    print()
    print(f"app 일치  {n_app_ok}/{n}")
    print(f"hour 일치 {n_hour_ok}/{n}")
    print(f"date 일치 {n_date_ok}/{n}")

    stats = get_stats()
    print()
    print(f"규칙으로 끝남 : {stats['rules']}/{n}")
    print(f"LLM까지 감    : {stats['llm']}/{n}  (LLM 비용이 발생한 질문 수)")


if __name__ == "__main__":
    main()
