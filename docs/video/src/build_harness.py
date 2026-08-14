"""실제 dashboard.html을 그대로 쓰되, /api/* 응답만 이미 받아둔 진짜 데이터로 갈아끼운다.

인증 뒤에 있는 API를 우회하려는 게 아니라, 답변·통계·근거를 이미 같은 함수로
직접 받아뒀기 때문에(capture_answers.py / dump_fixtures.py) 그 결과를 화면에
그리는 것뿐이다. 렌더링 코드는 프로덕션 dashboard.html 원본 그대로다.
"""
import json, re
from pathlib import Path

HERE = Path(__file__).parent
SRC = Path("/Users/sehoonkim/screenlog/src/screenlog/static")
FIX = json.loads((HERE / "fixtures.json").read_text())

# ── 공개용 마스킹 ────────────────────────────────────────────────
# 크롬 프로필 접미사에 이름과 계정이 그대로 붙는다 — 근거 카드마다 노출된다.
PROFILE = re.compile(r"\s*-\s*(Chrome|Whale)\s*-\s*sehoon\s*\([^)]*\)\)?|\s*-\s*Chrome\s*-\s*세훈")
REDACT = [
    (re.compile(r"김성용\s*교수님"), "김○○ 교수님"),
    # 마크다운으로 렌더되므로 * 는 강조 기호로 먹힌다 — 문자로만 가린다
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "xx.xx.xx.xx"),
    (re.compile(r"sevan\.kim\(김세훈\)"), "***"),
    (re.compile(r"김세훈님과"), "동료와"),
    (re.compile(r"김세훈님"), "동료"),
]


def scrub(s):
    if not isinstance(s, str):
        return s
    s = PROFILE.sub(" - Chrome", s)
    for pat, rep in REDACT:
        s = pat.sub(rep, s)
    return s


def scrub_all(o):
    """답변뿐 아니라 사이드바(digest/timeline)에도 이름이 그대로 실린다."""
    if isinstance(o, str):
        return scrub(o)
    if isinstance(o, list):
        return [scrub_all(x) for x in o]
    if isinstance(o, dict):
        return {k: scrub_all(v) for k, v in o.items()}
    return o


FIX["digest"] = scrub_all(FIX["digest"])
FIX["timeline"] = scrub_all(FIX["timeline"])

for t in FIX["turns"]:
    t["answer"] = scrub(t["answer"])
    for h in t["hits"]:
        h["window"] = scrub(h.get("window"))
        h["excerpt"] = scrub((h.get("text") or h.get("excerpt") or ""))[:200]
        h["distance"] = round(float(h["distance"]), 3)

# 어떤 도구를 거친 턴인지 — 에이전트 경로에서만 tool_start/tool_done이 온다
TOOLS = {"인수인계": ["draft_handover_doc"], "슬랙": ["draft_slack_message"]}
for t in FIX["turns"]:
    t["tools"] = TOOLS.get(t["kind"], [])
    t["plan_obj"] = json.loads(t["plan"].replace("'", '"').replace("None", "null")
                               .replace("True", "true").replace("False", "false")) \
        if t["plan"].startswith("{") else None

html = (SRC / "dashboard.html").read_text()
tokens = (SRC / "tokens.css").read_text()

# 1) tokens.css 인라인 — file://로 열어도 스타일이 살아있게
html = re.sub(r'<link[^>]+tokens\.css[^>]*>', f"<style>\n{tokens}\n</style>", html)

# 2) 정지 캡처용은 즉시 완료, 영상(live)용은 실제 타이핑 속도를 살린다
assert "const TYPE_MS = 15" in html, "타이핑 상수를 못 찾음 — dashboard.html이 바뀌었다"
html = html.replace("const TYPE_MS = 15", "const TYPE_MS = (window.__LIVE ? 20 : 0)")

stub = """
<script>
window.__FIX__ = %s;

// /api/* 를 이미 받아둔 실제 응답으로 되돌려준다. SSE는 대시보드가 읽는 것과
// 똑같은 "data: {json}\\n\\n" 형식으로 만들어 스트림에 흘린다.
const _sse = (turn) => {
  const ev = [];
  ev.push({type:"conversation", id: 1});
  if (turn.plan_obj) ev.push({type:"plan", plan: turn.plan_obj});
  ev.push({type:"hits", hits: turn.hits});
  if (turn.tools.length) { ev.push({type:"tool_start", tools: turn.tools});
                           ev.push({type:"tool_done",  tools: turn.tools}); }
  ev.push({type:"token", text: turn.answer});
  ev.push({type:"done"});
  const body = ev.map(e => "data: " + JSON.stringify(e) + "\\n\\n").join("");
  return new Response(new Blob([body]).stream(), {status:200,
    headers:{"Content-Type":"text/event-stream"}});
};

const _json = (o) => new Response(JSON.stringify(o), {status:200,
  headers:{"Content-Type":"application/json"}});

const TURN = new URLSearchParams(location.search).get("turn") | 0;
const LIVE = new URLSearchParams(location.search).has("live");
window.__LIVE = LIVE;

window.fetch = async (url, opts) => {
  const u = String(url);
  if (u.includes("/api/ask/stream"))    return _sse(window.__FIX__.turns[TURN]);
  if (u.includes("/api/stats"))         return _json(window.__FIX__.stats);
  if (u.includes("/api/digest"))        return _json(window.__FIX__.digest);
  if (u.includes("/api/timeline/"))     return _json(window.__FIX__.timeline);
  if (u.includes("/api/conversations")) return _json([]);
  return _json({});
};

// 페이지 자체 핸들러가 붙은 뒤에 질문을 그대로 제출한다 — 렌더링은 원본 코드가 한다.
addEventListener("load", () => setTimeout(() => {
  const t = window.__FIX__.turns[TURN];
  const input = document.querySelector("#qinput") || document.querySelector("input[type=text]");
  const form  = input && input.form;
  if (!form) { document.title = "NO_FORM"; return; }
  const submit = () => form.dispatchEvent(new Event("submit", {cancelable:true, bubbles:true}));

  if (LIVE) {
    // 영상에서는 질문이 한 글자씩 찍히는 게 보여야 한다 — 답만 튀어나오면
    // 화면이 아니라 스크린샷처럼 읽힌다.
    let i = 0;
    const typer = setInterval(() => {
      input.value = t.question.slice(0, ++i);
      if (i >= t.question.length) { clearInterval(typer); setTimeout(submit, 400); }
    }, 45);
  } else {
    input.value = t.question;
    submit();
  }
  // 근거 패널은 접힌 채로 그려진다 — 영상에서는 펼쳐서 보여준다.
  // 렌더 시점이 타이핑 애니메이션에 걸려 있어서, 한 번이 아니라 계속 열어둔다.
  // 근거는 답이 다 나온 뒤에 펼친다 — 사람이 읽고 나서 근거를 확인하는 순서.
  setTimeout(() => {
    const opener = setInterval(() => {
      document.querySelectorAll("details.hitsbox").forEach(d => d.open = true);
    }, 50);
    setTimeout(() => { clearInterval(opener);
                       document.documentElement.dataset.ready = "1"; }, 4000);
  }, LIVE ? 3400 : 0);
}, 120));
</script>
""" % json.dumps(FIX, ensure_ascii=False)

# 스텁은 반드시 페이지 자체 스크립트보다 먼저 실행돼야 한다 — 그렇지 않으면
# 최초의 loadStats()/loadDigest()가 진짜 fetch로 나가서 사이드바가 빈 채로 남는다.
assert "<head>" in html, "head 태그를 못 찾음"
html = html.replace("<head>", "<head>\n" + stub, 1)
(HERE / "harness.html").write_text(html)

print("턴:", [(i, t["kind"], t["question"][:34]) for i, t in enumerate(FIX["turns"])])
print("hits(검색):", len(FIX["turns"][0]["hits"]))
print("→", HERE / "harness.html")
