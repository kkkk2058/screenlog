"""검색 k(RETRIEVE_K) 스윕 — Phase 3.

Phase 2(chunk_sweep.py)가 고른 JACCARD_MIN 승자 하나로 컬렉션을 한 번만 만들고,
k=5/10/20/30에서 recall이 어떻게 변하는지 본다. RETRIEVE_K(현재 10)가
"더 늘려도 recall이 안 느는 지점" 근처인지 확인하는 게 목적이다 — 그 지점을
넘어서까지 k를 키우면 프롬프트만 커지고 recall 이득은 없다(ask.py 검색
로직의 CONTEXT_CHARS_PER_HIT 상한과 같은 이유).

chunk_sweep.py의 build_collection()/search()/recall_at_k()를 그대로 가져다
쓴다 — 컬렉션 만드는 법(원본 DB 폴백, AI_APPS 제외, 인메모리)과 채점 방식
((date,app,window) 매칭)이 여기서도 똑같이 필요하고, 두 번 짜면 한쪽만
고치고 한쪽은 안 고치는 실수가 난다.

JACCARD_MIN은 필수 인자다 — Phase 2 결과 없이 아무 값이나 넣고 돌리면
"이번 k 스윕이 어느 청킹 기준으로 잰 건지"가 불분명해진다.

사용:
    uv run python eval/k_sweep.py --jaccard-min 0.3
    uv run python eval/k_sweep.py --jaccard-min 0.3 --dates 2026-07-26,2026-07-30
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chunk_sweep import QUESTIONS, build_collection, recall_at_k, search  # noqa: E402

K_VALUES = [5, 10, 20, 30]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jaccard-min", type=float, required=True,
                        help="Phase 2에서 고른 JACCARD_MIN 값")
    parser.add_argument("--dates", default=None, help="YYYY-MM-DD,YYYY-MM-DD,... (생략하면 골든셋 전체가 필요로 하는 날짜)")
    args = parser.parse_args()

    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]
    all_needed = sorted({key["date"] for q in questions for key in q.get("expect_keys", [])})

    if args.dates:
        needed_dates = args.dates.split(",")
        questions = [q for q in questions
                     if q.get("expect_keys") and all(k["date"] in needed_dates for k in q["expect_keys"])]
        print(f"날짜 범위 축소: {needed_dates} ({len(questions)}개 질문만 채점)")
    else:
        needed_dates = all_needed

    print(f"JACCARD_MIN={args.jaccard_min} 로 컬렉션 생성 중 ({needed_dates})...")
    col = build_collection(args.jaccard_min, needed_dates)
    print(f"컬렉션 완성: {col.count()}개 이벤트\n")

    print(f"{'qid':4} " + " ".join(f"r@{k:<3}" for k in K_VALUES) + "  질문")
    print("-" * 90)

    sums = {k: 0.0 for k in K_VALUES}
    n_scored = 0
    for q in questions:
        expect_keys = q.get("expect_keys", [])
        if not expect_keys:
            continue
        hits = search(col, q["question"], k=max(K_VALUES), app=q.get("app"), site=q.get("site"),
                      dates=q.get("dates"))
        n_scored += 1
        recalls = {k: recall_at_k(hits, expect_keys, k) or 0.0 for k in K_VALUES}
        for k in K_VALUES:
            sums[k] += recalls[k]
        row = " ".join(f"{recalls[k]:.2f} " for k in K_VALUES)
        print(f"{q['qid']:4} {row}  {q['question'][:40]}")

    print("-" * 90)
    print("평균  " + " ".join(f"{sums[k] / n_scored:.2f} " for k in K_VALUES))
    print("\n각 k에서 평균이 이전 k보다 얼마나 더 느는지(수확체감 확인용):")
    prev = None
    for k in K_VALUES:
        avg = sums[k] / n_scored
        if prev is not None:
            print(f"  k={prev[0]}->{k}: {prev[1]:.2f} -> {avg:.2f} ({avg - prev[1]:+.2f})")
        prev = (k, avg)


if __name__ == "__main__":
    main()
