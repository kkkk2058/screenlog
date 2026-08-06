"""LangChain 버전 라우팅 — screenlog.router.route()와 입출력이 동일하다.

프롬프트/스키마/사후 검증 로직(주석의 "왜"까지 포함)은 원본 것을 그대로
재사용한다. 바뀐 건 구조화 출력을 뽑는 방법 하나뿐이다 — 원본은
`client.chat.completions.create(response_format=...)`를 직접 호출하고,
여기서는 `ChatOpenAI.with_structured_output()`으로 같은 일을 한다.
"""

from datetime import datetime

from langchain_openai import ChatOpenAI

from screenlog.config import API_KEY, BASE_URL, CHAT_MODEL, MAX_RANGE_DAYS
from screenlog.router import (
    ROUTE_PROMPT,
    _APP_HINT,
    _SITE_HINT,
    _expand_period,
    _parse_hour_range,
    _route_schema,
)
from screenlog.source import LOCAL_TZ

_llm = ChatOpenAI(model=CHAT_MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0)
# json schema(dict)를 그대로 넘기면 원본의 _route_schema()가 강제하던
# enum/anyOf 제약(예: app이 후보 밖의 값이 되는 걸 API 레벨에서 막는 것)이
# 그대로 유지된다. pydantic 모델로 다시 정의하지 않은 이유다.
# langchain-openai가 내부적으로 이 스키마를 함수 스키마로도 변환하는데,
# 그 경로가 top-level "title"을 함수 이름으로 요구해서 원본 스키마엔 없던
# 키를 여기서만 붙인다.
_ROUTE_SCHEMA = {"title": "route_plan", **_route_schema()}
_structured_llm = _llm.with_structured_output(_ROUTE_SCHEMA, method="json_schema")


def route(question, today=None):
    """질문 -> {app, site, hour_start, hour_end, periods, intent} 딕셔너리.

    원본과 같은 방어 로직을 그대로 쓴다: 구조화 출력 호출 자체가 실패하면
    "필터 없이 검색"으로 안전하게 떨어지고(원본은 json.JSONDecodeError만
    잡았지만, 여기선 LangChain 쪽 예외까지 폭넓게 잡는다 — 게이트웨이가
    파싱 실패 대신 다른 형태의 에러를 던질 수 있어서다), hour_range/periods는
    각각 _parse_hour_range/_expand_period로 한 번 더 검증한다.
    """
    if today is None:
        today = datetime.now(LOCAL_TZ)

    prompt = ROUTE_PROMPT.format(
        today=today.strftime("%Y-%m-%d"), app_hint=_APP_HINT, site_hint=_SITE_HINT,
        max_days=MAX_RANGE_DAYS, question=question,
    )

    try:
        parsed = _structured_llm.invoke(prompt)
    except Exception:
        parsed = {"app": None, "site": None, "hour_range": None, "periods": [], "intent": "검색"}

    hour_start, hour_end = _parse_hour_range(parsed.get("hour_range"))

    periods = []
    for p in parsed.get("periods") or []:
        try:
            dates = _expand_period(p["start"], p["end"])
        except (KeyError, ValueError, TypeError):
            continue
        periods.append({"label": p.get("label") or "", "dates": dates})

    intent = parsed.get("intent")
    if intent not in ("검색", "정리", "비교", "집계"):
        intent = "검색"

    return {
        "app": parsed.get("app"),
        "site": parsed.get("site"),
        "hour_start": hour_start,
        "hour_end": hour_end,
        "periods": periods,
        "intent": intent,
    }
