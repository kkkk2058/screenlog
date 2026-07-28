"""3. 색인 — 이벤트를 임베딩해서 chromadb에 넣는다

하루씩 넣는다. 전체를 한 번에 하면 오래 걸리고, 중간에 실패하면
그때까지 한 게 다 날아간다. 하루씩이면 실패해도 이전 날은 남는다.
"""

import hashlib

import chromadb
from sentence_transformers import SentenceTransformer

from screenlog.clean import to_events
from screenlog.config import CHROMA_DIR, COLLECTION, EMBED_BATCH_SIZE, EMBEDDING_MODEL
from screenlog.source import available_dates, load_frames

_model = None


def get_model():
    """임베딩 모델은 한 번만 불러온다. 넣을 때랑 찾을 때 둘 다 쓴다."""
    global _model
    if _model is None:
        print(f"임베딩 모델 로딩: {EMBEDDING_MODEL}")
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


def embed(texts):
    """텍스트 목록 -> 벡터 목록."""
    vectors = get_model().encode(
        texts,
        normalize_embeddings=True,
        batch_size=EMBED_BATCH_SIZE,   # 기본값 32로 두면 MPS 메모리가 터진다
        show_progress_bar=True,
    )
    return vectors.tolist()            # chroma는 numpy가 아니라 리스트를 받는다


def get_collection():
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    # 임베딩을 우리가 직접 만들어 넣으므로 chroma 기본 임베딩 함수는 끈다.
    return client.get_or_create_collection(
        COLLECTION,
        metadata={"hnsw:space": "cosine"},
        embedding_function=None,
    )


def event_id(event):
    """내용으로 고유한 id를 만든다.

    같은 이벤트는 몇 번을 넣어도 같은 id가 나오므로, 재실행해도
    중복이 쌓이지 않고 덮어쓰기만 된다.
    """
    key = f"{event['app']}|{event['window']}|{event['start']}|{event['text']}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def indexed_dates():
    """이미 색인한 날짜들."""
    col = get_collection()
    if col.count() == 0:
        return set()
    metas = col.get(include=["metadatas"])["metadatas"]
    return {m["date"] for m in metas}


def index_date(date):
    """하루치를 색인한다."""
    events = to_events(load_frames(date))
    if not events:
        print(f"[{date}] 이벤트 없음")
        return

    # 내용이 완전히 같은 이벤트는 하나로 합친다.
    # chroma는 한 번에 넣는 목록 안에 같은 id가 두 개 있으면 거부한다.
    unique = {}
    for event in events:
        unique[event_id(event)] = event

    ids = list(unique.keys())
    texts = []
    metas = []
    for event in unique.values():
        texts.append(event["text"])
        metas.append({k: v for k, v in event.items() if k != "text"})

    print(f"[{date}] 이벤트 {len(ids)}개 임베딩 시작")
    vectors = embed(texts)

    col = get_collection()
    # chroma는 한 번에 5000개 남짓까지만 받는다. 넘으면 나눠 넣는다.
    step = 5000
    for i in range(0, len(ids), step):
        col.upsert(
            ids=ids[i:i + step],
            embeddings=vectors[i:i + step],
            documents=texts[i:i + step],
            metadatas=metas[i:i + step],
        )
    print(f"[{date}] 저장 완료 (전체 {col.count()}개)")


def index_all():
    """아직 안 넣은 날짜를 전부 넣는다."""
    done = indexed_dates()
    for date in available_dates():
        if date in done:
            print(f"[{date}] 건너뜀 - 이미 있음")
            continue
        index_date(date)


if __name__ == "__main__":
    import sys

    # uv run python -m screenlog.index              아직 안 넣은 날짜 전부
    # uv run python -m screenlog.index 2026-07-28   그 하루만
    if len(sys.argv) > 1:
        index_date(sys.argv[1])
    else:
        index_all()
