"""screenlog의 LangGraph 버전.

route()/검색/캐시/history 같은 도메인 로직은 screenlog(원본)에서 그대로
가져다 쓴다. 여기서 새로 짠 건 "route() 이후 어떤 방식으로 답할지 고르고
답을 스트리밍하는" 오케스트레이션(graph.py의 StateGraph)뿐이다.
"""

from screenlog_langgraph.graph import ask_auto, build_graph, stream_ask_auto

__all__ = ["ask_auto", "build_graph", "stream_ask_auto"]
