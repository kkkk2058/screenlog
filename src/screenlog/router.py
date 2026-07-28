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

from screenlog.config import BASE_URL, CHAT_MODEL
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


def route_rules(question, today):
    """규칙만으로 라우팅. LLM 호출 없음, 공짜."""
    return {
        "app": find_app(question),
        "hour": find_hour(question),
        "date": find_date(question, today),
    }


LLM_PROMPT = """질문을 읽고 화면 기록 검색 필터를 뽑아라. JSON만 출력해라.

app: 다음 중 하나 또는 null - {apps}
    질문이 특정 앱이나 앱 종류(에디터, 메신저, 화상회의 등)를 가리킬 때만 채운다.
    "~에 대해 뭘 찾아봤어", "~얘기했어"처럼 내용을 묻는 질문은 앱을 추측하지 말고 null로 둔다.
hour: 0~23 정수 또는 null (한국 시간, "오후 3시"면 15)
date: "YYYY-MM-DD" 또는 null (오늘은 {today})

질문: {question}

출력 예시: {{"app": "카카오톡", "hour": null, "date": null}}
출력 예시(내용 질문): {{"app": null, "hour": null, "date": null}}"""


def route_llm(question, today):
    """규칙이 못 잡은 표현을 LLM에게 맡긴다. ("화상회의 앱에서", "그 채팅 앱" 등)

    LLM 응답은 신뢰할 수 없는 외부 입력으로 취급한다 — JSON이 아닐 수도 있고,
    코퍼스에 없는 앱 이름을 지어낼 수도 있다. 그래서 여기서만 방어적으로 검사한다.
    """
    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENAI_API_KEY"])
    prompt = LLM_PROMPT.format(
        apps=", ".join(APP_ALIASES.keys()),
        today=today.strftime("%Y-%m-%d"),
        question=question,
    )
    response = client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.choices[0].message.content

    # LLM이 ```json 같은 껍데기를 붙일 수 있어서, {...} 부분만 찾아낸다.
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {"app": None, "hour": None, "date": None}

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"app": None, "hour": None, "date": None}

    app = parsed.get("app")
    if app not in APP_ALIASES:              # 코퍼스에 없는 앱을 지어냈을 수 있다
        app = None

    hour = parsed.get("hour")
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        hour = None

    date = parsed.get("date")
    if not isinstance(date, str):
        date = None

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
    """질문 -> {app, hour, date} 딕셔너리.

    mode:
        rules   규칙만. 공짜, 즉시. 사전에 없는 표현은 못 잡는다.
        llm     LLM만. 유연하지만 호출마다 비용이 든다.
        hybrid  규칙 먼저, 아무것도 못 잡았을 때만 LLM (기본).

    today를 안 주면 실행 시점의 오늘 날짜를 쓴다. 테스트할 때는 특정
    날짜를 고정해서 넣을 수 있게 인자로 남겨뒀다.
    """
    if today is None:
        today = datetime.now(LOCAL_TZ)

    if mode == "rules":
        _stats["rules"] += 1
        return route_rules(question, today)

    if mode == "llm":
        _stats["llm"] += 1
        return route_llm(question, today)

    # hybrid
    plan = route_rules(question, today)
    if _has_filter(plan):
        _stats["rules"] += 1
        return plan
    _stats["llm"] += 1
    return route_llm(question, today)


if __name__ == "__main__":
    questions = [
        "카카오톡에서 무슨 대화를 했어?",
        "zoom 회의에서 뭘 했어?",
        "VS Code에서 무슨 코드를 봤어?",
        "어제 뭐 했어?",
        "7월 24일에 무슨 작업했어?",
        "오후 3시에 뭐 하고 있었어?",
        "오늘 하루 뭐 했는지 정리해줘",
    ]
    for q in questions:
        plan = route(q)
        print(f"app={str(plan['app']):15} hour={str(plan['hour']):5} "
              f"date={str(plan['date']):12} | {q}")
