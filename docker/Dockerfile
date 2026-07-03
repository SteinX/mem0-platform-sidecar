FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir -e .

EXPOSE 8765

CMD ["uvicorn", "mem0_sidecar.http_adapter.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
