import logging
from collections.abc import Callable
from datetime import UTC, datetime

import anyio
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from mem0_sidecar.core.memory_ops import MemoryService, MutationConflictError
from mem0_sidecar.mem0_client.client import Mem0RestClient
from mem0_sidecar.store.models import MutationIntent

LOGGER = logging.getLogger("mem0_sidecar.add_recovery")


class AddRecoveryWorker:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        mem0_client: Mem0RestClient,
        interval_seconds: float = 30.0,
    ) -> None:
        self.session_factory = session_factory
        self.mem0_client = mem0_client
        self.interval_seconds = interval_seconds

    async def run_once(self) -> int:
        with self.session_factory() as session:
            scopes = session.execute(
                select(MutationIntent.project_id, MutationIntent.app_id)
                .where(
                    MutationIntent.operation == "memory.add",
                    or_(
                        MutationIntent.status.in_(("UNKNOWN", "EXHAUSTED", "PENDING")),
                        and_(
                            MutationIntent.status == "ACTIVE",
                            or_(
                                MutationIntent.lease_expires_at.is_(None),
                                MutationIntent.lease_expires_at <= datetime.now(UTC),
                            ),
                        ),
                    ),
                )
                .group_by(MutationIntent.project_id, MutationIntent.app_id)
                .order_by(
                    func.min(MutationIntent.updated_at),
                    MutationIntent.project_id,
                    MutationIntent.app_id,
                )
                .limit(20)
            ).all()
        for project_id, app_id in scopes:
            with self.session_factory() as session:
                try:
                    result = await MemoryService(
                        session=session, mem0=self.mem0_client
                    ).recover_pending_mutations(
                        project_id=project_id, app_id=app_id, add_only=True
                    )
                    session.commit()
                    LOGGER.info(
                        "add_recovery_completed",
                        extra={
                            "project_id": project_id,
                            "app_id": app_id,
                            **result,
                        },
                    )
                except MutationConflictError:
                    session.rollback()
                    LOGGER.warning(
                        "add_recovery_waiting_for_evidence",
                        extra={
                            "project_id": project_id,
                            "app_id": app_id,
                        },
                    )
        return len(scopes)

    async def run_forever(self, stop: anyio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as error:
                LOGGER.error(
                    "add_recovery_poll_failed",
                    extra={
                        "error_type": type(error).__name__,
                    },
                )
            with anyio.move_on_after(self.interval_seconds):
                await stop.wait()
