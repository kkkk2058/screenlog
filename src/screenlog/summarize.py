"""4. 여러 날짜 조회 — "이번 주 뭐 했어" 같은 질문

search()는 벡터 검색이라 "제일 비슷한 것 top-k"만 골라준다. 날짜 범위를
통째로 봐야 하는 질문엔 안 맞는다 — 이벤트가 많은 날 하루가 며칠치를
독식해버린다.

세 가지 유형은 답을 만드는 방식이 완전히 다르다:
    정리형   하루씩 요약해서 그대로 이어붙인다
    비교형   하루씩 요약한 뒤, 그 요약들을 다시 비교하는 LLM 호출을 한 번 더 한다
    집계형   LLM한테 세게 시키지 않는다. metadata를 직접 센다.
"""
import asyncio
from collections import Counter

from screenlog.config import AI_APPS, API_KEY, BASE_URL, CHAT_MODEL, MAX_EVENTS_PER_DAY_SUMMARY
from screenlog.index import get_collection
from screenlog.source import weekday_ko
from openai import AsyncOpenAI


DAY_SUMMARY_PROMPT = """아래는 사용자의 {date}({weekday}) {scope} 화면 사용 기록이다.

{context}

이 시간에 있었던 일을 최대 5개 항목으로 요약하라. 규칙:
- 각 항목은 "* HH시MM분 - 내용" 형식으로, 한 줄에 하나씩 쓴다(번호 매기기나 줄글 금지).
- 시각과 앱 이름을 항목 안에 함께 밝힌다.
- 하루 전체를 본 것처럼("하루를 시작했습니다", "마지막으로" 등) 서술하지 말고,
  주어진 기록이 커버하는 시간 범위 안에서만 서술한다.
- 기록이 비어 있으면 "기록 없음"이라고만 답한다.
"""

COMPARE_PROMPT = """아래는 사용자의 최근 활동을 날짜별로 미리 요약해둔 것이다.

{context}

질문: {question}

규칙:
- 날짜별 요약에 근거해서만 답한다.
- 날짜를 밝히며 답한다.
"""

PERIOD_COMPARE_PROMPT = """아래는 서로 다른 기간의 활동을 기간별로 미리 요약해둔 것이다.

{context}

질문: {question}

규칙:
- 기간별 요약에 근거해서만 답한다.
- 어느 기간 이야기인지 밝히며 답한다.
"""

SUMMARY_EXCERPT = 300   # 하루 요약 프롬프트엔 이벤트를 다 넣으니, 하나당 길이를 줄인다.


async def _call_llm(prompt):
    client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
    response = await client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def _build_where(date, app, hour_start, hour_end):
    conditions = [{"date": date}]
    if app:
        conditions.append({"app": app})
    if hour_start is not None:
        conditions.append({"hour": {"$gte": hour_start}})
    if hour_end is not None:
        conditions.append({"hour": {"$lte": hour_end}})

    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def browse(date, app=None, hour_start=None, hour_end=None, site=None):
    """그 날짜의 이벤트를 시간순으로 전부 가져온다. 벡터 검색을 쓰지 않는다.

    "이번 주 뭐 했어"는 '비슷한 것 몇 개'로 답할 질문이 아니라 그 날 전체를
    훑어야 하는 질문이다. search()의 top-k로는 이벤트가 많은 앱이 며칠치를
    독식한다.

    site는 chroma where로 못 거른다 — app은 "Google Chrome"처럼 정확히
    일치하는 값이지만 site("YouTube")는 window 제목 안의 부분 문자열이라서다
    (실측: "뇌 진정 주파수 - YouTube - Chrome - ..."). chroma metadata는
    부분일치를 지원하지 않으니 여기서 파이썬으로 후처리한다.

    app을 AI_APPS(Claude/Code)로 명시하지 않았으면 그 앱 이벤트를 뺀다 —
    "재귀 오염"(docs/troubleshooting-star.md #8) 때문에 무관한 날짜/기간
    질문에 디버깅 출력이 진짜 활동으로 섞여 들어가는 걸 막는다. ask.py의
    search()와 같은 규칙이다.
    """
    where = _build_where(date, app, hour_start, hour_end)
    col = get_collection()
    result = col.get(where=where, include=["documents", "metadatas"])

    events = []
    for doc, meta in zip(result["documents"], result["metadatas"]):
        event = dict(meta)
        event["text"] = doc
        events.append(event)

    if site:
        events = [e for e in events if site.lower() in e.get("window", "").lower()]
    if app not in AI_APPS:
        events = [e for e in events if e["app"] not in AI_APPS]

    events.sort(key=lambda e: e["start"])
    return events


def _format_events(events):
    blocks = []
    for e in events:
        text = e["text"][:SUMMARY_EXCERPT]
        blocks.append(f"[{e['start']}, {e['app']} / {e['window']}]\n{text}")
    return "\n\n".join(blocks)


def _thin_out(events, max_events):
    """이벤트가 상한을 넘으면 일정한 간격으로 솎아낸다.

    앞에서부터 자르면 하루의 앞부분만 남고 오후·저녁이 통째로 사라진다.
    바쁜 날(실측: 하루 2,844개)은 이대로 프롬프트에 다 넣으면 100만 자를
    넘는다. 골고루 골라내면 개수는 줄어도 하루 전체를 훑을 수 있다.
    """
    if len(events) <= max_events:
        return events

    step = len(events) / max_events
    return [events[int(i * step)] for i in range(max_events)]


async def summarize_day(date, app=None, hour_start=None, hour_end=None, site=None):
    """하루치를 조회해서 5문장 이내로 요약한다."""
    weekday = weekday_ko(date)
    events = browse(date, app, hour_start, hour_end, site)
    if not events:
        return f"[{date}({weekday})] 기록 없음"

    events = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    context = _format_events(events)
    scope = f"{hour_start}시~{hour_end}시" if hour_start is not None else "하루"
    summary = await _call_llm(DAY_SUMMARY_PROMPT.format(date=date, weekday=weekday, scope=scope, context=context))
    return f"[{date}({weekday})]\n{summary}"


async def summarize_range(dates, app=None, hour_start=None, hour_end=None, site=None):
    """정리형 — 하루씩 요약해서 그대로 이어붙인다.

    2차 압축(비교형처럼 요약들을 또 요약)을 안 하는 이유는, "정리해줘"류
    질문에서는 그 압축이 정확한 시간대·도구명 같은 디테일만 깎아먹기 때문이다.
    """
    # asyncio.gather로 날짜별 요약을 "동시에" 날림
    days = await asyncio.gather(*[
        summarize_day(date, app, hour_start, hour_end, site) for date in dates
    ])
    return "\n\n".join(days)


async def compare_range(question, dates, app=None, hour_start=None, hour_end=None, site=None):
    """비교형 — 하루 요약들을 다시 한번 LLM에 넣어 비교/판단시킨다.

    "언제가 제일 바빴어?"는 하루 요약을 그냥 늘어놔선 답이 안 나온다. LLM이
    날짜를 가로질러 비교해야 하므로 2차 호출을 한 번 더 태운다.
    """
    days = await asyncio.gather(*[
        summarize_day(date, app, hour_start, hour_end, site) for date in dates
    ])

    combined = "\n\n".join(days)
    return await _call_llm(COMPARE_PROMPT.format(context=combined, question=question))


async def summarize_period(period, app=None, hour_start=None, hour_end=None, site=None):
    """기간 하나(label + dates)를 정리형으로 요약하고 라벨을 붙인다.

    compare_periods()가 기간을 통째로 하나의 블록으로 다루려면, 그 안의
    날짜들을 먼저 이 함수로 뭉쳐야 한다.
    """
    body = await summarize_range(period["dates"], app=app, hour_start=hour_start,
                            hour_end=hour_end, site=site)
    start, end = period["dates"][0], period["dates"][-1]
    return f"### {period['label']} ({start}({weekday_ko(start)}) ~ {end}({weekday_ko(end)}))\n{body}"


async def compare_periods(question, periods, app=None, hour_start=None, hour_end=None, site=None):
    """기간 자체가 여러 개 언급된 질문 — "저번주 정리하고 이번주랑 비교"류.

    compare_range()는 기간 하나 안에서 하루끼리 비교한다("이번 주 언제가
    제일 바빴어"). 여긴 그 축이 다르다 — 기간마다 먼저 통째로 요약한 뒤,
    그 요약들을 다시 비교시킨다.
    """
    blocks = await asyncio.gather(*[
    summarize_period(period, app, hour_start, hour_end, site) for period in periods
    ])
    combined = "\n\n".join(blocks)
    return await _call_llm(PERIOD_COMPARE_PROMPT.format(context=combined, question=question))


def count_range(dates, app=None, site=None, field="app"):
    """집계형 — LLM을 부르지 않는다. metadata를 직접 센다.

    "몇 번 켰어?" 같은 질문을 하루 요약 텍스트에서 LLM이 세게 하면 못 믿을
    답이 나온다. 요약 과정에서 이미 정보가 깎였고, 그 위에서 또 정확한
    카운트를 기대하는 건 무리다. chroma에 이미 있는 metadata를 그냥 세면
    LLM 호출 없이 정확한 답이 나온다.

    site는 chroma where로 못 거르는 부분 문자열 필터라 여기서도 결과를
    받은 뒤 파이썬에서 후처리한다(browse()와 동일한 이유).
    """
    conditions = [{"date": {"$in": dates}}]
    if app:
        conditions.append({"app": app})
    where = conditions[0] if len(conditions) == 1 else {"$and": conditions}

    col = get_collection()
    result = col.get(where=where, include=["metadatas"])
    metas = result["metadatas"]
    if site:
        metas = [m for m in metas if site.lower() in m.get("window", "").lower()]
    return Counter(meta[field] for meta in metas)


def format_count(counter, top_n=5):
    """Counter -> 사람이 읽는 문장. LLM 없이 문자열만 조합한다."""
    if not counter:
        return "기록에 없습니다."

    top_name, top_count = counter.most_common(1)[0]
    lines = [f"{name} {count}회" for name, count in counter.most_common(top_n)]
    return f"가장 많이 등장한 것은 {top_name}({top_count}회)입니다.\n\n" + "\n".join(lines)
