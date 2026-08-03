"""하루 요약 캐시 — 지난 날의 요약을 미리 만들어 두고 꺼내 쓴다.

"이번주 정리해줘"는 하루당 (browse 1번 + LLM 1번)이라 7일이면 6초가 넘었다.
그런데 지난 날의 화면 기록은 더 자라지 않으므로, 요약을 매번 새로 만들 이유가
없다. 색인이 끝난 뒤 한 번 만들어 두고, 질문이 오면 조회만 한다.

왜 chroma가 아니라 별도 sqlite인가:
    chroma는 벡터 검색용이라 upsert에 임베딩이 필요하다. 여긴 date -> 문자열
    키-값 조회가 전부라 임베딩이 낭비다. 표준 라이브러리 sqlite3면 의존성도
    안 늘고 파일 하나로 끝난다.
    (요약문 자체를 임베딩해서 검색 품질을 올리는 건 별개 작업이다 — 여기서
    같이 하면 "캐시가 느려서 고친 것"과 "검색이 부정확해서 고친 것"이 한
    변경에 섞여 나중에 무엇이 무엇을 고쳤는지 못 가린다.)

왜 오늘(today)은 캐시하지 않는가:
    오늘 기록은 계속 자란다. 오전에 만든 요약을 저녁에 그대로 주면 오후 활동이
    통째로 빠진 답이 나간다. 지난 날만 캐시하고 오늘은 항상 새로 만든다.
"""

import sqlite3
from datetime import datetime

from screenlog.config import SUMMARY_CACHE_DB
from screenlog.source import LOCAL_TZ

_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_summary (
    date        TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL
)
"""


def _connect():
    SUMMARY_CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SUMMARY_CACHE_DB)
    conn.execute(_SCHEMA)
    return conn


def today_str():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def is_cacheable_day(date):
    """오늘은 기록이 계속 자라므로 캐시 대상이 아니다."""
    return date < today_str()


def get(date, model):
    """캐시된 요약 or None.

    model을 같이 본다 — CHAT_MODEL을 바꾸면(gemini <-> claude) 말투와 형식이
    달라지는데, 예전 모델이 만든 요약이 그대로 섞여 나오면 답이 들쭉날쭉해진다.
    """
    if not is_cacheable_day(date):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT summary FROM day_summary WHERE date = ? AND model = ?",
            (date, model),
        ).fetchone()
    return row[0] if row else None


def put(date, summary, event_count, model):
    """요약을 저장한다. 오늘 날짜면 저장하지 않는다."""
    if not is_cacheable_day(date):
        return
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO day_summary "
            "(date, summary, event_count, model, created_at) VALUES (?, ?, ?, ?, ?)",
            (date, summary, event_count, model, datetime.now(LOCAL_TZ).isoformat()),
        )


def cached_dates(model):
    """이미 캐시된 날짜 집합. 무엇을 더 만들어야 하는지 고를 때 쓴다."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT date FROM day_summary WHERE model = ?", (model,)
        ).fetchall()
    return {r[0] for r in rows}


def stats():
    """(전체 개수, 모델별 개수) — CLI에서 상태를 보여줄 때 쓴다."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM day_summary").fetchone()[0]
        by_model = conn.execute(
            "SELECT model, COUNT(*) FROM day_summary GROUP BY model"
        ).fetchall()
    return total, by_model
