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

from screenlog import summary_cache
from screenlog.config import (
    AI_APPS,
    API_KEY,
    BASE_URL,
    CHAT_MODEL,
    MAX_EVENTS_PER_DAY_SUMMARY,
    SUMMARY_DETAIL_KEYWORDS,
)
from screenlog.index import get_collection, indexed_dates
from screenlog.router import _format_history
from screenlog.source import weekday_ko
from openai import AsyncOpenAI


DAY_SUMMARY_PROMPT = """아래는 사용자의 {date}({weekday}) {scope} 화면 사용 기록이다.
{history}
{context}

질문: {question}

위 기록을 질문의 요청 방식에 맞게 요약하라. 규칙:
- 질문이 "정리해줘"/"뭐 했어" 같은 목록·타임라인 요청이면, 각 항목을
  "* HH시MM분 - 내용" 형식으로 한 줄에 하나씩 쓴다(번호 매기기나 줄글 금지).
- 질문이 "잘했나"/"산만했나"/"어땠어"/"괜찮았어?"처럼 목록이 아니라 평가나
  의견을 묻는 것이면, 불릿 형식에 얽매이지 말고 기록에 나타난 근거(시각,
  앱, 무엇을 했는지)를 짚어가며 판단을 담은 줄글로 답한다. 기록에 없는
  근거로 판단하지 않는다.
- 시각과 앱 이름을 항목 안에 함께 밝힌다(줄글로 답할 때도 근거로 언급한
  시각/앱은 그대로 밝힌다).
- 하루 전체를 본 것처럼("하루를 시작했습니다", "마지막으로" 등) 서술하지 말고,
  주어진 기록이 커버하는 시간 범위 안에서만 서술한다.
- 항목 개수와 문장 난이도는 질문에 맞춘다. 질문에 특별한 요청이 없으면 5개
  안팎으로 쓴다. "쉽게"/"간단히"가 있으면 항목을 줄이고 쉬운 말로 쓰고,
  "자세히"/"자세하게"가 있으면 항목을 더 쪼개서 촘촘하게 쓴다 — "최대 5개"에
  얽매이지 않는다.
- 기록이 비어 있으면 "기록 없음"이라고만 답한다.
- 이전 대화가 있고 현재 질문이 그 답변을 더 설명해달라는 것이면(예: "더 자세히"),
  이전 대화도 참고해서 요약 분량/초점을 조정한다.
"""

COMPARE_PROMPT = """아래는 사용자의 최근 활동을 날짜별로 미리 요약해둔 것이다.
{history}
{context}

질문: {question}

규칙:
- 날짜별 요약에 근거해서만 답한다.
- 날짜를 밝히며 답한다.
- 이전 대화가 있으면 참고해서 팔로우업 질문("더 자세히", "그거 무슨 뜻이야")에 답한다.
"""

PERIOD_COMPARE_PROMPT = """아래는 서로 다른 기간의 활동을 기간별로 미리 요약해둔 것이다.
{history}
{context}

질문: {question}

규칙:
- 기간별 요약에 근거해서만 답한다.
- 어느 기간 이야기인지 밝히며 답한다.
- 이전 대화가 있으면 참고해서 팔로우업 질문("더 자세히", "그거 무슨 뜻이야")에 답한다.
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


def _is_plain_day_request(app, hour_start, hour_end, site, history, question):
    """"그 날 하루를 기본 분량으로 요약" 요청인지 — 캐시를 쓸 수 있는 조건.

    캐시에 들어 있는 건 필터 없이 기본 분량으로 만든 요약 하나뿐이라, 조건이
    조금이라도 다르면(앱/시각/사이트 필터, 이전 대화 참고, "자세히" 같은 분량
    요청) 캐시를 쓰면 안 된다. 애매하면 캐시를 안 쓰는 쪽으로 판단한다 —
    조금 느린 건 티가 안 나지만 엉뚱한 답은 바로 티가 난다.
    """
    if app or site or hour_start is not None or hour_end is not None:
        return False
    if history:
        return False
    if question and any(k in question for k in SUMMARY_DETAIL_KEYWORDS):
        return False
    return True


async def summarize_day(date, app=None, hour_start=None, hour_end=None, site=None, history=None, question=None):
    """하루치를 조회해서 요약한다. history는 멀티턴 컨텍스트, question은 사용자가
    실제로 뭐라고 물었는지("쉽게"/"자세히" 등) — 요약 분량/난이도를 여기 맞춘다.

    필터 없는 기본 요약이고 지난 날이면 캐시를 먼저 본다. 캐시가 맞으면
    browse()까지 통째로 건너뛴다 — browse()는 동기 호출이라 여러 날을
    asyncio.gather로 묶어도 이벤트 루프를 막아 순차로 도는데(실측: 하루당
    0.3~0.8초), 캐시 적중이면 그 비용도 같이 사라진다.
    """
    weekday = weekday_ko(date)
    plain = _is_plain_day_request(app, hour_start, hour_end, site, history, question)
    if plain:
        cached = summary_cache.get(date, CHAT_MODEL)
        if cached is not None:
            return cached

    events = browse(date, app, hour_start, hour_end, site)
    if not events:
        # 기록 없는 날은 캐시하지 않는다. 나중에 그 날을 뒤늦게 색인하면
        # "기록 없음"이 그대로 남아 영영 안 고쳐지는데, browse()가 빈 날엔
        # 어차피 금방 끝나서 아낄 것도 없다.
        return f"[{date}({weekday})] 기록 없음"

    raw_count = len(events)
    events = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    context = _format_events(events)
    scope = f"{hour_start}시~{hour_end}시" if hour_start is not None else "하루"
    summary = await _call_llm(DAY_SUMMARY_PROMPT.format(date=date, weekday=weekday, scope=scope,
                                                          history=_format_history(history), context=context,
                                                          question=question or "정리해줘"))
    result = f"[{date}({weekday})]\n{summary}"
    if plain:
        summary_cache.put(date, result, raw_count, CHAT_MODEL)
    return result


async def summarize_range(dates, app=None, hour_start=None, hour_end=None, site=None, history=None, question=None):
    """정리형 — 하루씩 요약해서 그대로 이어붙인다.

    2차 압축(비교형처럼 요약들을 또 요약)을 안 하는 이유는, "정리해줘"류
    질문에서는 그 압축이 정확한 시간대·도구명 같은 디테일만 깎아먹기 때문이다.
    """
    # asyncio.gather로 날짜별 요약을 "동시에" 날림
    days = await asyncio.gather(*[
        summarize_day(date, app, hour_start, hour_end, site, history=history, question=question)
        for date in dates
    ])
    return "\n\n".join(days)


async def compare_range(question, dates, app=None, hour_start=None, hour_end=None, site=None, history=None):
    """비교형 — 하루 요약들을 다시 한번 LLM에 넣어 비교/판단시킨다.

    "언제가 제일 바빴어?"는 하루 요약을 그냥 늘어놔선 답이 안 나온다. LLM이
    날짜를 가로질러 비교해야 하므로 2차 호출을 한 번 더 태운다.
    """
    days = await asyncio.gather(*[
        summarize_day(date, app, hour_start, hour_end, site) for date in dates
    ])

    combined = "\n\n".join(days)
    return await _call_llm(COMPARE_PROMPT.format(context=combined, question=question,
                                                   history=_format_history(history)))


async def summarize_period(period, app=None, hour_start=None, hour_end=None, site=None, history=None, question=None):
    """기간 하나(label + dates)를 정리형으로 요약하고 라벨을 붙인다.

    compare_periods()가 기간을 통째로 하나의 블록으로 다루려면, 그 안의
    날짜들을 먼저 이 함수로 뭉쳐야 한다.
    """
    body = await summarize_range(period["dates"], app=app, hour_start=hour_start,
                            hour_end=hour_end, site=site, history=history, question=question)
    start, end = period["dates"][0], period["dates"][-1]
    return f"### {period['label']} ({start}({weekday_ko(start)}) ~ {end}({weekday_ko(end)}))\n{body}"


async def compare_periods(question, periods, app=None, hour_start=None, hour_end=None, site=None, history=None):
    """기간 자체가 여러 개 언급된 질문 — "저번주 정리하고 이번주랑 비교"류.

    compare_range()는 기간 하나 안에서 하루끼리 비교한다("이번 주 언제가
    제일 바빴어"). 여긴 그 축이 다르다 — 기간마다 먼저 통째로 요약한 뒤,
    그 요약들을 다시 비교시킨다.
    """
    blocks = await asyncio.gather(*[
    summarize_period(period, app, hour_start, hour_end, site) for period in periods
    ])
    combined = "\n\n".join(blocks)
    return await _call_llm(PERIOD_COMPARE_PROMPT.format(context=combined, question=question,
                                                          history=_format_history(history)))


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
    # field="site"로 셀 때 빈 문자열("" = url 없음, 브라우저가 아니거나 url이
    # 캡처 당시 비어 있던 경우)까지 그대로 세면 "가장 많이 등장한 건 (공백)
    # 97회"처럼 의미 없는 1등이 나온다. app은 항상 값이 있어서 이 필터가
    # 영향을 안 준다.
    return Counter(meta[field] for meta in metas if meta.get(field))


def format_count(counter, top_n=5):
    """Counter -> 사람이 읽는 문장. LLM 없이 문자열만 조합한다."""
    if not counter:
        return "기록에 없습니다."

    top_name, top_count = counter.most_common(1)[0]
    lines = [f"{name} {count}회" for name, count in counter.most_common(top_n)]
    return f"가장 많이 등장한 것은 {top_name}({top_count}회)입니다.\n\n" + "\n".join(lines)


async def build_summary_cache(dates=None, concurrency=5):
    """지난 날의 기본 요약을 미리 만들어 캐시에 채운다.

    색인(index.py)과 분리해 둔 이유: 색인은 LLM 없이 임베딩만 하는 단계라
    API 키도 네트워크도 필요 없다. 여기서 색인 안에 LLM 호출을 끼워 넣으면
    게이트웨이가 죽었을 때 색인까지 같이 실패한다. 순서상 색인 뒤에 돌리되,
    실패해도 색인 결과는 남도록 별도 명령으로 둔다.

    concurrency로 동시 호출 수를 묶는다 — 13일치를 한꺼번에 던지면
    레이트리밋에 걸린다.
    """
    if dates is None:
        dates = sorted(indexed_dates())

    done = summary_cache.cached_dates(CHAT_MODEL)
    todo = [d for d in dates if summary_cache.is_cacheable_day(d) and d not in done]
    if not todo:
        print(f"만들 요약 없음 (이미 {len(done)}일치 캐시됨, model={CHAT_MODEL})")
        return []

    print(f"요약 생성 {len(todo)}일 ({todo[0]} ~ {todo[-1]}), model={CHAT_MODEL}")
    sem = asyncio.Semaphore(concurrency)

    async def one(date):
        async with sem:
            await summarize_day(date)   # 캐시 저장은 summarize_day가 알아서 한다
            print(f"  [{date}] 완료")
            return date

    return list(await asyncio.gather(*[one(d) for d in todo]))


if __name__ == "__main__":
    # uv run python -m screenlog.summarize          아직 없는 날짜만 만든다
    # uv run python -m screenlog.summarize --stats  캐시 상태만 본다
    import sys

    if "--stats" in sys.argv:
        total, by_model = summary_cache.stats()
        print(f"캐시된 하루 요약: {total}일")
        for model, n in by_model:
            print(f"  {model}: {n}일")
    else:
        asyncio.run(build_summary_cache())
