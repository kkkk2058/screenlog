"""LoRA 파인튜닝 전/후를 검증할 held-out 평가셋을 만든다.

generate_lora_dataset.py가 학습에서 제외한 3개 날짜(7/26, 8/3, 8/4)로만
만든다. 이 날짜들은 docs/local-llm-experiment-report.md의 3개 모델 비교
벤치마크에도 쓴 날이라, "학습 전에 이미 실패가 실측된 케이스"라는 성질이
있다 — 파인튜닝 후 같은 질문을 다시 던져서 그 실패가 고쳐졌는지를 본다.

정리 유형만 보지 않고 비교/슬랙까지 넣는 이유: LoRA가 목표한 능력(하루
커버리지)을 고치면서 다른 능력을 망가뜨리는(catastrophic forgetting) 걸
같이 확인해야 하기 때문이다. 학습 데이터의 유형 비중과 대응된다.

각 항목에 실제 이벤트의 시작/끝 시각을 같이 저장한다 — 답변에 인용된 시각이
실제 범위를 얼마나 덮는지 자동으로 계산해서, 사람이 답변을 읽고 판단하는 걸
보조하는 용도다(숫자만으로 판단하지는 않는다).

사용 (반드시 API 경로로 — USE_LOCAL_LLM 비우고):
    USE_LOCAL_LLM= uv run python eval/generate_heldout_eval.py > heldout_eval.json
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from screenlog.summarize import (
    DAY_SUMMARY_PROMPT, COMPARE_PROMPT, SLACK_PROMPT,
    browse, _format_events, _thin_out, _call_llm,
)
from screenlog.source import weekday_ko
from screenlog.router import _format_history
from screenlog.config import MAX_EVENTS_PER_DAY_SUMMARY, CHAT_MODEL

HELD_OUT_DATES = ["2026-07-26", "2026-08-03", "2026-08-04"]


def hhmm(iso_ts):
    return iso_ts[11:16]


def day_prompt(date, events, hour_start=None, hour_end=None, question="정리해줘"):
    thinned = _thin_out(events, MAX_EVENTS_PER_DAY_SUMMARY)
    scope = f"{hour_start}시~{hour_end}시" if hour_start is not None else "하루"
    return DAY_SUMMARY_PROMPT.format(
        date=date, weekday=weekday_ko(date), scope=scope,
        history="", context=_format_events(thinned), question=question,
    )


async def main():
    assert "gemini" in CHAT_MODEL, f"API 모델로 실행해야 함(지금: {CHAT_MODEL})."
    print(f"### teacher={CHAT_MODEL}", file=sys.stderr)

    items = []
    day_summaries = {}

    for date in HELD_OUT_DATES:
        events = browse(date)
        prompt = day_prompt(date, events)
        gold = await _call_llm(prompt)
        day_summaries[date] = gold
        items.append({
            "id": f"정리-{date}", "type": "정리", "date": date,
            "actual_start": hhmm(events[0]["start"]), "actual_end": hhmm(events[-1]["start"]),
            "n_events": len(events), "prompt": prompt, "gold_answer": gold,
        })
        print(f"  [정리/{date}] {len(events)}개 {hhmm(events[0]['start'])}~{hhmm(events[-1]['start'])}",
              file=sys.stderr)

    # 시간대 슬라이스 — 하루 전체보다 짧은 컨텍스트에서도 같은 문제가 나는지 본다.
    for date, (hs, he) in [("2026-08-03", (12, 18)), ("2026-08-04", (18, 24))]:
        events = browse(date, hour_start=hs, hour_end=he)
        if len(events) < 3:
            continue
        prompt = day_prompt(date, events, hs, he)
        gold = await _call_llm(prompt)
        items.append({
            "id": f"정리({hs}-{he}시)-{date}", "type": "정리(시간대)", "date": date,
            "actual_start": hhmm(events[0]["start"]), "actual_end": hhmm(events[-1]["start"]),
            "n_events": len(events), "prompt": prompt, "gold_answer": gold,
        })
        print(f"  [정리/{date}/{hs}~{he}시] {len(events)}개", file=sys.stderr)

    # 비교 — 다른 능력이 파인튜닝으로 망가지지 않았는지 확인용
    d1, d2 = "2026-08-03", "2026-08-04"
    combined = "\n\n".join(f"[{d}({weekday_ko(d)})]\n{day_summaries[d]}" for d in (d1, d2))
    question = f"{d1}이랑 {d2} 중 언제 더 바빴어?"
    prompt = COMPARE_PROMPT.format(context=combined, question=question, history=_format_history(None))
    items.append({
        "id": f"비교-{d1}vs{d2}", "type": "비교", "date": None,
        "actual_start": None, "actual_end": None, "n_events": None,
        "prompt": prompt, "gold_answer": await _call_llm(prompt),
    })
    print(f"  [비교/{d1} vs {d2}]", file=sys.stderr)

    # 슬랙 초안 — 지난 실험에서 Qwen7B가 도구 호출을 포기했던 유형
    date = "2026-08-03"
    question = f"{date} 정리해서 슬랙 메시지 초안도 써줘"
    prompt = SLACK_PROMPT.format(
        context_label="사용자의 활동을 날짜별로 미리 요약해둔 것이다",
        context=f"[{date}({weekday_ko(date)})]\n{day_summaries[date]}",
        question=question, history=_format_history(None),
    )
    items.append({
        "id": f"슬랙-{date}", "type": "슬랙", "date": date,
        "actual_start": None, "actual_end": None, "n_events": None,
        "prompt": prompt, "gold_answer": await _call_llm(prompt),
    })
    print(f"  [슬랙/{date}]", file=sys.stderr)

    print(json.dumps(items, ensure_ascii=False))
    print(f"### held-out {len(items)}개 생성 완료", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
