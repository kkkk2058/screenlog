"""검색 자체의 recall을 잰다 — Phase 1.

run_eval.py/eval_routing.py는 "라우팅이 app/date/hour을 맞게 뽑았는가"까지만
본다. 이 스크립트는 그보다 한 단계 아래 — retrieval_questions.jsonl(Phase 0
골든셋)에 적힌 "정답 이벤트"가 search()의 top-k 안에 실제로 들어오는지를 잰다.

recall@k = 그 질문의 정답 이벤트 중 top-k 안에 들어온 비율. 정답이 여러 개인
질문(예: 궁합 지수 스크린샷이 여러 번 찍힌 경우)은 일부만 들어와도 부분 점수를
받는다.

k를 하나만 재지 않고 5/10/20 세 값을 한 번에 본다 — k=20으로 한 번만 검색해서
5/10/20으로 슬라이싱하는 것뿐이라 비용이 따로 안 든다(청소년 chroma 쿼리는
n_results가 커도 임베딩 계산은 질문당 1번). 이후 Phase 3(k 스윕)에서 어느
k가 적절한지 판단할 때 이 결과를 그대로 재사용할 수 있다.

라벨링 스크립트(label_retrieval.py)가 쓰던 search_with_ids()를 그대로 가져다
쓴다 — AI_APPS 재귀 오염 제외, 화이트리스트 밖 도메인 raw 매칭 같은 버그를
그때 고쳐뒀는데 여기서 또 따로 짜면 같은 실수를 반복할 위험이 있다.

사용:
    uv run python eval/eval_retrieval.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # label_retrieval.py를 같은 폴더에서 import

from label_retrieval import search_with_ids  # noqa: E402

QUESTIONS = Path(__file__).parent / "retrieval_questions.jsonl"
RUNS_DIR = Path(__file__).parent / "runs"
K_VALUES = [5, 6, 7, 8, 9, 10, 20]


def recall_at_k(retrieved_ids, expect_ids, k):
    top_k = set(retrieved_ids[:k])
    hit = len(top_k & set(expect_ids))
    return hit / len(expect_ids)


def main():
    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]
    max_k = max(K_VALUES)

    print(f"{'qid':4} " + " ".join(f"r@{k:<3}" for k in K_VALUES) + "  질문")
    print("-" * 90)

    results = []
    sums = {k: 0.0 for k in K_VALUES}
    for q in questions:
        hits = search_with_ids(q["question"], k=max_k, app=q.get("app"), site=q.get("site"),
                                dates=q.get("dates"))
        retrieved_ids = [h["id"] for h in hits]
        recalls = {k: recall_at_k(retrieved_ids, q["expect_event_ids"], k) for k in K_VALUES}
        for k in K_VALUES:
            sums[k] += recalls[k]

        row = " ".join(f"{recalls[k]:.2f} " for k in K_VALUES)
        print(f"{q['qid']:4} {row}  {q['question'][:45]}")

        results.append({
            "qid": q["qid"], "question": q["question"], "expect_event_ids": q["expect_event_ids"],
            "retrieved_ids": retrieved_ids, "recall": recalls,
        })

    n = len(questions)
    print("-" * 90)
    print("평균  " + " ".join(f"{sums[k] / n:.2f} " for k in K_VALUES))

    RUNS_DIR.mkdir(exist_ok=True)
    out_path = RUNS_DIR / f"retrieval_baseline_{datetime.now().strftime('%Y%m%d_%H%M')}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
