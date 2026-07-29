"""3. 라우팅 — 질문 문장에서 app/hour/date를 자동으로 뽑는다

2단계에서는 사람이 `ask("질문", app="카카오톡")`처럼 필터를 직접 넣어줬다.
여기서는 그 필터를 **질문 문장만 보고** 알아내는 함수를 만든다.

규칙만으로는 한계가 있다 — APP_ALIASES에 없는 표현("화상회의 앱")은 규칙이
못 잡고, 그러면 필터 없이 조용히 vanilla 검색으로 떨어진다. 그래서 규칙이
아무것도 못 잡았을 때만 LLM에게 물어본다(hybrid). 매번 LLM을 부르지 않는
이유는 비용 때문이다 — 앱 이름을 그대로 말하는 흔한 질문은 규칙으로 공짜에
끝나고, 사전에 없는 표현일 때만 LLM 비용을 쓴다.
"""

import json
import os
import re
from datetime import datetime, timedelta

from openai import OpenAI

from screenlog.config import BASE_URL, CHAT_MODEL, MAX_RANGE_DAYS
from screenlog.source import LOCAL_TZ

# 코퍼스에 실제로 있는 app 값 -> 사람이 질문에 쓸 법한 표현들.
# where 필터는 완전 일치라서, "줌"이라고 물으면 "zoom.us"로 바꿔줘야 걸린다.
APP_ALIASES = {
    "카카오톡": ["카카오톡", "카톡"],
    "Google Chrome": ["크롬", "chrome", "구글 크롬"],
    "Claude": ["클로드", "claude"],
    "Code": ["vs code", "브이에스코드", "코드 에디터", "code"],
    "zoom.us": ["zoom", "줌"],
    "Discord": ["discord", "디스코드", "디코"],
    "Finder": ["finder", "파인더"],
    "터미널": ["터미널", "terminal"],
}


def find_app(question):
    """질문에서 앱 이름을 찾는다. 못 찾으면 None.

    별칭이 여러 개 걸릴 수 있어서, 가장 긴 별칭이 이긴 걸로 한다.
    "code"가 "vs code" 안의 "code"를 가로채지 않게 하려는 것이다.
    """
    question_lower = question.lower()

    best_app = None
    best_length = 0
    for app, aliases in APP_ALIASES.items():
        for alias in aliases:
            if alias.lower() in question_lower:
                if len(alias) > best_length:
                    best_app = app
                    best_length = len(alias)

    return best_app


def find_date(question, today):
    """질문에서 날짜를 찾는다. "어제", "7월 24일" 같은 표현만 다룬다.

    상대 표현("어제", "그제")은 today를 기준으로 계산한다. today를 인자로
    받는 이유는, 실행 시점의 실제 오늘 날짜는 이 함수가 알 필요 없고
    호출하는 쪽(route())이 한 곳에서만 계산하게 하기 위해서다.
    """
    if "그제" in question or "그저께" in question:
        day = today - timedelta(days=2)
        return day.strftime("%Y-%m-%d")

    if "어제" in question:
        day = today - timedelta(days=1)
        return day.strftime("%Y-%m-%d")

    if "오늘" in question:
        return today.strftime("%Y-%m-%d")

    # "7월 24일" 같은 절대 날짜. 연도는 질문에 안 나오니 today의 연도를 쓴다.
    match = re.search(r"(\d{1,2})월\s*(\d{1,2})일", question)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        return f"{today.year}-{month:02d}-{day:02d}"

    return None


def find_hour(question):
    """질문에서 시각을 찾는다. "오후 3시" -> 15. 못 찾으면 None.

    오전/오후가 없는 "3시"는 낮 시간으로 본다. 화면 기록을 새벽 3시에
    물어보는 경우는 드물기 때문이다. 틀리면 이 필터가 엉뚱한 시간대를
    줄 뿐이니, 나중에 eval로 확인하면 바로 드러난다.
    """
    match = re.search(r"(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시", question)
    if not match:
        return None

    ampm = match.group(1)
    hour = int(match.group(2))

    if hour > 23:
        return None

    if ampm in ("오후", "저녁", "밤"):
        if hour == 12:
            return 12
        return hour + 12

    if ampm in ("오전", "아침"):
        if hour == 12:
            return 0
        return hour

    # 오전/오후 표시가 없다. 1~8시는 새벽보다 오후로 보는 게 자연스럽다.
    if 1 <= hour <= 8:
        return hour + 12
    return hour


def find_date_range(question, today):
    """"이번 주"/"지난 3일" 같은 표현 -> 날짜 문자열 리스트(오름차순). 없으면 None.

    find_date()는 날짜 하나만 다룬다. 여러 날에 걸친 질문은 답을 만드는 방식
    자체가 다르므로(하루씩 요약 후 합치기), 여기서 따로 뽑아 리스트로 돌려준다.
    """
    if re.search(r"이번\s*주", question):
        monday = today - timedelta(days=today.weekday())
        n_days = today.weekday() + 1   # 월요일부터 오늘까지
        return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_days)]

    if re.search(r"(저번|지난)\s*주", question):
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        return [(last_monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    match = re.search(r"(?:최근|지난)\s*(\d{1,3})\s*일", question)
    if match:
        n = min(int(match.group(1)), MAX_RANGE_DAYS)   # 안전 상한
        return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]

    return None


# 여러 날짜 질문이 정리/비교/집계 중 뭔지 구분하는 신호 단어.
# COUNT_WORDS를 먼저 본다 — "가장 많이 쓴 앱"처럼 두 목록에 다 걸릴 수 있는
# 문장은 카운트 신호("많이")가 더 구체적이라 그쪽을 우선한다.
COUNT_WORDS = ["몇 번", "몇 개", "얼마나 자주", "가장 많이", "제일 많이",
               "집계", "모아", "합계", "통계", "총"]
COMPARE_WORDS = ["제일", "가장", "언제가", "비교", "차이", "패턴", "트렌드", "경향"]


def classify_range_question(question):
    """여러 날짜 질문을 정리/비교/집계 중 하나로 분류한다.

    셋이 답을 만드는 방식이 완전히 다르다 — 정리는 그냥 이어붙이고, 비교는
    LLM에게 다시 한번 판단시키고, 집계는 LLM 없이 metadata를 직접 센다.
    """
    if any(word in question for word in COUNT_WORDS):
        return "집계"
    if any(word in question for word in COMPARE_WORDS):
        return "비교"
    return "정리"


def route_rules(question, today):
    """규칙만으로 라우팅. LLM 호출 없음, 공짜."""
    return {
        "app": find_app(question),
        "hour": find_hour(question),
        "date": find_date(question, today),
    }


LLM_PROMPT = """질문을 읽고 화면 기록 검색 필터를 뽑아라.

세 필드 다 같은 규칙이다 — 질문에 그 정보가 실제로 언급됐을 때만 채우고,
없으면 추측하지 말고 null로 둔다.

app: 특정 앱이나 앱 종류(에디터, 메신저, 화상회의 등)를 가리킬 때만.
    "~에 대해 뭘 찾아봤어"처럼 내용만 묻는 질문은 null.
hour: "오후 3시"처럼 시각이 실제로 언급됐을 때만 (한국 시간, 0~23). "무슨 명령을
    실행했어?"처럼 시각 언급이 없으면 null. 오늘 몇 시인지와는 무관하다.
date: "어제", "오늘", "7월 24일"처럼 날짜가 실제로 언급됐을 때만 ("YYYY-MM-DD",
    오늘은 {today}). 날짜 언급이 없는 질문에 오늘 날짜를 채우지 않는다.

질문: {question}"""


# app을 이 스키마의 enum으로 강제한다. LLM이 코퍼스에 없는 앱 이름을 "지어내는" 것
# 자체가 API 레벨에서 불가능해진다 — 응답을 받은 뒤 골라내는 게 아니라, 애초에
# 이 8개 중 하나 또는 null만 낼 수 있게 막는다.
def _route_schema():
    return {
        "type": "object",
        "properties": {
            "app": {"type": ["string", "null"], "enum": [*APP_ALIASES.keys(), None]},
            "hour": {"type": ["integer", "null"], "minimum": 0, "maximum": 23},
            "date": {"type": ["string", "null"]},
        },
        "required": ["app", "hour", "date"],
        "additionalProperties": False,
    }


def route_llm(question, today):
    """규칙이 못 잡은 표현을 LLM에게 맡긴다. ("화상회의 앱에서", "그 채팅 앱" 등)

    response_format으로 스키마를 강제한다(structured output). app이 enum이라
    코퍼스에 없는 앱을 지어낼 수 없고, hour도 0~23 범위 밖으로 못 나온다 —
    응답을 받은 뒤 방어적으로 걸러내던 걸 API가 애초에 막아준다.
    date는 형식까지는 스키마로 강제 못 해서 최소한의 확인만 남겨둔다.
    """
    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENAI_API_KEY"])
    prompt = LLM_PROMPT.format(today=today.strftime("%Y-%m-%d"), question=question)
    response = client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "route_plan", "schema": _route_schema(), "strict": True},
        },
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        parsed = json.loads(response.choices[0].message.content)
    except json.JSONDecodeError:
        return {"app": None, "hour": None, "date": None}

    date = parsed.get("date")
    if not isinstance(date, str):
        date = None

    return {"app": parsed.get("app"), "hour": parsed.get("hour"), "date": date}

    return {"app": app, "hour": hour, "date": date}


def _has_filter(plan):
    """규칙이 뭐라도 잡았나. 셋 다 None이면 규칙이 한 일이 없다는 뜻이다."""
    return plan["app"] is not None or plan["hour"] is not None or plan["date"] is not None


# route()가 규칙으로 끝났는지 LLM까지 갔는지 센다. LLM 라우팅이 실제로 얼마나
# 자주 발동하는지는 짐작이 아니라 재봐야 안다 — 매번 부르면 비용이 늘고,
# 안 부르면 사전에 없는 표현을 놓친다.
_stats = {"rules": 0, "llm": 0}


def get_stats():
    """지금까지 route()가 규칙으로 끝난 횟수 / LLM까지 간 횟수."""
    return dict(_stats)


def reset_stats():
    _stats["rules"] = 0
    _stats["llm"] = 0


def route(question, mode="hybrid", today=None):
    """질문 -> {app, hour, date, dates, intent} 딕셔너리.

    mode:
        rules   규칙만. 공짜, 즉시. 사전에 없는 표현은 못 잡는다.
        llm     LLM만. 유연하지만 호출마다 비용이 든다.
        hybrid  규칙 먼저, 아무것도 못 잡았을 때만 LLM (기본).

    today를 안 주면 실행 시점의 오늘 날짜를 쓴다. 테스트할 때는 특정
    날짜를 고정해서 넣을 수 있게 인자로 남겨뒀다.

    dates/intent는 app/hour/date와 별개로 항상 규칙만으로 계산한다. 날짜
    범위를 LLM에게 맡길 정도로 애매한 표현은 아직 다루지 않기 때문이다.
    """
    if today is None:
        today = datetime.now(LOCAL_TZ)

    if mode == "rules":
        _stats["rules"] += 1
        plan = route_rules(question, today)
    elif mode == "llm":
        _stats["llm"] += 1
        plan = route_llm(question, today)
    else:
        # hybrid
        plan = route_rules(question, today)
        if _has_filter(plan):
            _stats["rules"] += 1
        else:
            _stats["llm"] += 1
            plan = route_llm(question, today)

    dates = find_date_range(question, today)
    plan["dates"] = dates
    plan["intent"] = classify_range_question(question) if dates else None
    return plan


if __name__ == "__main__":
    questions = [
        "카카오톡에서 무슨 대화를 했어?",
        "zoom 회의에서 뭘 했어?",
        "VS Code에서 무슨 코드를 봤어?",
        "어제 뭐 했어?",
        "7월 24일에 무슨 작업했어?",
        "오후 3시에 뭐 하고 있었어?",
        "오늘 하루 뭐 했는지 정리해줘",
        "이번 주에 주로 무슨 일을 했어?",
        "이번 주에 카카오톡을 몇 번 켰어?",
        "이번 주 언제가 제일 바빴어?",
    ]
    for q in questions:
        plan = route(q)
        dates = f"{len(plan['dates'])}일" if plan["dates"] else "-"
        print(f"app={str(plan['app']):15} hour={str(plan['hour']):5} "
              f"date={str(plan['date']):12} dates={dates:5} "
              f"intent={str(plan['intent']):5} | {q}")
