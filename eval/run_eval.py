"""질문 세트를 돌려서 [검색 결과 / 답변]을 전부 기록한다.

여기서 답변의 좋고 나쁨을 채점하지는 않는다. 목적은 **같은 질문 세트로 설정을
바꿔가며 비교**하는 것이다. 터미널에만 뜨는 숫자는 다음 날이면 사라진다.

자동으로 잴 수 있는 것만 잰다:
    app_hit    앱을 지정한 질문에서, 그 앱이 근거에 들어왔나
    date_hit   날짜를 지정한 질문에서, 그 날짜가 근거에 들어왔나
    hour_hit   시각을 지정한 질문에서, 그 시각이 근거에 들어왔나
    n_apps     근거가 몇 개 앱에서 왔나 (하루 요약이 한 앱에 쏠렸는지)
    says_none  "기록에 없습니다"라고 답했나 (데이터가 있는데 그러면 실패)

답변 텍스트 자체는 파일에 남겨서 사람이 읽고 판단한다.

사용:
    uv run python eval/run_eval.py            바닐라 — 필터 없이 순수 벡터 검색
    uv run python eval/run_eval.py --filter    2단계 — 정답 app/hour/date를 필터로 직접 넘김
    uv run python eval/run_eval.py --route     3단계 — router.route()가 질문에서 뽑은 필터로 넘김

--filter는 사람이 정답을 직접 넣은 것이라 "검색이 맞는가"만 본다.
--route는 라우팅이 스스로 뽑은 값을 쓰므로 "라우팅이 맞는가"까지 같이 본다.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from screenlog.ask import ask                      # noqa: E402
from screenlog.config import RETRIEVE_K            # noqa: E402
from screenlog.index import get_collection         # noqa: E402
from screenlog.router import route                 # noqa: E402

QUESTIONS = Path(__file__).parent / "questions.jsonl"
RUNS_DIR = Path(__file__).parent / "runs"

EXCERPT = 300      # 근거 원문은 화면 캡처라 길고 개인정보가 섞인다. 판단할 만큼만 남긴다.


def score(question, hits, answer):
    """자동으로 잴 수 있는 것만 잰다. 못 재는 항목은 None."""
    apps = [h["app"] for h in hits]
    dates = [h["date"] for h in hits]
    hours = [h["hour"] for h in hits]

    result = {
        "n_apps": len(set(apps)),
        "says_none": "기록에 없" in answer,
        "app_hit": None,
        "app_rank": None,
        "date_hit": None,
        "hour_hit": None,
    }

    if question["expect_app"]:
        want = question["expect_app"]
        result["app_hit"] = want in apps
        if want in apps:
            result["app_rank"] = apps.index(want) + 1   # 몇 번째 근거인지 (1등이 가장 유사)

    if question["expect_date"]:
        result["date_hit"] = question["expect_date"] in dates

    if question["expect_hour"] is not None:
        result["hour_hit"] = question["expect_hour"] in hours

    return result


def mark(value):
    """True/False/None -> 표에 넣을 한 글자."""
    if value is None:
        return "-"
    return "O" if value else "X"


def score_route(question, plan):
    """라우팅이 뽑은 app/hour/date가 정답(expect_*)과 같은지.

    검색 결과가 아니라 route() 함수 자체의 정확도를 본다. 이게 없으면
    "답이 틀렸을 때 라우팅이 틀렸나 검색이 틀렸나"를 구분할 수 없다.
    """
    return {
        "app_ok": plan["app"] == question["expect_app"],
        "hour_ok": plan["hour"] == question["expect_hour"],
        "date_ok": plan["date"] == question["expect_date"],
    }


def main():
    use_route = "--route" in sys.argv
    use_filter = "--filter" in sys.argv

    if use_route:
        stage = "routed"
    elif use_filter:
        stage = "meta_filter"
    else:
        stage = "vanilla"

    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]

    col = get_collection()
    print(f"단계={stage} / 코퍼스 {col.count()}개 이벤트 / 질문 {len(questions)}개 / k={RETRIEVE_K}\n")

    header = f"{'qid':4} {'분류':11} {'앱':3} {'순위':4} {'날짜':4} {'시각':4} {'앱수':3} {'없다':4} 질문"
    print(header)
    print("-" * 96)

    results = []
    for question in questions:
        route_plan = None
        route_result = None

        if use_route:
            # 정답을 넘기지 않는다. route()가 질문만 보고 뽑은 값을 그대로 쓴다.
            route_plan = route(question["question"])
            route_result = score_route(question, route_plan)
            answer, hits = ask(
                question["question"],
                app=route_plan["app"],
                hour=route_plan["hour"],
                date=route_plan["date"],
            )
        elif use_filter:
            # 정답 필터를 그대로 넘긴다. "필터가 맞게 주어지면 검색이 되는가"만 본다.
            answer, hits = ask(
                question["question"],
                app=question["expect_app"],
                hour=question["expect_hour"],
                date=question["expect_date"],
            )
        else:
            answer, hits = ask(question["question"])

        s = score(question, hits, answer)

        results.append({
            **question,
            "route_plan": route_plan,
            "route_result": route_result,
            "score": s,
            "answer": answer,
            "hits": [
                {
                    "app": h["app"],
                    "window": h["window"],
                    "start": h["start"],
                    "date": h["date"],
                    "hour": h["hour"],
                    "distance": round(h["distance"], 4),
                    "excerpt": h["text"][:EXCERPT],
                }
                for h in hits
            ],
        })

        print(f"{question['qid']:4} {question['category']:11} "
              f"{mark(s['app_hit']):3} {str(s['app_rank'] or '-'):4} "
              f"{mark(s['date_hit']):4} {mark(s['hour_hit']):4} "
              f"{s['n_apps']:3} {mark(s['says_none']):4} {question['question']}")

    if use_route:
        print("\n--- 라우팅 정확도 (route()가 뽑은 값 vs 정답) ---")
        n = len(results)
        app_ok = sum(1 for r in results if r["route_result"]["app_ok"])
        hour_ok = sum(1 for r in results if r["route_result"]["hour_ok"])
        date_ok = sum(1 for r in results if r["route_result"]["date_ok"])
        print(f"app 일치  {app_ok}/{n}")
        print(f"hour 일치 {hour_ok}/{n}")
        print(f"date 일치 {date_ok}/{n}")

    # 분류별 집계
    print("\n--- 분류별 ---")
    categories = {}
    for r in results:
        categories.setdefault(r["category"], []).append(r)

    for name, group in categories.items():
        checked = [r for r in group if r["score"]["app_hit"] is not None]
        hit = [r for r in checked if r["score"]["app_hit"]]
        date_checked = [r for r in group if r["score"]["date_hit"] is not None]
        date_hit = [r for r in date_checked if r["score"]["date_hit"]]
        none_said = [r for r in group if r["score"]["says_none"]]

        line = f"{name:11} 질문 {len(group)}개"
        if checked:
            line += f" | 앱 적중 {len(hit)}/{len(checked)}"
        if date_checked:
            line += f" | 날짜 적중 {len(date_hit)}/{len(date_checked)}"
        if none_said:
            line += f" | '기록에 없다' {len(none_said)}개"
        print(line)

    RUNS_DIR.mkdir(exist_ok=True)
    out = RUNS_DIR / f"{stage}_{datetime.now():%Y%m%d_%H%M}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        meta = {
            "_meta": True,
            "stage": stage,
            "run_at": datetime.now().isoformat(timespec="seconds"),
            "k": RETRIEVE_K,
            "n_events": col.count(),
        }
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
