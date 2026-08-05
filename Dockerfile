FROM python:3.14-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra langgraph-agent

COPY src ./src
RUN uv sync --frozen --no-dev --extra langgraph-agent

# distribution/은 uv sync가 안 쓰는 정적 파일(dmg 등)이라, 의존성 설치
# 캐시가 안 깨지게 맨 뒤에 복사한다. 여기가 자주 바뀌는 파일이다.
COPY distribution ./distribution

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "screenlog.api:app", "--host", "0.0.0.0", "--port", "8000"]
