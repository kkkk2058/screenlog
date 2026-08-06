"""screenlog의 LangChain 버전.

screenlog/(원본)의 데이터 계층(source, index, clean, ask.search, summarize.browse 등)은
그대로 재사용한다. 여기서 새로 짠 건 LLM을 호출하는 부분뿐이다:
    router.py    구조화 출력 라우팅 (ChatOpenAI.with_structured_output)
    chains.py    프롬프트 -> LLM -> 파싱을 LCEL(prompt | llm | parser)로 구성
    pipeline.py  route() 결과에 따라 위 체인들을 조합하는 ask_auto()

원본과 입출력이 동일하도록 짰다 — 같은 질문을 넣으면 같은 모양의 결과가
나와야 두 프레임워크 버전을 나란히 비교할 수 있다.
"""

from screenlog_langchain.pipeline import ask_auto
from screenlog_langchain.router import route

__all__ = ["ask_auto", "route"]
