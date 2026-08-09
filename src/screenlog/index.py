"""3. 색인 — 이벤트를 임베딩해서 chromadb에 넣는다

하루씩 넣는다. 전체를 한 번에 하면 오래 걸리고, 중간에 실패하면
그때까지 한 게 다 날아간다. 하루씩이면 실패해도 이전 날은 남는다.
"""

import hashlib
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import torch
from sentence_transformers import SentenceTransformer

from screenlog.clean import site_from_url, to_events
from screenlog.config import (
    CHROMA_DIR,
    COLLECTION,
    EMBED_BATCH_SIZE,
    EMBEDDING_MODEL,
    INDEX_CHECKPOINT_SIZE,
)
from screenlog.source import TZ_MODIFIER, available_dates, load_frames, to_local

# 저장소에 있지만 우리가 안 쓰는 것들. bge-m3 저장소를 통째로 받으면 4GB가
# 넘는데, sentence-transformers 경로에 실제로 필요한 건 가중치 하나와
# 토크나이저뿐이다:
#   onnx/       onnxruntime용 사본(model.onnx_data만 2GB대) — 우리는 torch로 돈다
#   imgs/,*.jpg README에 박힌 벤치마크 그림
#   *_linear.pt FlagEmbedding의 colbert/sparse 헤드 — dense 임베딩만 쓰는 우리와 무관
# 맥 앱은 이걸 첫 실행 때 사용자 회선으로 받으므로, 안 쓰는 2GB를 받게
# 두면 첫 동기화까지의 대기가 그냥 두 배가 된다.
MODEL_IGNORE_PATTERNS = ["onnx/*", "imgs/*", "*.jpg", "*.webp", "*_linear.pt", ".gitattributes"]


def model_is_ready() -> bool:
    """모델 가중치가 이미 로컬 캐시에 있으면 True.

    앱이 "모델 받는 중(수 분)"과 "이미 있는 걸 메모리에 올리는 중(수 초)"을
    구분해서 보여주려면 로딩을 시작하기 전에 알아야 한다 — 둘 다 겉으론
    그냥 멈춰 있는 것처럼 보이는데 기다려야 하는 시간이 자릿수로 다르다."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    try:
        snapshot_download(EMBEDDING_MODEL, local_files_only=True,
                          ignore_patterns=MODEL_IGNORE_PATTERNS)
        return True
    except (LocalEntryNotFoundError, OSError, ValueError):
        return False


def _wanted_model_file(name: str) -> bool:
    from fnmatch import fnmatch
    return not any(fnmatch(name, pattern) for pattern in MODEL_IGNORE_PATTERNS)


def model_download_size() -> int:
    """받아야 할 총 바이트. 앱이 "2.1GB 받습니다"를 미리 보여주는 용도.

    진행률의 분모로도 쓴다. 다운로드가 시작된 뒤에 막대들이 하나씩 생기는
    걸 보고 분모를 누적하면, 초반에 총량이 계속 커져서 진행률이 앞뒤로
    널뛴다 — 시작 전에 저장소 메타데이터로 총량을 확정해두면 그 문제가
    없다."""
    from huggingface_hub import HfApi
    info = HfApi().model_info(EMBEDDING_MODEL, files_metadata=True)
    return sum(s.size or 0 for s in info.siblings if _wanted_model_file(s.rfilename))


def ensure_model(on_progress=None):
    """모델이 없으면 받는다. 이미 있으면 아무것도 안 한다.

    맥 앱(.dmg)은 이 2GB짜리 가중치를 번들에 넣지 않고 첫 실행 때 받는다.
    번들에 넣으면 dmg가 2.8GB가 되는데, 그러면 애플 공증에 올리는 것부터
    앱을 한 줄 고쳐 재배포할 때마다 사용자가 전부 다시 받는 것까지 전
    과정이 무거워진다 — 내장된 녹화기(screenpipe)가 whisper 모델을 첫
    실행에 받는 것과 같은 이유다.

    on_progress(받은 바이트, 전체 바이트)를 주면 진행률을 흘려보낸다.
    중간에 끊겨도 huggingface_hub가 이어받기를 처리하므로, 다시 부르면
    받다 만 지점부터 계속된다.
    """
    from huggingface_hub import snapshot_download

    if model_is_ready():
        return

    def download():
        snapshot_download(EMBEDDING_MODEL, ignore_patterns=MODEL_IGNORE_PATTERNS)

    if on_progress is None:
        download()
        return

    # 진행률을 보여줘야 할 때만 hf-xet 경로를 끈다. xet은 큰 파일을 blobs/에
    # .incomplete로 키우지 않고 자기 방식으로 받아서, 폴더 크기로는 2GB를
    # 받는 내내 1%에 멈춰 있다가 끝나는 순간 100%로 튄다(실측). 사용자가
    # 첫 실행에 몇 분을 기다리는 화면이라 "멈춘 것처럼 보이지 않는 것"이
    # xet의 속도 이득보다 중요하다. 진행률이 필요 없는 호출(서버 등)은
    # 위에서 이미 반환했으므로 xet을 그대로 쓴다.
    from huggingface_hub import constants
    xet_was_disabled = constants.HF_HUB_DISABLE_XET
    constants.HF_HUB_DISABLE_XET = True
    try:
        _download_with_progress(download, on_progress)
    finally:
        constants.HF_HUB_DISABLE_XET = xet_was_disabled


def _download_with_progress(download, on_progress):
    """download()를 스레드로 돌리면서 캐시 폴더가 커지는 걸 1초마다 보고한다.

    huggingface_hub의 tqdm을 가로채는 방법도 있지만, 같은 다운로드를 "전체
    바이트" 막대와 "파일별 바이트" 막대로 두 번 세서 진행률이 194%까지
    갔다(실측). 어느 막대가 집계용인지는 라이브러리 버전에 따라 달라서
    걸러내기도 불안정하다 — 폴더 크기는 표시 방식과 무관하게 항상 맞다.
    """
    try:
        total = model_download_size()
    except Exception:
        total = 0   # 메타데이터를 못 받아도 다운로드 자체는 되어야 한다

    done_event = threading.Event()
    error = []
    # xet 스테이징은 저장소들이 공유하고 다운로드가 끝나도 남는다. 시작
    # 시점을 빼야 "이번에 받은 양"이 된다.
    xet_baseline = _dir_size(_xet_cache_dir())

    def run():
        try:
            download()
        except BaseException as e:   # noqa: BLE001 — 아래에서 그대로 다시 올린다
            error.append(e)
        finally:
            done_event.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    while not done_event.wait(1.0):
        try:
            on_progress(min(_downloaded_bytes(xet_baseline), total) if total else 0, total)
        except Exception:
            pass   # 진행률 표시가 실패해도 다운로드 자체는 계속돼야 한다
    worker.join()
    if error:
        raise error[0]
    on_progress(total, total)


def _dir_size(path: Path) -> int:
    if not path or not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _xet_cache_dir():
    """hf-xet이 받는 중인 조각을 쌓아두는 곳.

    hf-xet이 깔려 있으면(기본값) 큰 파일은 blobs/에 .incomplete로 자라지
    않고 여기로 먼저 간다. 그래서 blobs만 재면 2GB를 받는 내내 1%에
    멈춰 있다가 끝나는 순간 100%로 튄다(실측)."""
    from huggingface_hub import constants
    path = getattr(constants, "HF_XET_CACHE", None)
    return Path(path) if path else None


def _downloaded_bytes(xet_baseline: int) -> int:
    """지금까지 받은 바이트(대략). blobs에 안착한 것 + xet이 받아둔 조각."""
    from huggingface_hub.constants import HF_HUB_CACHE

    repo_dir = Path(HF_HUB_CACHE) / f"models--{EMBEDDING_MODEL.replace('/', '--')}"
    # blobs/만 센다. snapshots/는 blobs로 향하는 심볼릭 링크라 같이 세면 두 배가 된다.
    blobs = _dir_size(repo_dir / "blobs")
    xet = max(0, _dir_size(_xet_cache_dir()) - xet_baseline)
    return blobs + xet


_model = None
_model_lock = threading.Lock()


def get_model():
    """임베딩 모델은 한 번만 불러온다. 넣을 때랑 찾을 때 둘 다 쓴다.

    get_collection()과 같은 이유로 락을 건다 — 동시 요청 두 개가 _model이
    아직 None인 순간에 같이 들어오면 SentenceTransformer가 두 번 생성돼
    메모리를 낭비하거나(수백MB~1GB급) 초기화 중 상태가 깨질 수 있다."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
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


_collection = None
_collection_lock = threading.Lock()


def get_collection():
    """chroma 클라이언트도 모델처럼 한 번만 연다. PersistentClient를 매번 새로
    열면 db.sqlite가 커질수록(현재 900MB대) 연결에만 수 초가 걸린다.

    락을 거는 이유: 호출부가 늘면서(에이전트가 도구 여러 개를 동시에 부르는
    경로 등) 여러 스레드가 _collection이 아직 None인 순간에 동시에 들어올 수
    있다. 그러면 PersistentClient()가 같은 프로세스에서 두 번 겹쳐 생성돼
    chromadb의 프로세스 전역 SharedSystemClient가 깨진다(실측: "Could not
    connect to tenant default_tenant" 에러). 락으로 최초 생성 구간만 직렬화한다.

    chromadb를 모듈 최상단이 아니라 여기서 부르는 이유: 맥 앱은 벡터를
    만들어 HTTP로 서버에 올릴 뿐 로컬에 chroma를 두지 않는다(sync.py 참고).
    최상단에서 부르면 앱 번들에 chromadb와 그 의존성(onnxruntime 등)이
    통째로 딸려 들어간다."""
    import chromadb

    global _collection
    if _collection is not None:
        return _collection
    with _collection_lock:
        if _collection is None:
            client = chromadb.PersistentClient(path=CHROMA_DIR)
            # 임베딩을 우리가 직접 만들어 넣으므로 chroma 기본 임베딩 함수는 끈다.
            _collection = client.get_or_create_collection(
                COLLECTION,
                metadata={"hnsw:space": "cosine"},
                embedding_function=None,
            )
    return _collection


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
    started = time.monotonic()
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

    elapsed = time.monotonic() - started
    chars = sum(len(unique[cid]["text"]) for cid in pending)
    print(f"[{date}] 저장 완료 (전체 {col.count()}개, {elapsed:.1f}초, "
          f"{chars:,}자, 1000자당 {elapsed / chars * 1000:.3f}초)")


def index_all():
    """아직 안 넣은 날짜를 전부 넣는다."""
    done = indexed_dates()
    for date in available_dates():
        if date in done:
            print(f"[{date}] 건너뜀 - 이미 있음")
            continue
        index_date(date)


def backfill_site(dates=None, fields=("site", "url")):
    """이미 색인된 이벤트에 site/url 필드를 채운다.

    site/url은 event_id()의 해시 키(app|window|start|text)에 안 들어가므로,
    clean.py의 make_event()가 나중에 필드를 더 채우게 바뀌어도 기존 id가
    그대로 유지된다 — 그래서 index_date()를 다시 돌려도 이미 있는 id는
    건너뛰어서 새 필드가 절대 안 채워진다. 원본 프레임을 다시 읽어 같은
    id로 이벤트를 재구성한 뒤, 임베딩은 그대로 두고 metadata만 patch한다."""
    col = get_collection()
    dates = sorted(dates) if dates else sorted(indexed_dates())
    for date in dates:
        events = to_events(load_frames(date))
        unique = {event_id(e): e for e in events}
        ids = list(unique.keys())
        if not ids:
            continue

        existing = col.get(ids=ids, include=["metadatas"])
        update_ids, update_metas = [], []
        for eid, meta in zip(existing["ids"], existing["metadatas"]):
            patch = {f: unique[eid][f] for f in fields if meta.get(f) != unique[eid].get(f)}
            if not patch:
                continue  # 이미 같은 값 — 재실행해도 매번 새로 안 쓴다
            update_ids.append(eid)
            update_metas.append({**meta, **patch})

        if update_ids:
            col.update(ids=update_ids, metadatas=update_metas)
        print(f"[{date}] {'/'.join(fields)} 채움: {len(update_ids)}/{len(ids)}개")


def backfill_site_from_source(dates, source_db):
    """SCREENPIPE_DB(리덕션본)가 보존 기간이 지나 회전되면서 원본 프레임을
    잃어버린 날짜용 — 다른 DB(예: 리덕션 전 원본)에서 프레임을 읽어와
    site를 채운다.

    event_id()(app|window|start|text 해시)로 매칭하지 않는다. site는
    url에서만 뽑히고 text(리덕션 대상이라 두 DB 사이에 다를 수 있다)는
    안 쓰는데, 해시엔 text가 들어가서 안 맞을 수 있다. 대신 이미 색인된
    이벤트의 metadata(app/window/start~end 시간대)로 그 구간에 속하는
    원본 프레임을 찾아 url만 가져온다 — text 자체를 옮기지 않으므로
    리덕션 여부는 상관없다."""
    conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    col = get_collection()

    for date in dates:
        existing = col.get(where={"date": date}, include=["metadatas"])
        if not existing["ids"]:
            print(f"[{date}] 색인된 이벤트 없음")
            continue

        rows = conn.execute(
            f"SELECT timestamp, app_name, window_name, browser_url FROM frames "
            f"WHERE date(timestamp, '{TZ_MODIFIER}') = ? ORDER BY timestamp", (date,),
        ).fetchall()
        frames = [
            {"t": to_local(r["timestamp"]), "app": r["app_name"] or "",
             "window": r["window_name"] or "", "url": r["browser_url"]}
            for r in rows
        ]

        update_ids, update_metas = [], []
        for eid, meta in zip(existing["ids"], existing["metadatas"]):
            if meta.get("site") and meta.get("url"):
                continue
            # meta의 start/end는 초 단위까지만 저장돼 있다(isoformat(timespec="seconds")).
            # 원본 프레임 타임스탬프는 마이크로초가 있어서, 프레임 쪽도 초 단위로
            # 깎지 않으면 (특히 프레임 1개짜리 이벤트에서 start==end일 때) end보다
            # 미세하게 늦은 것으로 잘못 판정돼 거의 다 매칭에서 빠진다.
            start, end = datetime.fromisoformat(meta["start"]), datetime.fromisoformat(meta["end"])
            match = next(
                (f for f in frames if f["app"] == meta["app"] and f["window"] == meta["window"]
                 and start <= f["t"].replace(microsecond=0) <= end),
                None,
            )
            url = (match["url"] or "") if match else ""
            site = site_from_url(url)
            if site or url:
                update_ids.append(eid)
                update_metas.append({**meta, "site": site, "url": url})

        if update_ids:
            col.update(ids=update_ids, metadatas=update_metas)
        print(f"[{date}] site/url 채움(원본 DB 매칭): {len(update_ids)}/{len(existing['ids'])}개")

    conn.close()


if __name__ == "__main__":
    import sys

    # uv run python -m screenlog.index              아직 안 넣은 날짜 전부
    # uv run python -m screenlog.index 2026-07-28   그 하루만
    if len(sys.argv) > 1:
        index_date(sys.argv[1])
    else:
        index_all()
