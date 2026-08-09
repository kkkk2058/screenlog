"""리랭커(cross-encoder) 실험 — Phase 4-2.

dense 검색은 recall@10=1.00이라 "정답을 아예 못 찾는" 문제는 거의 없다. 낮은
점수(r08, r15, r24 등)는 전부 "top-10 안에는 있는데 top-5 밖으로 밀린다"는
순위 문제였다 — 이건 정확히 리랭커가 잘하는 일이다.

dense가 top-N(넉넉하게)을 가져오면, 그 N개를 질문-문서 쌍으로 cross-encoder에
넣어 다시 채점한다. bi-encoder(BGE-M3, 지금 쓰는 임베딩)는 질문과 문서를 각각
독립적으로 벡터화해서 유사도를 재는 반면, cross-encoder는 질문+문서를 한 번에
모델에 같이 넣어서 "이 문서가 이 질문에 얼마나 맞는지"를 직접 판단한다 —
느리지만(문서 하나하나 모델을 다시 돌려야 함) 훨씬 정밀하다. 그래서 "일단
빠르게 top-N을 넓게 가져오고(dense), 그 안에서만 정밀하게 다시 줄 세운다
(rerank)"는 2단계 구조로 쓴다 — 전체 코퍼스에 cross-encoder를 바로 돌리면
느려서 실용적이지 않다.

사용:
    uv run python eval/rerank_sweep.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sentence_transformers import CrossEncoder  # noqa: E402

from label_retrieval import search_with_ids     # noqa: E402

QUESTIONS = Path(__file__).parent / "retrieval_questions.jsonl"
RUNS_DIR = Path(__file__).parent / "runs"
RERANK_POOL = 20   # dense에서 이만큼 넓게 가져온 뒤 리랭커로 다시 줄 세운다
K_VALUES = [5, 10]

_reranker = None


def get_reranker():
    global _reranker
    if _reranker is None:
        print("리랭커 모델 로딩 중 (BAAI/bge-reranker-v2-m3, 최초 1회)...")
        _reranker = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
    return _reranker


def rerank(question, hits):
    """dense hits(딕셔너리 리스트, 'text' 포함)를 cross-encoder 점수로 재정렬한다."""
    if not hits:
        return hits
    pairs = [(question, h["text"]) for h in hits]
    scores = get_reranker().predict(pairs)
    order = sorted(range(len(hits)), key=lambda i: -scores[i])
    return [hits[i] for i in order]


def recall_at_k(hits, expect_keys, k):
    top_keys = {(h.get("date"), h.get("app"), h.get("window")) for h in hits[:k]}
    expect = {(e["date"], e["app"], e["window"]) for e in expect_keys}
    if not expect:
        return None
    return len(top_keys & expect) / len(expect)


def main():
    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]
    get_reranker()  # 첫 로딩을 미리 끝내서, 아래 루프 타이밍이 안 헷갈리게 한다

    methods = ["dense", "rerank"]
    sums = {m: {k: 0.0 for k in K_VALUES} for m in methods}

    header = "qid  " + "".join(f"{m}@{k:<3}".ljust(9) for m in methods for k in K_VALUES)
    print(header)
    print("-" * len(header))

    results = []
    started = time.monotonic()
    for q in questions:
        expect_keys = q.get("expect_keys", [])
        if not expect_keys:
            continue

        dense_hits = search_with_ids(q["question"], k=RERANK_POOL, app=q.get("app"), site=q.get("site"),
                                     dates=q.get("dates"))
        reranked_hits = rerank(q["question"], dense_hits)

        row = f"{q['qid']:4} "
        per_method = {}
        for m, hits in zip(methods, [dense_hits, reranked_hits]):
            per_method[m] = {}
            for k in K_VALUES:
                r = recall_at_k(hits, expect_keys, k) or 0.0
                per_method[m][k] = r
                sums[m][k] += r
                row += f"{r:.2f}".ljust(9)
        print(row)
        results.append({"qid": q["qid"], "question": q["question"], **per_method})

    n = len(results)
    print("-" * len(header))
    footer = "평균 "
    for m in methods:
        for k in K_VALUES:
            footer += f"{sums[m][k] / n:.2f}".ljust(9)
    print(footer)
    print(f"\n총 소요 {time.monotonic() - started:.1f}초 ({n}문항, 질문당 {RERANK_POOL}개 재정렬)")

    RUNS_DIR.mkdir(exist_ok=True)
    out_path = RUNS_DIR / f"rerank_{time.strftime('%Y%m%d_%H%M')}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"결과 저장: {out_path}")


if __name__ == "__main__":
    main()
