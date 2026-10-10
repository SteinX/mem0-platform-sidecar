import base64
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from mem0_sidecar.core.memory_scan import MemoryScanService
from mem0_sidecar.core.memory_scan_cursor import (
    CursorState,
    cursor_scope,
    decode_cursor,
    encode_cursor,
)
from mem0_sidecar.core.memory_scan_types import (
    MemoryScanFilters,
    MemoryScanRequest,
    MemoryScanValidationError,
)
from tests.core.memory_scan_fixtures import CURSOR_SECRET, seed_memories


def _replace_cursor_field(token: str, field: str, value) -> str:
    padded = token + "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    payload[field] = value
    return base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


@pytest.mark.asyncio
async def test_cursor_is_bound_to_full_request_scope_before_reads(db_session) -> None:
    core = seed_memories(db_session, 2)
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )
    first = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            filters=MemoryScanFilters(user_id="alice"),
            mode="page",
            page_size=1,
        )
    )
    assert first.next_cursor is not None
    core.calls.clear()

    rebound_requests = (
        MemoryScanRequest(
            project_id="repo-b",
            app_id="app-a",
            project_wide=False,
            filters=MemoryScanFilters(user_id="alice"),
            cursor=first.next_cursor,
        ),
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-b",
            project_wide=False,
            filters=MemoryScanFilters(user_id="alice"),
            cursor=first.next_cursor,
        ),
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            filters=MemoryScanFilters(user_id="bob"),
            cursor=first.next_cursor,
        ),
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            filters=MemoryScanFilters(user_id="alice", type="decision"),
            cursor=first.next_cursor,
        ),
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            filters=MemoryScanFilters(user_id="alice"),
            cursor=first.next_cursor,
            include_expired=True,
        ),
    )
    for rebound in rebound_requests:
        with pytest.raises(
            MemoryScanValidationError,
            match="memory scan cursor does not match request scope",
        ):
            await service.scan(rebound)
    assert core.calls == []


@pytest.mark.asyncio
async def test_malformed_and_oversized_cursors_are_rejected(db_session) -> None:
    core = seed_memories(db_session, 1)
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )

    nested = base64.urlsafe_b64encode(("[" * 1100 + "]" * 1100).encode()).decode()
    for cursor in ("not-base64!", "x" * 4097, "é", nested):
        with pytest.raises(
            MemoryScanValidationError, match="invalid memory scan cursor"
        ):
            await service.scan(
                MemoryScanRequest(
                    project_id="repo-a",
                    app_id="app-a",
                    project_wide=False,
                    cursor=cursor,
                )
            )


@pytest.mark.asyncio
async def test_tampered_cursor_state_is_rejected_before_reads(db_session) -> None:
    core = seed_memories(db_session, 2)
    service = MemoryScanService(
        session=db_session, mem0=core, cursor_secret=CURSOR_SECRET
    )
    first = await service.scan(
        MemoryScanRequest(
            project_id="repo-a",
            app_id="app-a",
            project_wide=False,
            page_size=1,
        )
    )
    assert first.next_cursor is not None
    padded = first.next_cursor + "=" * (-len(first.next_cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    mutations = {
        "upper": ["2099-01-01T00:00:00+00:00", "mem-99999"],
        "after": ["2026-10-10T00:00:00+00:00", "mem--1"],
        "snapshot_at": "2099-01-01T00:00:00+00:00",
        "total": payload["total"] + 1,
    }
    sql_reads: list[str] = []

    def count_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        sql_reads.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", count_sql)
    core.calls.clear()
    try:
        for field, value in mutations.items():
            token = _replace_cursor_field(first.next_cursor, field, value)
            with pytest.raises(
                MemoryScanValidationError, match="invalid memory scan cursor"
            ):
                await service.scan(
                    MemoryScanRequest(
                        project_id="repo-a",
                        app_id="app-a",
                        project_wide=False,
                        page_size=1,
                        cursor=token,
                    )
                )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", count_sql)
    assert sql_reads == []
    assert core.calls == []


def test_cross_key_non_ascii_mac_and_datetime_overflow_are_rejected() -> None:
    request = MemoryScanRequest(
        project_id="repo-a", app_id="app-a", project_wide=False
    )
    scope = cursor_scope(request)
    keyset = (datetime(2026, 10, 10, tzinfo=UTC), "mem-1")
    token = encode_cursor(
        CursorState(datetime.now(UTC), keyset, keyset, 1), scope, CURSOR_SECRET
    )

    invalid_tokens = (
        (token, b"different-cursor-secret-for-tests"),
        (_replace_cursor_field(token, "mac", "é" * 64), CURSOR_SECRET),
        (
            encode_cursor(
                CursorState(
                    datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
                    keyset,
                    keyset,
                    1,
                ),
                scope,
                CURSOR_SECRET,
            ),
            CURSOR_SECRET,
        ),
    )
    for invalid_token, secret in invalid_tokens:
        with pytest.raises(
            MemoryScanValidationError, match="invalid memory scan cursor"
        ):
            decode_cursor(invalid_token, scope, secret)
