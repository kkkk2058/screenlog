"""Ollama에 올린 양자화된 LoRA 모델을 원본과 held-out 7개로 비교한다.

Colab에서 병합→GGUF→Q4_K_M 양자화까지 끝낸 뒤 받은 파일을 로컬에서
`ollama create screenlog-lora-q4 -f Modelfile`로 등록했다는 전제.

이 스크립트가 필요한 이유: 처음 recency bias를 발견했을 때도, 지금까지의
LoRA 학습/held-out 검증(Colab, bf16)도 정밀도가 서로 다르다(Ollama Q4_K_M
vs Colab bf16). 여기서는 원본(qwen2.5:7b-instruct, Q4_K_M)과 LoRA
적용본(screenlog-lora-q4, 같은 Q4_K_M)을 **같은 정밀도**로 맞춰서 비교한다
— 이래야 "LoRA가 실제로 쓸 형태에서도 효과가 남아있는지"를 공정하게 본다.

사용:
    uv run python eval/eval_lora_quantized.py heldout_eval.json > lora_quantized_result.json
"""

import json
import re
import sys
import time
from pathlib import Path

from openai import OpenAI

BASE_URL = "http://localhost:11434/v1"
API_KEY = "ollama"
ORIGINAL_MODEL = "qwen2.5:7b-instruct"
LORA_MODEL = "screenlog-lora-q4"

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
    if not start:
        return None
    span = max(to_min(end) - to_min(start), 1)
    mins = cited_minutes(text)
    return 0.0 if not mins else min((max(mins) - min(mins)) / span, 1.0)


def call(client, model, prompt):
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model, temperature=0, messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content, round(time.time() - t0, 1)


def main():
    heldout_path = sys.argv[1] if len(sys.argv) > 1 else "heldout_eval.json"
    heldout = json.loads(Path(heldout_path).read_text())

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

    results = []
    for h in heldout:
        original, t1 = call(client, ORIGINAL_MODEL, h["prompt"])
        lora, t2 = call(client, LORA_MODEL, h["prompt"])
        results.append({
            **h, "original": original, "lora": lora,
            "original_sec": t1, "lora_sec": t2,
            "original_coverage": coverage(original, h["actual_start"], h["actual_end"]),
            "lora_coverage": coverage(lora, h["actual_start"], h["actual_end"]),
            "gold_coverage": coverage(h["gold_answer"], h["actual_start"], h["actual_end"]),
        })
        print(f"  [{h['id']}] original={t1}s lora={t2}s", file=sys.stderr)

    print(json.dumps(results, ensure_ascii=False))

    print("\n" + "=" * 80, file=sys.stderr)
    print(f"{'항목':<28}{'원본':>8}{'LoRA':>8}{'GOLD':>8}", file=sys.stderr)
    for r in results:
        if r["original_coverage"] is None:
            continue
        print(f"{r['id']:<28}{r['original_coverage']:>8.2f}{r['lora_coverage']:>8.2f}"
              f"{r['gold_coverage']:>8.2f}", file=sys.stderr)


if __name__ == "__main__":
    main()
