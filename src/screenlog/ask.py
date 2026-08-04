"""4. 질문 — 검색한 내용을 근거로 LLM이 답한다

두 단계로 나눠뒀다:
    search()  질문과 비슷한 이벤트를 찾는다
    ask()     찾은 내용을 프롬프트에 넣고 LLM에게 묻는다

나눈 이유는 답이 틀렸을 때 검색이 틀렸는지 LLM이 틀렸는지 가리기 위해서다.
합쳐두면 엉뚱한 걸 가져와도 그럴듯한 답이 나와서 눈치채지 못한다.
"""

import asyncio
from datetime import datetime

from openai import AsyncOpenAI

from screenlog.config import (
    AI_APPS,
    API_KEY,
    BASE_URL,
    CHAT_MODEL,
    CONTEXT_CHARS_PER_HIT,
    MAX_PERIOD_SEARCH_K,
    RETRIEVE_K,
)
from screenlog.index import embed, get_collection
from screenlog.router import _format_history, route
from screenlog.source import LOCAL_TZ, weekday_ko
from screenlog.summarize import (
    compare_periods,
    compare_range,
    count_range,
    format_count,
    summarize_period,
    summarize_range,
)

PROMPT = """아래는 사용자의 컴퓨터 화면 사용 기록이다.

오늘은 {today}({weekday})이다. 질문에 "오늘"/"어제"/"엊그제" 같은 상대 날짜
표현이 있어도 다시 계산하지 않는다 — 검색 단계에서 이미 그 날짜로 필터링해서
아래 근거를 골라왔으니, 근거에 실제로 붙어있는 날짜/요일만 그대로 쓴다.
그 결과 오늘 날짜와 안 맞아 보여도(예: "엊그제"인데 근거 날짜가 다르게
느껴져도) 근거의 날짜가 맞다 — 재계산해서 다른 날짜를 답하지 않는다.
{history}
{context}

질문: {question}

규칙:
- 각 근거는 "[캡처 시각, 앱 / 창]" 형태로 시작한다. 화면이 실제로 찍힌
  시각은 이 캡처 시각이다 — 카카오톡 대화 내용 안에 있는 "오후 11:16"
  같은 메시지 전송 시각과 혼동하지 않는다. 질문의 시간대와 맞는지는
  캡처 시각으로 판단한다.
- 근거에 있는 내용만으로 답한다. 근거에 없는 사건/시각/앱은 만들어내지 않는다.
- 캡처 시각이 질문 시간대에 맞는 근거가 하나라도 있으면, 본문에 그 정확한
  단어가 없어도(예: "1시"라는 글자가 없어도) 그 근거로 답한다. 관련
  근거가 하나도 없을 때만 "기록에 없습니다"라고 답한다.
- 답할 때 근거에 적힌 캡처 시각과 앱 이름을 그대로 밝힌다.
- 이전 대화가 있고 현재 질문이 그 답변 내용을 더 설명해달라는 것이면(예:
  "더 자세히", "그거 무슨 뜻이야"), 이전 대화도 참고해서 답한다.
"""


def build_where(app=None, hour_start=None, hour_end=None, dates=None):
    """app/hour_range/dates 조건으로 chroma where 딕셔너리를 만든다.

    조건이 하나도 없으면 None을 돌려준다 (필터 없이 전체에서 검색).
    조건이 2개 이상이면 chroma가 $and로 묶어달라고 요구한다.

    hour는 범위($gte/$lte)로 건다 — 단일 시각("1시")도 start==end로 오므로
    같은 코드 경로로 처리된다. dates는 날짜 하나짜리 질문도 포함해서
    항상 리스트로 받는다($in) — "하루면 이 필드, 여러 날이면 저 필드"처럼
    나누지 않는다(router.route() 참고).
    """
    conditions = []
    if app:
        conditions.append({"app": app})
    if hour_start is not None:
        conditions.append({"hour": {"$gte": hour_start}})
    if hour_end is not None:
        conditions.append({"hour": {"$lte": hour_end}})
    if dates:
        conditions.append({"date": {"$in": dates}})

    if len(conditions) == 0:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def search(question, k=RETRIEVE_K, app=None, hour_start=None, hour_end=None, site=None, dates=None):
    """질문과 비슷한 이벤트 k개를 찾는다.

    app/hour_range/dates를 주면 그 조건을 만족하는 이벤트 안에서만 찾는다.
    벡터 유사도는 "이 문서의 앱이 뭔가" 같은 축을 못 보기 때문에,
    앱/시각/날짜는 벡터가 아니라 metadata 필터로 걸러야 한다.

    site("YouTube" 등)는 window 제목 안의 부분 문자열이라 chroma where로
    못 거른다(summarize.browse()와 같은 이유). chroma가 골라준 top-k를
    다시 site로 걸러내면 k개보다 적게 남을 수 있어서, site가 있을 때는
    5배 더 가져온 뒤 걸러서 최대한 k개를 채운다.

    app이 AI_APPS(Claude/Code)를 명시적으로 지정한 게 아니면 그 두 앱의
    이벤트는 후보에서 뺀다 — "재귀 오염"(docs/troubleshooting-star.md #8):
    이 도구가 디버깅하며 터미널/에디터에 출력한 요약문이 화면 캡처로
    다시 색인돼서, 무관한 질문에 "근거"로 잡혀 LLM이 존재하지 않는
    시각/이벤트를 인용하는 사고가 실측으로 확인됐다. "코딩 몇 시간
    했어?"처럼 app=Code로 명시한 질문은 걸러지면 안 되므로 그때는 예외.
    """
    col = get_collection()
    where = build_where(app, hour_start, hour_end, dates)
    exclude_ai_apps = app not in AI_APPS
    n_results = k * 5 if (site or exclude_ai_apps) else k
    result = col.query(query_embeddings=embed([question]), n_results=n_results, where=where)

    # chroma는 결과를 [[...]] 로 한 겹 싸서 준다. 질문을 여러 개 던질 수 있어서다.
    hits = []
    for doc, meta, distance in zip(result["documents"][0],
                                   result["metadatas"][0],
                                   result["distances"][0]):
        hit = dict(meta)
        hit["text"] = doc
        hit["distance"] = distance     # 0에 가까울수록 질문과 비슷하다
        hits.append(hit)

    if site:
        hits = [h for h in hits if site.lower() in h.get("window", "").lower()]
    if exclude_ai_apps:
        hits = [h for h in hits if h["app"] not in AI_APPS]

    return hits[:k]


def build_context(hits):
    """검색 결과를 프롬프트에 넣을 형태로 만든다.

    시각과 앱 이름을 본문 앞에 붙인다. 이게 없으면 LLM이 '언제 있었던 일인지'를
    아예 모르는 채로 답한다.

    근거 하나당 글자 수에 상한을 둔다. 이벤트 크기가 고르지 않아서(중앙값 1,270자,
    최대 36,870자) 상한이 없으면 큰 이벤트 몇 개가 걸리는 것만으로 프롬프트가
    한 번에 수십 배로 부푼다.
    """
    blocks = []
    for hit in hits:
        text = hit["text"]
        if len(text) > CONTEXT_CHARS_PER_HIT:
            text = text[:CONTEXT_CHARS_PER_HIT] + " …(잘림)"
        blocks.append(f"[{hit['start']}({weekday_ko(hit['start'])}), {hit['app']} / {hit['window']}]\n{text}")
    return "\n\n".join(blocks)


async def ask(question, k=RETRIEVE_K, app=None, hour_start=None, hour_end=None, site=None, dates=None,
              history=None):
    """질문 -> (답변, 근거 목록).

    app/hour_range/site/dates는 그대로 search()에 넘긴다. history는 멀티턴
    컨텍스트("그날"/"더 자세히" 같은 팔로우업 해석용) — [{"question","answer"}, ...].
    """
    hits = search(question, k, app=app, hour_start=hour_start, hour_end=hour_end,
                  site=site, dates=dates)
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    prompt = PROMPT.format(today=today, weekday=weekday_ko(today), history=_format_history(history),
                            context=build_context(hits), question=question)

    client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
    response = await client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
        )
    return response.choices[0].message.content, hits




async def stream_ask(question, k=RETRIEVE_K, app=None, hour_start=None, hour_end=None, site=None, dates=None,
                     history=None):
    hits = search(question, k, app=app, hour_start=hour_start, hour_end=hour_end,
                  site=site, dates=dates)
    yield {"type": "hits", "hits": hits}          # ← 메타데이터 먼저 한 번 던짐

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    prompt = PROMPT.format(today=today, weekday=weekday_ko(today), history=_format_history(history),
                            context=build_context(hits), question=question)

    client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
    response = await client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    )

    async for chunk in response:                         # ← 토큰 조각들이 하나씩 들어옴
        delta = chunk.choices[0].delta.content
        if delta:                                   # ← None인 조각이 섞여있음, 꼭 체크해야 함
            yield {"type": "token", "text": delta}

    yield {"type": "done"}                          # ← 스트림 끝났다는 신호


async def stream_ask_auto(question, k=RETRIEVE_K, history=None):
    """ask_auto()와 같은 라우팅/분기를 쓰되, 결과를 (답변, plan, hits) 튜플로 한 번에
    돌려주는 대신 plan/hits/token/done 이벤트로 쪼개서 yield한다.

    intent가 "검색"이면 stream_ask()로 넘겨서 진짜 토큰 단위 스트리밍을 한다.
    "정리"/"비교"/"집계"는 ask_auto()에서처럼 LLM을 여러 번 호출하거나(정리/비교)
    LLM 없이 metadata를 직접 세는(집계) 구조라 토큰 단위로 쪼갤 수가 없다 — 그래서
    완성된 답을 한 번에 만든 뒤 단일 token 이벤트로 보낸다. 이 경로들까지 진짜
    스트리밍하는 게 목적이 아니라, 최소한 ask_auto()와 같은 정확한 함수로
    답하게 만드는 게 목적이다(전에는 이 경로들도 무조건 검색 방식으로 답해서
    "몇 번 켰어?" 같은 집계 질문이 LLM이 대충 세는 부정확한 답으로 샜다).

    history: [{"question", "answer"}, ...] — 멀티턴 컨텍스트. 지금은 검색
    intent에만 반영한다(route()가 지시어를 풀 때, stream_ask()가 답변 만들 때).
    정리/비교/집계는 아직 미반영 — 필요해지면 그때 확장.
    """
    plan = await route(question, history=history)
    yield {"type": "plan", "plan": plan}

    periods = plan["periods"]
    hour_start, hour_end = plan["hour_start"], plan["hour_end"]

    if plan["intent"] == "검색":
        dates = [d for period in periods for d in period["dates"]] or None
        search_k = MAX_PERIOD_SEARCH_K if dates else k
        async for item in stream_ask(question, k=search_k, app=plan["app"], hour_start=hour_start,
                               hour_end=hour_end, site=plan["site"], dates=dates, history=history):
            yield item
        return

    if len(periods) >= 2:
        if plan["intent"] == "집계":
            blocks = []
            for period in periods:
                counter = count_range(period["dates"], app=plan["app"], site=plan["site"],
                                       field="site" if plan["count_by_site"] else "app")
                blocks.append(f"[{period['label']}]\n{format_count(counter)}")
            answer = "\n\n".join(blocks)
        elif plan["intent"] == "정리":
            blocks = await asyncio.gather(*[
                summarize_period(period, app=plan["app"], hour_start=hour_start,
                                  hour_end=hour_end, site=plan["site"], history=history, question=question)
                for period in periods
            ])
            answer = "\n\n".join(blocks)
        else:
            answer = await compare_periods(question, periods, app=plan["app"], hour_start=hour_start,
                                            hour_end=hour_end, site=plan["site"], history=history)
        yield {"type": "hits", "hits": []}
        yield {"type": "token", "text": answer}
        yield {"type": "done"}
        return

    if len(periods) == 1:
        dates = periods[0]["dates"]
        if plan["intent"] == "집계":
            counter = count_range(dates, app=plan["app"], site=plan["site"],
                                   field="site" if plan["count_by_site"] else "app")
            answer = format_count(counter)
        elif plan["intent"] == "비교":
            answer = await compare_range(question, dates, app=plan["app"], hour_start=hour_start,
                                    hour_end=hour_end, site=plan["site"], history=history)
        else:
            answer = await summarize_range(dates, app=plan["app"], hour_start=hour_start,
                                      hour_end=hour_end, site=plan["site"], history=history, question=question)
        yield {"type": "hits", "hits": []}
        yield {"type": "token", "text": answer}
        yield {"type": "done"}
        return

    # periods가 없는데 intent가 검색이 아닌 드문 경우 — ask_auto()와 동일하게
    # 필터 없는 일반 검색 방식으로 답한다(stream_ask가 done까지 알아서 yield한다).
    async for item in stream_ask(question, k=k, app=plan["app"], hour_start=hour_start,
                                  hour_end=hour_end, site=plan["site"], history=history):
        yield item


async def ask_auto(question, k=RETRIEVE_K):
    """질문만 받아서 route()로 필터를 뽑고, 알아서 알맞은 방식으로 답한다.

    (답변, plan, hits) 3개를 돌려준다. hits는 정리/비교/집계 경로엔 이벤트
    단위 근거가 없어서 None이다 — 요약으로 뭉쳐 답하지, top-k 이벤트를 직접
    돌려주는 구조가 아니기 때문이다. 검색 경로는 항상 hits를 돌려준다.

    intent가 "검색"이면 periods가 있어도 기간 전체를 훑어 요약하지 않는다.
    "이번 주에 카톡에서 약속 잡은 거 찾아봐"는 그 주에 있었던 일 전체가
    아니라 "약속" 관련 대화만 원하는 질문이라, periods가 있다고 무조건
    summarize_range()로 보내면 관련 없는 내용까지 다 정리해버린다(실측으로
    확인된 문제). 그래서 검색은 기간이 있으면 그 기간 안에서(dates=$in),
    없으면 전체 기록에서 벡터 검색(ask())으로 답한다.

    intent가 검색이 아니면(정리/비교/집계) periods 개수로 갈린다.
    plan["periods"]가 2개 이상이면("저번주엔 몇 번, 이번주엔 몇 번 켰어"처럼
    기간 자체가 여러 개 언급된 질문) intent에 따라 다르게 답한다. 집계를
    LLM 요약 비교로 흘려보내면 LLM이 요약문에서 "며칠 언급됐는지"를 세는
    식으로 틀린 숫자를 만들어낸다 — 실측으로 확인됨(실제 340회를 "5회"로
    답함). 그래서 집계는 기간이 여러 개여도 count_range()로 metadata를
    직접 센다.
        집계   기간별로 summarize.count_range() — LLM 없이 metadata를 센다
        정리   기간별로 summarize.summarize_period() — 그대로 이어붙임
        비교   summarize.compare_periods() — 기간별 요약을 다시 LLM으로 비교

    plan["periods"]가 1개면(단일 기간 질문) 같은 세 갈래를 하루 단위로 적용한다:
        집계   summarize.count_range() — LLM 없이 metadata를 센다
        비교   summarize.compare_range() — 하루 요약들을 다시 LLM으로 비교
        정리   summarize.summarize_range() — 하루 요약들을 그대로 이어붙임
    """
    plan = await route(question)
    periods = plan["periods"]
    hour_start, hour_end = plan["hour_start"], plan["hour_end"]

    if plan["intent"] == "검색":
        dates = [d for period in periods for d in period["dates"]] or None
        search_k = MAX_PERIOD_SEARCH_K if dates else k
        answer, hits = await ask(question, k=search_k, app=plan["app"], hour_start=hour_start,
                                  hour_end=hour_end, site=plan["site"], dates=dates)
        return answer, plan, hits

    if len(periods) >= 2:
        if plan["intent"] == "집계":
            blocks = []
            for period in periods:
                counter = count_range(period["dates"], app=plan["app"], site=plan["site"],
                                       field="site" if plan["count_by_site"] else "app")
                blocks.append(f"[{period['label']}]\n{format_count(counter)}")
            answer = "\n\n".join(blocks)
        elif plan["intent"] == "정리":
            blocks = await asyncio.gather(*[
                summarize_period(period, app=plan["app"], hour_start=hour_start,
                                  hour_end=hour_end, site=plan["site"], question=question)
                for period in periods
            ])
            answer = "\n\n".join(blocks)
        else:
            answer = await compare_periods(question, periods, app=plan["app"], hour_start=hour_start,
                                            hour_end=hour_end, site=plan["site"])
        return answer, plan, None

    if len(periods) == 1:
        dates = periods[0]["dates"]
        if plan["intent"] == "집계":
            counter = count_range(dates, app=plan["app"], site=plan["site"],
                                   field="site" if plan["count_by_site"] else "app")
            answer = format_count(counter)
        elif plan["intent"] == "비교":
            answer = await compare_range(question, dates, app=plan["app"], hour_start=hour_start,
                                          hour_end=hour_end, site=plan["site"])
        else:
            answer = await summarize_range(dates, app=plan["app"], hour_start=hour_start,
                                            hour_end=hour_end, site=plan["site"], question=question)
        return answer, plan, None

    answer, hits = await ask(question, k=k, app=plan["app"], hour_start=hour_start,
                              hour_end=hour_end, site=plan["site"])
    return answer, plan, hits


if __name__ == "__main__":
    import asyncio

    async def main():
        while True:
            question = input("\n질문 (엔터로 종료) > ").strip()
            if not question:
                break

            answer, plan, hits = await ask_auto(question)
            periods = ", ".join(f"{p['label']}({len(p['dates'])}일)" for p in plan["periods"])
            hour = (f"{plan['hour_start']}-{plan['hour_end']}"
                    if plan["hour_start"] is not None else "-")
            print(f"\n[라우팅: app={plan['app']} hour={hour} "
                  f"periods=[{periods}] intent={plan['intent']}]")
            print(f"\n{answer}")

            # 답과 근거를 같이 본다. 답만 보면 검색이 엉뚱한 걸 가져온 건지 LLM이
            # 답을 쓰면서 틀린 건지 구분이 안 된다. 여러 날짜 경로는 이벤트 단위
            # 근거가 없어서(하루 요약으로 답하므로) hits가 None이다.
            if hits is None:
                print("\n(여러 날짜 요약 경로 — 이벤트 단위 근거 없음)")
            elif hits:
                print(f"\n--- 근거 {len(hits)}개 ---")
                for hit in hits:
                    print(f"  [{hit['distance']:.3f}] {hit['start']}  "
                          f"{hit['app']} / {hit['window'][:40]}")
            else:
                print("\n")

    asyncio.run(main())