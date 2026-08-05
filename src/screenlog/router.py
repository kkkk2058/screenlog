"""3. 라우팅 — 질문 문장에서 app/hour/기간/intent를 뽑는다

원래는 정규식 규칙(app 별칭 매칭, 날짜 정규식, 신호 단어 목록)을 먼저 쓰고
규칙이 못 잡을 때만 LLM을 불렀다(hybrid). 그런데 "저번주 정리하고 이번주랑
비교" 처럼 기간이 여러 개 섞인 문장에서 규칙이 계속 깨졌다 — 정규식은 기간을
하나만 뽑게 짜여 있었고, 신호 단어 우선순위도 문장이 조금만 비틀리면
틀린 쪽을 골랐다. 규칙을 계속 추가하며 쫓아가는 대신, 질문 분석 자체를
LLM 구조화 출력 하나로 통일했다.

개인용 도구라 질문당 LLM 호출 1번의 비용/지연은 감당할 만하고, 그 대신
"기간이 여러 개", "기간+비교가 같이 오는" 같은 조합을 규칙 추가 없이
처리할 수 있다. app 필드는 여전히 enum으로 강제해서 코퍼스에 없는 앱을
지어내는 것 자체를 API 레벨에서 막는다.
"""

import json
from datetime import datetime, timedelta

from openai import AsyncOpenAI

from screenlog.config import API_KEY, BASE_URL, CHAT_MODEL, MAX_RANGE_DAYS
from screenlog.source import LOCAL_TZ

# 코퍼스에 실제로 있는 app 값 -> 사람이 질문에 쓸 법한 표현들.
# LLM에게 정확한 enum 값을 알려주는 용도로 프롬프트에 그대로 박아 넣는다.
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

_APP_HINT = "\n".join(f"    {app}: {'/'.join(aliases)}" for app, aliases in APP_ALIASES.items())

# app은 "Google Chrome"처럼 실행 중인 프로그램 단위라, "유튜브에서 본 영상"처럼
# 브라우저 *안에서* 방문한 사이트는 app만으로 못 거른다("이번 주 유튜브 정리해줘"가
# 크롬 전체를 다 정리해버린 게 이 문제였다) — window 제목에 사이트 이름이 그대로
# 찍히길래(실측: "... - YouTube - Chrome - ...") 그걸 부분 문자열로 거르는
# 별도 필터를 뒀다. chroma where는 metadata 부분일치를 못 해서, site는 chroma
# 쿼리가 아니라 가져온 이벤트를 파이썬에서 후처리로 거른다(browse_events.py 참고).
SITE_ALIASES = {
    "YouTube": ["유튜브", "youtube"],
    "Notion": ["노션", "notion"],
    "Gmail": ["지메일", "gmail"],
    "GitHub": ["깃허브", "github"],
    "Google Docs": ["구글 독스", "google docs", "docs.google.com"],
    "Google Calendar": ["구글 캘린더", "google calendar", "calendar.google.com"],
}

_SITE_HINT = "\n".join(f"    {site}: {'/'.join(aliases)}" for site, aliases in SITE_ALIASES.items())

# SITE_ALIASES는 사용자 말투("유튜브")를 친숙한 이름("YouTube")으로 좁히는
# 용도고, 이건 그 친숙한 이름을 실제 metadata 도메인("youtube.com")으로
# 다시 좁히는 용도다 — 둘이 하는 일이 다르다(자연어 이해 vs 정확한 필터링).
# 실측 도메인 기준으로 채웠다(예: Notion은 notion.so가 아니라 app.notion.com로
# 찍힘). 서비스가 도메인을 바꾸면 여기만 손보면 된다.
SITE_DOMAINS = {
    "YouTube": ("youtube.com",),
    "Notion": ("app.notion.com", "notion.so", "notion.site"),
    "Gmail": ("mail.google.com",),
    "GitHub": ("github.com",),
    "Google Docs": ("docs.google.com",),
    "Google Calendar": ("calendar.google.com",),
}


def site_matches(site, meta):
    """plan['site'](예: "YouTube")가 이벤트 하나(meta 또는 event dict)와
    맞는지 판단한다.

    meta에 site(도메인, clean.py의 site_from_url 참고)가 있으면 정확히
    비교한다 — 창 제목보다 신뢰도가 높다(실측: 창 제목이 실제보다 늦게
    바뀐 채로 남아있던 사례). site가 없는 이벤트(8/1 이전 미백필 데이터)는
    도메인을 모르니, 그 기간 검색이 통째로 안 되는 것보단 낫다고 보고
    예전 방식(창 제목 부분 문자열)으로 폴백한다."""
    event_site = meta.get("site")
    if event_site:
        return event_site in SITE_DOMAINS.get(site, ())
    return site.lower() in meta.get("window", "").lower()






ROUTE_PROMPT = """질문을 읽고 화면 기록 조회 계획을 세워라. 오늘은 {today}이다.
{history}
현재 질문에 "그날"/"거기서"/"그거" 같은 지시어가 있으면, 위 이전 대화를 참고해서
무엇을 가리키는지 판단한 뒤 아래 필드를 채운다. 이전 대화가 없으면 이 문단은 무시한다.

app: 특정 앱이나 앱 종류를 가리킬 때만 아래 후보 중 하나로 채운다. 없으면 null.
{app_hint}

site: 브라우저 안에서 방문한 특정 사이트/서비스를 가리킬 때만 아래 후보 중
    하나로 채운다("유튜브에서 뭐 봤어" 등). app이 "Google Chrome"이어도 site가
    없으면 브라우저 전체를 다 보게 되니, 사이트가 언급됐으면 반드시 채운다.
{site_hint}

hour_range: "오후 3시"나 "2시부터 4시까지"처럼 시각이 실제로 언급됐을 때만
    채운다 (한국 시간, 0~23). {{"start": int, "end": int}}이고, 시각이
    하나만 언급되면 start==end. 언급이 없으면 start/end 둘 다 null.

periods: 질문이 다루는 기간을 리스트로 뽑는다.
    - "어제 뭐 했어"처럼 하루짜리 질문도 기간 하나로 취급한다 (start==end).
    - "이번 주", "저번 주", "최근 7일"처럼 범위 질문도 기간 하나.
    - "저번주 정리하고 이번주랑 비교"처럼 기간이 여러 개 언급되면 각각을
      별도 항목으로 넣는다 (이 예시는 2개).
    - 특정 기간 언급이 전혀 없는 질문("카카오톡에서 무슨 얘기 했어?")은
      빈 리스트로 둔다 — 그 경우 전체 기록에서 검색한다.
    각 항목은 {{"label": str, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}이고
    start<=end. 한 기간이 {max_days}일을 넘지 않게 한다.

intent: 답을 만드는 방식을 고른다. periods가 비어 있으면 무조건 검색이다.
    periods가 있어도 아래 기준으로 넷 중 하나를 고른다:
    검색 — 기간이 있든 없든, "찾아봐", "무슨 얘기 했어", "~에 대해 뭐라고
        했어"처럼 기간 전체가 아니라 **그 안의 특정 내용/대화/키워드**를
        찾아야 하는 질문. "이번 주에 카톡에서 약속 잡은 거 찾아봐"는 그
        주에 있었던 일 전체가 아니라 "약속" 관련 대화만 원하는 것이므로
        periods가 있어도 검색이다.

        
    정리 — 기간 안에 있었던 일 전반을 그대로 보여주면 되는 질문
        ("~정리해줘", "~뭐 했어" 같이 특정 주제로 좁히지 않는 경우)
    비교 — 기간 사이의 차이나 경향, "언제가/며칠이 제일 ~했는지"처럼 날짜를
        가로질러 판단해야 하는 질문 (기간이 여러 개면 보통 이거다)
    집계 — "몇 번 켰어", "얼마나 자주 썼어"처럼 사용 횟수를 세면 끝나는 질문.
        "제일"이 있어도 세는 대상이 "며칠"이 아니라 "앱/행동의 횟수"일 때만
        집계다. "언제가 제일 바빴어"는 날짜를 비교하는 것이므로 비교다.

count_by_site: intent가 집계일 때만 의미가 있다. "크롬에서 뭘 많이 봤어",
    "브라우저에서 어떤 사이트 많이 갔어"처럼 앱 하나의 총 횟수가 아니라
    **그 안에서 방문한 사이트(도메인)별로 나눠서 세야** 답이 되는 질문이면
    true. app을 안 물어보고 그냥 "몇 번 켰어"처럼 앱 단위로 세면 되는
    질문은 false.

compound: 아래 둘 중 하나라도 해당하면 true.
    (1) 위 intent 하나(+periods 여러 개)만으로는 완전히 답할 수 없는 경우.
        "저번주 정리하고 이번주랑 비교"는 periods가 2개인 비교 하나로 이미
        답이 되므로 false다 — 기간이 여러 개인 것 자체는 복합이 아니다.
        "이번주 유튜브 몇 번 봤는지랑 어떤 영상 봤는지 같이 알려줘"처럼
        집계와 검색처럼 서로 다른 intent가 둘 다 필요한 경우만 true다.
    (2) "인수인계", "작업기록", "핸드오프", "슬랙"(슬랙 메시지/공유)처럼
        검색/정리/비교/집계 네 가지 방식 자체로는 표현이 안 되는 별도
        양식/능력을 요구하는 경우 (예: "다음에 이어받을 수 있게 정리해줘"도
        인수인계 요청이고, "슬랙 메시지 써줘"도 슬랙 공유 요청이다 —
        "슬랙"이 언급되면 정리 내용이라도 이 양식이 필요한 것이다).
    둘 다 아니면 false — 애매해도 우선 false로 둔다(비용이 더 드는 쪽은
    틀렸을 때 손해가 크다).

질문: {question}"""







def _route_schema():
    # app/site는 "anyOf: [값 있는 타입, null]" 형태로 nullable을 표현한다.
    # {"type": ["string", "null"], "enum": [...]}처럼 enum과 유니언 타입을
    # 섞으면 게이트웨이(OpenAI 호환)는 받아주지만 Anthropic의 구조화 출력은
    # 스키마 자체를 거부한다 — anyOf가 둘 다 되는 더 이식성 있는 형태다.
    #
    # hour 범위(0~23)는 minimum/maximum으로 스키마에 강제하고 싶었지만
    # Anthropic 구조화 출력이 integer에 그 키워드를 지원하지 않아서 뺐다.
    # 대신 route()에서 파싱 후 범위를 직접 검증한다.
    nullable_int = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "app": {"anyOf": [{"type": "string", "enum": [*APP_ALIASES.keys()]}, {"type": "null"}]},
            "site": {"anyOf": [{"type": "string", "enum": [*SITE_ALIASES.keys()]}, {"type": "null"}]},
            "hour_range": {
                "type": "object",
                "properties": {"start": nullable_int, "end": nullable_int},
                "required": ["start", "end"],
                "additionalProperties": False,
            },
            "periods": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["label", "start", "end"],
                    "additionalProperties": False,
                },
            },
            "intent": {"type": "string", "enum": ["검색", "정리", "비교", "집계"]},
            "count_by_site": {"type": "boolean"},
            "compound": {"type": "boolean"},
        },
        "required": ["app", "site", "hour_range", "periods", "intent", "count_by_site", "compound"],
        "additionalProperties": False,
    }


def _expand_period(start, end):
    """start~end(포함, YYYY-MM-DD) -> 날짜 문자열 리스트.

    LLM이 순서를 뒤집거나(end < start) 너무 긴 기간을 낼 수 있어서, 여기서
    최종 방어선으로 뒤집기 교정과 MAX_RANGE_DAYS 상한을 건다.
    """
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    n_days = min((end_d - start_d).days + 1, MAX_RANGE_DAYS)
    return [(start_d + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_days)]


def _parse_hour_range(raw):
    """{"start", "end"} -> (hour_start, hour_end) 검증된 정수 쌍, 또는 (None, None).

    시각이 하나만 언급된 질문("1시")은 start==end로 오므로 별도 케이스가
    필요 없다. 값이 범위(0~23) 밖이거나 타입이 이상하면 통째로 버린다 —
    절반만 정상인 범위(예: start만 유효)를 억지로 쓰면 의도와 다른 필터가
    걸릴 수 있어서다.
    """
    if not isinstance(raw, dict):
        return None, None
    start, end = raw.get("start"), raw.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None, None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None, None
    if end < start:
        start, end = end, start
    return start, end


def _format_history(history):
    """[{"question", "answer"}, ...] -> 프롬프트에 끼워 넣을 텍스트.

    history가 없으면 빈 문자열을 돌려줘서, 프롬프트에서 그 자리가 그냥
    빈 줄로 남게 한다(멀티턴 아닌 기존 질문은 프롬프트가 그대로 유지됨).
    """
    if not history:
        return ""
    turns = "\n".join(f"Q: {h['question']}\nA: {h['answer']}" for h in history)
    return f"\n이전 대화(참고용, 오래된 순):\n{turns}\n"


async def route(question, today=None, history=None):
    """질문 -> {app, site, hour_start, hour_end, periods, intent} 딕셔너리.

    periods: [{"label", "dates": [...]}, ...]. 기간 언급이 없는 질문은
    빈 리스트다 — 그 경우 ask.py는 전체 기록에서 검색한다. 하루짜리
    질문도 그냥 기간 1개(날짜 1개)로 표현한다. "하루면 date, 여러 날이면
    periods"처럼 표현을 나누지 않는 이유는, 그 이중 표현 자체가 이후
    단계들이 매번 두 필드를 다 확인해야 하는 복잡도였기 때문이다.

    today를 안 주면 실행 시점의 오늘 날짜를 쓴다. 테스트할 때는 특정
    날짜를 고정해서 넣을 수 있게 인자로 남겨뒀다.

    history: [{"question", "answer"}, ...] (최근 순 정렬은 호출부 책임).
    "그날"/"그거" 같은 지시어를 이전 대화로 풀어내기 위한 멀티턴 컨텍스트.
    """
    if today is None:
        today = datetime.now(LOCAL_TZ)

    client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
    prompt = ROUTE_PROMPT.format(
        today=today.strftime("%Y-%m-%d"), app_hint=_APP_HINT, site_hint=_SITE_HINT,
        max_days=MAX_RANGE_DAYS, question=question, history=_format_history(history),
    )
    response = await client.chat.completions.create(
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
        parsed = {"app": None, "site": None, "hour_range": None, "periods": [], "intent": "검색",
                  "count_by_site": False, "compound": False}

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
        # 집계일 때만 의미 있다 — 앱 단위가 아니라 방문 사이트(도메인)별로
        # 나눠서 세라는 신호. count_range()의 field="site" 인자로 그대로 간다.
        "count_by_site": bool(parsed.get("count_by_site")) and intent == "집계",
        # screenlog_langgraph.agent가 이 필드로 고정 경로/에이전트 루프를
        # 가른다. 예전엔 별도 LLM 호출(is_compound())로 뽑았는데, route()가
        # 이미 질문을 통째로 분석하니 같은 호출에 필드 하나만 얹어서
        # 질문당 LLM 호출을 1회 줄였다(트러블슈팅 문서 참고).
        "compound": bool(parsed.get("compound")),
    }








if __name__ == "__main__":
    import asyncio

    async def main():
        questions = [
            "카카오톡에서 무슨 대화를 했어?",
            "zoom 회의에서 뭘 했어?",
            "VS Code에서 무슨 코드를 봤어?",
            "어제 뭐 했어?",
            "7월 24일에 무슨 작업했어?",
            "오후 3시에 뭐 하고 있었어?",
            "2시부터 4시까지 뭐 했어?",
            "오늘 하루 뭐 했는지 정리해줘",
            "이번 주에 주로 무슨 일을 했어?",
            "이번 주에 카카오톡을 몇 번 켰어?",
            "이번 주 언제가 제일 바빴어?",
            "저번주 정리 그리고 이번주와 비교",
        ]
        for q in questions:
            plan = await route(q)
            periods = ", ".join(f"{p['label']}({len(p['dates'])}일)" for p in plan["periods"])
            hour = f"{plan['hour_start']}-{plan['hour_end']}" if plan["hour_start"] is not None else "-"
            print(f"app={str(plan['app']):15} hour={hour:6} "
                  f"intent={str(plan['intent']):5} periods=[{periods}] | {q}")

    asyncio.run(main())
