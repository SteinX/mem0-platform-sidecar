import asyncio
import logging
from typing import Any

from mem0_sidecar.config import SidecarSettings
from mem0_sidecar.core.memory_ops import MemoryService

LOGGER = logging.getLogger("mem0_sidecar.direct_write_sync")


class DirectWriteSyncWorker:
    def __init__(
        self,
        *,
        settings: SidecarSettings,
        session_factory: Any,
        mem0_client: Any,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.mem0_client = mem0_client

    async def run_once(self) -> dict[str, Any]:
        with self.session_factory() as session:
            try:
                return await MemoryService(
                    session=session,
                    mem0=self.mem0_client,
                ).mirror_direct_writes(
                    project_id=self.settings.default_project_id,
                    default_app_id=self.settings.direct_write_sync_default_app_id,
                    scan_limit=self.settings.direct_write_sync_scan_limit,
                    legacy_cap=self.settings.direct_write_sync_legacy_cap,
                )
            except BaseException:
                session.rollback()
                raise

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self.run_once()
                LOGGER.info("direct_write_sync_completed", extra=result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.error(
                    "direct_write_sync_failed",
                    extra={"error_type": type(error).__name__},
                )

            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.settings.direct_write_sync_interval_seconds,
                )
            except TimeoutError:
                pass
