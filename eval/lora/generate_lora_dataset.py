"""로컬 LLM(Qwen2.5-7B) LoRA 파인튜닝용 (prompt, completion) 학습 데이터를 만든다.

배경: docs/local-llm-experiment-report.md에서 확인한 대로, 로컬 모델은
"정리" 질문에서 하루의 대부분을 누락하거나(recency bias), 희소한 날엔 없는
시각을 지어낸다(환각). API(gemini-3.1-flash-lite)는 같은 조건에서 실제
이벤트 시간대와 정확히 일치하는 요약을 낸다는 것도 원본 이벤트 대조로 이미
검증했다. 그 검증된 API 출력을 정답(teacher label)으로 삼아 로컬 모델을
지식 증류(distillation) 방식으로 파인튜닝하기 위한 데이터를 만든다.

v1(하루/시간대 슬라이스만, 38개)의 문제 두 가지를 이번에 고친다:
1. "하루 전체"와 특정 시간대 슬라이스가 사실상 같은 이벤트를 보는 경우가
   많아 겉보기 개수만큼 독립적이지 않았다 — 슬라이스가 하루 이벤트의
   REDUNDANT_RATIO 이상을 차지하면 건너뛴다.
2. 정리(DAY_SUMMARY_PROMPT) 하나에만 의존해서 축이 얕았다 — 이번엔
   앱 필터, 질문 표현(자세히/쉽게), 그리고 비교/인수인계/슬랙 프롬프트까지
   섞어서 축을 늘린다. "정리"류에 비중을 더 준다(가장 확실히 검증된 문제라
   여기서 성공 확률이 높음) — 나머지 세 유형은 보조적으로만 섞는다.

held-out 날짜(8/3, 8/4, 7/26)는 지금까지 3개 모델 비교 벤치마크에 이미 쓴
날짜라 전부 제외한다 — 파인튜닝 전/후 효과를 이 날짜들로 재검증해야
"학습 데이터로 시험 본" 게 아니게 된다.

사용 (반드시 API 경로로 실행 — USE_LOCAL_LLM을 비워야 함):
    USE_LOCAL_LLM= uv run python eval/generate_lora_dataset.py > lora_train_data.jsonl
"""

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from screenlog.index import indexed_dates
from screenlog.summarize import (
    DAY_SUMMARY_PROMPT, COMPARE_PROMPT, HANDOVER_PROMPT, SLACK_PROMPT,
    browse, _format_events, _thin_out, _call_llm,
)
from screenlog.source import weekday_ko
from screenlog.router import _format_history
from screenlog.config import MAX_EVENTS_PER_DAY_SUMMARY, CHAT_MODEL

HELD_OUT_DATES = {"2026-08-03", "2026-08-04", "2026-07-26"}
HOUR_SLICES = [(0, 12, "오전"), (12, 18, "오후"), (18, 24, "저녁")]
MIN_EVENTS = 3
REDUNDANT_RATIO = 0.9  # 슬라이스가 하루 이벤트의 90% 이상이면 "하루"와 사실상 같음 -> 건너뜀
QUESTION_VARIANTS = ["자세히 정리해줘", "쉽게 정리해줘"]
N_QUESTION_VARIANT_DATES = 4  # 질문 표현 변형은 일부 날짜에만(전체에 다 적용하면 축이 아니라 그냥 배수가 됨)
N_APP_FILTER_DATES = 6


def day_summary_prompt(date, events, hour_start=None, hour_end=None, question="정리해줘"):
    weekday = weekday_ko(date)
    thinned = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    context = _format_events(thinned)
    scope = f"{hour_start}시~{hour_end}시" if hour_start is not None else "하루"
    return DAY_SUMMARY_PROMPT.format(date=date, weekday=weekday, scope=scope,
                                      history="", context=context, question=question)


async def gen_day_summary_examples(dates):
    """정리 유형 — 하루 전체 + 중복 아닌 시간대 슬라이스 + 앱 필터 + 질문 표현 변형."""
    examples = []
    day_summaries = {}  # 나중에 비교/인수인계/슬랙에서 재사용

    for i, date in enumerate(dates):
        full_events = browse(date)
        if len(full_events) < MIN_EVENTS:
            continue

        prompt = day_summary_prompt(date, full_events)
        completion = await _call_llm(prompt)
        examples.append({"type": "정리", "date": date, "hour_range": [None, None],
                          "prompt": prompt, "completion": completion})
        day_summaries[date] = completion
        print(f"  [정리/{date}/하루] {len(full_events)}개", file=sys.stderr)

        for hour_start, hour_end, label in HOUR_SLICES:
            slice_events = browse(date, hour_start=hour_start, hour_end=hour_end)
            if len(slice_events) < MIN_EVENTS:
                continue
            if len(slice_events) >= REDUNDANT_RATIO * len(full_events):
                print(f"  [정리/{date}/{label}] 하루와 중복(90%+) -> 건너뜀", file=sys.stderr)
                continue
            prompt = day_summary_prompt(date, slice_events, hour_start, hour_end)
            completion = await _call_llm(prompt)
            examples.append({"type": "정리", "date": date, "hour_range": [hour_start, hour_end],
                              "prompt": prompt, "completion": completion})
            print(f"  [정리/{date}/{label}] {len(slice_events)}개", file=sys.stderr)

        if i < N_APP_FILTER_DATES:
            top_app = Counter(e["app"] for e in full_events).most_common(1)[0][0]
            app_events = browse(date, app=top_app)
            if len(app_events) >= MIN_EVENTS and len(app_events) < REDUNDANT_RATIO * len(full_events):
                prompt = day_summary_prompt(date, app_events)
                completion = await _call_llm(prompt)
                examples.append({"type": "정리(앱필터)", "date": date, "app": top_app,
                                  "prompt": prompt, "completion": completion})
                print(f"  [정리/{date}/앱={top_app}] {len(app_events)}개", file=sys.stderr)

        if i < N_QUESTION_VARIANT_DATES:
            for q in QUESTION_VARIANTS:
                prompt = day_summary_prompt(date, full_events, question=q)
                completion = await _call_llm(prompt)
                examples.append({"type": "정리(질문변형)", "date": date, "question": q,
                                  "prompt": prompt, "completion": completion})
                print(f"  [정리/{date}/질문='{q}'] ", file=sys.stderr)

    return examples, day_summaries


async def gen_compare_examples(dates, day_summaries, n=10):
    """날짜 쌍을 고를 때 combinations()를 그대로 앞에서 자르면 첫 날짜가 모든 쌍에
    끼어 편중된다. 인접한 날짜끼리 비교하는 게 자연스럽기도 하고(멀리 떨어진
    날짜보다 실제로 비교할 법한 조합), 각 날짜가 최대 두 쌍에만 등장해 균형이
    맞다 — (dates[0],dates[1]), (dates[1],dates[2]), ... 슬라이딩.
    """
    examples = []
    pairs = list(zip(dates, dates[1:]))[:n]
    for d1, d2 in pairs:
        if d1 not in day_summaries or d2 not in day_summaries:
            continue
        combined = f"[{d1}({weekday_ko(d1)})]\n{day_summaries[d1]}\n\n[{d2}({weekday_ko(d2)})]\n{day_summaries[d2]}"
        question = f"{d1}이랑 {d2} 중 언제 더 바빴어?"
        prompt = COMPARE_PROMPT.format(context=combined, question=question, history=_format_history(None))
        completion = await _call_llm(prompt)
        examples.append({"type": "비교", "dates": [d1, d2], "prompt": prompt, "completion": completion})
        print(f"  [비교/{d1} vs {d2}]", file=sys.stderr)
    return examples


async def gen_handover_examples(dates, day_summaries, n=5, window=3):
    examples = []
    for i in range(0, min(len(dates) - window + 1, n * window), window):
        group = dates[i:i + window]
        if not all(d in day_summaries for d in group):
            continue
        combined = "\n\n".join(f"[{d}({weekday_ko(d)})]\n{day_summaries[d]}" for d in group)
        question = "지금까지 진행 상황 인수인계 문서로 정리해줘"
        prompt = HANDOVER_PROMPT.format(context=combined, question=question, history=_format_history(None))
        completion = await _call_llm(prompt)
        examples.append({"type": "인수인계", "dates": group, "prompt": prompt, "completion": completion})
        print(f"  [인수인계/{group}]", file=sys.stderr)
    return examples


async def gen_slack_examples(dates, day_summaries, n=5):
    examples = []
    for date in dates[:n]:
        if date not in day_summaries:
            continue
        question = f"{date} 정리해서 슬랙 메시지 초안도 써줘"
        prompt = SLACK_PROMPT.format(
            context_label="사용자의 활동을 날짜별로 미리 요약해둔 것이다",
            context=f"[{date}({weekday_ko(date)})]\n{day_summaries[date]}",
            question=question, history=_format_history(None),
        )
        completion = await _call_llm(prompt)
        examples.append({"type": "슬랙", "date": date, "prompt": prompt, "completion": completion})
        print(f"  [슬랙/{date}]", file=sys.stderr)
    return examples


async def main():
    assert "gemini" in CHAT_MODEL, f"API 모델로 실행해야 함(지금: {CHAT_MODEL}). USE_LOCAL_LLM을 비우고 다시 실행할 것."
    print(f"### teacher model={CHAT_MODEL}", file=sys.stderr)

    dates = [d for d in sorted(indexed_dates()) if d not in HELD_OUT_DATES]
    print(f"### 학습 대상 날짜 {len(dates)}개 (held-out {sorted(HELD_OUT_DATES)} 제외)", file=sys.stderr)

    day_examples, day_summaries = await gen_day_summary_examples(dates)
    compare_examples = await gen_compare_examples(dates, day_summaries)
    handover_examples = await gen_handover_examples(dates, day_summaries)
    slack_examples = await gen_slack_examples(dates, day_summaries)

    all_examples = day_examples + compare_examples + handover_examples + slack_examples
    for ex in all_examples:
        print(json.dumps(ex, ensure_ascii=False))

    by_type = Counter(ex["type"] for ex in all_examples)
    print(f"### 총 {len(all_examples)}개 생성 완료: {dict(by_type)}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
