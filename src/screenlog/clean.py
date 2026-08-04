"""2. 정제 — 프레임을 이벤트로 묶는다

프레임은 검색 단위가 아니다. 몇 초마다 거의 같은 화면이 찍히기 때문에
그대로 임베딩하면 무엇을 물어도 사이드바 메뉴가 1등으로 잡힌다.

세 가지를 한다:
  묶기    앱+창이 같은 연속 프레임을 한 덩어리로
  줄이기  그 안에서 이미 본 줄은 버린다
  나누기  같은 창이어도 화면이 바뀌거나 너무 길면 끊는다
"""

from urllib.parse import urlparse

from screenlog.config import JACCARD_MIN, MAX_EVENT_CHARS, MIN_EVENT_CHARS


def get_lines(text):
    """텍스트를 비어있지 않은 줄 목록으로."""
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    return lines


def overlap(lines_a, lines_b):
    """두 줄 집합이 얼마나 겹치는지 0~1로. 낮을수록 화면이 많이 바뀐 것."""
    if not lines_a or not lines_b:
        return 0.0
    same = len(lines_a & lines_b)
    total = len(lines_a | lines_b)
    return same / total


def group_frames(frames):
    """앱과 창이 같은 연속 프레임을 한 그룹으로 묶는다.

    연속인 것만 묶는다. 카톡 -> 크롬 -> 카톡이면 카톡 그룹이 두 개다.
    시간이 떨어진 걸 하나로 합치면 "언제"가 뭉개진다.
    """
    groups = []
    for frame in frames:
        if groups:
            last = groups[-1][-1]
            if last["app"] == frame["app"] and last["window"] == frame["window"]:
                groups[-1].append(frame)
                continue
        groups.append([frame])
    return groups


def site_from_url(url):
    """방문한 URL -> 도메인("youtube.com" 등). "www."는 뗀다.

    site를 window 제목에서 정규식/화이트리스트로 뽑던 걸 관뒀다 — 제목은
    Chrome이 붙이는 잡음(메모리 사용량, 오디오 표시)이 섞여 있고 갱신도
    늦다(실측: 유튜브로 넘어갔는데 제목은 아직 이전 탭 제목). screenpipe가
    이미 실제 URL을 주므로(browser_url 컬럼) 그걸 그대로 쓰면 화이트리스트
    유지보수 없이 어떤 사이트든 정확히 잡힌다.

    url이 없으면(브라우저가 아닌 앱이거나 캡처가 비어 있던 경우) 빈
    문자열 — chroma metadata는 None을 못 받는다."""
    if not url:
        return ""
    netloc = urlparse(url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def make_event(lines, frames):
    """모아둔 줄과 그 줄이 나온 프레임들로 이벤트 하나를 만든다."""
    start = frames[0]["t"]
    end = frames[-1]["t"]
    return {
        "text": "\n".join(lines),
        "app": frames[0]["app"],
        "window": frames[0]["window"],
        "site": site_from_url(frames[0].get("url")),
        "frame_count": len(frames),
        # 사람이 읽는 용도
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        # 나중에 필터 걸 용도. 지금은 날짜만 색인에서 쓴다.
        "date": start.strftime("%Y-%m-%d"),
        "hour": start.hour,
    }


def split_group(group):
    """한 그룹을 이벤트 여러 개로 나눈다.

    창 제목이 안 바뀌는 앱(Claude, Code)에서는 서로 다른 작업이
    한 그룹에 다 들어온다. 그래서 그룹 안에서도 둘 중 하나면 끊는다:
      1) 앞 프레임과 겹치는 줄이 거의 없다  = 화면이 통째로 바뀜
      2) 모은 글자가 상한을 넘었다          = 천천히 계속 불어남

    이미 본 줄을 기억하는 seen은 그룹 전체에서 유지한다.
    이벤트마다 비우면 사이드바 같은 반복 줄이 이벤트마다 되살아난다.
    """
    events = []
    seen = set()          # 이 그룹에서 이미 담은 줄
    lines = []            # 지금 만드는 중인 이벤트의 줄
    chars = 0             # 그 줄들의 글자 수
    used_frames = []      # 지금 만드는 중인 이벤트에 들어간 프레임
    prev_lines = set()    # 바로 앞 프레임의 줄 (겹침 계산용)

    for frame in group:
        current = get_lines(frame["text"])
        current_set = set(current)

        changed = overlap(prev_lines, current_set) < JACCARD_MIN
        too_long = chars >= MAX_EVENT_CHARS
        if used_frames and (changed or too_long):
            # 프레임 사이에서만 끊는다. 이 프레임은 통째로 다음 이벤트로 간다.
            if chars >= MIN_EVENT_CHARS:
                events.append(make_event(lines, used_frames))
            lines = []
            chars = 0
            used_frames = []

        for line in current:
            if line not in seen:
                seen.add(line)
                lines.append(line)
                chars += len(line) + 1

        used_frames.append(frame)
        prev_lines = current_set

    # 그룹 끝에 남은 것 마무리
    if used_frames and chars >= MIN_EVENT_CHARS:
        events.append(make_event(lines, used_frames))

    return events


def to_events(frames):
    """프레임 목록 -> 이벤트 목록."""
    events = []
    for group in group_frames(frames):
        events.extend(split_group(group))
    return events


if __name__ == "__main__":
    import random
    import sys

    from screenlog.source import available_dates, load_frames

    date = sys.argv[1] if len(sys.argv) > 1 else available_dates()[-1]

    frames = load_frames(date)
    frame_chars = sum(len(f["text"]) for f in frames)

    groups = group_frames(frames)
    events = to_events(frames)
    event_chars = sum(len(e["text"]) for e in events)

    print(f"[{date}]")
    print(f"프레임 {len(frames):5}개 / {frame_chars:12,}자")
    print(f"그룹   {len(groups):5}개")
    print(f"이벤트 {len(events):5}개 / {event_chars:12,}자"
          f"  ({100 - event_chars * 100 // frame_chars}% 줄었다)")

    # 통과 기준: 무작위로 몇 개 읽었을 때 "아 이때 이거 했지"가 읽히는가.
    # 안 읽히면 색인으로 넘어가지 않는다. 임베딩은 시간이 든다.
    print("\n--- 무작위 3개 ---")
    for e in random.sample(events, min(3, len(events))):
        print(f"\n[{e['start']}] {e['app']} / {e['window']} ({e['frame_count']}프레임)")
        print(e["text"][:200].replace("\n", " / "))
