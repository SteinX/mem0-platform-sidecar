import pytest

from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.repositories import (
    ExportJobRepository,
    MemoryIndexRepository,
    ProjectRepository,
)


class ExportFakeMem0:
    def __init__(self, memories):
        self.memories = memories

    async def get_memory(self, memory_id: str):
        value = self.memories[memory_id]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [{}, {"app_id": None}, {"app_id": "*"}, {"app_id": "foreign"}],
)
async def test_create_export_forces_concrete_app_scope(db_session, filters):
    from mem0_sidecar.core.exports import ExportService

    ProjectRepository(db_session).upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo = MemoryIndexRepository(db_session)
    for memory_id, app_id in (
        ("mem-runtime", "runtime-app"),
        ("mem-foreign", "foreign"),
    ):
        memory_repo.upsert_memory(
            project_id="default",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id=app_id,
            agent_id=None,
            run_id=None,
            category=None,
            metadata={},
        )
    service = ExportService(
        exports=ExportJobRepository(db_session),
        memories=memory_repo,
        mem0=ExportFakeMem0(
            {
                "mem-runtime": {"id": "mem-runtime", "memory": "allowed"},
                "mem-foreign": {"id": "mem-foreign", "memory": "denied"},
            }
        ),
    )

    result = await service.create_export(
        project_id="default",
        app_id="runtime-app",
        export_format="json",
        filters=filters,
    )

    assert result["filters"]["app_id"] == "runtime-app"
    assert service.download_export("default", result["id"], "runtime-app")[
        "memories"
    ] == [{"id": "mem-runtime", "memory": "allowed"}]


def test_export_access_is_exactly_app_scoped_and_none_is_project_wide(db_session):
    from mem0_sidecar.core.exports import ExportService

    repository = ExportJobRepository(db_session)
    service = ExportService(exports=repository, memories=None, mem0=None)
    app_job = repository.create(
        project_id="default", export_format="json", filters={"app_id": "app-a"}
    )
    foreign_job = repository.create(
        project_id="default", export_format="json", filters={"app_id": "app-b"}
    )
    global_job = repository.create(
        project_id="default", export_format="json", filters={}
    )
    null_job = repository.create(
        project_id="default", export_format="json", filters={"app_id": None}
    )
    wildcard_job = repository.create(
        project_id="default", export_format="json", filters={"app_id": "*"}
    )

    scoped_jobs = service.list_exports("default", "app-a")["results"]
    assert [job["id"] for job in scoped_jobs] == [app_job.id]
    assert {job["id"] for job in service.list_exports("default")["results"]} == {
        app_job.id,
        foreign_job.id,
        global_job.id,
        null_job.id,
        wildcard_job.id,
    }
    for inaccessible_job in (foreign_job, global_job, null_job, wildcard_job):
        with pytest.raises(KeyError):
            service.get_export("default", inaccessible_job.id, "app-a")
        with pytest.raises(KeyError):
            service.download_export("default", inaccessible_job.id, "app-a")


@pytest.mark.asyncio
async def test_create_export_succeeds_and_skips_missing_upstream_memory(db_session):
    from mem0_sidecar.core.exports import ExportService

    ProjectRepository(db_session).upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo = MemoryIndexRepository(db_session)
    for memory_id in ("mem-a", "mem-missing"):
        memory_repo.upsert_memory(
            project_id="default",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="codex",
            agent_id=None,
            run_id=None,
            category=None,
            metadata={},
        )

    service = ExportService(
        exports=ExportJobRepository(db_session),
        memories=memory_repo,
        mem0=ExportFakeMem0(
            {
                "mem-a": {"id": "mem-a", "memory": "User likes dark mode"},
                "mem-missing": Mem0UpstreamError(
                    method="GET",
                    path="/memories/mem-missing",
                    status_code=404,
                    response_text="not found",
                    message="not found",
                ),
            }
        ),
    )

    result = await service.create_export(
        project_id="default",
        export_format="json",
        filters={"user_id": "root", "app_id": "codex"},
    )

    assert result["status"] == "SUCCEEDED"
    assert result["total_count"] == 2
    assert result["exported_count"] == 1
    assert result["skipped_count"] == 1

    download = service.download_export("default", result["id"])
    assert download["memories"] == [{"id": "mem-a", "memory": "User likes dark mode"}]
    assert download["skipped"][0]["id"] == "mem-missing"


@pytest.mark.asyncio
async def test_create_export_marks_job_failed_on_upstream_500(db_session):
    from mem0_sidecar.core.exports import ExportService

    ProjectRepository(db_session).upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo = MemoryIndexRepository(db_session)
    memory_repo.upsert_memory(
        project_id="default",
        mem0_memory_id="mem-a",
        user_id="root",
        app_id="codex",
        agent_id=None,
        run_id=None,
        category=None,
        metadata={},
    )
    service = ExportService(
        exports=ExportJobRepository(db_session),
        memories=memory_repo,
        mem0=ExportFakeMem0(
            {
                "mem-a": Mem0UpstreamError(
                    method="GET",
                    path="/memories/mem-a",
                    status_code=500,
                    response_text="boom",
                    message="boom",
                )
            }
        ),
    )

    result = await service.create_export(
        project_id="default",
        export_format="json",
        filters={"user_id": "root"},
    )

    assert result["status"] == "FAILED"
    assert result["error"]["upstream_status_code"] == 500


@pytest.mark.asyncio
async def test_create_export_skips_empty_upstream_get_response(db_session):
    from mem0_sidecar.core.exports import ExportService

    ProjectRepository(db_session).upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo = MemoryIndexRepository(db_session)
    for memory_id in ("mem-a", "mem-empty"):
        memory_repo.upsert_memory(
            project_id="default",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="codex",
            agent_id=None,
            run_id=None,
            category=None,
            metadata={},
        )

    service = ExportService(
        exports=ExportJobRepository(db_session),
        memories=memory_repo,
        mem0=ExportFakeMem0(
            {
                "mem-a": {"id": "mem-a", "memory": "User likes dark mode"},
                "mem-empty": {},
            }
        ),
    )

    result = await service.create_export(
        project_id="default",
        export_format="json",
        filters={"user_id": "root", "app_id": "codex"},
    )

    assert result["status"] == "SUCCEEDED"
    assert result["total_count"] == 2
    assert result["exported_count"] == 1
    assert result["skipped_count"] == 1

    download = service.download_export("default", result["id"])
    assert download["memories"] == [{"id": "mem-a", "memory": "User likes dark mode"}]
    assert download["skipped"] == [{"id": "mem-empty", "reason": "upstream_mismatch"}]


@pytest.mark.asyncio
async def test_create_export_skips_mismatched_upstream_get_response(db_session):
    from mem0_sidecar.core.exports import ExportService

    ProjectRepository(db_session).upsert_default_project(
        project_id="default",
        name="default",
        mem0_base_url="http://mem0:8000",
    )
    memory_repo = MemoryIndexRepository(db_session)
    for memory_id in ("mem-a", "mem-stale"):
        memory_repo.upsert_memory(
            project_id="default",
            mem0_memory_id=memory_id,
            user_id="root",
            app_id="codex",
            agent_id=None,
            run_id=None,
            category=None,
            metadata={},
        )

    service = ExportService(
        exports=ExportJobRepository(db_session),
        memories=memory_repo,
        mem0=ExportFakeMem0(
            {
                "mem-a": {"id": "mem-a", "memory": "User likes dark mode"},
                "mem-stale": {"id": "different-id", "memory": "stale"},
            }
        ),
    )

    result = await service.create_export(
        project_id="default",
        export_format="json",
        filters={"user_id": "root", "app_id": "codex"},
    )

    assert result["status"] == "SUCCEEDED"
    assert result["total_count"] == 2
    assert result["exported_count"] == 1
    assert result["skipped_count"] == 1

    download = service.download_export("default", result["id"])
    assert download["memories"] == [{"id": "mem-a", "memory": "User likes dark mode"}]
    assert download["skipped"] == [{"id": "mem-stale", "reason": "upstream_mismatch"}]
