"""4. 질문 — 검색한 내용을 근거로 LLM이 답한다

두 단계로 나눠뒀다:
    search()  질문과 비슷한 이벤트를 찾는다
    ask()     찾은 내용을 프롬프트에 넣고 LLM에게 묻는다

나눈 이유는 답이 틀렸을 때 검색이 틀렸는지 LLM이 틀렸는지 가리기 위해서다.
합쳐두면 엉뚱한 걸 가져와도 그럴듯한 답이 나와서 눈치채지 못한다.
"""

import os
from datetime import datetime

from openai import OpenAI

from screenlog.config import BASE_URL, CHAT_MODEL, CONTEXT_CHARS_PER_HIT, RETRIEVE_K
from screenlog.index import embed, get_collection
from screenlog.source import LOCAL_TZ

PROMPT = """아래는 사용자의 컴퓨터 화면 사용 기록이다.

오늘은 {today}이다. "오늘", "어제" 같은 표현은 이 날짜를 기준으로 판단한다.

{context}

질문: {question}

규칙:
- 기록에 있는 내용만으로 답한다. 없으면 "기록에 없습니다"라고 답한다.
- 답할 때 시각과 앱 이름을 함께 밝힌다.
"""


def build_where(app=None, hour=None, date=None):
    """app/hour/date 조건으로 chroma where 딕셔너리를 만든다.

    조건이 하나도 없으면 None을 돌려준다 (필터 없이 전체에서 검색).
    조건이 2개 이상이면 chroma가 $and로 묶어달라고 요구한다.
    """
    conditions = []
    if app:
        conditions.append({"app": app})
    if hour is not None:
        conditions.append({"hour": hour})
    if date:
        conditions.append({"date": date})

    if len(conditions) == 0:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def search(question, k=RETRIEVE_K, app=None, hour=None, date=None):
    """질문과 비슷한 이벤트 k개를 찾는다.

    app/hour/date를 주면 그 조건을 만족하는 이벤트 안에서만 찾는다.
    벡터 유사도는 "이 문서의 앱이 뭔가" 같은 축을 못 보기 때문에,
    앱/시각/날짜는 벡터가 아니라 metadata 필터로 걸러야 한다.
    """
    col = get_collection()
    where = build_where(app, hour, date)
    result = col.query(query_embeddings=embed([question]), n_results=k, where=where)

    # chroma는 결과를 [[...]] 로 한 겹 싸서 준다. 질문을 여러 개 던질 수 있어서다.
    hits = []
    for doc, meta, distance in zip(result["documents"][0],
                                   result["metadatas"][0],
                                   result["distances"][0]):
        hit = dict(meta)
        hit["text"] = doc
        hit["distance"] = distance     # 0에 가까울수록 질문과 비슷하다
        hits.append(hit)
    return hits


def build_context(hits):
    """검색 결과를 프롬프트에 넣을 형태로 만든다.

    시각과 앱 이름을 본문 앞에 붙인다. 이게 없으면 LLM이 '언제 있었던 일인지'를
    아예 모르는 채로 답한다.

    근거 하나당 글자 수에 상한을 둔다. 이벤트 크기가 고르지 않아서(중앙값 1,270자,
    최대 36,870자) 상한이 없으면 큰 이벤트 몇 개가 걸리는 것만으로 프롬프트가
    한 번에 수십 배로 부푼다.
    """
    blocks = []
    for hit in hits:
        text = hit["text"]
        if len(text) > CONTEXT_CHARS_PER_HIT:
            text = text[:CONTEXT_CHARS_PER_HIT] + " …(잘림)"
        blocks.append(f"[{hit['start']}, {hit['app']} / {hit['window']}]\n{text}")
    return "\n\n".join(blocks)


def ask(question, k=RETRIEVE_K, app=None, hour=None, date=None):
    """질문 -> (답변, 근거 목록).

    app/hour/date는 그대로 search()에 넘긴다. 아직 질문 문장에서
    자동으로 뽑아내는 건 안 만들었다 (다음 단계인 라우팅의 몫).
    """
    hits = search(question, k, app=app, hour=hour, date=date)
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    prompt = PROMPT.format(today=today, context=build_context(hits), question=question)

    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content, hits


if __name__ == "__main__":
    while True:
        question = input("\n질문 (엔터로 종료) > ").strip()
        if not question:
            break

        answer, hits = ask(question)
        print(f"\n{answer}")

        # 답과 근거를 같이 본다. 답만 보면 검색이 엉뚱한 걸 가져온 걸 놓친다.
        print(f"\n--- 근거 {len(hits)}개 ---")
        for hit in hits:
            print(f"  [{hit['distance']:.3f}] {hit['start']}  "
                  f"{hit['app']} / {hit['window'][:40]}")
