"""LangChain 버전 — route() 결과를 보고 알맞은 체인으로 보내는 ask_auto().

원본(screenlog.ask.ask_auto)의 분기 자체는 여기서도 그대로 일반 Python
if/elif로 남겨뒀다. RunnableBranch로 옮길 수도 있었지만, 이 분기는
"route() 한 번의 결과로 확정되고 그 뒤로 재판단이 없는" 고정된 흐름이라
(원본 summarize.py 주석의 표현을 빌리면 map-reduce와 같은 성격) 그래프/체인
분기로 감싼다고 더 명확해지지 않는다. LangChain을 쓴 부분은 실제로 LLM을
부르는 지점(chains.py)이고, 그걸 어떤 순서로 부를지는 여전히 평범한 코드다.
"""

from screenlog.config import MAX_PERIOD_SEARCH_K, RETRIEVE_K
from screenlog.summarize import count_range, format_count
from screenlog_langchain.chains import ask, compare_periods, compare_range, summarize_period, summarize_range
from screenlog_langchain.router import route


def ask_auto(question, k=RETRIEVE_K):
    """질문만 받아서 route()로 필터를 뽑고, 알맞은 방식으로 답한다.

    (답변, plan, hits) 3개를 돌려준다 — 원본 ask.ask_auto()와 동일한 계약.
    """
    plan = route(question)
    periods = plan["periods"]
    app, hour_start, hour_end, site = plan["app"], plan["hour_start"], plan["hour_end"], plan["site"]

    if plan["intent"] == "검색":
        dates = [d for period in periods for d in period["dates"]] or None
        search_k = MAX_PERIOD_SEARCH_K if dates else k
        answer, hits = ask(question, k=search_k, app=app, hour_start=hour_start,
                            hour_end=hour_end, site=site, dates=dates)
        return answer, plan, hits

    if len(periods) >= 2:
        if plan["intent"] == "집계":
            blocks = []
            for period in periods:
                counter = count_range(period["dates"], app=app, site=site)
                blocks.append(f"[{period['label']}]\n{format_count(counter)}")
            answer = "\n\n".join(blocks)
        elif plan["intent"] == "정리":
            answer = "\n\n".join(
                summarize_period(period, app=app, hour_start=hour_start, hour_end=hour_end, site=site)
                for period in periods
            )
        else:
            answer = compare_periods(question, periods, app=app, hour_start=hour_start,
                                      hour_end=hour_end, site=site)
        return answer, plan, None

    if len(periods) == 1:
        dates = periods[0]["dates"]
        if plan["intent"] == "집계":
            counter = count_range(dates, app=app, site=site)
            answer = format_count(counter)
        elif plan["intent"] == "비교":
            answer = compare_range(question, dates, app=app, hour_start=hour_start,
                                    hour_end=hour_end, site=site)
        else:
            answer = summarize_range(dates, app=app, hour_start=hour_start, hour_end=hour_end, site=site)
        return answer, plan, None

    answer, hits = ask(question, k=k, app=app, hour_start=hour_start, hour_end=hour_end, site=site)
    return answer, plan, hits
