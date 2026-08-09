"""맥 앱 전용 — 로컬에서 이벤트화·임베딩하고 서버로 올린다.

index.py와 하는 일은 같지만 도착지가 다르다. index.py는 로컬 chroma에
직접 넣고(서버가 자기 데이터를 색인할 때 쓴다), 이쪽은 벡터를 HTTP로
서버에 올린다(맥 앱이 쓴다). 정제(clean.py)·임베딩(index.embed)·id 규칙
(index.event_id)은 그대로 재사용하므로, 어느 쪽으로 넣든 같은 이벤트는
같은 id에 같은 벡터가 된다.

로컬에 chroma를 두지 않는다. 예전 동기화는 맥에서 chroma를 통째로 만든
뒤(688MB) rsync로 서버에 덮어썼는데, 그러면 (1) 같은 데이터를 두 벌
들고 있어야 하고 (2) 서버 디렉토리를 통째로 덮으니 팀원 두 명이 쓰면
나중 사람이 앞사람 걸 지운다. 여기서는 "이미 보낸 id"만 작은 sqlite에
적어두고, 진짜 데이터는 서버 한 곳에만 둔다.

실행: SCREENLOG_ROLE=client python -m screenlog.sync
"""

import sqlite3
from pathlib import Path

import httpx

from screenlog.config import (
    CHROMA_DIR,
    INGEST_MAX_BATCH,
    SCREENLOG_PASSWORD,
    SCREENLOG_SERVER_URL,
    SCREENLOG_USER,
)
from screenlog.index import embed, ensure_model, event_id
from screenlog.source import available_dates, load_frames
from screenlog.clean import to_events

# 서버가 이미 갖고 있다고 확인된 id를 적어둔다. 서버에 물어보는 왕복
# 자체를 아끼려는 캐시일 뿐이라, 지워도 다시 물어보면 그만이다(정확성은
# 서버 쪽 upsert 멱등성이 보장한다).
SENT_DB = Path(CHROMA_DIR) / "sent_events.sqlite"

# 임베딩(느림)과 전송(빠름)을 같은 크기로 묶는다. 서버의 INGEST_MAX_BATCH를
# 넘기면 413이 나므로 그 이하로 맞춘다.
BATCH = min(INGEST_MAX_BATCH, 200)

# "이거 이미 있어?"는 id만 보내는 가벼운 질의라 훨씬 크게 묶어도 되지만,
# 서버가 두는 상한(INGEST_MAX_BATCH x 10) 아래로 맞춘다.
KNOWN_QUERY_BATCH = INGEST_MAX_BATCH * 5

# 임베딩은 하루치가 수 분씩 걸린다. 그 사이 서버가 잠깐 안 되는 것 때문에
# 통째로 날리지 않도록 넉넉히 잡는다.
HTTP_TIMEOUT = 120.0


class SyncError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 실패."""


def _sent_db():
    SENT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SENT_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY)")
    return conn


def _already_sent(conn, ids):
    """ids 중 이 맥에서 이미 보낸 걸로 기록된 것."""
    found = set()
    # sqlite 파라미터 개수 상한(기본 999)에 걸리지 않게 나눠서 묻는다.
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT id FROM sent WHERE id IN ({placeholders})", chunk)
        found.update(r[0] for r in rows)
    return found


def _client():
    if not (SCREENLOG_USER and SCREENLOG_PASSWORD):
        raise SyncError("서버 계정이 설정되지 않았습니다. 앱 설정에서 아이디/비밀번호를 입력하세요.")
    return httpx.Client(base_url=SCREENLOG_SERVER_URL,
                        auth=(SCREENLOG_USER, SCREENLOG_PASSWORD),
                        timeout=HTTP_TIMEOUT)


def _post(client, path, payload):
    try:
        response = client.post(path, json=payload)
    except httpx.RequestError as e:
        raise SyncError(f"서버에 연결할 수 없습니다 ({SCREENLOG_SERVER_URL}): {e}") from e
    if response.status_code == 401:
        raise SyncError("서버 로그인에 실패했습니다. 아이디/비밀번호를 확인하세요.")
    if response.status_code >= 400:
        raise SyncError(f"서버가 요청을 거부했습니다 ({response.status_code}): {response.text[:200]}")
    return response.json()


def sync_date(date, on_status=None):
    """하루치를 서버로 올린다. 올린 이벤트 개수를 돌려준다.

    이미 올린 이벤트는 임베딩조차 하지 않고 건너뛴다 — 이 파이프라인에서
    제일 비싼 단계가 임베딩이라, 두 번째 동기화부터는 그게 곧 전체 소요
    시간이다.
    """
    def status(message):
        if on_status:
            on_status(message)

    events = to_events(load_frames(date))
    if not events:
        return 0

    # 내용이 같은 이벤트는 하나로 접는다(index.py와 같은 이유 — 한 요청
    # 안에 같은 id가 두 번 있으면 chroma가 거부한다).
    unique = {event_id(e): e for e in events}
    ids = list(unique)

    conn = _sent_db()
    try:
        pending = [i for i in ids if i not in _already_sent(conn, ids)]
        if not pending:
            return 0

        with _client() as client:
            # 이 맥의 기록엔 없지만 서버엔 있을 수 있다(다른 기기에서 올렸거나,
            # sent_events.sqlite를 지웠거나). 임베딩 전에 한 번 물어본다.
            # 바쁜 하루는 이벤트가 5,000개를 넘어서 서버 상한에 걸리므로
            # 나눠서 묻는다(실측: 8/6 하루가 413).
            known = set()
            for i in range(0, len(pending), KNOWN_QUERY_BATCH):
                chunk = pending[i:i + KNOWN_QUERY_BATCH]
                known.update(_post(client, "/api/ingest/known", {"ids": chunk})["known"])
            if known:
                conn.executemany("INSERT OR IGNORE INTO sent VALUES (?)", [(i,) for i in known])
                conn.commit()
                pending = [i for i in pending if i not in known]
            if not pending:
                return 0

            uploaded = 0
            for i in range(0, len(pending), BATCH):
                chunk = pending[i:i + BATCH]
                chunk_events = [unique[cid] for cid in chunk]
                status(f"[{date}] 임베딩 {i + len(chunk)}/{len(pending)}")
                vectors = embed([e["text"] for e in chunk_events])

                status(f"[{date}] 전송 {i + len(chunk)}/{len(pending)}")
                _post(client, "/api/ingest", {"events": [
                    {"id": cid, "text": e["text"], "embedding": v,
                     "metadata": {k: val for k, val in e.items() if k != "text"}}
                    for cid, e, v in zip(chunk, chunk_events, vectors)
                ]})

                # 전송이 확인된 뒤에만 기록한다. 반대로 하면 전송이 실패한
                # 이벤트가 "보냄"으로 남아서 영영 안 올라간다.
                conn.executemany("INSERT OR IGNORE INTO sent VALUES (?)", [(c,) for c in chunk])
                conn.commit()
                uploaded += len(chunk)
            return uploaded
    finally:
        conn.close()


def sync_all(on_status=None, on_model_progress=None):
    """아직 안 올린 날짜를 전부 올린다. (올린 이벤트 수, 처리한 날짜 수)."""
    def status(message):
        if on_status:
            on_status(message)

    status("임베딩 모델 확인 중")
    ensure_model(on_progress=on_model_progress)

    dates = available_dates()
    total = 0
    for date in dates:
        total += sync_date(date, on_status=status)
    return total, len(dates)


if __name__ == "__main__":
    import sys

    try:
        if len(sys.argv) > 1:
            count = sync_date(sys.argv[1], on_status=print)
            print(f"{sys.argv[1]}: {count}건 업로드")
        else:
            count, days = sync_all(on_status=print)
            print(f"{days}일치에서 {count}건 업로드")
    except SyncError as e:
        raise SystemExit(f"동기화 실패: {e}")
