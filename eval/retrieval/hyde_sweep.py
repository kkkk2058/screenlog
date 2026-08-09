"""HyDE(가상 답변 임베딩) 실험 — Phase 4-3. recall/precision/F1 전부 측정.

지금까지(BM25/확장 BM25/하이브리드/리랭커) 전부 dense 단독을 못 이겼다. HyDE는
질문을 그대로 임베딩하는 대신, LLM에게 "이 질문에 대한 그럴듯한 화면 캡처
내용"을 먼저 만들게 하고 그 텍스트를 임베딩해서 검색한다 — 질문(짧고 구어체)과
실제 문서(길고 화면 캡처 특유의 지저분한 텍스트) 사이의 문체 차이를 좁히는
효과를 노린다.

리랭커 실험에서 "정답이 여러 개인 질문(r08, r25)"은 하나의 해석으로 수렴하는
방법(리랭커)에 불리하다는 걸 확인했다 — HyDE도 가상 답변을 "하나만" 만들면
같은 함정에 빠질 수 있다. 그래서 이번엔 recall뿐 아니라 precision/F1도 같이
재서, "덜 가져오지만 정확한지 / 많이 가져오지만 부정확한지"까지 구분한다.

    recall@k    = 찾아야 할 정답 중 top-k 안에 들어온 비율
    precision@k = top-k로 가져온 것 중 실제 정답인 비율
    F1@k        = 위 둘의 조화평균

사용:
    uv run python eval/hyde_sweep.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai import AsyncOpenAI                  # noqa: E402

from label_retrieval import search_with_ids, _matches_site  # noqa: E402
from screenlog.ask import build_where            # noqa: E402
from screenlog.config import AI_APPS, API_KEY, BASE_URL, CHAT_MODEL  # noqa: E402
from screenlog.index import embed, get_collection  # noqa: E402

QUESTIONS = Path(__file__).parent / "retrieval_questions.jsonl"
RUNS_DIR = Path(__file__).parent / "runs"
K_VALUES = [5, 10]

HYDE_PROMPT = """아래 질문에 대한 답이 담겨 있을 법한 "화면 캡처 텍스트"를 하나 지어내라.
실제 정보를 몰라도 된다 — 카카오톡 대화, 노션 페이지, 터미널 출력, 브라우저 화면
등 그 질문과 어울리는 형식으로, 짧게(3~5줄) 그럴듯하게 써라. 설명하지 말고
화면에 찍혔을 법한 텍스트 그 자체만 출력해라.

질문: {question}"""


async def hyde_expand(question):
    client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
    response = await client.chat.completions.create(
        model=CHAT_MODEL, temperature=0.7,
        messages=[{"role": "user", "content": HYDE_PROMPT.format(question=question)}],
    )
    return response.choices[0].message.content


def dense_search_with_text(query_text, k, app=None, site=None, dates=None):
    """label_retrieval.search_with_ids()와 같은 필터링이지만, 실제 질문이 아니라
    임의의 텍스트(HyDE가 만든 가상 답변)를 임베딩해서 검색한다."""
    col = get_collection()
    where = build_where(app, None, None, dates)
    exclude_ai_apps = app not in AI_APPS
    n_results = 100 if (site or exclude_ai_apps) else max(K_VALUES)
    result = col.query(query_embeddings=embed([query_text]), n_results=n_results, where=where)

    hits = []
    for eid, meta, distance in zip(result["ids"][0], result["metadatas"][0], result["distances"][0]):
        hit = dict(meta)
        hit["id"] = eid
        hits.append(hit)

    if site:
        hits = [h for h in hits if _matches_site(site, h)]
    if exclude_ai_apps:
        hits = [h for h in hits if h["app"] not in AI_APPS]
    return hits[:max(K_VALUES)]


def metrics_at_k(hits, expect_keys, k):
    top = hits[:k]
    top_keys = {(h.get("date"), h.get("app"), h.get("window")) for h in top}
    expect = {(e["date"], e["app"], e["window"]) for e in expect_keys}
    if not expect or not top:
        return 0.0, 0.0, 0.0
    tp = len(top_keys & expect)
    recall = tp / len(expect)
    # 분모를 len(top)(리스트)이 아니라 len(top_keys)(중복 제거된 집합)로 잡는다 —
    # 같은 (date,app,window)를 여러 이벤트가 공유하는 케이스(예: 반복 캡처)가
    # top-k 안에 여럿 있어도 "같은 곳을 몇 번 더 가져온 것"이지 별개의 오답이
    # 아니므로, 집합 크기를 정밀도의 분모로 쓰는 게 더 정확하다.
    precision = tp / len(top_keys) if top_keys else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return recall, precision, f1


async def main():
    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]

    methods = ["dense", "hyde"]
    metrics = ["R", "P", "F1"]
    sums = {m: {k: {metric: 0.0 for metric in metrics} for k in K_VALUES} for m in methods}

    header = "qid  " + "".join(f"{m}-{metric}@{k}".ljust(11) for m in methods for k in K_VALUES for metric in metrics)
    print(header)
    print("-" * len(header))

    results = []
    for q in questions:
        expect_keys = q.get("expect_keys", [])
        if not expect_keys:
            continue

        dense_hits = search_with_ids(q["question"], k=max(K_VALUES), app=q.get("app"), site=q.get("site"),
                                     dates=q.get("dates"))
        hyde_text = await hyde_expand(q["question"])
        hyde_hits = dense_search_with_text(hyde_text, k=max(K_VALUES), app=q.get("app"), site=q.get("site"),
                                           dates=q.get("dates"))

        row = f"{q['qid']:4} "
        per_method = {}
        for m, hits in zip(methods, [dense_hits, hyde_hits]):
            per_method[m] = {}
            for k in K_VALUES:
                r, p, f1 = metrics_at_k(hits, expect_keys, k)
                per_method[m][k] = {"R": r, "P": p, "F1": f1}
                for metric, val in zip(metrics, [r, p, f1]):
                    sums[m][k][metric] += val
                    row += f"{val:.2f}".ljust(11)
        print(row)
        results.append({"qid": q["qid"], "question": q["question"], "hyde_text": hyde_text, **per_method})

    n = len(results)
    print("-" * len(header))
    footer = "평균 "
    for m in methods:
        for k in K_VALUES:
            for metric in metrics:
                footer += f"{sums[m][k][metric] / n:.2f}".ljust(11)
    print(footer)

    RUNS_DIR.mkdir(exist_ok=True)
    out_path = RUNS_DIR / f"hyde_{time.strftime('%Y%m%d_%H%M')}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
