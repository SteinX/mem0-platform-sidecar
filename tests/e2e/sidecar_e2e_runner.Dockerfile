FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests

RUN python -m pip install --no-cache-dir -e .[dev]

CMD ["python", "-m", "pytest", "tests/e2e/test_live_mem0_oss.py", "-v", "-rs", "-p", "no:cacheprovider"]
