"""chunked 요약(블록별 map) 뒤에 reduce(재정리) 단계를 추가해서, 언어 오염이
비교/인수인계/슬랙으로 전파되는 문제가 해결되는지 확인한다.

experiment_chunked_summary.py에서 발견한 문제: 블록 요약을 단순히 이어붙이면
한 블록이 중국어로 나왔을 때 그 오염이 뒤 단계(비교/인수인계/슬랙)까지 그대로
전파된다. 여기서는 이어붙이기 전에 "블록 요약들을 하나의 일관된 한국어
요약으로 다시 정리해줘"라는 reduce 호출을 한 번 더 넣는다.

summarize.py는 건드리지 않는다 — DAY_SUMMARY_PROMPT/COMPARE_PROMPT 등은
그대로 import해서 쓰고, reduce 프롬프트만 이 실험 파일에 새로 정의한다.

사용:
    USE_LOCAL_LLM=1 uv run python eval/experiment_chunked_reduce.py heldout_eval.json
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from screenlog.summarize import (
    DAY_SUMMARY_PROMPT, COMPARE_PROMPT, SLACK_PROMPT,
    browse, _format_events, _thin_out, _call_llm,
)
from screenlog.router import _format_history
from screenlog.source import weekday_ko
from screenlog.config import MAX_EVENTS_PER_DAY_SUMMARY

BLOCKS = [(0, 6), (6, 12), (12, 18), (18, 24)]

REDUCE_PROMPT = """아래는 {date}({weekday}) 하루를 시간대별로 나눠서 각각 따로
요약한 결과다. 이걸 하나로 합쳐서 정리해라.

{blocks}

규칙:
- 반드시 한국어로만 쓴다. 다른 언어가 섞여 있으면 한국어로 바꿔 써라.
- 형식은 "* HH시MM분 - 내용" 한 줄씩.
- 중복되거나 비슷한 항목은 하나로 합친다.
- 사소한 것(알림, 광고 등)보다 실질적인 작업/학습/회의 내용을 우선한다.
- 시간 순서대로 정렬한다.
"""

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


def has_non_korean(text):
    # 한글/영문/숫자/기본 문장부호 외의 CJK 확장 문자(중국어 간체 등)가 섞였는지 대략 탐지
    cjk_han = re.findall(r'[一-鿿]', text)
    return len(cjk_han) > 5  # 한자 단어 몇 개는 정상(코드/지명 등)일 수 있어 여유를 둠


def build_prompt(date, events, hour_start=None, hour_end=None):
    weekday = weekday_ko(date)
    thinned = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    scope = f"{hour_start}시~{hour_end}시" if hour_start is not None else "하루"
    return DAY_SUMMARY_PROMPT.format(date=date, weekday=weekday, scope=scope,
                                      history="", context=_format_events(thinned), question="정리해줘")


async def chunked_then_reduce(date):
    parts = []
    for hs, he in BLOCKS:
        events = browse(date, hour_start=hs, hour_end=he)
        if len(events) < 3:
            continue
        answer = await _call_llm(build_prompt(date, events, hs, he))
        parts.append(f"[{hs}시~{he}시]\n{answer}")

    raw_concat = "\n".join(parts)
    reduce_prompt = REDUCE_PROMPT.format(date=date, weekday=weekday_ko(date), blocks=raw_concat)
    reduced = await _call_llm(reduce_prompt)
    return raw_concat, reduced


async def compare(d1, d2, s1, s2, question):
    combined = f"[{d1}({weekday_ko(d1)})]\n{s1}\n\n[{d2}({weekday_ko(d2)})]\n{s2}"
    prompt = COMPARE_PROMPT.format(context=combined, question=question, history=_format_history(None))
    return await _call_llm(prompt)


async def slack(date, s, question):
    prompt = SLACK_PROMPT.format(context_label="사용자의 활동을 날짜별로 미리 요약해둔 것이다",
                                  context=f"[{date}({weekday_ko(date)})]\n{s}",
                                  question=question, history=_format_history(None))
    return await _call_llm(prompt)


async def main():
    heldout_path = sys.argv[1] if len(sys.argv) > 1 else "heldout_eval.json"
    heldout = {h["id"]: h for h in json.loads(Path(heldout_path).read_text())}
    date_info = {h["date"]: h for h in heldout.values() if h.get("type") == "정리" and h.get("date")}

    d1, d2 = "2026-08-03", "2026-08-04"

    print(f"[{d1}] chunked+reduce 진행 중...", file=sys.stderr)
    raw1, reduced1 = await chunked_then_reduce(d1)
    print(f"[{d2}] chunked+reduce 진행 중...", file=sys.stderr)
    raw2, reduced2 = await chunked_then_reduce(d2)

    out = {"reduced": {d1: reduced1, d2: reduced2}}

    for d, reduced in ((d1, reduced1), (d2, reduced2)):
        info = date_info.get(d)
        if not info:
            continue
        cov = coverage(reduced, info["actual_start"], info["actual_end"])
        contaminated = has_non_korean(reduced)
        print(f"  [{d}] coverage={cov:.2f} 비한국어오염={'예' if contaminated else '아니오'}", file=sys.stderr)
        out.setdefault("coverage", {})[d] = cov
        out.setdefault("contaminated", {})[d] = contaminated

    print("\n[비교] reduce 기반 생성 중...", file=sys.stderr)
    q_compare = f"{d1}이랑 {d2} 중 언제 더 바빴어?"
    out["compare_reduced"] = await compare(d1, d2, reduced1, reduced2, q_compare)
    out["compare_gold"] = heldout.get(f"비교-{d1}vs{d2}", {}).get("gold_answer")

    print("[슬랙] reduce 기반 생성 중...", file=sys.stderr)
    q_slack = f"{d1} 정리해서 슬랙 메시지 초안도 써줘"
    out["slack_reduced"] = await slack(d1, reduced1, q_slack)
    out["slack_gold"] = heldout.get(f"슬랙-{d1}", {}).get("gold_answer")

    print(json.dumps(out, ensure_ascii=False))
    print("\n완료", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
