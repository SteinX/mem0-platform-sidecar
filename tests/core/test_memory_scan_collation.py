import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from mem0_sidecar.core.memory_scan_cursor import (
    CursorState,
    cursor_scope,
    decode_cursor,
    encode_cursor,
)
from mem0_sidecar.core.memory_scan_types import MemoryScanRequest
from tests.core.memory_scan_fixtures import CURSOR_SECRET


def test_signed_cursor_preserves_database_collation_order() -> None:
    def locale_order(left: str, right: str) -> int:
        left_key, right_key = left.replace("ä", "a"), right.replace("ä", "a")
        return (left_key > right_key) - (left_key < right_key)

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_collation("memory_locale", locale_order)
        connection.execute("CREATE TABLE ids (id TEXT COLLATE memory_locale)")
        connection.executemany("INSERT INTO ids VALUES (?)", [("z",), ("ä",)])
        ordered = connection.execute("SELECT id FROM ids ORDER BY id").fetchall()
    assert ordered == [("ä",), ("z",)]
    timestamp = datetime(2026, 10, 10, tzinfo=UTC)
    scope = cursor_scope(
        MemoryScanRequest(project_id="repo-a", app_id="app-a", project_wide=False)
    )
    token = encode_cursor(
        CursorState(
            snapshot_at=timestamp,
            upper=(timestamp, str(ordered[-1][0])),
            after=(timestamp, str(ordered[0][0])),
            total=2,
        ),
        scope,
        CURSOR_SECRET,
    )
    decoded = decode_cursor(token, scope, CURSOR_SECRET)
    assert decoded.after == (timestamp, "ä")
    assert decoded.upper == (timestamp, "z")
