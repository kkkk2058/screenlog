# FastAPI 스트리밍 + 비동기 적용 (2026-08-03)

`/api/ask`가 답을 다 만들 때까지 기다렸다가 한 번에 보여주던 걸 SSE 스트리밍으로
바꾸고, 그 과정에서 동기 코드를 비동기로 전환한 기록.

## 배경

- 원래 문제: 질문하면 몇 초~십몇 초간 로딩만 뜨다가 답이 통째로 나타남 (개인
  프로젝트 TODO의 "fastapi 스트리밍 적용", "동기 비동기 적용(답변 속도가 느림)")
- 접근: ① 먼저 스트리밍으로 체감 속도 개선 → ② intent(검색/정리/비교/집계)별로
  답변 생성 방식이 갈리는 걸 스트리밍 경로에도 반영 → ③ 정리/비교처럼 날짜를
  여러 개 순회하는 경로는 `asyncio.gather`로 동시 처리해서 실제 소요시간도 단축

## 1. SSE 스트리밍 — `/api/ask/stream`

### 백엔드 (`ask.py`, `api.py`)

- `stream_ask()` — 검색 intent 전용. OpenAI `stream=True`로 토큰이 오는 대로
  `{"type": "token", "text": ...}`를 yield. 맨 처음 `{"type": "hits", ...}`,
  맨 끝 `{"type": "done"}`.
- `stream_ask_auto()` — `ask_auto()`와 동일한 라우팅/분기를 쓰되 이벤트 스트림으로
  쪼갠 버전. `route()`로 plan을 뽑아 제일 먼저 `{"type": "plan", ...}`을 보내고,
  intent가:
  - **검색**이면 `stream_ask()`로 넘겨서 진짜 토큰 단위 스트리밍
  - **정리/비교/집계**면 `summarize_period`/`compare_periods`/`compare_range`/
    `summarize_range`/`count_range`(+`format_count`)로 완성된 답을 만든 뒤
    단일 `token` 이벤트로 한 번에 전송 (이 경로들은 LLM을 여러 번 호출하거나
    아예 안 써서 토큰 단위로 못 쪼갬)
- `api.py`의 `api_ask_stream`은 이 이벤트들을 `data: {...}\n\n` 형식(SSE)으로
  포장해서 `StreamingResponse`로 흘려보내기만 함. 에러는 `{"type": "error", ...}`로
  변환해서 스트림 끝까지 신호를 보냄(중간에 조용히 끊기지 않게).

### 프론트 (`static/dashboard.html`)

- `askStream()` — `fetch()` + `response.body.getReader()`로 SSE를 직접 파싱
  (`"\n\n"` 단위로 이벤트 분리, `"data: "` 접두사 제거 후 JSON.parse)
- 타이핑 효과 — 서버가 문장 덩어리로 뭉텅이째 보내도, 도착한 텍스트를 버퍼에
  쌓아두고 15ms 간격 `setInterval`로 2글자씩 꺼내 화면에 채워 고르게 흘러나오게 함
- `mdLite()` — `**bold**`, `* ` 불릿(중첩 2단), `[날짜(요일)]`/`### 기간` 헤더를
  가벼운 마크다운으로 렌더링. 헤더를 만나면 `<details>` 아코디언으로 묶어서
  여러 날짜치 답변을 접었다 펼 수 있게 함
- 시각(`14시 30분`) 패턴을 정규식으로 찾아 강조 span 처리
- 스켈레톤 로딩 placeholder, `plan`(app/hour/기간/intent) 배지 렌더링

## 2. 동기 → 비동기 전환

### 핵심 규칙

- `async def`로 선언한 함수는 `await`를 붙여야 실행됨 (안 붙이면 "실행 안 된
  코루틴 객체"만 생김)
- `await`는 `async def` 함수 안에서만 쓸 수 있음 → 그래서 한 함수를 async로
  바꾸면, 그 함수를 부르는 함수들도 전부 async로 바뀌어야 함(위로 전염)
- OpenAI 호출 부분은 동기 클라이언트(`OpenAI`)를 비동기 클라이언트
  (`AsyncOpenAI`)로 교체해야 `await`가 의미를 가짐

### 바뀐 함수 (호출 체인 순서)

```
router.py:   route()                                    → async
summarize.py: _call_llm(), summarize_day(), summarize_range(),
              compare_range(), summarize_period(), compare_periods() → async
ask.py:      ask(), stream_ask(), ask_auto(), stream_ask_auto()      → async
api.py:      api_ask(), api_ask_stream()                             → async
```
`search()`, `browse()`, `count_range()`, `format_count()` 등 LLM을 안 부르는
함수는 그대로 동기로 남김 — 바꿔봤자 이득이 없음.

### 진짜 속도 이득이 나는 지점 — `asyncio.gather`

"저번주 정리해줘"처럼 여러 날짜를 순회하는 질문은, 날짜별 요약을 순차로 하나씩
기다리는 대신 전부 동시에 요청을 던지고 한꺼번에 기다리게 바꿈:

```python
# summarize_range() 안
days = await asyncio.gather(*[
    summarize_day(date, app, hour_start, hour_end, site) for date in dates
])
```

단발 검색 질문(LLM 호출 1번)은 비동기로 바꿔도 그 요청 자체가 빨라지지 않음 —
이득은 "여러 개를 동시에 기다릴 때"와 "여러 사용자가 동시에 요청할 때"에서만 남.

## 3. 실측 검증

| 항목 | 전 | 후 |
|---|---|---|
| "저번주 정리해줘" (7일치) | ~20초+ (순차) | **6.4초** (`asyncio.gather`) |
| "오늘 뭐 했어?" (검색, LLM 1회) | 변화 없음 | 변화 없음 (병렬화할 게 없어서 정상) |
| 스트리밍 여부 | 답 다 만들 때까지 대기 | 토큰 단위로 실시간 표시(검색 intent) |



## 어디서 이득이 나고 어디서 안 나는지 (정리)

| 질문 종류 | 전 | 후 | 차이 |
|---|---|---|---|
| "오늘 뭐 했어?" (검색, LLM 1번 호출) | 3초 | 3초 | **없음** — await 하나뿐이라 병렬화할 게 없음 |
| "저번주 정리해줘" (7일 요약) | 24초 | ~7초 | **큼** — `asyncio.gather`가 7개를 겹쳐 처리 |
| "저번주 vs 이번주 비교" (14일 요약 + 비교 1번) | ~45초 | ~10초 | **큼** — 14일이 한꺼번에 처리됨 |
| 동시에 여러 사람이 질문 | 스레드풀 소진되면 뒤 요청이 밀림 | 요청끼리 서로 안 막음 | 다인원 시나리오에서만 체감 |

즉, **"정리"/"비교"처럼 날짜를 여러 개 순회하는 질문에서 체감 효과가 제일 크고**, 단발 검색 질문은 지금 그대로여도 별 차이가 없음.



## 4. 과정에서 발견하고 고친 버그

- **시간대 필터 프롬프트 버그**: `DAY_SUMMARY_PROMPT`가 `hour_start`/`hour_end`로
  좁혀진 기록을 요약할 때도 "하루 전체"라고 서술하게 시켜서, LLM이 "하루를
  시작했습니다"/"마지막으로" 같은 표현으로 좁은 시간대를 하루처럼 지어냄 →
  프롬프트에 `{scope}`(예: "14시~15시")를 넣어 정확한 범위로만 서술하게 수정
- **`await` outside async function (SyntaxError)**: `summarize_range()`를
  `async def` 없이 `await asyncio.gather`만 추가해서 파일 import 자체가 실패
- **동기 클라이언트에 `await`**: `ask()`/`stream_ask()`가 `async def`로는
  바뀌었는데 `client = OpenAI(...)`(동기)를 그대로 써서 `await` 시
  `TypeError: 'ChatCompletion' object can't be awaited` → `AsyncOpenAI`로 교체
- **`await` 누락 (전염 체인 중간에서 끊김)**: `summarize_day()`/`summarize_period()`가
  `async def`가 안 됐는데 안에서 async 함수를 그냥 호출 → 결과가 실제 값이
  아니라 "코루틴 객체"로 텍스트에 섞여 들어감. `stream_ask_auto()`의
  `plan = route(question)`도 같은 이유로 `await` 누락 → `TypeError: 'coroutine'
  object is not subscriptable`. 파이썬이 `RuntimeWarning: coroutine 'X' was
  never awaited`로 어느 함수인지 정확히 알려줘서 위치를 바로 찾을 수 있었음.

## 참고: async/await 판단 기준

부르려는 함수의 정의부가 `async def`면 `await`를 붙이고, 그냥 `def`면 안
붙인다. 빼먹으면 대부분 `RuntimeWarning: coroutine 'X' was never awaited`나
`TypeError`로 바로 티가 나서, 파이썬이 어디가 문제인지 알려주는 편.
