"""프롬프트 분할(시간대별 map-reduce) 실험 — summarize.py는 건드리지 않고 결과만 뽑는다.

배경: recency bias(하루 정리 시 뒷부분에만 집중하는 문제)를 LoRA로 고쳐봤지만
데이터가 68개뿐이라 효과가 부분적이었다(docs/local-llm-experiment-report.md).
이 스크립트는 대안으로, 모델은 그대로 두고 "하루를 시간대 블록으로 쪼개서
블록마다 따로 요약(map) 후 합치는(reduce)" 방식이 얼마나 개선되는지를 잰다.

3가지를 같은 held-out 날짜(7/26, 8/3, 8/4)로 비교한다:
    baseline - 지금처럼 하루 전체를 한 번에 요약 (1번 호출)
    chunked  - 6시간 블록 4개로 나눠 각각 요약 후 이어붙임 (블록 수+1번 호출)
    gold     - heldout_eval.json에 이미 있는 API 정답 (참고용)

summarize.py의 DAY_SUMMARY_PROMPT/browse/_format_events/_thin_out/_call_llm을
그대로 재사용한다 — 로직을 새로 짜면 지금까지 검증해둔 프롬프트 구조와
달라져서 비교가 불공정해진다.

사용:
    uv run python eval/experiment_chunked_summary.py heldout_eval.json
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from screenlog.summarize import DAY_SUMMARY_PROMPT, browse, _format_events, _thin_out, _call_llm
from screenlog.source import weekday_ko
from screenlog.config import MAX_EVENTS_PER_DAY_SUMMARY

HELD_OUT_DATES = ["2026-07-26", "2026-08-03", "2026-08-04"]
BLOCKS = [(0, 6), (6, 12), (12, 18), (18, 24)]

PATTERNS = [re.compile(r"(\d{1,2})\s*시\s*(\d{1,2})\s*분"), re.compile(r"(\d{1,2}):(\d{2})")]


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


def build_prompt(date, events, hour_start=None, hour_end=None):
    weekday = weekday_ko(date)
    thinned = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    scope = f"{hour_start}시~{hour_end}시" if hour_start is not None else "하루"
    return DAY_SUMMARY_PROMPT.format(date=date, weekday=weekday, scope=scope,
                                      history="", context=_format_events(thinned), question="정리해줘")


async def baseline_summary(date):
    events = browse(date)
    prompt = build_prompt(date, events)
    t0 = time.time()
    answer = await _call_llm(prompt)
    return answer, round(time.time() - t0, 1), len(events)


async def chunked_summary(date):
    """블록별로 map 호출 후, 결과를 그냥 이어붙인다(reduce는 LLM 재호출 없이
    단순 결합) — 이게 제일 싸고 빠른 버전이라 먼저 이걸로 효과를 본다."""
    t0 = time.time()
    parts = []
    n_calls = 0
    for hs, he in BLOCKS:
        events = browse(date, hour_start=hs, hour_end=he)
        if len(events) < 3:
            continue
        prompt = build_prompt(date, events, hs, he)
        answer = await _call_llm(prompt)
        n_calls += 1
        parts.append(answer)
    combined = "\n".join(parts)
    return combined, round(time.time() - t0, 1), n_calls


async def main():
    heldout_path = sys.argv[1] if len(sys.argv) > 1 else "heldout_eval.json"
    heldout = {h["date"]: h for h in json.loads(Path(heldout_path).read_text())
               if h.get("date") in HELD_OUT_DATES and h["type"] == "정리"}

    results = []
    for date in HELD_OUT_DATES:
        gold = heldout.get(date)
        actual_start, actual_end = (gold["actual_start"], gold["actual_end"]) if gold else (None, None)

        base_text, base_sec, n_events = await baseline_summary(date)
        chunk_text, chunk_sec, n_calls = await chunked_summary(date)

        row = {
            "date": date, "actual_start": actual_start, "actual_end": actual_end, "n_events": n_events,
            "baseline": base_text, "baseline_sec": base_sec,
            "chunked": chunk_text, "chunked_sec": chunk_sec, "chunked_calls": n_calls,
        }
        if gold:
            row["gold"] = gold["gold_answer"]
        results.append(row)
        print(f"  [{date}] baseline={base_sec}s / chunked={chunk_sec}s({n_calls}호출)", file=sys.stderr)

    print(json.dumps(results, ensure_ascii=False))

    print("\n" + "=" * 80, file=sys.stderr)
    print(f"{'날짜':<14}{'baseline':>10}{'chunked':>10}{'gold':>10}", file=sys.stderr)
    for r in results:
        if not r["actual_start"]:
            continue
        b = coverage(r["baseline"], r["actual_start"], r["actual_end"])
        c = coverage(r["chunked"], r["actual_start"], r["actual_end"])
        g = coverage(r["gold"], r["actual_start"], r["actual_end"]) if "gold" in r else None
        g_str = f"{g:>10.2f}" if g is not None else f"{'N/A':>10}"
        print(f"{r['date']:<14}{b:>10.2f}{c:>10.2f}{g_str}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
