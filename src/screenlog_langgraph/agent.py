"""LangGraph 버전 확장 — route()가 검색/정리/비교/집계 중 하나로 못 묶는
"복합 질문"만 여기로 떨어져서 tool-calling(ReAct) 루프를 탄다.

graph.py의 고정 분기는 그대로 둔다. 이 파일은 새 경로 하나를 추가할 뿐이다:
    route() 이후 별도의 "복합 질문인가?" 판별을 한 번 더 거치고,
    복합이면 여기 정의된 도구 4개(검색/집계/정리/비교) 중 필요한 걸
    LLM이 스스로 골라 여러 번 부르게 한다.

도구는 새로 로직을 짜지 않는다 — screenlog.ask.ask() / screenlog.summarize.*를
그대로 감싼 것뿐이다. 그래서 "집계는 LLM이 세지 않고 count_range()로 직접
센다" 같은, 실측 버그로 얻은 불변식이 에이전트가 어떤 순서로 도구를 부르든
깨지지 않는다 — 그 불변식은 도구 호출 순서가 아니라 도구 내부에 있다.

집계/비교 정확성이 이미 검증된 단일-intent 질문은 이 경로를 절대 타지
않는다(비용/지연 때문 — 도구 호출마다 LLM 왕복이 하나씩 더 든다). 오직
route() 4갈래로 못 답하는 질문만 이 무거운 경로로 폴백한다.
"""

from datetime import datetime
from typing import Optional, TypedDict

from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import create_react_agent

from screenlog.ask import ask as _ask
from screenlog.config import API_KEY, BASE_URL, CHAT_MODEL, RETRIEVE_K
from screenlog.router import _APP_HINT, _SITE_HINT, _expand_period, _format_history
from screenlog.source import LOCAL_TZ, weekday_ko
from screenlog.summarize import compare_range as _compare_range
from screenlog.summarize import count_range as _count_range
from screenlog.summarize import format_count as _format_count
from screenlog.summarize import summarize_range as _summarize_range

# --- 복합 질문 판별 ------------------------------------------------------
# route()의 intent 스키마(검색/정리/비교/집계 enum)는 원본과 공유하는
# 계약이라 여기서 다섯 번째 값을 끼워 넣지 않는다. 대신 route() 결과와는
# 별도로, 훨씬 가벼운 예/아니오 판별 하나를 추가로 둔다 — "이 질문이 검색
# /정리/비교/집계 중 하나로 충분히 답변되는가?"만 묻는다.
_COMPOUND_CHECK_PROMPT = """질문 하나를 보고, 아래 네 방식 중 "하나만" 적용해서
완전히 답할 수 있는지 판단해라.

    검색 — 기간 안의 특정 내용/대화/키워드를 찾는다
    정리 — 기간 안에 있었던 일 전반을 그대로 보여준다
    비교 — 기간 사이의 차이나 경향을 판단한다
    집계 — 사용 횟수를 센다

"저번주 정리하고 이번주랑 비교"처럼 두 방식이 순서대로 필요하거나,
"이번주 유튜브 몇 번 봤는지랑 어떤 영상 봤는지 같이 알려줘"처럼 집계와
검색이 한 질문에 같이 필요하면 복합이다.

네 방식 중 하나로 충분하면 복합이 아니다 — 애매해도 우선 아니라고 답해라
(비용이 더 드는 쪽은 틀렸을 때 손해가 크다).

질문: {question}"""

_compound_llm = ChatOpenAI(model=CHAT_MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0)
_compound_schema = {
    "title": "compound_check",
    "type": "object",
    "properties": {"is_compound": {"type": "boolean"}},
    "required": ["is_compound"],
    "additionalProperties": False,
}
_compound_structured_llm = _compound_llm.with_structured_output(_compound_schema, method="json_schema", strict=True)


async def is_compound(question: str) -> bool:
    try:
        result = await _compound_structured_llm.ainvoke(_COMPOUND_CHECK_PROMPT.format(question=question))
    except Exception:
        return False  # 판별 자체가 실패하면 기존 고정 경로(더 검증된 쪽)로 보낸다
    return bool(result.get("is_compound"))


# --- 도구 4개: 기존 함수를 감싸기만 한다 ----------------------------------
# 날짜는 도구 경계에서 "YYYY-MM-DD 문자열 쌍"으로 받는다 — LLM이 채우기
# 쉬운 형태를 도구 인터페이스로 쓰고, 내부에서 _expand_period()로
# route()와 똑같이 날짜 리스트로 편다(뒤집힘 보정/최대 기간도 그대로 적용됨).

_TOOL_DOC_SUFFIX = f"""

app 후보(반드시 이 중 하나거나 비워둔다):
{_APP_HINT}

site 후보(브라우저 안에서 방문한 사이트, 반드시 이 중 하나거나 비워둔다):
{_SITE_HINT}
"""


@tool(description="기간 안에서 특정 내용/대화/키워드를 찾아 답한다. \"며칠에 무슨 일 있었는지 "
                  "전부\"가 아니라 \"그 안의 특정 주제\"를 찾을 때 쓴다. start_date/end_date를 "
                  "안 주면 전체 기록에서 찾는다 (YYYY-MM-DD, 둘 다 포함)." + _TOOL_DOC_SUFFIX)
async def search_events(
    question: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    app: Optional[str] = None,
    site: Optional[str] = None,
) -> str:
    dates = _expand_period(start_date, end_date) if start_date and end_date else None
    answer, _hits = await _ask(question, k=RETRIEVE_K, app=app, site=site, dates=dates)
    return answer


@tool(description="기간 안에서 앱/행동이 몇 번 있었는지 센다. LLM이 어림잡지 않고 기록을 "
                  "직접 세므로, \"몇 번 켰어\"/\"얼마나 자주 썼어\"류 질문은 반드시 이 도구를 "
                  "쓴다 — 요약문을 보고 직접 세지 않는다.\n\n"
                  "group_by=\"site\"로 주면 앱이 아니라 방문한 사이트 도메인별로 나눠서 센다"
                  "(예: \"크롬 안에서 뭘 많이 봤어\"류 질문엔 app=\"Google Chrome\", "
                  "group_by=\"site\"로 부른다). 도메인은 실제 URL에서 뽑은 값이라 "
                  "site 후보 목록에 없는 사이트도 그대로 나온다." + _TOOL_DOC_SUFFIX)
def count_events(
    start_date: str, end_date: str, app: Optional[str] = None, site: Optional[str] = None,
    group_by: str = "app",
) -> str:
    dates = _expand_period(start_date, end_date)
    counter = _count_range(dates, app=app, site=site, field=group_by)
    return _format_count(counter, top_n=10)


@tool(description="기간 안에 있었던 일 전반을 하루씩 요약해서 그대로 이어붙인다. "
                  "(YYYY-MM-DD, 둘 다 포함)" + _TOOL_DOC_SUFFIX)
async def summarize_days(
    start_date: str, end_date: str, app: Optional[str] = None, site: Optional[str] = None,
) -> str:
    dates = _expand_period(start_date, end_date)
    return await _summarize_range(dates, app=app, site=site)


@tool(description="기간 안의 날짜들을 서로 비교해서 차이/경향/\"언제가 제일 ~했는지\"를 "
                  "판단한다. 하루씩 요약한 뒤 그 요약들을 다시 LLM으로 비교한다. "
                  "(YYYY-MM-DD, 둘 다 포함)" + _TOOL_DOC_SUFFIX)
async def compare_days(
    question: str, start_date: str, end_date: str, app: Optional[str] = None, site: Optional[str] = None,
) -> str:
    dates = _expand_period(start_date, end_date)
    return await _compare_range(question, dates, app=app, site=site)


_TOOLS = [search_events, count_events, summarize_days, compare_days]

# 무제한 자유 루프가 아니라 가드레일을 둔다:
#   - 호출 가능 도구는 위 4개로 고정(화이트리스트) — 새 능력을 여기서 만들지 않는다.
#   - recursion_limit으로 스텝 수 상한(도구 호출-응답 왕복 기준 대략 절반).
_AGENT_SYSTEM_PROMPT = """사용자의 화면 사용 기록에 대한 복합 질문에 답한다. 오늘은 {today}({weekday})이다.
"이번 주"/"저번 주"/"어제" 같은 상대 날짜는 이 오늘 날짜를 기준으로 직접
계산해서 도구의 start_date/end_date(YYYY-MM-DD)를 채운다 — 사용자에게 날짜를
되묻지 않는다.

질문을 필요한 만큼 나눠서 도구를 순서대로 불러 답을 모은 뒤, 마지막에
사용자 질문 원문에 맞는 하나의 답으로 정리해서 말한다.

규칙:
- "몇 번"/"얼마나 자주" 같은 횟수는 반드시 count_events를 쓴다. 다른 도구가
  돌려준 요약문이나 검색 결과를 보고 직접 세지 않는다 — 세는 도구가 따로 있다.
- 도구가 이미 문장으로 답을 만들어 돌려주므로, 그 내용을 사실과 다르게
  바꾸거나 새 사실을 지어내지 않는다. 여러 도구 결과를 이어 붙이고 필요하면
  요약만 한다.
- 날짜 말고 앱/사이트가 불확실하면(예: 후보 목록에 없는 이름) 도구를 부르기
  전에 질문에서 다시 확인한다."""


def _agent_prompt(state):
    # 문자열 대신 콜러블을 쓰는 이유: route()의 ROUTE_PROMPT처럼 "오늘" 날짜를
    # 매 호출 시점 기준으로 새로 계산해서 넣어야 한다 — 모듈 로드 시점에 한 번
    # 굳혀버리면 프로세스가 오래 떠 있을 때(서버 등) 날짜가 밀린다.
    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    system_text = _AGENT_SYSTEM_PROMPT.format(today=today_str, weekday=weekday_ko(today_str))
    return [SystemMessage(content=system_text), *state["messages"]]


_react_llm = ChatOpenAI(model=CHAT_MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0)
_react_agent = create_react_agent(_react_llm, tools=_TOOLS, prompt=_agent_prompt)


async def run_agent(question: str, history=None) -> str:
    # history는 도구 파라미터로 안 넘긴다 — "그날"/"더 자세히" 같은 지시어를
    # 도구 호출 전에 LLM이 스스로 구체적인 날짜/질문으로 풀어내라고, 대화
    # 맥락을 유저 메시지 안에 route()의 _format_history()와 같은 형식으로
    # 얹어준다. 도구(search_events 등)는 이미 구체화된 인자만 받으면 된다 —
    # 원본 함수들의 history= 파라미터(지시어 해석용)까지 여기서 중복으로
    # 다시 흘려보낼 필요가 없다.
    history_text = _format_history(history)
    user_content = f"{history_text}\n질문: {question}" if history_text else question

    # recursion_limit은 "LLM 호출/도구 실행" 각각을 1스텝으로 세므로, 8이면
    # 도구 호출 3~4번 정도까지 이어붙일 수 있다. 그 안에 못 끝내면
    # GraphRecursionError가 나는데, 4개짜리 도구 목록으로 답을 못 냈다면
    # 더 돌려봐야 나아질 가능성이 낮아서(질문이 도구로 못 푸는 형태거나
    # LLM이 같은 도구를 계속 잘못 부르는 경우) 재시도 대신 안내 문구로 끝낸다.
    try:
        result = await _react_agent.ainvoke(
            {"messages": [{"role": "user", "content": user_content}]},
            config={"recursion_limit": 8},
        )
    except GraphRecursionError:
        return "질문이 복잡해서 도구 호출 한도 안에 답을 못 만들었습니다. 더 구체적으로 나눠서 물어봐 주세요."
    return result["messages"][-1].content


# --- 기존 graph.py에 붙이는 얇은 레이어 -----------------------------------
# route()는 그대로 부른다 — 복합 판별은 별개 호출이라, route()가 뽑아준
# app/site 같은 부가 정보는 에이전트 경로에선 안 쓴다(에이전트가 도구
# 인자로 스스로 다시 채운다). 이렇게 나눈 이유: route()의 4-intent 스키마를
# 안 건드리고 복합 판별을 완전히 별도 관심사로 뺄 수 있어서다.

class AgentState(TypedDict, total=False):
    question: str
    k: int
    history: Optional[list]
    compound: bool
    answer: str


async def _classify_node(state: AgentState) -> dict:
    return {"compound": await is_compound(state["question"])}


async def _agent_node(state: AgentState) -> dict:
    answer = await run_agent(state["question"], history=state.get("history"))
    writer = get_stream_writer()
    writer({"type": "plan", "plan": {"intent": "복합"}})
    writer({"type": "hits", "hits": []})
    writer({"type": "token", "text": answer})
    writer({"type": "done"})
    return {"answer": answer}


async def _fixed_node(state: AgentState) -> dict:
    # 지연 임포트: graph.py를 불러올 때 이 모듈까지 항상 끌려오지 않게(순환 방지 목적은
    # 아니고, 두 진입점을 완전히 독립적으로 유지하려는 목적).
    from screenlog_langgraph.graph import ask_auto as _fixed_ask_auto

    answer, plan, hits = await _fixed_ask_auto(state["question"], k=state["k"], history=state.get("history"))
    writer = get_stream_writer()
    writer({"type": "plan", "plan": plan})
    writer({"type": "hits", "hits": hits or []})
    writer({"type": "token", "text": answer})
    writer({"type": "done"})
    return {"answer": answer}


def _branch(state: AgentState) -> str:
    return "agent" if state["compound"] else "fixed"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("classify", _classify_node)
    graph.add_node("agent", _agent_node)
    graph.add_node("fixed", _fixed_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", _branch, {"agent": "agent", "fixed": "fixed"})
    graph.add_edge("agent", END)
    graph.add_edge("fixed", END)

    return graph.compile()


_graph = build_graph()


async def ask_auto(question, k=RETRIEVE_K, history=None):
    """질문 -> 답변 문자열. 복합 질문이면 에이전트, 아니면 기존 고정 경로."""
    result = await _graph.ainvoke({"question": question, "k": k, "history": history})
    return result["answer"]


async def stream_ask_auto(question, k=RETRIEVE_K, history=None):
    """screenlog.ask.stream_ask_auto()와 같은 이벤트(plan/hits/token/done)를 낸다.
    에이전트 경로는 도구 호출 중간 과정을 스트리밍하지 않는다 — 완성된 답을
    한 번에 token 이벤트로 보낸다(3번째 답에서 설명한 스트리밍 트레이드오프)."""
    async for chunk in _graph.astream({"question": question, "k": k, "history": history},
                                       stream_mode="custom"):
        yield chunk


if __name__ == "__main__":
    import asyncio

    async def main():
        questions = [
            "이번 주에 카카오톡을 몇 번 켰어?",           # 단일 intent -> fixed
            "저번주 정리하고 이번주랑 비교해줘",              # 복합 -> agent
            "이번주 유튜브 몇 번 봤는지랑 어떤 영상 봤는지 같이 알려줘",  # 복합 -> agent
        ]
        for q in questions:
            compound = await is_compound(q)
            print(f"\n[복합={compound}] {q}")
            answer = await ask_auto(q)
            print(answer)

    asyncio.run(main())
