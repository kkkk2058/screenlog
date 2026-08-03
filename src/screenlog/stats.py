"""대시보드용 집계 — chroma metadata만 읽는다.

본문(text)은 아예 안 읽는다. 화면 기록엔 메신저 대화와 로그인 화면이 섞여
있어서, 집계에는 숫자와 앱 이름만 쓴다.

'활동량'의 단위는 프레임 수다. 이벤트 수로 세면 짧은 이벤트와 긴 이벤트가
똑같이 1로 잡혀서, 실제로 화면을 오래 본 것과 잠깐 스친 것이 구분되지 않는다.
프레임 수는 그 이벤트가 몇 번 캡처됐는지라서 실사용량에 더 가깝다.
"""

from collections import Counter, defaultdict
from datetime import datetime

from screenlog.config import IDLE_GAP_SEC
from screenlog.index import get_collection

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 리본에서 이보다 짧은 조각은 같은 앱끼리 이어붙인다. 몇 초짜리 조각을 다 그리면
# 픽셀보다 얇아서 보이지도 않고 DOM만 수천 개가 된다.
MIN_BLOCK_SEC = 10


def build_timeline(date):
    """하루치 이벤트를 리본용 블록 목록으로 만든다.

    이벤트의 start~end만 쓰면 93%가 폭 0이다 — 캡처가 1장뿐이면 시작과 끝이
    같은 시각이기 때문이다. 그래서 각 이벤트의 끝을 "다음 이벤트가 시작될
    때까지"로 늘린다. 단 IDLE_GAP_SEC을 넘는 공백은 자리를 비운 것으로 보고
    늘리지 않는다.

    돌려주는 블록: {"app", "start_sec", "end_sec"} — start_sec은 자정부터의 초.
    """
    col = get_collection()
    res = col.get(where={"date": date}, include=["metadatas"])
    metas = res["metadatas"]
    if not metas:
        return {"date": date, "blocks": [], "app_seconds": {}}

    events = sorted(metas, key=lambda m: m["start"])

    # 1) 각 이벤트에 '다음 이벤트까지'의 체류를 붙인다.
    spans = []
    for i, e in enumerate(events):
        start = datetime.fromisoformat(e["start"])
        end = datetime.fromisoformat(e["end"])
        if i + 1 < len(events):
            next_start = datetime.fromisoformat(events[i + 1]["start"])
            gap = (next_start - end).total_seconds()
            if 0 <= gap <= IDLE_GAP_SEC:
                end = next_start
        spans.append((e["app"], start, end))

    # 2) 같은 앱이 연달아 나오면 하나로 합친다.
    merged = []
    for app, start, end in spans:
        if merged and merged[-1][0] == app and start <= merged[-1][2]:
            # 앞 블록과 겹치거나 이어지면 끝만 늘린다
            merged[-1][2] = max(merged[-1][2], end)
        else:
            merged.append([app, start, end])

    # 3) 너무 짧은 조각은 버린다 (픽셀보다 얇아서 안 보인다)
    midnight = datetime.fromisoformat(events[0]["start"]).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    blocks = []
    app_seconds = Counter()
    for app, start, end in merged:
        seconds = (end - start).total_seconds()
        app_seconds[app] += seconds
        if seconds < MIN_BLOCK_SEC:
            continue
        blocks.append({
            "app": app,
            "start_sec": int((start - midnight).total_seconds()),
            "end_sec": int((end - midnight).total_seconds()),
        })

    return {
        "date": date,
        "blocks": blocks,
        "app_seconds": {app: int(sec) for app, sec in app_seconds.most_common()},
    }


_stats_cache = {"count": None, "result": None}


def build_stats():
    """chroma에 있는 전체 이벤트를 훑어서 대시보드가 쓸 숫자를 만든다.

    이벤트 전체(현재 18,000개대)를 매번 훑으면 새로고침마다 0.5초 정도가
    그냥 나간다 — 근데 이 숫자들은 색인이 새로 돌 때만 바뀐다. col.count()는
    메타데이터를 다 안 읽고도 바로 나오는 값이라, 그게 지난번과 같으면
    데이터가 안 늘었다는 뜻이고 계산 결과를 그대로 재사용해도 된다.
    """
    col = get_collection()
    count = col.count()
    if _stats_cache["result"] is not None and _stats_cache["count"] == count:
        return _stats_cache["result"]

    metas = col.get(include=["metadatas"])["metadatas"]

    if not metas:
        return {"dates": [], "apps": [], "calendar": [], "per_day": [], "overall": {}}

    dates = sorted({m["date"] for m in metas})

    # 앱별 프레임 수 (전체 기간)
    app_frames = Counter()
    for m in metas:
        app_frames[m["app"]] += m["frame_count"]

    # 앱별 이벤트(청크) 수 — 검색 후보를 어느 앱이 독식하는지 보려는 것
    app_events = Counter(m["app"] for m in metas)

    # 날짜 x 시각 격자
    calendar = Counter()
    for m in metas:
        calendar[(m["date"], m["hour"])] += m["frame_count"]

    # 날짜별 집계
    day_frames = Counter()
    day_events = Counter()
    day_app_frames = defaultdict(Counter)
    for m in metas:
        day_frames[m["date"]] += m["frame_count"]
        day_events[m["date"]] += 1
        day_app_frames[m["date"]][m["app"]] += m["frame_count"]

    per_day = []
    for date in dates:
        weekday = datetime.strptime(date, "%Y-%m-%d").weekday()
        per_day.append({
            "date": date,
            "weekday_kr": WEEKDAY_KR[weekday],
            "frames": day_frames[date],
            "events": day_events[date],
            "app_frames": dict(day_app_frames[date].most_common()),
        })

    # 프레임이 많은 앱 순. 프론트가 이 순서대로 색을 배정한다.
    apps = [app for app, _ in app_frames.most_common()]

    busiest = max(per_day, key=lambda d: d["frames"])
    top_app = apps[0]

    result = {
        "dates": dates,
        "apps": apps,
        "app_frames": dict(app_frames.most_common()),
        "app_events": dict(app_events.most_common()),
        "calendar": [
            {"date": date, "hour": hour, "frames": frames}
            for (date, hour), frames in sorted(calendar.items())
        ],
        "per_day": per_day,
        "overall": {
            "total_events": len(metas),
            "total_frames": sum(app_frames.values()),
            "day_count": len(dates),
            "busiest_day": busiest["date"],
            "busiest_day_frames": busiest["frames"],
            "top_app": top_app,
            "top_app_frames": app_frames[top_app],
            # 상위 앱이 검색 후보의 몇 %를 차지하나 — RAG 쏠림의 크기
            "top_app_event_share": round(app_events[top_app] / len(metas) * 100),
        },
    }
    _stats_cache["count"] = count
    _stats_cache["result"] = result
    return result
