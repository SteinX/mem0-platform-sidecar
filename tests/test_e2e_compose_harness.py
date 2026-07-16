import re
import subprocess
from pathlib import Path

import pytest

import scripts.run_live_e2e_compose as compose_runner
from scripts.run_live_e2e_compose import (
    build_runner_env,
    compose_build_runner_command,
    compose_command,
    compose_down_command,
    compose_run_command,
    compose_up_command,
    resolve_upstream_context,
)

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker" / "docker-compose.e2e.yml"
MOCKED_BROWSER_SMOKE = (
    ROOT
    / "integrations"
    / "mem0-dashboard-overlay"
    / "scripts"
    / "run-browser-smoke.cjs"
)
REAL_BROWSER_SMOKE = (
    ROOT
    / "integrations"
    / "mem0-dashboard-overlay"
    / "scripts"
    / "run-browser-destructive-e2e.cjs"
)


def _compose_service(content: str, service_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service_name)}:\n.*?(?=^  [a-z0-9-]+:\n|^volumes:\n|\Z)",
        content,
    )
    assert match is not None, f"Compose service {service_name!r} is missing"
    return match.group(0)


def test_live_runner_retains_postgres_mocked_ui_and_real_browser_gates() -> None:
    postgres_smoke = ROOT / "scripts" / "run_postgres_migration_smoke.py"

    assert postgres_smoke.is_file()
    assert MOCKED_BROWSER_SMOKE.is_file()
    assert REAL_BROWSER_SMOKE.is_file()
    assert compose_runner.postgres_smoke_command("sidecar-e2e-test")[-3:] == [
        "python",
        "/app/scripts/run_postgres_migration_smoke.py",
        "--database-url=postgresql+psycopg://postgres:e2e-postgres@postgres/postgres",
    ]
    assert hasattr(compose_runner, "mocked_browser_smoke_command")
    assert hasattr(compose_runner, "browser_destructive_smoke_command")
    assert compose_runner.mocked_browser_smoke_command("sidecar-e2e-test")[-2:] == [
        "node",
        "/app/run-browser-smoke.cjs",
    ]
    assert compose_runner.browser_destructive_smoke_command(
        "sidecar-e2e-test"
    )[-2:] == [
        "node",
        "/app/run-browser-destructive-e2e.cjs",
    ]


def test_postgres_smoke_retains_phase2_exact_roundtrip_and_head_parity() -> None:
    source = (ROOT / "scripts" / "run_postgres_migration_smoke.py").read_text()

    assert "MutationIntent" in source
    assert "MutationIntentTarget" in source
    assert "_seed_head_roundtrip(engine)" in source
    assert "_verify_head_roundtrip(engine)" in source
    assert source.count('_migrate(config, "head")') == 5
    assert "_verify_intent_downgrade_guard(engine, config)" in source
    assert "_convert_ready_artifacts_to_exact_b502a26_legacy(engine)" in source
    assert "_verify_compat_snapshot_serialization(engine, config)" in source
    assert source.index("session.query(MutationIntent)") < source.index(
        "session.query(Event)"
    )


def test_browser_smoke_allows_for_first_compile_on_entity_route() -> None:
    browser_smoke = MOCKED_BROWSER_SMOKE.read_text()

    assert 'await waitText("No entities found.", 30000);' in browser_smoke


def test_existing_browser_smoke_is_labeled_mocked_ui_not_acceptance() -> None:
    browser_smoke = MOCKED_BROWSER_SMOKE.read_text().lower()

    assert "mocked ui behavior smoke" in browser_smoke
    assert "not the deployed proxy acceptance gate" in browser_smoke


def test_browser_smoke_mock_uses_singular_encoded_detail_contract() -> None:
    harness = (
        ROOT
        / "integrations"
        / "mem0-dashboard-overlay"
        / "scripts"
        / "test-browser-smoke-contract.cjs"
    )

    result = subprocess.run(
        ["node", str(harness)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "singular encoded detail route passed" in result.stdout


def test_browser_smoke_requires_response_detail_and_zero_browser_errors() -> None:
    browser_smoke = MOCKED_BROWSER_SMOKE.read_text()

    assert (
        'await waitText("browser-smoke-detail-query-from-response");'
        in browser_smoke
    )
    assert "request drawer loaded response-derived detail content" in browser_smoke
    for zero_error_gate in (
        "browserDiagnostics.unhandledRoutes.length === 0",
        "browserDiagnostics.windowErrors.length === 0",
        "pageErrors.length === 0",
        "consoleErrors.length === 0",
        "browserDiagnostics.unhandledRejections.length === 0",
    ):
        assert zero_error_gate in browser_smoke


def test_browser_smoke_retains_opaque_memory_id_action_matrix() -> None:
    browser_smoke = MOCKED_BROWSER_SMOKE.read_text()

    assert 'const opaqueMemoryIds = ["a/b", "a%b", "a%2Fb"]' in browser_smoke
    assert "opaque memory IDs stayed distinct across all item actions" in browser_smoke


def test_real_browser_destructive_script_never_installs_response_mocks() -> None:
    assert REAL_BROWSER_SMOKE.is_file(), (
        "real deployed-proxy browser acceptance script is missing"
    )
    source = REAL_BROWSER_SMOKE.read_text()

    assert re.search(r"window\.fetch\s*=", source) is None
    assert "Page.addScriptToEvaluateOnNewDocument" not in source
    assert "Fetch.fulfillRequest" not in source
    assert "Network.setRequestInterception" not in source


def test_real_browser_destructive_script_contract_is_end_to_end() -> None:
    assert REAL_BROWSER_SMOKE.is_file(), (
        "real deployed-proxy browser acceptance script is missing"
    )
    source = REAL_BROWSER_SMOKE.read_text()

    for contract in (
        "MEM0_E2E_BROWSER_CDP",
        "MEM0_E2E_DASHBOARD_URL",
        "MEM0_E2E_SIDECAR_URL",
        "MEM0_E2E_MEM0_URL",
        "seedFixtureThroughSidecar",
        "openMemoryDetails",
        "confirmExactMemoryId",
        "waitForMemoryToDisappear",
        "assertSidecarAbsent",
        "assertMem0Absent",
        "cleanupFixture",
        'cdp.on("Network.requestWillBeSent"',
        'cdp.on("Network.responseReceived"',
        'method === "DELETE"',
        "status >= 200",
        "status < 300",
        "/api/sidecar/v1/memories/",
        "finally",
    ):
        assert contract in source


def test_browser_runner_image_contains_mocked_and_real_scripts() -> None:
    dockerfile = (ROOT / "tests" / "e2e" / "browser_smoke.Dockerfile").read_text()

    assert "run-browser-smoke.cjs" in dockerfile
    assert "run-browser-destructive-e2e.cjs" in dockerfile


def test_prepare_dashboard_context_applies_overlay_and_browser_shell(
    tmp_path,
) -> None:
    dashboard = tmp_path / "upstream" / "server" / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "package.json").write_text(
        '{"name":"mem0-dashboard","scripts":{"typecheck":"tsc --noEmit"}}'
    )
    (dashboard / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (dashboard / "pnpm-workspace.yaml").write_text("packages:\n  - '.'\n")

    prepared = compose_runner.prepare_dashboard_context(
        tmp_path / "upstream",
        tmp_path / "prepared",
    )

    assert (
        prepared / "src" / "app" / "(root)" / "dashboard" / "memories" / "page.tsx"
    ).is_file()
    client_layout = (
        prepared / "src" / "app" / "(root)" / "clientLayout.tsx"
    ).read_text()
    assert "AuthLoadingState" not in client_layout
    assert "TooltipProvider" in client_layout
    assert (prepared / "Dockerfile.e2e").is_file()
    assert hasattr(compose_runner, "mocked_browser_smoke_command")
    assert compose_runner.mocked_browser_smoke_command("sidecar-e2e-test")[-2:] == [
        "node",
        "/app/run-browser-smoke.cjs",
    ]


def test_build_runner_env_points_live_e2e_at_compose_service(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_E2E_BASE_URL", "http://external.example")
    monkeypatch.setenv("MEM0_E2E_API_KEY", "external-key")
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/custom-upstream")

    env = build_runner_env(project_id="sidecar-local-e2e")

    assert env["MEM0_E2E_BASE_URL"] == "http://mem0:8000"
    assert env["MEM0_E2E_PROJECT_ID"] == "sidecar-local-e2e"
    assert env["MEM0_E2E_UPSTREAM_CONTEXT"] == "/tmp/custom-upstream"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "MEM0_E2E_API_KEY" not in env


def test_build_runner_env_defaults_upstream_context_from_git_layout(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MEM0_E2E_UPSTREAM_CONTEXT", raising=False)

    env = build_runner_env(project_id="sidecar-local-e2e")

    assert env["MEM0_E2E_UPSTREAM_CONTEXT"] == str(resolve_upstream_context())


def test_resolve_upstream_context_prefers_explicit_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/explicit-upstream")

    assert str(resolve_upstream_context()) == "/tmp/explicit-upstream"


def test_compose_command_uses_e2e_file_and_isolated_project() -> None:
    command = compose_command("sidecar-e2e-test")

    assert command[:3] == ["docker", "compose", "-f"]
    assert command[3].endswith("docker/docker-compose.e2e.yml")
    assert command[-2:] == ["-p", "sidecar-e2e-test"]


def test_compose_up_command_starts_local_stack_detached() -> None:
    command = compose_up_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-9:] == [
        "up",
        "-d",
        "--build",
        "openai-stub",
        "postgres",
        "mem0",
        "sidecar",
        "dashboard",
        "browser",
    ]


def test_compose_run_command_executes_pytest_inside_compose_network() -> None:
    command = compose_run_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-4:] == ["run", "--rm", "--no-deps", "e2e-runner"]
    assert "--build" not in command


def test_compose_run_command_can_select_dedicated_adoption_runner() -> None:
    command = compose_run_command(
        "sidecar-e2e-test",
        service_name="e2e-adoption-runner",
    )

    assert command[-4:] == [
        "run",
        "--rm",
        "--no-deps",
        "e2e-adoption-runner",
    ]


def test_compose_build_runner_command_builds_all_isolated_runners() -> None:
    command = compose_build_runner_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-4:] == [
        "build",
        "e2e-runner",
        "e2e-adoption-runner",
        "browser-smoke",
    ]


def test_compose_down_command_removes_local_test_images() -> None:
    command = compose_down_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-5:] == ["down", "-v", "--remove-orphans", "--rmi", "local"]


def test_compose_cleanup_check_lists_remaining_project_resources() -> None:
    command = compose_runner.compose_cleanup_check_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-3:] == ["ps", "--all", "--quiet"]


def test_compose_cleanup_checks_project_containers_networks_volumes_and_images():
    commands = compose_runner.compose_cleanup_resource_commands(
        "sidecar-e2e-test"
    )

    assert commands == {
        "containers": [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=sidecar-e2e-test",
        ],
        "networks": [
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=sidecar-e2e-test",
        ],
        "volumes": [
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=sidecar-e2e-test",
        ],
        "images": [
            "docker",
            "image",
            "ls",
            "--quiet",
            "--filter",
            "reference=sidecar-e2e-test-*",
        ],
    }


def test_verify_compose_cleanup_rejects_remaining_resources(monkeypatch) -> None:
    monkeypatch.setattr(
        compose_runner.subprocess,
        "run",
        lambda command, **kwargs: compose_runner.subprocess.CompletedProcess(
            command,
            0,
            stdout="container-id\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="resources remain"):
        compose_runner.verify_compose_cleanup(
            "sidecar-e2e-test",
            env={},
        )


def test_e2e_postgres_healthcheck_waits_for_final_server() -> None:
    content = COMPOSE_FILE.read_text()

    assert "cat /proc/1/comm" in content
    assert "pg_isready -q -d postgres -U postgres" in content
    assert "start_period: 60s" in content


def test_e2e_compose_runs_real_sidecar_with_ephemeral_database_and_health() -> None:
    content = COMPOSE_FILE.read_text()
    sidecar = _compose_service(content, "sidecar")

    assert "context: .." in sidecar
    assert "dockerfile: docker/Dockerfile" in sidecar
    assert "MEM0_SIDECAR_MEM0_BASE_URL: http://mem0:8000" in sidecar
    assert (
        "MEM0_SIDECAR_DEFAULT_PROJECT_ID: "
        "${MEM0_E2E_PROJECT_ID:-sidecar-e2e}" in sidecar
    )
    assert "MEM0_SIDECAR_DATABASE_URL: sqlite:////data/sidecar-e2e.sqlite3" in sidecar
    assert "sidecar-data:/data" in sidecar
    assert "condition: service_healthy" in sidecar
    assert "http://127.0.0.1:8765/readyz" in sidecar
    assert re.search(r"(?m)^  sidecar-data:\s*$", content)


def test_dashboard_uses_healthy_sidecar_with_exact_project_and_app() -> None:
    content = COMPOSE_FILE.read_text()
    dashboard = _compose_service(content, "dashboard")

    assert "SIDECAR_INTERNAL_API_URL: http://sidecar:8765" in dashboard
    assert "SIDECAR_PROJECT_ID: ${MEM0_E2E_PROJECT_ID:-sidecar-e2e}" in dashboard
    assert "SIDECAR_APP_ID: ${MEM0_E2E_APP_ID:-sidecar-e2e-app}" in dashboard
    assert re.search(
        r"(?ms)depends_on:\s+sidecar:\s+condition: service_healthy",
        dashboard,
    )


def test_browser_runner_receives_exact_live_stack_endpoints_and_scope() -> None:
    browser_runner = _compose_service(COMPOSE_FILE.read_text(), "browser-smoke")

    for contract in (
        "MEM0_E2E_DASHBOARD_URL: http://dashboard:3000",
        "MEM0_E2E_SIDECAR_URL: http://sidecar:8765",
        "MEM0_E2E_MEM0_URL: http://mem0:8000",
        "MEM0_E2E_PROJECT_ID: ${MEM0_E2E_PROJECT_ID:-sidecar-e2e}",
        "MEM0_E2E_APP_ID: ${MEM0_E2E_APP_ID:-sidecar-e2e-app}",
    ):
        assert contract in browser_runner


def test_sidecar_container_volume_image_and_logs_are_in_cleanup_contract() -> None:
    content = COMPOSE_FILE.read_text()
    runner_source = (ROOT / "scripts" / "run_live_e2e_compose.py").read_text()
    resource_commands = compose_runner.compose_cleanup_resource_commands(
        "sidecar-e2e-test"
    )

    assert "  sidecar:" in content
    assert "  sidecar-data:" in content
    assert "sidecar" in compose_up_command("sidecar-e2e-test")
    assert '"sidecar"' in runner_source
    assert "-v" in compose_down_command("sidecar-e2e-test")
    assert "local" in compose_down_command("sidecar-e2e-test")
    assert "label=com.docker.compose.project=sidecar-e2e-test" in (
        resource_commands["containers"]
    )
    assert "label=com.docker.compose.project=sidecar-e2e-test" in (
        resource_commands["volumes"]
    )
    assert "reference=sidecar-e2e-test-*" in resource_commands["images"]


def test_e2e_compose_keeps_unscoped_adoption_gate_on_dedicated_runner() -> None:
    compose_file = ROOT / "docker" / "docker-compose.e2e.yml"
    content = compose_file.read_text()
    default_start = content.index("  e2e-runner:")
    adoption_start = content.index("  e2e-adoption-runner:")
    volumes_start = content.index("\nvolumes:")
    default_runner = content[default_start:adoption_start]
    adoption_runner = content[adoption_start:volumes_start]

    assert "MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED" not in default_runner
    assert 'MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED: "true"' in adoption_runner
    assert 'MEM0_E2E_ADOPTION_ENABLED: "true"' in adoption_runner
    assert "MEM0_E2E_PROJECT_ID:" in adoption_runner
    assert '"not adoption_e2e"' in default_runner
    assert '"adoption_e2e"' in adoption_runner
    assert 'MEM0_OSS_LIST_FETCH_LIMIT: "5000"' in content


def test_compose_main_runs_api_runners_real_gate_then_mocked_ui_smoke(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("MEM0_E2E_PROJECT_ID", "unique-project")
    monkeypatch.setenv("MEM0_E2E_COMPOSE_PROJECT", "unique-compose")
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/upstream")
    monkeypatch.setattr(
        compose_runner,
        "run",
        lambda command, *, env: commands.append(command),
    )
    monkeypatch.setattr(
        compose_runner,
        "wait_for_mem0_ready",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        compose_runner,
        "prepare_dashboard_context",
        lambda upstream, target: target,
    )

    def fake_subprocess_run(command, **kwargs):
        commands.append(command)
        return compose_runner.subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(compose_runner.subprocess, "run", fake_subprocess_run)

    assert compose_runner.main() == 0
    run_services = [
        command[-1]
        for command in commands
        if len(command) >= 4 and command[-4:-1] == ["run", "--rm", "--no-deps"]
    ]
    assert run_services == ["e2e-runner", "e2e-adoption-runner"]
    assert compose_runner.postgres_smoke_command("unique-compose") in commands
    assert hasattr(compose_runner, "mocked_browser_smoke_command")
    assert hasattr(compose_runner, "browser_destructive_smoke_command")
    mocked_command = compose_runner.mocked_browser_smoke_command("unique-compose")
    real_command = compose_runner.browser_destructive_smoke_command(
        "unique-compose"
    )
    assert mocked_command in commands
    assert real_command in commands
    assert commands.index(real_command) < commands.index(mocked_command)


def test_compose_main_reports_cleanup_failure_without_primary(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_E2E_PROJECT_ID", "unique-project")
    monkeypatch.setenv("MEM0_E2E_COMPOSE_PROJECT", "unique-compose")
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/upstream")
    monkeypatch.setattr(compose_runner, "run", lambda command, *, env: None)
    monkeypatch.setattr(
        compose_runner,
        "wait_for_mem0_ready",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        compose_runner,
        "prepare_dashboard_context",
        lambda upstream, target: target,
    )

    def fail_down(command, **kwargs):
        return compose_runner.subprocess.CompletedProcess(
            command,
            1 if "down" in command else 0,
            stdout="",
            stderr="cleanup failed" if "down" in command else "",
        )

    monkeypatch.setattr(compose_runner.subprocess, "run", fail_down)

    with pytest.raises(RuntimeError, match="Compose cleanup failed"):
        compose_runner.main()


def test_compose_main_reports_resource_cleanup_failure_without_primary(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MEM0_E2E_PROJECT_ID", "unique-project")
    monkeypatch.setenv("MEM0_E2E_COMPOSE_PROJECT", "unique-compose")
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/upstream")
    monkeypatch.setattr(compose_runner, "run", lambda command, *, env: None)
    monkeypatch.setattr(
        compose_runner,
        "wait_for_mem0_ready",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        compose_runner,
        "prepare_dashboard_context",
        lambda upstream, target: target,
    )
    monkeypatch.setattr(
        compose_runner.subprocess,
        "run",
        lambda command, **kwargs: compose_runner.subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        compose_runner,
        "verify_compose_cleanup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("resource cleanup failed")
        ),
    )

    with pytest.raises(RuntimeError, match="resource cleanup failed"):
        compose_runner.main()


class PrimaryComposeFailure(Exception):
    pass


@pytest.mark.parametrize("cleanup_failure", ["down", "resources"])
def test_compose_main_cleanup_does_not_mask_primary_failure(
    monkeypatch,
    capsys,
    cleanup_failure,
) -> None:
    monkeypatch.setenv("MEM0_E2E_PROJECT_ID", "unique-project")
    monkeypatch.setenv("MEM0_E2E_COMPOSE_PROJECT", "unique-compose")
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/upstream")
    primary = PrimaryComposeFailure("primary runner failure")
    monkeypatch.setattr(
        compose_runner,
        "prepare_dashboard_context",
        lambda upstream, target: target,
    )
    monkeypatch.setattr(
        compose_runner,
        "run",
        lambda command, *, env: (_ for _ in ()).throw(primary),
    )

    def subprocess_result(command, **kwargs):
        down_failed = cleanup_failure == "down" and "down" in command
        return compose_runner.subprocess.CompletedProcess(
            command,
            1 if down_failed else 0,
            stdout="",
            stderr="down cleanup failed" if down_failed else "",
        )

    monkeypatch.setattr(
        compose_runner.subprocess,
        "run",
        subprocess_result,
    )
    if cleanup_failure == "resources":
        monkeypatch.setattr(
            compose_runner,
            "verify_compose_cleanup",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("resource cleanup failed")
            ),
        )

    with pytest.raises(PrimaryComposeFailure) as exc_info:
        compose_runner.main()

    assert exc_info.value is primary
    assert f"{cleanup_failure.rstrip('s')} cleanup failed" in (
        capsys.readouterr().err
    )


def test_e2e_docs_cover_explorer_reconcile_and_cleanup_contracts() -> None:
    content = (ROOT / "docs" / "e2e.md").read_text()

    for contract in (
        "add -> query -> detail -> patch -> history -> delete",
        "entity, category, and date filters",
        "stale_skipped",
        "adopt_unscoped",
        "MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED",
        "one-project migration",
        "shared upstream stores",
        "unique Compose project",
        "deadline",
        "cleanup",
        "active projection/query results",
        "deleted_at tombstone",
    ):
        assert contract in content


def test_e2e_docs_distinguish_mocked_ui_smoke_from_real_acceptance_gate() -> None:
    content = (ROOT / "docs" / "e2e.md").read_text().lower()

    assert "mocked ui behavior smoke" in content
    assert "not the deployed-proxy acceptance gate" in content
    assert "chromium -> next /api/sidecar -> sidecar -> mem0" in content
    assert "real destructive browser" in content
