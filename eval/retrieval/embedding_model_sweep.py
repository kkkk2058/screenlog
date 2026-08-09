"""임베딩 모델 비교 — Phase 4-4.

청킹(JACCARD_MIN=0.3, 기본값 그대로)은 손대지 않고, 같은 이벤트 텍스트를
다른 임베딩 모델로 다시 벡터화했을 때 recall/precision/F1이 달라지는지 본다.

비교 대상:
    bge-m3(baseline)         지금 프로덕션이 쓰는 모델
    multilingual-e5-large    다국어, BGE-M3와 비슷한 체급
    ko-sroberta-multitask    한국어 전용, 더 가벼움

E5 계열 모델은 비대칭 검색(질문엔 "query: ", 문서엔 "passage: " 접두어)으로
학습됐다 — 접두어 없이 그냥 넣으면 원래 성능이 안 나온다(모델 카드에 명시된
요구사항). ko-sroberta는 그런 접두어 요구사항이 없다.

Phase 2(청킹 스윕)처럼 골든셋이 필요로 하는 9일치로 범위를 좁혀서, 원본 DB
폴백(chunk_sweep.frames_for)까지 그대로 재사용한다.

사용:
    uv run python eval/embedding_model_sweep.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chromadb                                   # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

from chunk_sweep import frames_for               # noqa: E402
from label_retrieval import _matches_site        # noqa: E402
from screenlog.ask import build_where            # noqa: E402
from screenlog.clean import to_events            # noqa: E402
from screenlog.config import AI_APPS, JACCARD_MIN  # noqa: E402
from screenlog.index import event_id             # noqa: E402

QUESTIONS = Path(__file__).parent / "retrieval_questions.jsonl"
RUNS_DIR = Path(__file__).parent / "runs"
K_VALUES = [5, 10]

MODELS = {
    "bge-m3": {"name": "BAAI/bge-m3", "query_prefix": "", "doc_prefix": ""},
    "e5-large": {"name": "intfloat/multilingual-e5-large", "query_prefix": "query: ", "doc_prefix": "passage: "},
    "ko-sroberta": {"name": "jhgan/ko-sroberta-multitask", "query_prefix": "", "doc_prefix": ""},
}


def build_events(dates):
    """청킹은 프로덕션 기본값(JACCARD_MIN, config.py 그대로) 그대로 두고
    이벤트만 만든다 — 모델 비교에서 청킹까지 같이 흔들면 뭐가 원인인지
    구분이 안 된다."""
    events = {}
    for date in dates:
        frames = frames_for(date)
        for e in to_events(frames):
            events[event_id(e)] = e
    return events


CHROMA_UPSERT_LIMIT = 5000  # chromadb 실측 상한(5,461)보다 여유 있게. index.py의
# INDEX_CHECKPOINT_SIZE와 같은 이유 — 한 번에 다 upsert하려다 이 상한을 넘겨서
# (이벤트 14,381개) InternalError가 났다(실측). 나눠 넣으면 그 문제가 없다.


def build_collection_with_model(model, model_key, events):
    conf = MODELS[model_key]
    client = chromadb.EphemeralClient()
    col = client.create_collection(f"embed_{model_key}", metadata={"hnsw:space": "cosine"})

    ids = list(events.keys())
    texts = [conf["doc_prefix"] + events[i]["text"] for i in ids]
    metas = [{k: v for k, v in events[i].items() if k != "text"} for i in ids]

    vectors = model.encode(texts, normalize_embeddings=True, batch_size=8, show_progress_bar=False).tolist()
    for i in range(0, len(ids), CHROMA_UPSERT_LIMIT):
        chunk = slice(i, i + CHROMA_UPSERT_LIMIT)
        col.upsert(ids=ids[chunk], embeddings=vectors[chunk], documents=texts[chunk], metadatas=metas[chunk])
    return col


def search(col, model, model_key, question, k, app=None, site=None, dates=None):
    conf = MODELS[model_key]
    where = build_where(app, None, None, dates)
    exclude_ai_apps = app not in AI_APPS
    n_results = 100 if (site or exclude_ai_apps) else k
    query_vec = model.encode([conf["query_prefix"] + question], normalize_embeddings=True).tolist()
    result = col.query(query_embeddings=query_vec, n_results=min(n_results, col.count()), where=where)

    hits = []
    for eid, meta in zip(result["ids"][0], result["metadatas"][0]):
        hits.append(dict(meta, id=eid))
    if site:
        hits = [h for h in hits if _matches_site(site, h)]
    if exclude_ai_apps:
        hits = [h for h in hits if h["app"] not in AI_APPS]
    return hits[:k]


def metrics_at_k(hits, expect_keys, k):
    top_keys = {(h.get("date"), h.get("app"), h.get("window")) for h in hits[:k]}
    expect = {(e["date"], e["app"], e["window"]) for e in expect_keys}
    if not expect or not top_keys:
        return 0.0, 0.0, 0.0
    tp = len(top_keys & expect)
    recall = tp / len(expect)
    precision = tp / len(top_keys)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return recall, precision, f1


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default=None,
                        help="쉼표로 구분한 모델 키(기본: 전부). 예: --models e5-large "
                             "— 메모리가 빠듯할 때 한 번에 하나씩 순차로 돌리는 용도")
    args = parser.parse_args()
    model_keys = args.models.split(",") if args.models else list(MODELS.keys())

    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]
    needed_dates = sorted({key["date"] for q in questions for key in q.get("expect_keys", [])})
    print(f"필요한 날짜: {needed_dates}")
    print(f"청킹은 고정(JACCARD_MIN={JACCARD_MIN}, 기본값)")
    print(f"이번 실행 모델: {model_keys}\n")

    events = build_events(needed_dates)
    print(f"이벤트 {len(events)}개 준비됨\n")

    max_k = max(K_VALUES)
    metric_names = ["R", "P", "F1"]
    summary = {}

    for model_key in model_keys:
        conf = MODELS[model_key]
        print(f"{'=' * 60}\n{model_key} ({conf['name']}) 로딩 및 임베딩 중...")
        started = time.monotonic()
        model = SentenceTransformer(conf["name"])
        col = build_collection_with_model(model, model_key, events)
        print(f"컬렉션 완성: {col.count()}개, {time.monotonic() - started:.1f}초")

        sums = {k: {mt: 0.0 for mt in metric_names} for k in K_VALUES}
        n_scored = 0
        for q in questions:
            expect_keys = q.get("expect_keys", [])
            if not expect_keys or not all(k["date"] in needed_dates for k in expect_keys):
                continue
            hits = search(col, model, model_key, q["question"], k=max_k, app=q.get("app"),
                         site=q.get("site"), dates=q.get("dates"))
            n_scored += 1
            for k in K_VALUES:
                r, p, f1 = metrics_at_k(hits, expect_keys, k)
                sums[k]["R"] += r
                sums[k]["P"] += p
                sums[k]["F1"] += f1

        summary[model_key] = {k: {mt: round(sums[k][mt] / n_scored, 3) for mt in metric_names} for k in K_VALUES}
        print(f"질문 {n_scored}개 채점 — " +
              " ".join(f"R/P/F1@{k}={summary[model_key][k]['R']:.2f}/{summary[model_key][k]['P']:.2f}/"
                       f"{summary[model_key][k]['F1']:.2f}" for k in K_VALUES))

    print(f"\n{'=' * 60}\n최종 비교")
    print(f"{'model':14} " + "  ".join(f"{k}: R/P/F1" for k in K_VALUES))
    for model_key in model_keys:
        row = f"{model_key:14} "
        for k in K_VALUES:
            s = summary[model_key][k]
            row += f"  {s['R']:.2f}/{s['P']:.2f}/{s['F1']:.2f}"
        print(row)

    RUNS_DIR.mkdir(exist_ok=True)
    tag = "-".join(model_keys)
    out_path = RUNS_DIR / f"embedding_model_sweep_{tag}_{time.strftime('%Y%m%d_%H%M')}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
