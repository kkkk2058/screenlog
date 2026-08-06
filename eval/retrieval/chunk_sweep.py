"""청킹 파라미터(JACCARD_MIN) 스윕 — Phase 2.

retrieval_questions.jsonl(Phase 0)의 정답은 event_id(해시)로 저장돼 있는데,
JACCARD_MIN을 바꾸면 이벤트 경계가 달라져서 해시 자체가 바뀐다. 그래서 여기서는
id 대신 "정답이 어느 (date, app, window)에서 나왔는가"로 비교한다 — 청킹이
바뀌어도 같은 화면이 같은 창/같은 날짜에서 나온다는 사실은 안 바뀐다. 이
(date, app, window) 목록은 retrieval_questions.jsonl의 expect_keys 필드에
미리 뽑아뒀다(원본 "events" 컬렉션이 청킹 실험으로 오염되기 전에 계산함).

원본 DB(SCREENPIPE_DB, ~/.screenpipe-redacted)는 최근 5일치만 남아있다 —
골든셋 대부분(7월 날짜)의 원본 프레임이 이미 회전(rotation)돼 사라졌다.
대신 리덕션 전 원본(~/.screenpipe/db.sqlite)에 11일치가 남아있어서, 7월
날짜는 그쪽에서 읽는다. 이건 PII 리덕션이 안 된 텍스트라 프로덕션 색인과
완전히 동일하진 않다 — 청킹(줄 단위 겹침 판정)에는 큰 영향이 없을 거라
보지만, 실험용 컬렉션일 뿐 절대 프로덕션 컬렉션과 섞지 않는다(전부 인메모리).

컬렉션 3개(JACCARD_MIN=0.1/0.3/0.5)를 인메모리로 만들고, 골든셋 전체에 대해
recall@5/10을 재서 비교한다. 디스크에는 아무것도 안 남긴다(청킹 실험 컬렉션을
영구 저장할 이유가 없다 — 다음 스윕 때 또 새로 만들면 됨).

사용:
    uv run python eval/chunk_sweep.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chromadb                                        # noqa: E402

import screenlog.clean as clean_module                  # noqa: E402
from label_retrieval import _matches_site                # noqa: E402
from screenlog.ask import build_where                     # noqa: E402
from screenlog.clean import to_events                      # noqa: E402
from screenlog.config import AI_APPS, SCREENPIPE_DB         # noqa: E402
from screenlog.index import embed, event_id                  # noqa: E402
from screenlog.source import COLUMNS, TZ_MODIFIER, to_local    # noqa: E402
import sqlite3                                                  # noqa: E402

QUESTIONS = Path(__file__).parent / "retrieval_questions.jsonl"
ORIGINAL_DB = Path.home() / ".screenpipe" / "db.sqlite"   # 리덕션 전, 보존기간 더 김
JACCARD_VALUES = [0.1, 0.3, 0.5]
K_VALUES = [5, 10]


def load_frames_from(db_path, date):
    """source.py의 load_frames()와 같은 쿼리를 다른 DB 경로에 대고 돌린다."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = (f"SELECT {COLUMNS} FROM frames "
           f"WHERE date(timestamp, '{TZ_MODIFIER}') = ? ORDER BY timestamp")
    rows = conn.execute(sql, (date,)).fetchall()
    conn.close()
    return [
        {
            "frame_id": r["id"], "t": to_local(r["timestamp"]), "app": r["app_name"] or "",
            "window": r["window_name"] or "", "url": r["browser_url"],
            "trigger": r["capture_trigger"], "source": r["text_source"], "text": r["full_text"] or "",
        }
        for r in rows
    ]


def frames_for(date):
    """redacted DB(운영 중, 최근 며칠만)에 있으면 그걸 쓰고, 없으면 원본(더 오래 남음)에서 읽는다."""
    frames = load_frames_from(SCREENPIPE_DB, date)
    if frames:
        return frames
    return load_frames_from(ORIGINAL_DB, date)


def build_collection(jaccard_min, dates):
    """이 JACCARD_MIN으로 dates를 전부 재청킹해서 인메모리 컬렉션 하나를 만든다."""
    clean_module.JACCARD_MIN = jaccard_min   # to_events()가 참조하는 모듈 전역을 바꿔치기
    client = chromadb.EphemeralClient()
    col = client.create_collection(f"sweep_j{jaccard_min}", metadata={"hnsw:space": "cosine"})

    for date in dates:
        frames = frames_for(date)
        events = to_events(frames)
        if not events:
            continue
        unique = {event_id(e): e for e in events}
        ids = list(unique.keys())
        texts = [unique[i]["text"] for i in ids]
        metas = [{k: v for k, v in unique[i].items() if k != "text"} for i in ids]
        vectors = embed(texts)
        col.upsert(ids=ids, embeddings=vectors, documents=texts, metadatas=metas)

    return col


def search(col, question, k, app=None, site=None, dates=None):
    where = build_where(app, None, None, dates)
    exclude_ai_apps = app not in AI_APPS
    n_results = k * 5 if (site or exclude_ai_apps) else k
    result = col.query(query_embeddings=embed([question]), n_results=min(n_results, col.count()), where=where)

    hits = []
    for eid, meta, distance in zip(result["ids"][0], result["metadatas"][0], result["distances"][0]):
        hit = dict(meta)
        hit["id"] = eid
        hits.append(hit)

    if site:
        hits = [h for h in hits if _matches_site(site, h)]
    if exclude_ai_apps:
        hits = [h for h in hits if h["app"] not in AI_APPS]
    return hits[:k]


def recall_at_k(retrieved, expect_keys, k):
    top_k = retrieved[:k]
    top_keys = {(h["date"], h["app"], h["window"]) for h in top_k}
    expect = {(e["date"], e["app"], e["window"]) for e in expect_keys}
    if not expect:
        return None
    hit = len(top_keys & expect)
    return hit / len(expect)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", default=None, help="YYYY-MM-DD,YYYY-MM-DD,... (생략하면 골든셋 전체가 필요로 하는 날짜)")
    args = parser.parse_args()

    questions = [json.loads(line) for line in QUESTIONS.open(encoding="utf-8") if line.strip()]
    all_needed = sorted({key["date"] for q in questions for key in q.get("expect_keys", [])})

    if args.dates:
        needed_dates = args.dates.split(",")
        # 정답이 이 범위 밖 날짜에 있는 질문은 애초에 이 컬렉션에 정답이 없어서
        # 채점하면 "검색이 놓쳤다"가 아니라 "범위 밖이라 원천적으로 못 찾는다"가
        # 되므로, 정답 날짜가 전부 범위 안에 있는 질문만 채점 대상으로 남긴다.
        questions = [q for q in questions
                     if q.get("expect_keys") and all(k["date"] in needed_dates for k in q["expect_keys"])]
        print(f"날짜 범위 축소: {needed_dates} ({len(questions)}개 질문만 채점)")
    else:
        needed_dates = all_needed
    print(f"필요한 날짜: {needed_dates}")

    print("\n원본 프레임 확보 중...")
    for d in needed_dates:
        n = len(frames_for(d))
        print(f"  {d}: {n} 프레임")

    summary = {}
    for jaccard_min in JACCARD_VALUES:
        print(f"\n{'=' * 60}\nJACCARD_MIN={jaccard_min} 로 재청킹 중...")
        started = time.monotonic()
        col = build_collection(jaccard_min, needed_dates)
        elapsed = time.monotonic() - started
        print(f"컬렉션 완성: {col.count()}개 이벤트, {elapsed:.1f}초")

        sums = {k: 0.0 for k in K_VALUES}
        n_scored = 0
        for q in questions:
            expect_keys = q.get("expect_keys", [])
            if not expect_keys:
                continue
            hits = search(col, q["question"], k=max(K_VALUES), app=q.get("app"), site=q.get("site"),
                          dates=q.get("dates"))
            n_scored += 1
            for k in K_VALUES:
                r = recall_at_k(hits, expect_keys, k)
                sums[k] += r or 0.0

        print(f"질문 {n_scored}개 채점 — " + " ".join(f"r@{k}={sums[k]/n_scored:.2f}" for k in K_VALUES))
        summary[jaccard_min] = {k: sums[k] / n_scored for k in K_VALUES}

    print(f"\n{'=' * 60}\n최종 비교")
    print(f"{'JACCARD_MIN':12} " + " ".join(f"r@{k:<5}" for k in K_VALUES))
    for jaccard_min in JACCARD_VALUES:
        row = " ".join(f"{summary[jaccard_min][k]:.2f}  " for k in K_VALUES)
        print(f"{jaccard_min:<12} {row}")


if __name__ == "__main__":
    main()
