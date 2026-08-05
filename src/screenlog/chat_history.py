"""대화 기록 저장 — 왼쪽 사이드바 "이전 질문들 모음"용.

CONV_HISTORY는 원래 브라우저 메모리에만 있어서 새로고침하면 날아갔다(의도된
트레이드오프였음, streaming-and-async.md 참고). 그걸 서버 sqlite로 옮겨서
새로고침해도, 나중에 다시 열어도 이전 대화를 찾아볼 수 있게 한다.

제목은 LLM으로 새로 만들지 않는다 — 첫 질문 텍스트를 그대로 잘라 쓴다.
요약 캐시 때 "요약을 위한 요약"을 만들려다 걷어낸 적이 있는데(2차 LLM 호출+
캐시 테이블), 여기서도 같은 함정이라 처음부터 안 판다.
"""

import sqlite3
import uuid
from datetime import datetime

from screenlog.config import CHAT_HISTORY_DB
from screenlog.source import LOCAL_TZ

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""

TITLE_MAX = 28


def _connect():
    CHAT_HISTORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CHAT_HISTORY_DB)
    conn.executescript(_SCHEMA)
    return conn


def _now():
    return datetime.now(LOCAL_TZ).isoformat()


def _make_title(question):
    q = question.strip().replace("\n", " ")
    return q if len(q) <= TITLE_MAX else q[:TITLE_MAX] + "…"


def create_conversation(first_question):
    """새 대화를 만들고 id를 돌려준다. 제목은 첫 질문에서 그대로 뽑는다."""
    conv_id = uuid.uuid4().hex[:12]
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, _make_title(first_question), now, now),
        )
    return conv_id


def add_message(conversation_id, role, content):
    """메시지 하나를 저장하고, 그 대화의 updated_at을 지금으로 올린다
    (사이드바 목록이 최근 대화 순으로 뜨게 하는 용도)."""
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, now),
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))


def list_conversations(limit=50):
    """사이드바에 뿌릴 대화 목록. 최근 순."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]


def get_messages(conversation_id):
    """그 대화의 메시지 전체. role/content만 — plan/hits는 애초에 저장 대상이
    아니다(다시 열어봤을 때 "근거"까지 재현하려면 그것도 저장해야 하는데,
    지금은 질문-답변 텍스트만 있으면 팔로우업 맥락은 충분하다)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def get_recent_turns(conversation_id, limit=None):
    """팔로우업 질문 해석용 (question, answer) 턴 목록 — route()/에이전트가
    "그날"/"더 자세히" 같은 지시어를 풀 때 쓴다.

    예전엔 이걸 브라우저가 CONV_HISTORY로 따로 들고 있다가 매 질문마다
    통째로 다시 보냈다 — 근데 서버가 이미 messages 테이블에 다 저장해두고
    있으니, 그걸 다시 읽는 게 클라이언트가 매번 재전송하는 것보다 낫다
    (네트워크 왕복 없음, 브라우저 상태와 어긋날 일도 없음).

    messages는 user/assistant가 번갈아 쌓인다. 답변 생성 중 에러가 나면
    user만 저장되고 assistant가 안 붙는 턴이 생길 수 있어서, 짝이 안 맞는
    메시지는 건너뛴다."""
    messages = get_messages(conversation_id)
    turns = []
    i = 0
    while i + 1 < len(messages):
        if messages[i]["role"] == "user" and messages[i + 1]["role"] == "assistant":
            turns.append({"question": messages[i]["content"], "answer": messages[i + 1]["content"]})
            i += 2
        else:
            i += 1
    return turns[-limit:] if limit else turns
