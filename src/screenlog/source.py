"""1. 데이터 불러오기 — screenpipe sqlite -> 프레임 리스트

읽기 전용(mode=ro)으로 연다. screenpipe가 돌고 있어도 안전하고,
실수로도 원본을 건드릴 수 없다.

프레임 하나는 dict다:
    {"frame_id", "t", "app", "window", "url", "trigger", "source", "text"}
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from screenlog.config import SCREENPIPE_DB, TZ_OFFSET_HOURS

LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
TZ_MODIFIER = f"+{TZ_OFFSET_HOURS} hours"   # sqlite date() 에 넘길 보정값

COLUMNS = """
    id, timestamp, app_name, window_name, browser_url,
    capture_trigger, text_source, full_text
"""


def to_local(ts: str) -> datetime:
    """UTC 문자열 -> KST datetime.

    들어올 때 한 번만 변환하고, 그 뒤로는 전부 로컬로 생각한다.
    UTC로 들고 있다가 쓸 때마다 9를 빼면 날짜 경계에서 틀린다 —
    KST 7/22 08:00은 UTC로 7/21 23:00이라 "어제"가 통째로 밀린다.
    """
    return datetime.fromisoformat(ts).astimezone(LOCAL_TZ)


_WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


def weekday_ko(date_str: str) -> str:
    """"2026-07-27" (또는 그 앞 10글자가 날짜인 타임스탬프) -> "월".

    LLM한테 요일 계산을 맡기면 틀린다(실측: 7/27을 일요일이라고 답함) —
    프롬프트에 날짜를 보여줄 때 요일을 미리 계산해서 박아 넣어, LLM이
    직접 계산할 일 자체를 없앤다.
    """
    return _WEEKDAYS_KO[datetime.strptime(date_str[:10], "%Y-%m-%d").weekday()]


def _connect() -> sqlite3.Connection:
    if not SCREENPIPE_DB.exists():
        raise SystemExit(f"screenpipe DB 없음: {SCREENPIPE_DB}")
    conn = sqlite3.connect(f"file:{SCREENPIPE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def available_dates() -> list[str]:
    """DB에 든 날짜 목록(KST 기준, 오름차순).

    어느 하루로 실험할지 고르려면 먼저 무슨 날이 있는지 알아야 한다.
    날짜 변환을 sqlite에 맡긴다 — 전량을 파이썬으로 끌어와 세지 않으려고.
    """
    conn = _connect()
    rows = conn.execute(
        f"SELECT DISTINCT date(timestamp, '{TZ_MODIFIER}') AS d FROM frames ORDER BY d"
    ).fetchall()
    conn.close()
    return [r["d"] for r in rows if r["d"]]


def load_frames(date: str | None = None) -> list[dict]:
    """프레임을 시간순으로. date("YYYY-MM-DD")를 주면 그 하루만.

    날짜 필터를 SQL에서 건다. 전량을 읽어 파이썬에서 거르면
    하루치를 보려고 며칠치를 메모리에 올리게 된다.

    실험은 하루치로 한다 — 전량은 임베딩이 10분을 넘겨서
    하루에 여러 번 재실험하는 게 불가능해진다.
    """
    conn = _connect()
    if date:
        sql = (f"SELECT {COLUMNS} FROM frames "
               f"WHERE date(timestamp, '{TZ_MODIFIER}') = ? ORDER BY timestamp")
        rows = conn.execute(sql, (date,)).fetchall()
    else:
        rows = conn.execute(f"SELECT {COLUMNS} FROM frames ORDER BY timestamp").fetchall()
    conn.close()

    return [
        {
            "frame_id": r["id"],
            "t": to_local(r["timestamp"]),
            "app": r["app_name"] or "",
            "window": r["window_name"] or "",
            # 지금은 안 쓰지만 같이 읽어둔다. text_source는 이 텍스트가
            # 접근성 트리에서 왔는지 OCR 폴백인지를 구분해준다 — 나중에
            # 근거의 신뢰도를 표시할 때 필요하다.
            "url": r["browser_url"],
            "trigger": r["capture_trigger"],
            "source": r["text_source"],
            "text": r["full_text"] or "",
        }
        for r in rows
    ]


if __name__ == "__main__":
    import sys
    from collections import Counter

    dates = available_dates()
    print(f"날짜 {len(dates)}개: {dates[0]} ~ {dates[-1]}")

    # 인자가 없으면 가장 최근 날짜로 본다.
    date = sys.argv[1] if len(sys.argv) > 1 else dates[-1]
    frames = load_frames(date)

    chars = sum(len(f["text"]) for f in frames)
    print(f"\n[{date}] 프레임 {len(frames)}개 / {chars:,}자")
    if frames:
        print(f"프레임당 평균 {chars // len(frames):,}자")

    # 정제 전 베이스라인. 2단계에서 이 숫자가 얼마나 줄었는지 비교한다.
    print("\n앱 분포(상위 8개):")
    for app, n in Counter(f["app"] for f in frames).most_common(8):
        print(f"  {n:5}  {app}")
