"""검색 골든셋 라벨링 — Phase 0.

route()/questions.jsonl은 "필터(app/date/hour)가 맞았는가"만 잰다. 이 스크립트는
그보다 한 단계 아래, "search()가 실제로 정답 이벤트를 top-k 안에 가져왔는가"를
재기 위한 골든셋(retrieval_questions.jsonl)을 만든다.

질문을 주면 필터 없이(또는 --app/--site/--dates로 좁혀서) 후보를 k개 보여주고,
사람이 그중 정답 이벤트 번호를 고르면 event_id와 함께 jsonl에 이어붙인다.
event_id는 index.py의 event_id()와 같은 해시라, 나중에 청킹 파라미터를 바꿔
재색인해도(같은 내용이면) 같은 id로 매칭된다 — 청킹을 바꿨을 때 "같은 이벤트가
여전히 있는지 없는지"까지 이 id로 확인할 수 있다.

한 번에 여러 질문을 라벨링할 수 있게 루프로 돈다. 질문 입력에서 그냥 엔터만
치면 종료한다. 진행 중 언제든 Ctrl+C로 나가도 이미 저장된 항목은 안전하다 —
한 문항 라벨링이 끝날 때마다 즉시 파일에 이어쓴다.

사용:
    uv run python eval/label_retrieval.py
    uv run python eval/label_retrieval.py --app 카카오톡 --k 20
"""

import argparse
import json
import pydoc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from screenlog.ask import build_where          # noqa: E402
from screenlog.config import AI_APPS           # noqa: E402
from screenlog.index import embed, get_collection  # noqa: E402
from screenlog.router import SITE_DOMAINS      # noqa: E402

OUT = Path(__file__).parent / "retrieval_questions.jsonl"
# api.py의 EXCERPT(200자)는 프론트로 내보내는 값이라 짧게 잘랐지만, 여긴 본인
# 터미널에서만 보는 라벨링 화면이라 자를 이유가 없다 — 판단하려면 본문 전체가
# 필요하다. 그래도 한 이벤트가 워낙 길면(최대 2000자, MAX_EVENT_CHARS) 화면이
# 안 끝나니 상한만 넉넉하게 둔다.
EXCERPT = 2000


def _matches_site(site, meta):
    """router.site_matches()는 SITE_ALIASES 화이트리스트(YouTube/Notion/Gmail/GitHub/
    Google Docs/Google Calendar)에 있는 "친숙한 이름"만 받는다. 라벨링은 화이트리스트에
    없는 사이트(예: codetree.ai)도 필터로 써야 하니, site가 알려진 이름이면 그 도메인
    목록으로, 아니면 site 자체를 도메인으로 보고 metadata의 site(clean.py의
    site_from_url())와 직접 비교한다."""
    event_site = meta.get("site") or ""
    if site in SITE_DOMAINS:
        return event_site in SITE_DOMAINS[site]
    if event_site:
        return event_site.lower() == site.lower()
    return site.lower() in meta.get("window", "").lower()


def search_with_ids(question, k, app=None, hour_start=None, hour_end=None, site=None, dates=None):
    """screenlog.ask.search()와 같은 필터링이지만, 라벨링에 필요한 event_id(=chroma id)까지 돌려준다.

    ask.search()는 근거 표시용이라 id를 안 돌려준다(app.py가 프론트에 노출할 필요가
    없어서 hit dict에서 빠져 있음) — 라벨링은 정답을 id로 저장해야 하니 여기서만
    따로 query해서 ids를 챙긴다.

    ask.search()와 마찬가지로 app을 Claude/Code로 명시하지 않은 이상 AI_APPS는
    후보에서 뺀다 — 안 빼면 "이 도구로 라벨링하는 중"이라는 화면 자체가 재색인돼
    거의 모든 질문에서 상위권을 차지해버린다(재귀 오염, 실측으로 확인됨).
    """
    col = get_collection()
    where = build_where(app, hour_start, hour_end, dates)
    exclude_ai_apps = app not in AI_APPS
    n_results = k * 5 if (site or exclude_ai_apps) else k
    result = col.query(query_embeddings=embed([question]), n_results=n_results, where=where)

    hits = []
    for eid, doc, meta, distance in zip(result["ids"][0], result["documents"][0],
                                        result["metadatas"][0], result["distances"][0]):
        hit = dict(meta)
        hit["id"] = eid
        hit["text"] = doc
        hit["distance"] = distance
        hits.append(hit)

    if site:
        hits = [h for h in hits if _matches_site(site, h)]
    if exclude_ai_apps:
        hits = [h for h in hits if h["app"] not in AI_APPS]
    return hits[:k]


def next_qid():
    if not OUT.exists():
        return "r01"
    n = sum(1 for line in OUT.open(encoding="utf-8") if line.strip())
    return f"r{n + 1:02d}"


def print_candidates(hits):
    """후보 전체를 한 화면에 다 찍으면 터미널 스크롤백을 넘어가서 위로 못
    올라가는 문제가 생긴다(실측). pydoc.pager()가 $PAGER(보통 less)로 띄워서
    화면 안에서 스크롤하게 해준다 — less가 없으면 그냥 print로 폴백한다."""
    lines = []
    for i, hit in enumerate(hits, 1):
        text = hit["text"][:EXCERPT]
        lines.append(f"[{i:2}] {hit['distance']:.3f}  {hit['start']}  {hit['app']} / {hit['window']}")
        for line in text.split("\n"):
            lines.append(f"     {line}")
        lines.append(f"     {'-' * 60}")
    pydoc.pager("\n".join(lines))


def label_one(args):
    question = input("질문 (엔터로 종료) > ").strip()
    if not question:
        return False

    dates = args.dates.split(",") if args.dates else None
    hits = search_with_ids(question, k=args.k, app=args.app, site=args.site, dates=dates)

    if not hits:
        print("  (후보 없음 — 필터가 너무 좁거나 코퍼스에 없는 내용일 수 있다)")
        return True

    print_candidates(hits)
    raw = input("정답 이벤트 번호 (쉼표 구분, 없으면 엔터로 스킵) > ").strip()
    if not raw:
        print("  스킵함")
        return True

    try:
        picked = [int(x) - 1 for x in raw.split(",")]
        event_ids = [hits[i]["id"] for i in picked]
    except (ValueError, IndexError):
        print("  번호를 잘못 입력함 — 이 질문은 저장 안 함")
        return True

    note = input("메모 (왜 이 질문/답이 검증 가능한지, 생략 가능) > ").strip()

    record = {
        "qid": next_qid(),
        "question": question,
        "app": args.app,
        "site": args.site,
        "dates": dates,
        "expect_event_ids": event_ids,
        "note": note,
    }
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"  저장함: {record['qid']} ({len(event_ids)}개 정답)")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default=None)
    parser.add_argument("--site", default=None)
    parser.add_argument("--dates", default=None, help="YYYY-MM-DD,YYYY-MM-DD,...")
    parser.add_argument("--k", type=int, default=20)
    args = parser.parse_args()

    print(f"라벨링 결과는 {OUT}에 이어쓴다.\n")
    while label_one(args):
        pass


if __name__ == "__main__":
    main()
