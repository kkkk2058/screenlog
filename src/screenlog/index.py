"""3. 색인 — 이벤트를 임베딩해서 chromadb에 넣는다

하루씩 넣는다. 전체를 한 번에 하면 오래 걸리고, 중간에 실패하면
그때까지 한 게 다 날아간다. 하루씩이면 실패해도 이전 날은 남는다.
"""

import hashlib

import chromadb
import torch
from sentence_transformers import SentenceTransformer

from screenlog.clean import to_events
from screenlog.config import (
    CHROMA_DIR,
    COLLECTION,
    EMBED_BATCH_SIZE,
    EMBEDDING_MODEL,
    INDEX_CHECKPOINT_SIZE,
)
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
    # MPS 캐싱 할당자는 텍스트 길이가 배치마다 들쭉날쭉하면(100~36,870자) 그때마다
    # 새 크기의 메모리 블록을 캐시에 쌓기만 하고 반납하지 않는다. 안 비우면
    # 하루치를 다 돌기 전에 시스템 메모리를 다 먹어버린다(실측: 15GB까지 증가).
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()
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
    """하루치를 색인한다.

    청크(INDEX_CHECKPOINT_SIZE개)씩 임베딩하고 그때그때 저장한다.
    중간에 죽어도 이미 저장된 청크는 남고, 재실행하면 그 뒤부터
    이어서 한다 — id가 이벤트 내용으로 정해지므로 이미 들어간
    id는 다시 물어보고 건너뛴다.
    """
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
    col = get_collection()

    # 이미 저장된 id는 이어서 할 때 다시 임베딩하지 않도록 건너뛴다.
    done = set()
    if col.count() > 0:
        done = set(col.get(ids=ids, include=[])["ids"])
    pending = [i for i in ids if i not in done]

    if not pending:
        print(f"[{date}] 이미 전부 색인됨 ({len(ids)}개)")
        return
    if done:
        print(f"[{date}] 이어서 진행: {len(done)}개 완료, {len(pending)}개 남음")
    else:
        print(f"[{date}] 이벤트 {len(pending)}개 임베딩 시작")

    step = INDEX_CHECKPOINT_SIZE
    for i in range(0, len(pending), step):
        chunk_ids = pending[i:i + step]
        chunk_events = [unique[cid] for cid in chunk_ids]
        texts = [event["text"] for event in chunk_events]
        metas = [{k: v for k, v in event.items() if k != "text"} for event in chunk_events]

        vectors = embed(texts)
        col.upsert(ids=chunk_ids, embeddings=vectors, documents=texts, metadatas=metas)
        print(f"[{date}] {min(i + step, len(pending))}/{len(pending)} 저장 (누적 {col.count()}개)")

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
