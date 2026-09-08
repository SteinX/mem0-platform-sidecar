FROM python:3.11-slim AS sqlite-build

RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

ADD https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip /tmp/sqlite.zip
RUN echo '1e71ddf93849c6a6ecf58b827c0692073d2dd7ee40196158068f7b29f422e87d  /tmp/sqlite.zip' | sha256sum --check \
    && python -m zipfile -e /tmp/sqlite.zip /tmp \
    && gcc -O2 -fPIC -shared -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 \
       -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_COLUMN_METADATA \
       -Wl,-soname,libsqlite3.so.0 /tmp/sqlite-amalgamation-3530400/sqlite3.c \
       -o /tmp/libsqlite3.so.0 -lpthread -ldl -lm

FROM python:3.11-slim

COPY --from=sqlite-build /tmp/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
RUN ldconfig && python -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4", sqlite3.sqlite_version'

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir -e .

EXPOSE 8765

CMD ["sh", "-c", "python -m alembic upgrade head && exec uvicorn mem0_sidecar.http_adapter.app:create_app --factory --host 0.0.0.0 --port 8765"]
