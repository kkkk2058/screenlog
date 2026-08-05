"""슬랙 Web API 저수준 호출 — chat.postMessage 하나만 감싼다.

draft_slack_message(agent.py)가 만든 초안 텍스트를 실제로 보낼 때만 쓴다.
LLM에는 이 함수를 도구로 주지 않는다 — 실제 전송은 항상 api.py의 전용
엔드포인트(사용자가 프론트에서 명시적으로 "보내기"를 눌렀을 때)를 거친다.
"""

import httpx

from screenlog.config import SLACK_BOT_TOKEN


class SlackError(Exception):
    """슬랙 API가 ok=false를 돌려줬을 때."""


async def post_message(channel: str, text: str) -> dict:
    """channel에 text를 보낸다. 실패하면 SlackError를 던진다.

    토큰 자체가 없는 경우는 여기서 검사하지 않는다 — 호출 전에
    (api.py에서) 501로 미리 막아서 여기까지 오지 않는 게 정상 경로다.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": channel, "text": text},
        )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        # 슬랙은 실패해도 HTTP 200을 주고 body의 ok/error로만 실패를 알린다.
        raise SlackError(data.get("error", "unknown_error"))
    return data
