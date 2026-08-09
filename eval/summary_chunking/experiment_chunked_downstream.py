"""분할(chunked) 요약이 하위 기능(비교/인수인계/슬랙)에도 도움이 되는지 확인한다.

experiment_chunked_summary.py가 "정리" 하나만 봤다면, 이건 그 결과를 재료로
써서 compare_range()/handover_range()/draft_slack_range()가 실제로 만드는
COMPARE_PROMPT/HANDOVER_PROMPT/SLACK_PROMPT까지 chunked 요약을 넣었을 때와
baseline(기존 방식) 요약을 넣었을 때를 비교한다. summarize.py는 그대로 두고
import만 한다.

사용:
    uv run python eval/experiment_chunked_downstream.py /tmp/chunked_result.json heldout_eval.json
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from screenlog.summarize import COMPARE_PROMPT, HANDOVER_PROMPT, SLACK_PROMPT, _call_llm
from screenlog.router import _format_history
from screenlog.source import weekday_ko


async def compare(d1, d2, s1, s2, question):
    combined = f"[{d1}({weekday_ko(d1)})]\n{s1}\n\n[{d2}({weekday_ko(d2)})]\n{s2}"
    prompt = COMPARE_PROMPT.format(context=combined, question=question, history=_format_history(None))
    return await _call_llm(prompt)


async def handover(dates, summaries, question):
    combined = "\n\n".join(f"[{d}({weekday_ko(d)})]\n{s}" for d, s in zip(dates, summaries))
    prompt = HANDOVER_PROMPT.format(context=combined, question=question, history=_format_history(None))
    return await _call_llm(prompt)


async def slack(date, s, question):
    prompt = SLACK_PROMPT.format(context_label="사용자의 활동을 날짜별로 미리 요약해둔 것이다",
                                  context=f"[{date}({weekday_ko(date)})]\n{s}",
                                  question=question, history=_format_history(None))
    return await _call_llm(prompt)


async def main():
    chunked_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/chunked_result.json"
    heldout_path = sys.argv[2] if len(sys.argv) > 2 else "heldout_eval.json"

    chunked_data = {r["date"]: r for r in json.loads(Path(chunked_path).read_text())}
    heldout = json.loads(Path(heldout_path).read_text())
    gold_by_id = {h["id"]: h for h in heldout}

    d1, d2 = "2026-08-03", "2026-08-04"
    base1, base2 = chunked_data[d1]["baseline"], chunked_data[d2]["baseline"]
    chunk1, chunk2 = chunked_data[d1]["chunked"], chunked_data[d2]["chunked"]

    results = {}

    print("### 비교", file=sys.stderr)
    q_compare = f"{d1}이랑 {d2} 중 언제 더 바빴어?"
    results["compare_baseline"] = await compare(d1, d2, base1, base2, q_compare)
    results["compare_chunked"] = await compare(d1, d2, chunk1, chunk2, q_compare)
    results["compare_gold"] = gold_by_id.get(f"비교-{d1}vs{d2}", {}).get("gold_answer")

    print("### 인수인계", file=sys.stderr)
    q_handover = "지금까지 진행 상황 인수인계 문서로 정리해줘"
    results["handover_baseline"] = await handover([d1, d2], [base1, base2], q_handover)
    results["handover_chunked"] = await handover([d1, d2], [chunk1, chunk2], q_handover)

    print("### 슬랙", file=sys.stderr)
    q_slack = f"{d1} 정리해서 슬랙 메시지 초안도 써줘"
    results["slack_baseline"] = await slack(d1, base1, q_slack)
    results["slack_chunked"] = await slack(d1, chunk1, q_slack)
    results["slack_gold"] = gold_by_id.get(f"슬랙-{d1}", {}).get("gold_answer")

    print(json.dumps(results, ensure_ascii=False))
    print("완료", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
