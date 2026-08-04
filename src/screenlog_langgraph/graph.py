"""LangGraph 버전 — route() 결과에 따라 검색/정리/비교/집계로 분기하는 그래프.

도메인 로직(프롬프트, 검색, 캐시, history 처리, 스트리밍 원시 호출)은 전부
screenlog.*(원본)에서 그대로 가져다 쓴다. 여기서 새로 짜는 건 그 함수들을
"어떤 순서/조건으로 부르느냐" — 즉 StateGraph의 노드 분기와 상태 전이뿐이다.
원본 screenlog.ask.ask_auto()/stream_ask_auto()의 if/elif 사슬을 조건부
엣지로 옮긴 것과 같다.

스트리밍은 LangGraph의 커스텀 스트림(get_stream_writer)으로 처리한다. 노드는
매번 이벤트를 writer로 흘려보내면서 동시에 최종 상태(state update)도 그대로
리턴한다 — writer는 스트리밍 컨텍스트 밖(ainvoke)에서는 자동으로 no-op이라,
같은 노드 코드가 스트리밍/논스트리밍 양쪽에 다 쓰인다. 그래서
astream(..., stream_mode="custom")으로 부르면 스트리밍, ainvoke()로 부르면
논스트리밍이 되고, 노드 코드는 하나만 있으면 된다.
"""

import asyncio
from typing import Optional, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from screenlog.ask import stream_ask
from screenlog.config import MAX_PERIOD_SEARCH_K, RETRIEVE_K
from screenlog.router import route
from screenlog.summarize import (
    compare_periods,
    compare_range,
    count_range,
    format_count,
    summarize_period,
    summarize_range,
)


class State(TypedDict, total=False):
    question: str
    k: int
    history: Optional[list]
    plan: dict
    answer: str
    hits: Optional[list]


async def _route_node(state: State) -> dict:
    plan = await route(state["question"], history=state.get("history"))
    get_stream_writer()({"type": "plan", "plan": plan})
    return {"plan": plan}


async def _stream_and_collect(**kwargs) -> dict:
    """stream_ask()를 그대로 소비하면서 각 이벤트를 커스텀 스트림으로 흘려보내고,
    answer/hits를 모아 상태 업데이트로 돌려준다. done도 stream_ask가 직접 낸다."""
    writer = get_stream_writer()
    answer_parts = []
    hits = []
    async for event in stream_ask(**kwargs):
        writer(event)
        if event["type"] == "hits":
            hits = event["hits"]
        elif event["type"] == "token":
            answer_parts.append(event["text"])
    return {"answer": "".join(answer_parts), "hits": hits}


async def _search_node(state: State) -> dict:
    """검색 intent — 기간이 있으면 그 안에서(dates=$in), 없으면 전체에서 벡터 검색."""
    plan = state["plan"]
    periods = plan["periods"]
    dates = [d for period in periods for d in period["dates"]] or None
    search_k = MAX_PERIOD_SEARCH_K if dates else state["k"]
    return await _stream_and_collect(
        question=state["question"], k=search_k, app=plan["app"], hour_start=plan["hour_start"],
        hour_end=plan["hour_end"], site=plan["site"], dates=dates, history=state.get("history"),
    )


async def _fallback_search_node(state: State) -> dict:
    """periods가 비어있고 intent도 검색이 아닌 드문 경우 — 필터 없는 검색으로 떨어진다."""
    plan = state["plan"]
    return await _stream_and_collect(
        question=state["question"], k=state["k"], app=plan["app"], hour_start=plan["hour_start"],
        hour_end=plan["hour_end"], site=plan["site"], history=state.get("history"),
    )


def _emit_answer(answer: str) -> dict:
    """이벤트 단위 근거가 없는 경로(정리/비교/집계) 공통 마무리 — hits/token/done을 낸다."""
    writer = get_stream_writer()
    writer({"type": "hits", "hits": []})
    writer({"type": "token", "text": answer})
    writer({"type": "done"})
    return {"answer": answer, "hits": None}


async def _multi_period_node(state: State) -> dict:
    """periods가 2개 이상 — 기간별로 집계/정리/비교."""
    plan = state["plan"]
    periods = plan["periods"]
    question = state["question"]
    history = state.get("history")
    app, hour_start, hour_end, site = plan["app"], plan["hour_start"], plan["hour_end"], plan["site"]

    if plan["intent"] == "집계":
        field = "site" if plan["count_by_site"] else "app"
        blocks = []
        for period in periods:
            counter = count_range(period["dates"], app=app, site=site, field=field)
            blocks.append(f"[{period['label']}]\n{format_count(counter)}")
        answer = "\n\n".join(blocks)
    elif plan["intent"] == "정리":
        blocks = await asyncio.gather(*[
            summarize_period(period, app=app, hour_start=hour_start, hour_end=hour_end, site=site,
                              history=history, question=question)
            for period in periods
        ])
        answer = "\n\n".join(blocks)
    else:
        answer = await compare_periods(question, periods, app=app, hour_start=hour_start,
                                        hour_end=hour_end, site=site, history=history)

    return _emit_answer(answer)


async def _single_period_node(state: State) -> dict:
    """periods가 1개(단일 기간) — 위와 같은 세 갈래를 하루 단위로 적용."""
    plan = state["plan"]
    dates = plan["periods"][0]["dates"]
    question = state["question"]
    history = state.get("history")
    app, hour_start, hour_end, site = plan["app"], plan["hour_start"], plan["hour_end"], plan["site"]

    if plan["intent"] == "집계":
        counter = count_range(dates, app=app, site=site,
                               field="site" if plan["count_by_site"] else "app")
        answer = format_count(counter)
    elif plan["intent"] == "비교":
        answer = await compare_range(question, dates, app=app, hour_start=hour_start, hour_end=hour_end,
                                      site=site, history=history)
    else:
        answer = await summarize_range(dates, app=app, hour_start=hour_start, hour_end=hour_end, site=site,
                                        history=history, question=question)

    return _emit_answer(answer)


def _branch(state: State) -> str:
    plan = state["plan"]
    if plan["intent"] == "검색":
        return "search"
    periods = plan["periods"]
    if len(periods) >= 2:
        return "multi_period"
    if len(periods) == 1:
        return "single_period"
    return "fallback_search"


def build_graph():
    graph = StateGraph(State)
    graph.add_node("route", _route_node)
    graph.add_node("search", _search_node)
    graph.add_node("multi_period", _multi_period_node)
    graph.add_node("single_period", _single_period_node)
    graph.add_node("fallback_search", _fallback_search_node)

    graph.set_entry_point("route")
    graph.add_conditional_edges("route", _branch, {
        "search": "search",
        "multi_period": "multi_period",
        "single_period": "single_period",
        "fallback_search": "fallback_search",
    })
    for node in ("search", "multi_period", "single_period", "fallback_search"):
        graph.add_edge(node, END)

    return graph.compile()


_graph = build_graph()


async def ask_auto(question, k=RETRIEVE_K, history=None):
    """질문 -> (답변, plan, hits). screenlog.ask.ask_auto()와 같은 반환 형태."""
    result = await _graph.ainvoke({"question": question, "k": k, "history": history})
    return result["answer"], result["plan"], result.get("hits")


async def stream_ask_auto(question, k=RETRIEVE_K, history=None):
    """screenlog.ask.stream_ask_auto()와 같은 이벤트(plan/hits/token/done)를 순서대로 낸다."""
    async for chunk in _graph.astream({"question": question, "k": k, "history": history},
                                       stream_mode="custom"):
        yield chunk
