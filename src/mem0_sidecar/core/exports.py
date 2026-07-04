import json
from typing import Any

from mem0_sidecar.core.memory_ops import _validate_get_response
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.observability import get_request_id
from mem0_sidecar.store.models import ExportJob, ExportStatus
from mem0_sidecar.store.repositories import ExportJobRepository, MemoryIndexRepository


class ExportValidationError(ValueError):
    pass


def _job_to_dict(job: ExportJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "project_id": job.project_id,
        "status": job.status.value,
        "format": job.format,
        "filters": json.loads(job.filters_json),
        "total_count": job.total_count,
        "exported_count": job.exported_count,
        "skipped_count": job.skipped_count,
        "error": json.loads(job.error_json),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"error_type": type(exc).__name__, "message": str(exc)}
    if request_id := get_request_id():
        payload["request_id"] = request_id
    if isinstance(exc, Mem0UpstreamError):
        payload["upstream_method"] = exc.method
        payload["upstream_path"] = exc.path
        payload["upstream_status_code"] = exc.status_code
        if exc.response_text:
            payload["upstream_response_text"] = exc.response_text[:1000]
    return payload


class ExportService:
    def __init__(
        self,
        *,
        exports: ExportJobRepository,
        memories: MemoryIndexRepository,
        mem0: Any,
    ) -> None:
        self.exports = exports
        self.memories = memories
        self.mem0 = mem0

    async def create_export(
        self,
        *,
        project_id: str,
        export_format: str,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        if export_format != "json":
            raise ExportValidationError("Only json export format is supported")

        job = self.exports.create(
            project_id=project_id,
            export_format=export_format,
            filters=filters,
        )
        self.exports.mark_running(project_id, job.id)

        candidates = self.memories.list_export_candidates(
            project_id=project_id,
            filters=filters,
        )
        exported: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        try:
            for candidate in candidates:
                try:
                    response = await self.mem0.get_memory(candidate.mem0_memory_id)
                except Mem0UpstreamError as exc:
                    if exc.status_code == 404:
                        skipped.append(
                            {
                                "id": candidate.mem0_memory_id,
                                "reason": "upstream_not_found",
                            }
                        )
                        continue
                    raise
                try:
                    exported.append(
                        _validate_get_response(candidate.mem0_memory_id, response)
                    )
                except KeyError:
                    skipped.append(
                        {
                            "id": candidate.mem0_memory_id,
                            "reason": "upstream_mismatch",
                        }
                    )

            result = {
                "project_id": project_id,
                "format": export_format,
                "filters": filters,
                "memories": exported,
                "skipped": skipped,
            }
            job = self.exports.mark_succeeded(
                project_id,
                job.id,
                result=result,
                total_count=len(candidates),
                exported_count=len(exported),
                skipped_count=len(skipped),
            )
            return _job_to_dict(job)
        except Exception as exc:
            job = self.exports.mark_failed(
                project_id,
                job.id,
                error=_error_payload(exc),
            )
            return _job_to_dict(job)

    def list_exports(self, project_id: str) -> dict[str, Any]:
        return {
            "results": [
                _job_to_dict(job)
                for job in self.exports.list_project_exports(project_id)
            ]
        }

    def get_export(self, project_id: str, job_id: str) -> dict[str, Any]:
        return _job_to_dict(self.exports.get(project_id, job_id))

    def download_export(self, project_id: str, job_id: str) -> dict[str, Any]:
        job = self.exports.get(project_id, job_id)
        if job.status != ExportStatus.SUCCEEDED:
            raise ExportValidationError("Export is not complete")
        return json.loads(job.result_json)
