"""LangGraph 버전 확장 — route()가 검색/정리/비교/집계 중 하나로 못 묶는
"복합 질문"만 여기로 떨어져서 tool-calling(ReAct) 루프를 탄다.

graph.py의 고정 분기는 그대로 둔다. 이 파일은 새 경로 하나를 추가할 뿐이다:
    route()가 뽑아준 plan["compound"]를 보고, 복합이면 여기 정의된 도구
    (검색/집계/정리/비교/인수인계 등) 중 필요한 걸 LLM이 스스로 골라 여러
    번 부르게 한다.

복합 판별은 처음엔 route()와 별개인 전용 LLM 호출(is_compound())이었다.
근데 그러면 질문 하나마다 route() 1번 + 판별 1번, 최소 LLM 호출이 2번씩
든다 — route()가 이미 질문을 통째로 분석하는 김에 "이거 복합이야?"까지
같은 호출에서 답하게 하면 질문당 1회를 아낄 수 있다(router.py의
ROUTE_PROMPT에 compound 필드 추가, 트러블슈팅 문서 참고). 그래서 여기
classify 노드는 route()를 직접 부르고, 그 결과(plan)를 고정 경로에도
그대로 넘겨서(graph.py의 route 노드가 재계산 안 하도록) LLM 호출이 한
번도 중복되지 않게 했다.

도구는 새로 로직을 짜지 않는다 — screenlog.ask.ask() / screenlog.summarize.*를
그대로 감싼 것뿐이다. 그래서 "집계는 LLM이 세지 않고 count_range()로 직접
센다" 같은, 실측 버그로 얻은 불변식이 에이전트가 어떤 순서로 도구를 부르든
깨지지 않는다 — 그 불변식은 도구 호출 순서가 아니라 도구 내부에 있다.

집계/비교 정확성이 이미 검증된 단일-intent 질문은 이 경로를 절대 타지
않는다(비용/지연 때문 — 도구 호출마다 LLM 왕복이 하나씩 더 든다). 오직
route() 4갈래로 못 답하는 질문만 이 무거운 경로로 폴백한다.
"""

from datetime import datetime
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode

from screenlog.ask import ask as _ask
from screenlog.ask import build_context as _build_context
from screenlog.ask import search as _search
from screenlog.config import AGENT_CHAT_MODEL, API_KEY, BASE_URL, MAX_PERIOD_SEARCH_K, RETRIEVE_K
from screenlog.router import _APP_HINT, _SITE_HINT, _expand_period, _format_history, route
from screenlog.source import LOCAL_TZ, weekday_ko
from screenlog.summarize import compare_range as _compare_range
from screenlog.summarize import count_range as _count_range
from screenlog.summarize import draft_slack_from_search as _draft_slack_from_search
from screenlog.summarize import draft_slack_from_text as _draft_slack_from_text
from screenlog.summarize import draft_slack_range as _draft_slack_range
from screenlog.summarize import format_count as _format_count
from screenlog.summarize import handover_from_context as _handover_from_context
from screenlog.summarize import handover_range as _handover_range
from screenlog.summarize import summarize_range as _summarize_range

# --- 도구들: 기존 함수를 감싸기만 한다 ------------------------------------
# 날짜는 도구 경계에서 "YYYY-MM-DD 문자열 쌍"으로 받는다 — LLM이 채우기
# 쉬운 형태를 도구 인터페이스로 쓰고, 내부에서 _expand_period()로
# route()와 똑같이 날짜 리스트로 편다(뒤집힘 보정/최대 기간도 그대로 적용됨).

@tool(description="기간 안에서 특정 내용/대화/키워드를 찾아 답한다. \"며칠에 무슨 일 있었는지 "
                  "전부\"가 아니라 \"그 안의 특정 주제\"를 찾을 때 쓴다. start_date/end_date를 "
                  "안 주면 전체 기록에서 찾는다 (YYYY-MM-DD, 둘 다 포함). app/site 후보는 "
                  "시스템 프롬프트 참고.")
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
                  "site 후보 목록에 없는 사이트도 그대로 나온다.")
def count_events(
    start_date: str, end_date: str, app: Optional[str] = None, site: Optional[str] = None,
    group_by: str = "app",
) -> str:
    dates = _expand_period(start_date, end_date)
    counter = _count_range(dates, app=app, site=site, field=group_by)
    return _format_count(counter, top_n=10)


@tool(description="기간 안에 있었던 일 전반을 하루씩 요약해서 그대로 이어붙인다. "
                  "(YYYY-MM-DD, 둘 다 포함)")
async def summarize_days(
    start_date: str, end_date: str, app: Optional[str] = None, site: Optional[str] = None,
) -> str:
    dates = _expand_period(start_date, end_date)
    return await _summarize_range(dates, app=app, site=site)


@tool(description="기간 안의 날짜들을 서로 비교해서 차이/경향/\"언제가 제일 ~했는지\"를 "
                  "판단한다. 하루씩 요약한 뒤 그 요약들을 다시 LLM으로 비교한다. "
                  "(YYYY-MM-DD, 둘 다 포함)")
async def compare_days(
    question: str, start_date: str, end_date: str, app: Optional[str] = None, site: Optional[str] = None,
) -> str:
    dates = _expand_period(start_date, end_date)
    return await _compare_range(question, dates, app=app, site=site)


@tool(description="기간 안의 활동을 \"인수인계/작업기록\" 문서 형식(진행한 작업 / "
                  "진행 중·이어서 할 것 / 참고할 점)으로 정리한다. \"인수인계\", "
                  "\"작업기록\", \"핸드오프\" 같은 요청, 또는 \"내가 뭐까지 했는지 "
                  "다음에 이어받을 수 있게 정리해줘\"류 요청에 쓴다. 그냥 \"정리해줘\"는 "
                  "summarize_days를 써라 — 이건 인수인계 양식이 명시적으로 필요할 때만.\n\n"
                  "요청이 특정 주제로 좁혀져 있으면(예: \"AWS 관련 작업만 인수인계로\") "
                  "search_query에 그 주제를 채워라 — 그러면 기간 전체를 훑는 대신 그 안에서 "
                  "벡터 검색으로 관련 이벤트만 찾아 문서를 만든다. \"이번주 다 정리해줘\"처럼 "
                  "주제 제한이 없으면 search_query를 비워둬라(기간 전체 요약)."
                  "(YYYY-MM-DD, 둘 다 포함)")
async def draft_handover_doc(
    question: str, start_date: str, end_date: str, app: Optional[str] = None, site: Optional[str] = None,
    search_query: Optional[str] = None,
) -> str:
    dates = _expand_period(start_date, end_date)
    if search_query:
        hits = _search(search_query, k=MAX_PERIOD_SEARCH_K, app=app, site=site, dates=dates)
        return await _handover_from_context(question, _build_context(hits))
    return await _handover_range(question, dates, app=app, site=site)


@tool(description="기간 안의 활동을 슬랙 채널에 바로 올릴 수 있는 짧은 공유 메시지 초안으로 "
                  "쓴다. \"슬랙으로 공유해줘\", \"팀 채널에 알려줘\", \"슬랙 메시지 써줘\" 같은 "
                  "요청에 쓴다. 초안만 만들 뿐 실제로 전송하지는 않는다 — 이 도구를 부른 뒤 "
                  "사용자에게 초안을 보여주고 승인을 기다려라, 되묻지 않고 자동으로 올리지 않는다. "
                  "인수인계 문서 형식이 필요하면 draft_handover_doc을 대신 써라.\n\n"
                  "start_date/end_date는 둘 다 줄 때만 새로 조회한다(YYYY-MM-DD, 둘 다 포함). "
                  "그중 요청이 특정 주제로 좁혀져 있으면(예: \"AWS 얘기만 슬랙으로\") search_query에 "
                  "그 주제를 채워라 — 기간 전체를 훑는 대신 벡터 검색으로 관련 이벤트만 찾는다. "
                  "주제 제한이 없으면(\"이번주 다\") search_query는 비워둬라.\n\n"
                  "\"방금 그거 슬랙으로 보내자\"처럼 새 기간/주제 언급 없이 직전 답변을 그대로 "
                  "공유해달라는 요청이면 start_date/end_date를 둘 다 비워둬라 — 그러면 새로 "
                  "조회하지 않고 바로 직전 답변을 그대로 재포맷한다(내용이 달라질 위험이 없다). "
                  "직접 다시 타이핑해서 옮기지 마라, 도구가 알아서 가져온다.")
async def draft_slack_message(
    question: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    app: Optional[str] = None,
    site: Optional[str] = None,
    search_query: Optional[str] = None,
    history: Annotated[Optional[list], InjectedState("history")] = None,
) -> str:
    if start_date and end_date:
        dates = _expand_period(start_date, end_date)
        if search_query:
            hits = _search(search_query, k=MAX_PERIOD_SEARCH_K, app=app, site=site, dates=dates)
            return await _draft_slack_from_search(question, _build_context(hits))
        return await _draft_slack_range(question, dates, app=app, site=site)
    if not history:
        return "직전 답변이 없어서 재사용할 내용이 없습니다. 기간을 지정해서 다시 요청해주세요."
    return await _draft_slack_from_text(question, history[-1]["answer"])


_TOOLS = [search_events, count_events, summarize_days, compare_days, draft_handover_doc, draft_slack_message]

# 무제한 자유 루프가 아니라 가드레일을 둔다:
#   - 호출 가능 도구는 위 6개로 고정(화이트리스트) — 새 능력을 여기서 만들지 않는다.
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
  전에 질문에서 다시 확인한다.
- draft_slack_message/draft_handover_doc은 초안만 만들 뿐 실제로 어디에도
  전송/게시하지 않는다. 이 도구를 불렀다고 "보냈다"/"올렸다"고 말하지 않는다
  — 항상 "이 초안대로 괜찮은지" 확인을 구하는 톤으로 답한다.

app 후보(반드시 이 중 하나거나 비워둔다):
{app_hint}

site 후보(브라우저 안에서 방문한 사이트, 반드시 이 중 하나거나 비워둔다):
{site_hint}"""

def _plan_hint(plan):
    """route()가 이미 뽑아둔 app/site/기간을 에이전트한테 힌트로 준다.

    이게 없으면 에이전트가 도구 인자(app, start_date 등)를 처음부터 다시
    추론한다 — route()가 이미 한 일을 중복으로 또 하는 셈이다. 힌트로
    주면 같은 필터를 일관되게 쓰게 돼서 정확도도 올라간다. "참고"라고
    명시해서 강제는 아니게 뒀다 — route()의 intent/compound는 4갈래
    분류일 뿐이라 app/site를 잘못 좁혔을 수도 있어서, 에이전트가 필요하면
    무시하고 다시 확인할 여지를 남긴다."""
    if not plan:
        return ""
    parts = []
    if plan.get("app"):
        parts.append(f"app={plan['app']}")
    if plan.get("site"):
        parts.append(f"site={plan['site']}")
    if plan.get("periods"):
        labels = ", ".join(f"{p['label']}({p['dates'][0]}~{p['dates'][-1]})" for p in plan["periods"])
        parts.append(f"기간={labels}")
    if not parts:
        return ""
    return "참고: 질문에서 이미 뽑아낸 정보 — " + ", ".join(parts) + ". 확실하면 도구 인자에 그대로 써라(다시 추론할 필요 없음).\n"


def _agent_prompt(messages):
    # 문자열 대신 콜러블을 쓰는 이유: route()의 ROUTE_PROMPT처럼 "오늘" 날짜를
    # 매 호출 시점 기준으로 새로 계산해서 넣어야 한다 — 모듈 로드 시점에 한 번
    # 굳혀버리면 프로세스가 오래 떠 있을 때(서버 등) 날짜가 밀린다.
    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    system_text = _AGENT_SYSTEM_PROMPT.format(
        today=today_str, weekday=weekday_ko(today_str), app_hint=_APP_HINT, site_hint=_SITE_HINT,
    )
    return [SystemMessage(content=system_text), *messages]


_react_llm = ChatOpenAI(model=AGENT_CHAT_MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0)
_react_llm_with_tools = _react_llm.bind_tools(_TOOLS)
_tool_node = ToolNode(_TOOLS)


class ReactState(MessagesState):
    # draft_slack_message가 InjectedState로 직접 읽는다 — LLM에게 도구 인자로
    # 다시 타이핑시키지 않고, 대화 기록을 코드가 그대로 꽂아 넣기 위한 자리.
    history: Optional[list]


async def _agent_call_node(state: ReactState) -> dict:
    response = await _react_llm_with_tools.ainvoke(_agent_prompt(state["messages"]))
    if response.tool_calls:
        # 진행 상황 표시용 — screenpipe 앱의 "Reviewed your activity"처럼,
        # 도구 호출마다 뭘 하는 중인지 이벤트로 흘려보낸다. 실제 실행은
        # 다음 노드(_tools_call_node)에서 하고, 여기선 "이제 이걸 부를거다"만
        # 알린다 — LLM이 결정한 시점과 실제 도구 실행 시점을 UI에서
        # 구분해서 보여줄 수 있게.
        get_stream_writer()({"type": "tool_start", "tools": [c["name"] for c in response.tool_calls]})
    return {"messages": [response]}


def _after_agent(state: ReactState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "end"


async def _tools_call_node(state: ReactState) -> dict:
    # 실제 도구 실행(ToolNode)을 그대로 위임하고, 끝났을 때 이벤트만 하나
    # 더 낸다 — tool_start는 _agent_call_node가 "부르기로 결정했다" 시점에
    # 이미 냈으니, 여기선 "실행이 끝났다"만 알리면 된다.
    last = state["messages"][-1]
    names = [c["name"] for c in last.tool_calls]
    result = await _tool_node.ainvoke(state)
    get_stream_writer()({"type": "tool_done", "tools": names})
    return result


def _build_react_graph():
    # 도구를 몇 개 썼든 항상 agent로 돌아가서 "더 필요한가"를 다시 판단하게
    # 한다. 예전엔 "도구 1개짜리 첫 라운드면 끝"이라는 지름길(shortcut)이
    # 있었는데, 이 그래프는 compound=true(정리/검색/비교/집계 네 갈래로 안
    # 풀리는 복합 요청)일 때만 타는 경로라 그 가정이 위험했다 — 예를 들어
    # "8월 3일 정리해서 슬랙으로 보내"에서 LLM이 정리 도구부터 1개만
    # 부르면, 슬랙 초안 도구는 호출 기회도 없이 그 결과로 바로 끝나버릴 수
    # 있었다. LLM 호출이 매번 하나씩 더 붙는 대신, 그 재확인 자체가 이
    # 경로의 존재 이유다.
    g = StateGraph(ReactState)
    g.add_node("agent", _agent_call_node)
    g.add_node("tools", _tools_call_node)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", _after_agent, {"tools": "tools", "end": END})
    g.add_edge("tools", "agent")
    return g.compile()


_react_agent = _build_react_graph()


async def run_agent(question: str, history=None, plan=None) -> str:
    # history는 두 경로로 쓰인다:
    #   1) 텍스트로 프롬프트에 얹기 — "그날"/"더 자세히" 같은 지시어를 도구
    #      호출 전에 LLM이 스스로 구체적인 날짜/질문으로 풀어내게 한다.
    #      대부분의 도구(search_events 등)는 이미 구체화된 인자만 받으면
    #      되니 이걸로 충분하다.
    #   2) ReactState에 구조 그대로 얹기 — draft_slack_message처럼 "직전
    #      답변을 그대로 재사용"해야 하는 도구는 텍스트에서 다시 추론하게
    #      하면 위험하다(재조회 필터를 잘못 추론하면 완전히 무관한 내용이
    #      나온 사고가 실측됨). InjectedState로 코드가 직접 history[-1]을
    #      꺼내 쓰게 해서, LLM이 다시 타이핑/추론할 필요 자체를 없앤다.
    hint_text = _plan_hint(plan)
    history_text = _format_history(history)
    prefix = f"{hint_text}{history_text}"
    user_content = f"{prefix}\n질문: {question}" if prefix else question

    # recursion_limit은 "LLM 호출/도구 실행" 각각을 1스텝으로 세므로, 8이면
    # 도구 호출 3~4번 정도까지 이어붙일 수 있다. 그 안에 못 끝내면
    # GraphRecursionError가 나는데, 지금 있는 도구 목록으로 답을 못 냈다면
    # 더 돌려봐야 나아질 가능성이 낮아서(질문이 도구로 못 푸는 형태거나
    # LLM이 같은 도구를 계속 잘못 부르는 경우) 재시도 대신 안내 문구로 끝낸다.
    #
    # ainvoke() 대신 astream(stream_mode=["custom", "values"])을 쓰는 이유:
    # 내부 그래프(_agent_call_node/_tools_call_node)가 get_stream_writer()로
    # 낸 tool_start/tool_done 이벤트는, 내부 그래프를 ainvoke()로만 부르면
    # 아무 데도 안 잡히고 사라진다(스트리밍 컨텍스트가 없으면 no-op).
    # 여기(run_agent)는 바깥 그래프(_agent_node)가 이미 astream()으로 돌고
    # 있는 도중에 호출되므로, 안쪽 이벤트를 여기서 직접 받아 바깥
    # writer로 그대로 릴레이해야 진행 상황이 실제로 밖에 전달된다.
    # "values" 모드는 매 스텝 전체 state를 주므로, 마지막 값에서 최종
    # 답변(messages[-1])을 꺼낸다 — custom 모드만 쓰면 최종 답을 못 얻는다.
    writer = get_stream_writer()
    final_messages = None
    try:
        async for mode, chunk in _react_agent.astream(
            {"messages": [{"role": "user", "content": user_content}], "history": history},
            config={"recursion_limit": 8},
            stream_mode=["custom", "values"],
        ):
            if mode == "custom":
                writer(chunk)
            else:
                final_messages = chunk["messages"]
    except GraphRecursionError:
        return "질문이 복잡해서 도구 호출 한도 안에 답을 못 만들었습니다. 더 구체적으로 나눠서 물어봐 주세요."
    return final_messages[-1].content


# --- 기존 graph.py에 붙이는 얇은 레이어 -----------------------------------
# classify 노드가 route()를 직접 부르고 plan 전체를 state에 남긴다.
# fixed로 가면 이 plan을 graph.py에 그대로 넘겨서 route()가 두 번 안
# 불리게 한다(위 모듈 docstring 참고) — agent로 가면 plan은 그냥 버려진다
# (에이전트는 도구 인자로 스스로 app/site/기간을 다시 채운다).

class AgentState(TypedDict, total=False):
    question: str
    k: int
    history: Optional[list]
    plan: dict
    answer: str


async def _classify_node(state: AgentState) -> dict:
    plan = await route(state["question"], history=state.get("history"))
    return {"plan": plan}


async def _agent_node(state: AgentState) -> dict:
    answer = await run_agent(state["question"], history=state.get("history"), plan=state.get("plan"))
    writer = get_stream_writer()
    # classify 노드가 이미 뽑아둔 진짜 plan을 그대로 내보낸다. 예전엔
    # {"intent": "복합"}이라는 자리표시자만 보냈는데, 프론트(renderPlan())가
    # plan.periods를 항상 있다고 가정하고 읽어서 실제로 던지면(TypeError:
    # Cannot read properties of undefined (reading 'reduce')) 터졌다 — 지금까지
    # api.py가 이 경로를 아예 안 썼어서 안 드러났을 뿐이었다.
    writer({"type": "plan", "plan": state.get("plan") or {}})
    writer({"type": "hits", "hits": []})
    writer({"type": "token", "text": answer})
    writer({"type": "done"})
    return {"answer": answer}


async def _fixed_node(state: AgentState) -> dict:
    # 지연 임포트: graph.py를 불러올 때 이 모듈까지 항상 끌려오지 않게(순환 방지 목적은
    # 아니고, 두 진입점을 완전히 독립적으로 유지하려는 목적).
    from screenlog_langgraph.graph import ask_auto as _fixed_ask_auto

    answer, plan, hits = await _fixed_ask_auto(state["question"], k=state["k"], history=state.get("history"),
                                                plan=state["plan"])
    writer = get_stream_writer()
    writer({"type": "plan", "plan": plan})
    writer({"type": "hits", "hits": hits or []})
    writer({"type": "token", "text": answer})
    writer({"type": "done"})
    return {"answer": answer}


def _branch(state: AgentState) -> str:
    return "agent" if state["plan"]["compound"] else "fixed"


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
    """screenlog.ask.stream_ask_auto()와 같은 이벤트(plan/hits/token/done)에
    더해, 에이전트 경로에선 tool_start/tool_done도 낸다. 최종 답 자체는
    토큰 단위로 못 쪼갠다(도구가 이미 완성된 문장을 돌려주므로) — 대신
    "지금 어떤 도구가 실행 중인지"를 실시간으로 흘려보내서, 답이 나오기
    전까지 화면이 먹통처럼 안 보이게 한다(screenpipe 앱의 진행 상황
    표시와 같은 목적)."""
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
            plan = await route(q)
            print(f"\n[복합={plan['compound']}] {q}")
            answer = await ask_auto(q)
            print(answer)

    asyncio.run(main())
