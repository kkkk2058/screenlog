"""chunked 요약에서 언어 오염된 블록만 골라서 재시도한다 — reduce(통째로
다시 요약)는 그 자체가 recency bias를 재도입한다는 게 확인됐으므로(실험
결과) 쓰지 않는다. 대신 블록 단위로 검사 -> 오염된 것만 좁게 재호출한다.

summarize.py는 건드리지 않는다.

사용:
    USE_LOCAL_LLM=1 uv run python eval/experiment_chunked_retry.py heldout_eval.json
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from screenlog.summarize import DAY_SUMMARY_PROMPT, COMPARE_PROMPT, SLACK_PROMPT, browse, _format_events, _thin_out
from screenlog.router import _format_history
from screenlog.source import weekday_ko
from screenlog.config import MAX_EVENTS_PER_DAY_SUMMARY, CHAT_MODEL, BASE_URL, API_KEY
from openai import AsyncOpenAI

BLOCKS = [(0, 6), (6, 12), (12, 18), (18, 24)]
MAX_RETRY = 2
PATTERNS = [re.compile(r"(\d{1,2})\s*시\s*(\d{1,2})\s*분"), re.compile(r"(\d{1,2}):(\d{2})")]

client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)


def cited_minutes(text):
    mins = []
    for p in PATTERNS:
        for h, m in p.findall(text):
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                mins.append(h * 60 + m)
    return mins


def to_min(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def coverage(text, start, end):
    span = max(to_min(end) - to_min(start), 1)
    mins = cited_minutes(text)
    return 0.0 if not mins else min((max(mins) - min(mins)) / span, 1.0)


def has_non_korean(text):
    return len(re.findall(r'[一-鿿]', text)) > 5


async def call(prompt, temperature=0):
    resp = await client.chat.completions.create(
        model=CHAT_MODEL, temperature=temperature, messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content


def build_prompt(date, events, hour_start, hour_end):
    weekday = weekday_ko(date)
    thinned = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    scope = f"{hour_start}시~{hour_end}시"
    return DAY_SUMMARY_PROMPT.format(date=date, weekday=weekday, scope=scope,
                                      history="", context=_format_events(thinned), question="정리해줘")


async def summarize_block(date, hs, he):
    events = browse(date, hour_start=hs, hour_end=he)
    if len(events) < 3:
        return None
    prompt = build_prompt(date, events, hs, he)

    answer = await call(prompt, temperature=0)
    attempts = 1
    while has_non_korean(answer) and attempts <= MAX_RETRY:
        # temperature=0로 같은 프롬프트를 다시 보내면 100% 같은 답이 나오므로
        # (Ollama가 결정론적), 재시도 때는 명시적 지시를 덧붙이고 온도도 살짝 올려
        # 실제로 다른 출력이 나오게 만든다.
        retry_prompt = prompt + "\n\n(중요: 반드시 한국어로만 답하라. 중국어/영어를 섞지 마라.)"
        answer = await call(retry_prompt, temperature=0.3)
        attempts += 1

    return {"hour_range": [hs, he], "answer": answer, "attempts": attempts,
            "still_contaminated": has_non_korean(answer)}


async def chunked_with_retry(date):
    blocks = []
    for hs, he in BLOCKS:
        r = await summarize_block(date, hs, he)
        if r:
            blocks.append(r)
    combined = "\n".join(b["answer"] for b in blocks)
    return combined, blocks


async def compare(d1, d2, s1, s2, question):
    combined = f"[{d1}({weekday_ko(d1)})]\n{s1}\n\n[{d2}({weekday_ko(d2)})]\n{s2}"
    prompt = COMPARE_PROMPT.format(context=combined, question=question, history=_format_history(None))
    return await call(prompt)


async def slack(date, s, question):
    prompt = SLACK_PROMPT.format(context_label="사용자의 활동을 날짜별로 미리 요약해둔 것이다",
                                  context=f"[{date}({weekday_ko(date)})]\n{s}",
                                  question=question, history=_format_history(None))
    return await call(prompt)


async def main():
    heldout_path = sys.argv[1] if len(sys.argv) > 1 else "heldout_eval.json"
    heldout = {h["id"]: h for h in json.loads(Path(heldout_path).read_text())}
    date_info = {h["date"]: h for h in heldout.values() if h.get("type") == "정리" and h.get("date")}

    d1, d2 = "2026-08-03", "2026-08-04"
    out = {}

    for d in (d1, d2):
        print(f"[{d}] 블록별 생성 + 재시도 중...", file=sys.stderr)
        combined, blocks = await chunked_with_retry(d)
        out[d] = {"combined": combined, "blocks": blocks}
        info = date_info.get(d)
        if info:
            cov = coverage(combined, info["actual_start"], info["actual_end"])
            contaminated = has_non_korean(combined)
            n_retried = sum(1 for b in blocks if b["attempts"] > 1)
            print(f"  coverage={cov:.2f} 오염={'예' if contaminated else '아니오'} "
                  f"재시도된블록={n_retried}/{len(blocks)}", file=sys.stderr)
            out[d]["coverage"] = cov
            out[d]["contaminated"] = contaminated

    print("\n[비교]", file=sys.stderr)
    q_compare = f"{d1}이랑 {d2} 중 언제 더 바빴어?"
    out["compare"] = await compare(d1, d2, out[d1]["combined"], out[d2]["combined"], q_compare)
    out["compare_gold"] = heldout.get(f"비교-{d1}vs{d2}", {}).get("gold_answer")

    print("[슬랙]", file=sys.stderr)
    q_slack = f"{d1} 정리해서 슬랙 메시지 초안도 써줘"
    out["slack"] = await slack(d1, out[d1]["combined"], q_slack)
    out["slack_gold"] = heldout.get(f"슬랙-{d1}", {}).get("gold_answer")

    print(json.dumps(out, ensure_ascii=False))
    print("\n완료", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
