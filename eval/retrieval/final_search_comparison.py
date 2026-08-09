"""검색 전략 최종 비교 — Phase 4 전체를 recall/precision/F1로 통일해서 다시 잰다.

지금까지 BM25/하이브리드/리랭커/HyDE를 따로따로 실험했는데, 스크립트마다 잰
지표가 달랐다(어떤 건 recall만, 어떤 건 recall+precision+F1). 리포트에 넣기
전에 **7개 방법을 한 스크립트에서 같은 채점 함수로 통일**해서 다시 잰다 —
안 그러면 스크립트 간 미묘한 구현 차이(RRF 상수, k 상한 등)가 섞여
비교가 왜곡될 수 있다.

방법 7개:
    dense     프로덕션 방식 (BGE-M3 벡터 검색)
    bm25      형태소 분석 + BM25Okapi (쿼리 확장 없음)
    bm25x     bm25 + screenpipe식 접두어/부분 매칭 쿼리 확장
    hybrid    dense + bm25 (RRF)
    hybridx   dense + bm25x (RRF)
    rerank    dense top-20을 cross-encoder(bge-reranker-v2-m3)로 재정렬
    hyde      질문 대신 LLM이 지어낸 가상 답변을 임베딩해서 검색

사용:
    uv run python eval/final_search_comparison.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sentence_transformers import CrossEncoder   # noqa: E402

from hyde_sweep import dense_search_with_text, hyde_expand  # noqa: E402
from label_retrieval import search_with_ids       # noqa: E402
from search_strategy_sweep import (                # noqa: E402
    bm25_search, build_bm25_cache, build_vocab, rrf_merge,
)

QUESTIONS = Path(__file__).parent / "retrieval_questions.jsonl"
RUNS_DIR = Path(__file__).parent / "runs"
K_VALUES = [5, 10]
POOL = 20  # dense/bm25에서 넉넉히 가져와서 rerank/hybrid 재료로 쓸 개수

_reranker = None


def get_reranker():
    global _reranker
    if _reranker is None:
        print("리랭커 모델 로딩 중 (BAAI/bge-reranker-v2-m3)...")
        _reranker = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
    return _reranker


def rerank(question, hits):
    if not hits:
        return hits
    pairs = [(question, h["text"]) for h in hits]
    scores = get_reranker().predict(pairs)
    order = sorted(range(len(hits)), key=lambda i: -scores[i])
    return [hits[i] for i in order]


def metrics_at_k(hits, expect_keys, k):
    top = hits[:k]
    top_keys = {(h.get("date"), h.get("app"), h.get("window")) for h in top}
    expect = {(e["date"], e["app"], e["window"]) for e in expect_keys}
    if not expect or not top_keys:
        return 0.0, 0.0, 0.0
    tp = len(top_keys & expect)
    recall = tp / len(expect)
    precision = tp / len(top_keys)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return recall, precision, f1


async def main():
    id_to_tokens = build_bm25_cache()
    vocab = build_vocab(id_to_tokens)
    get_reranker()

    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]

    methods = ["dense", "bm25", "bm25x", "hybrid", "hybridx", "rerank", "hyde"]
    metric_names = ["R", "P", "F1"]
    sums = {m: {k: {mt: 0.0 for mt in metric_names} for k in K_VALUES} for m in methods}
    n_scored = {m: 0 for m in methods}

    results = []
    started = time.monotonic()
    for q in questions:
        expect_keys = q.get("expect_keys", [])
        if not expect_keys:
            continue

        app, site, dates = q.get("app"), q.get("site"), q.get("dates")

        dense_hits = search_with_ids(q["question"], k=POOL, app=app, site=site, dates=dates)
        bm25_hits = bm25_search(q["question"], k=POOL, id_to_tokens=id_to_tokens, app=app, site=site, dates=dates)
        bm25x_hits = bm25_search(q["question"], k=POOL, id_to_tokens=id_to_tokens, app=app, site=site,
                                 dates=dates, vocab=vocab)
        hybrid_hits = rrf_merge(dense_hits, bm25_hits, k=POOL)
        hybridx_hits = rrf_merge(dense_hits, bm25x_hits, k=POOL)
        rerank_hits = rerank(q["question"], dense_hits)
        hyde_text = await hyde_expand(q["question"])
        hyde_hits = dense_search_with_text(hyde_text, k=POOL, app=app, site=site, dates=dates)

        per_method = {}
        for m, hits in zip(methods, [dense_hits, bm25_hits, bm25x_hits, hybrid_hits, hybridx_hits,
                                     rerank_hits, hyde_hits]):
            per_method[m] = {}
            n_scored[m] += 1
            for k in K_VALUES:
                r, p, f1 = metrics_at_k(hits, expect_keys, k)
                per_method[m][k] = {"R": round(r, 3), "P": round(p, 3), "F1": round(f1, 3)}
                for mt, val in zip(metric_names, [r, p, f1]):
                    sums[m][k][mt] += val

        results.append({"qid": q["qid"], "question": q["question"], **per_method})
        print(f"{q['qid']} 완료 ({len(results)}/{len(questions)})")

    print(f"\n총 소요 {time.monotonic() - started:.1f}초\n")

    # 요약 표
    print(f"{'method':10} " + "  ".join(f"{k}: R/P/F1" for k in K_VALUES))
    print("-" * 60)
    summary = {}
    for m in methods:
        n = n_scored[m]
        row = f"{m:10} "
        summary[m] = {}
        for k in K_VALUES:
            r = sums[m][k]["R"] / n
            p = sums[m][k]["P"] / n
            f1 = sums[m][k]["F1"] / n
            summary[m][k] = {"R": round(r, 3), "P": round(p, 3), "F1": round(f1, 3)}
            row += f"  {r:.2f}/{p:.2f}/{f1:.2f}"
        print(row)

    RUNS_DIR.mkdir(exist_ok=True)
    out_path = RUNS_DIR / f"final_search_comparison_{time.strftime('%Y%m%d_%H%M')}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({"summary": summary, "per_question": results}, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
