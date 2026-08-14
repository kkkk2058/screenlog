#!/usr/bin/env bash
# 캡처 → 클립 → 최종 MP4 2종.
#
# 슬라이드는 정지 이미지가 아니라 프레임 시퀀스로 찍는다(capture.py). 요소가
# 순서대로 쌓이고 수치가 올라가는 게 영상에서 보여야 하기 때문이다.
# 기능 5종(s22~s26)은 실제 dashboard.html이 돌아가는 화면을 가상 시계로
# 33ms씩 밀면서 찍는다 — 타이핑도 토큰 스트리밍도 진짜 코드가 그린다.
#
# 요구: Google Chrome · ffmpeg · Playwright(파이썬)
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-$(command -v python3)}"
if ! "$PY" -c "import playwright" 2>/dev/null; then
  echo "Playwright가 없다. 설치:"
  echo "  uv venv .venv && uv pip install --python .venv/bin/python playwright   # 브라우저는 설치된 Chrome 사용"
  echo "  PY=.venv/bin/python ./build.sh      # 브라우저는 설치된 Chrome을 그대로 쓴다"
  exit 1
fi

echo "▸ 화면 캡처 크롭 (빈 여백 제거)"
ffmpeg -y -loglevel error -i ../../images/q_count.png -vf "crop=1400:525:0:0" \
  -update 1 shots/q_count_crop.png 2>/dev/null || true

echo "▸ 프레임 캡처"
"$PY" capture.py all

echo "▸ 클립 + 최종 합성"
"$PY" compose.py all

mv -f out/*.mp4 ..
echo "✓ 완료 — docs/video/screenlog-full.mp4, screenlog-recap.mp4"
