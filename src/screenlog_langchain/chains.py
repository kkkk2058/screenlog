"""LangChain 버전 — 하루 요약/비교/질의응답을 LCEL 체인으로 짠다.

원본(screenlog/summarize.py, screenlog/ask.py)의 프롬프트 문자열과 그 프롬프트를
쓰는 이유(주석)는 그대로 두고, `_call_llm()` 직접 호출부만 `PromptTemplate | ChatOpenAI
| StrOutputParser` 체인으로 바꿨다. 검색/필터링/이벤트 조회 같은 비-LLM 로직은
원본 함수를 그대로 import해서 쓴다 — 프레임워크와 무관한 코드를 다시 짤 이유가 없다.
"""

from datetime import datetime

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

from screenlog.ask import PROMPT as ASK_PROMPT
from screenlog.ask import build_context, search
from screenlog.config import API_KEY, BASE_URL, CHAT_MODEL, MAX_EVENTS_PER_DAY_SUMMARY
from screenlog.source import LOCAL_TZ, weekday_ko
from screenlog.summarize import (
    COMPARE_PROMPT,
    DAY_SUMMARY_PROMPT,
    PERIOD_COMPARE_PROMPT,
    _format_events,
    _thin_out,
    browse,
)

_llm = ChatOpenAI(model=CHAT_MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0)


def _chain(template):
    return PromptTemplate.from_template(template) | _llm | StrOutputParser()


_day_summary_chain = _chain(DAY_SUMMARY_PROMPT)
_compare_chain = _chain(COMPARE_PROMPT)
_period_compare_chain = _chain(PERIOD_COMPARE_PROMPT)
_ask_chain = _chain(ASK_PROMPT)


def summarize_day(date, app=None, hour_start=None, hour_end=None, site=None):
    """하루치를 조회해서 5문장 이내로 요약한다. (원본 summarize.summarize_day 동일)"""
    weekday = weekday_ko(date)
    events = browse(date, app, hour_start, hour_end, site)
    if not events:
        return f"[{date}({weekday})] 기록 없음"

    events = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    context = _format_events(events)
    summary = _day_summary_chain.invoke({"date": date, "weekday": weekday, "context": context})
    return f"[{date}({weekday})]\n{summary}"


def summarize_range(dates, app=None, hour_start=None, hour_end=None, site=None):
    """정리형 — 하루씩 요약해서 그대로 이어붙인다."""
    days = [summarize_day(date, app, hour_start, hour_end, site) for date in dates]
    return "\n\n".join(days)


def compare_range(question, dates, app=None, hour_start=None, hour_end=None, site=None):
    """비교형 — 하루 요약들을 다시 한번 LLM에 넣어 비교/판단시킨다."""
    days = [summarize_day(date, app, hour_start, hour_end, site) for date in dates]
    combined = "\n\n".join(days)
    return _compare_chain.invoke({"context": combined, "question": question})


def summarize_period(period, app=None, hour_start=None, hour_end=None, site=None):
    """기간 하나(label + dates)를 정리형으로 요약하고 라벨을 붙인다."""
    body = summarize_range(period["dates"], app=app, hour_start=hour_start,
                            hour_end=hour_end, site=site)
    start, end = period["dates"][0], period["dates"][-1]
    return f"### {period['label']} ({start}({weekday_ko(start)}) ~ {end}({weekday_ko(end)}))\n{body}"


def compare_periods(question, periods, app=None, hour_start=None, hour_end=None, site=None):
    """기간 자체가 여러 개 언급된 질문 — 기간마다 요약 후 그 요약들을 비교시킨다."""
    blocks = [summarize_period(period, app, hour_start, hour_end, site) for period in periods]
    combined = "\n\n".join(blocks)
    return _period_compare_chain.invoke({"context": combined, "question": question})


def ask(question, k, app=None, hour_start=None, hour_end=None, site=None, dates=None):
    """질문 -> (답변, 근거 목록). 검색은 원본 ask.search()를 그대로 쓴다."""
    hits = search(question, k, app=app, hour_start=hour_start, hour_end=hour_end,
                  site=site, dates=dates)
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    answer = _ask_chain.invoke({
        "today": today, "weekday": weekday_ko(today),
        "context": build_context(hits), "question": question,
    })
    return answer, hits
