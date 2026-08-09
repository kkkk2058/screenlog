"""검색 전략 비교 — Phase 4 (dense vs BM25 vs 하이브리드).

지금 프로덕션은 BGE-M3 dense 벡터 검색만 쓴다. eval/routing/REPORT.md에 이미 남아있는
실측 사례(q14, "chromadb"를 "Chrome"으로 오인식)처럼 형태소가 비슷한 고유명사는
dense 임베딩이 약할 수 있다 — BM25(키워드 빈도 기반) 검색을 더하면 이런 케이스를
보완하는지 보는 게 목적이다.

세 방식을 비교한다:
    dense   지금 프로덕션 방식 (label_retrieval.search_with_ids 그대로 재사용)
    bm25    형태소 분석(kiwipiepy) 후 BM25Okapi로 순위
    hybrid  RRF(Reciprocal Rank Fusion)로 두 순위를 합침

BM25는 필터(app/site/dates)가 걸린 후보군 안에서만 순위를 매긴다 — dense와
공정하게 비교하려면 같은 후보 풀에서 경쟁해야 한다. 전체 코퍼스(26,408개)를
형태소 분석하는 데 ~2.5분이 걸려서, 한 번 토큰화한 뒤 eval/runs/.bm25_cache.pkl에
캐싱하고 재사용한다(문서 내용이 안 바뀌는 한 계속 유효).

사용:
    uv run python eval/search_strategy_sweep.py
"""

import json
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kiwipiepy import Kiwi                      # noqa: E402
from rank_bm25 import BM25Okapi                 # noqa: E402

from label_retrieval import _matches_site, search_with_ids  # noqa: E402
from screenlog.ask import build_where           # noqa: E402
from screenlog.config import AI_APPS            # noqa: E402
from screenlog.index import get_collection      # noqa: E402

QUESTIONS = Path(__file__).parent / "retrieval_questions.jsonl"
RUNS_DIR = Path(__file__).parent / "runs"
CACHE = RUNS_DIR / ".bm25_cache.pkl"
K_VALUES = [5, 10]
RRF_C = 60  # RRF 공식의 상수. 값이 클수록 하위 순위 문서의 기여도가 완만해진다(관용적으로 60을 씀).

_kiwi = Kiwi()


def tokenize(text):
    return [t.form for t in _kiwi.tokenize(text[:2000])]  # MAX_EVENT_CHARS(2000)만큼만


def build_bm25_cache():
    """전체 코퍼스를 형태소 분석해서 {id: tokens} 캐시를 만들거나 불러온다."""
    if CACHE.exists():
        with CACHE.open("rb") as f:
            return pickle.load(f)

    print("전체 코퍼스 형태소 분석 중 (최초 1회, ~2~3분)...")
    col = get_collection()
    all_docs = col.get(include=["documents"])
    started = time.monotonic()
    id_to_tokens = {i: tokenize(d) for i, d in zip(all_docs["ids"], all_docs["documents"])}
    print(f"완료: {len(id_to_tokens)}개, {time.monotonic() - started:.1f}초")

    RUNS_DIR.mkdir(exist_ok=True)
    with CACHE.open("wb") as f:
        pickle.dump(id_to_tokens, f)
    return id_to_tokens


def build_vocab(id_to_tokens):
    vocab = set()
    for tokens in id_to_tokens.values():
        vocab.update(tokens)
    return vocab


def expand_query_tokens(query_tokens, vocab, min_len=2):
    """screenpipe의 expand_search_query()를 흉내낸다 — OCR이 붙여 쓴 단어나
    형태소 경계가 애매한 경우를 잡기 위해, 쿼리 토큰과 접두어/부분 문자열
    관계인 코퍼스 어휘를 찾아 쿼리에 같이 넣는다(OR 확장과 같은 효과 — BM25는
    점수를 토큰별로 더하므로, 추가된 토큰이 있는 문서만 그만큼 점수가 붙는다).

    screenpipe(Rust, FTS5)는 SQL 접두어 매칭(`"word"*`)을 문자 그대로 쓸 수
    있지만, rank_bm25는 그런 연산자가 없어서 우리가 직접 어휘집합을 순회하며
    흉내낸다."""
    expanded = list(query_tokens)
    for qt in query_tokens:
        if len(qt) < min_len:
            continue
        for v in vocab:
            if v != qt and (v.startswith(qt) or qt.startswith(v)):
                expanded.append(v)
    return expanded


def bm25_search(question, k, id_to_tokens, app=None, site=None, dates=None, vocab=None):
    """dense search_with_ids()와 같은 필터(app/site/dates, AI_APPS 제외)를 적용한
    후보 풀 안에서 BM25로 순위를 매긴다. vocab을 주면 쿼리 토큰을 접두어/부분
    매칭으로 확장한다(screenpipe의 expand_search_query() 흉내, 위 참고) — 안
    주면 형태소 그대로만 쓰는 날것의 BM25다."""
    col = get_collection()
    where = build_where(app, None, None, dates)
    candidates = col.get(where=where, include=["metadatas"]) if where else col.get(include=["metadatas"])

    exclude_ai_apps = app not in AI_APPS
    ids, metas = [], []
    for i, m in zip(candidates["ids"], candidates["metadatas"]):
        if site and not _matches_site(site, m):
            continue
        if exclude_ai_apps and m["app"] in AI_APPS:
            continue
        ids.append(i)
        metas.append(m)

    if not ids:
        return []

    corpus_tokens = [id_to_tokens.get(i, []) for i in ids]
    bm25 = BM25Okapi(corpus_tokens)
    query_tokens = tokenize(question)
    if vocab is not None:
        query_tokens = expand_query_tokens(query_tokens, vocab)
    scores = bm25.get_scores(query_tokens)
    ranked_idx = sorted(range(len(ids)), key=lambda i: -scores[i])[:k]
    return [dict(metas[i], id=ids[i]) for i in ranked_idx]


def rrf_merge(dense_hits, bm25_hits, k):
    """두 순위 목록을 RRF로 합친다. score = sum(1 / (RRF_C + rank))."""
    scores = {}
    pool = {}
    for rank, h in enumerate(dense_hits, 1):
        scores[h["id"]] = scores.get(h["id"], 0) + 1 / (RRF_C + rank)
        pool[h["id"]] = h
    for rank, h in enumerate(bm25_hits, 1):
        scores[h["id"]] = scores.get(h["id"], 0) + 1 / (RRF_C + rank)
        pool[h["id"]] = h
    ranked_ids = sorted(scores, key=lambda i: -scores[i])[:k]
    return [pool[i] for i in ranked_ids]


def recall_at_k(hits, expect_keys, k):
    top_keys = {(h.get("date"), h.get("app"), h.get("window")) for h in hits[:k]}
    expect = {(e["date"], e["app"], e["window"]) for e in expect_keys}
    if not expect:
        return None
    return len(top_keys & expect) / len(expect)


def main():
    id_to_tokens = build_bm25_cache()
    vocab = build_vocab(id_to_tokens)
    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]
    max_k = max(K_VALUES)

    methods = ["dense", "bm25", "bm25x", "hybridx"]
    sums = {m: {k: 0.0 for k in K_VALUES} for m in methods}

    header = "qid  " + "".join(f"{m}@{k:<3}".ljust(9) for m in methods for k in K_VALUES)
    print(header)
    print("-" * len(header))

    results = []
    for q in questions:
        expect_keys = q.get("expect_keys", [])
        if not expect_keys:
            continue

        dense_hits = search_with_ids(q["question"], k=max_k, app=q.get("app"), site=q.get("site"),
                                     dates=q.get("dates"))
        bm25_hits = bm25_search(q["question"], k=max_k, id_to_tokens=id_to_tokens, app=q.get("app"),
                                site=q.get("site"), dates=q.get("dates"))
        # bm25x: screenpipe식 접두어/부분 매칭 쿼리 확장을 더한 BM25
        bm25x_hits = bm25_search(q["question"], k=max_k, id_to_tokens=id_to_tokens, app=q.get("app"),
                                 site=q.get("site"), dates=q.get("dates"), vocab=vocab)
        hybridx_hits = rrf_merge(dense_hits, bm25x_hits, k=max_k)

        row = f"{q['qid']:4} "
        per_method = {}
        for m, hits in zip(methods, [dense_hits, bm25_hits, bm25x_hits, hybridx_hits]):
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

    out_path = RUNS_DIR / f"search_strategy_{time.strftime('%Y%m%d_%H%M')}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
