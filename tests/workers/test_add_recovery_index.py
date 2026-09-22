import pytest
from sqlalchemy import create_engine, event, text

from mem0_sidecar.store.database import create_session_factory
from mem0_sidecar.store.models import Base
from mem0_sidecar.workers.add_recovery import AddRecoveryWorker

TERMINAL_HISTORY_COUNT = 5_000
MAX_RECOVERY_SCAN_VM_STEPS = 10_000
ADD_RECOVERY_INDEX = "ix_mutation_intents_add_recovery_scan"


@pytest.mark.asyncio
async def test_add_recovery_scan_skips_terminal_global_history(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'add-recovery-index.sqlite3'}"
    engine = create_engine(database_url, future=True)
    measured_vm_steps = [0]
    measure_worker_query = [False]

    def count_vm_steps() -> int:
        if measure_worker_query[0]:
            measured_vm_steps[0] += 100
        return 0

    def configure_sqlite_connection(dbapi_connection, connection_record) -> None:
        del connection_record
        dbapi_connection.set_progress_handler(count_vm_steps, 100)

    event.listen(engine, "connect", configure_sqlite_connection)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                WITH RECURSIVE ordinal(n) AS (
                    SELECT 1
                    UNION ALL
                    SELECT n + 1 FROM ordinal WHERE n < :history_count
                )
                INSERT INTO mutation_intents (
                    id, project_id, app_id, event_id, operation, operation_key,
                    status, payload_json, result_json, error_json, attempt_count,
                    created_at, updated_at, completed_at
                )
                SELECT
                    printf('completed-intent-%d', n),
                    'history-project',
                    'history-app',
                    printf('completed-event-%d', n),
                    'memory.add',
                    printf('completed-key-%d', n),
                    'COMPLETED', '{}', '{}', '{}', 1,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM ordinal
                """
            ),
            {"history_count": TERMINAL_HISTORY_COUNT},
        )

    selected_queries: list[tuple[str, tuple[object, ...]]] = []

    def capture_recovery_query(
        connection,
        cursor,
        statement: str,
        parameters: tuple[object, ...],
        context,
        executemany: bool,
    ) -> None:
        del connection, cursor, context, executemany
        if "FROM mutation_intents" in statement:
            selected_queries.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_recovery_query)
    measure_worker_query[0] = True
    worker = AddRecoveryWorker(
        session_factory=create_session_factory(engine),
        mem0_client=object(),
    )
    assert await worker.run_once() == 0
    measure_worker_query[0] = False
    event.remove(engine, "before_cursor_execute", capture_recovery_query)

    assert measured_vm_steps[0] < MAX_RECOVERY_SCAN_VM_STEPS
    assert len(selected_queries) == 1
    statement, parameters = selected_queries[0]
    with engine.connect() as connection:
        plan_rows = connection.exec_driver_sql(
            f"EXPLAIN QUERY PLAN {statement}", parameters
        ).all()
    plan_details = [row[3] for row in plan_rows]
    assert any(
        f"USING COVERING INDEX {ADD_RECOVERY_INDEX} " in detail
        and "operation=? AND status=?" in detail
        for detail in plan_details
    )
