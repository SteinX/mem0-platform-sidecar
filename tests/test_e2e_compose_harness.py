import base64
import json
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
    resolve_mcp_context,
    resolve_upstream_context,
    verify_source_revisions,
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
REAL_BROWSER_CONTRACT = ROOT / "tests" / "e2e" / "test-browser-destructive-contract.cjs"


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
    assert compose_runner.browser_destructive_smoke_command("sidecar-e2e-test")[
        -2:
    ] == [
        "node",
        "/app/run-browser-destructive-e2e.cjs",
    ]
    assert hasattr(compose_runner, "export_browser_evidence")


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
        'await waitText("browser-smoke-detail-query-from-response");' in browser_smoke
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


def test_browser_smoke_verifies_browser_local_request_times() -> None:
    browser_smoke = MOCKED_BROWSER_SMOKE.read_text()

    for contract in (
        'timezoneId: "America/Los_Angeles"',
        'locale: "en-US"',
        "request list used browser-local relative time",
        "request timeline used browser-local time",
        "request tooltip used browser-local time",
        "request drawer used browser-local time",
    ):
        assert contract in browser_smoke


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
        "MEM0_E2E_AUTH_DASHBOARD_URL",
        "MEM0_E2E_SIDECAR_URL",
        "MEM0_E2E_SIDECAR_API_KEY",
        "MEM0_E2E_MEM0_URL",
        "proveSidecarRejectsMissingCredentials",
        "Sidecar default authentication did not fail closed",
        "Sidecar operator authentication was not accepted",
        "/v1/events/query",
        "seedFixtureThroughSidecar",
        "sidecarHeaders",
        "openMemoryDetails",
        "confirmExactMemoryId",
        "waitForMemoryToDisappear",
        "assertSidecarAbsent",
        "assertMem0Absent",
        "cleanupFixture",
        "createClientKeyThroughDashboard",
        "assertClientKeyIsOneTimeOnly",
        "revokeClientKeyThroughDashboard",
        "waitForCoreClientKey",
        "cleanupClientKey",
        "proveUnauthenticatedClientKeysRedirect",
        "/dashboard/api-keys",
        "api-key-new",
        "Copy client key",
        "Client key copied",
        "nativeExecCommand",
        "__mem0E2ECopyPayloads",
        '"globalThis.__mem0E2ECopyPayloads.at(-1)"',
        "client-keys-created-copied-desktop.png",
        'padEnd(\n      255,\n      "x",',
        "clickBySelector",
        '"Input.dispatchMouseEvent"',
        "CDP ${method} timed out after ${timeoutMs}ms",
        "Revoke client key",
        "Revoking...",
        "client-keys-revoke-pending-desktop.png",
        "Network.emulateNetworkConditions",
        'cdp.on("Network.requestWillBeSent"',
        'cdp.on("Network.responseReceived"',
        'method === "DELETE"',
        "status >= 200",
        "status < 300",
        "/api/sidecar/v1/memories/",
        "finally",
    ):
        assert contract in source


def test_real_browser_auth_check_uses_an_auth_enabled_dashboard() -> None:
    content = COMPOSE_FILE.read_text()
    mem0 = _compose_service(content, "mem0")
    auth_dashboard = _compose_service(content, "dashboard-auth-check")
    browser = _compose_service(content, "browser")
    browser_runner = _compose_service(content, "browser-smoke")

    assert "DASHBOARD_URL: http://dashboard:3000" in mem0
    assert 'AUTH_DISABLED: "false"' in mem0
    assert 'AUTH_DISABLED: "false"' in auth_dashboard
    assert "dashboard-auth-check:" in browser
    assert (
        "--unsafely-treat-insecure-origin-as-secure=http://dashboard:3000"
        in browser
    )
    assert "condition: service_healthy" in browser
    assert (
        "MEM0_E2E_AUTH_DASHBOARD_URL: http://dashboard-auth-check:3000"
        in browser_runner
    )
    assert "MEM0_E2E_BROWSER_EVIDENCE_DIR: /evidence" in browser_runner
    assert "MEM0_E2E_SIDECAR_API_KEY:" in browser_runner
    assert "MEM0_E2E_EVIDENCE_DIR" in browser_runner


def test_e2e_sidecar_uses_a_private_operator_key() -> None:
    content = COMPOSE_FILE.read_text()
    mem0 = _compose_service(content, "mem0")
    sidecar = _compose_service(content, "sidecar")
    dashboard = _compose_service(content, "dashboard")
    auth_dashboard = _compose_service(content, "dashboard-auth-check")

    assert "ADMIN_API_KEY: e2e-sidecar-operator-key-00000001" in mem0
    assert "MEM0_SIDECAR_MEM0_API_KEY: e2e-sidecar-operator-key-00000001" in sidecar
    for service in (dashboard, auth_dashboard):
        assert "SIDECAR_INTERNAL_API_KEY: e2e-sidecar-operator-key-00000001" in service


def test_real_browser_capture_waits_for_stable_animations_and_compact_fit() -> None:
    source = REAL_BROWSER_SMOKE.read_text()

    assert "waitForVisualStability" in source
    assert "document.getAnimations().every" in source
    assert 'document.querySelectorAll("nextjs-portal")' in source
    assert 'style.setProperty("display", "none", "important")' in source
    assert "async function setViewport" in source
    assert '"Browser.getWindowForTarget"' in source
    assert '"Browser.setWindowBounds"' in source
    assert '"Emulation.setVisibleSize"' in source
    assert '"Emulation.setPageScaleFactor"' in source
    assert "screenWidth: width" in source
    assert "assertClientKeysFitViewport" in source
    assert "document.documentElement.scrollWidth <= viewportWidth" in source
    assert "window.visualViewport?.width" in source
    narrow_metrics = source.index("await setViewport(cdp, {\n      width: 960")
    assert "mobile: false" in source[narrow_metrics : narrow_metrics + 160]
    first_desktop_metrics = source.index("await setViewport(cdp, {\n      width: 1440")
    assert first_desktop_metrics < source.index(
        'stage = "prove unauthenticated Client Keys redirect"'
    )
    assert source.index(
        'await waitForVisualStability(cdp, "compact Client Keys list")'
    ) < source.index(
        'await captureBrowserEvidence(cdp, "client-keys-list-compact.png")'
    )
    assert source.index(
        'await waitForVisualStability(cdp, "Client Keys revoke pending")'
    ) < source.index(
        'await captureBrowserEvidence(cdp, "client-keys-revoke-pending-desktop.png")'
    )


def test_real_browser_delete_finds_action_inside_radix_sheet_dialog() -> None:
    source = REAL_BROWSER_SMOKE.read_text()
    confirm_start = source.index("async function confirmExactMemoryId")
    confirm_end = source.index("\nfunction observeExactDelete", confirm_start)
    confirm_source = source[confirm_start:confirm_end]

    assert "!item.closest('[role=\"dialog\"]')" not in confirm_source
    assert 'item.innerText.includes("Memory details")' in confirm_source
    assert 'drawer.querySelectorAll("button")' in confirm_source


def test_real_browser_direct_mem0_absence_contract_is_executable() -> None:
    result = subprocess.run(
        ["node", str(REAL_BROWSER_CONTRACT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "direct Mem0 absence contract passed" in result.stdout


def test_browser_runner_image_contains_mocked_and_real_scripts() -> None:
    dockerfile = (ROOT / "tests" / "e2e" / "browser_smoke.Dockerfile").read_text()

    assert "run-browser-smoke.cjs" in dockerfile
    assert "run-browser-destructive-e2e.cjs" in dockerfile


def test_browser_evidence_export_command_reads_exact_expected_files() -> None:
    command = compose_runner.browser_evidence_export_command("sidecar-e2e-test")

    assert command[-3:-1] == ["node", "-e"]
    source = command[-1]
    assert "/evidence/" in source
    assert compose_runner.BROWSER_EVIDENCE_PREFIX in source
    for filename in compose_runner.BROWSER_EVIDENCE_FILENAMES:
        assert filename in source


def test_export_browser_evidence_copies_valid_pngs_from_daemon(
    monkeypatch,
    tmp_path,
) -> None:
    png = compose_runner.PNG_SIGNATURE + b"browser-evidence"
    payload = {
        filename: base64.b64encode(png).decode("ascii")
        for filename in compose_runner.BROWSER_EVIDENCE_FILENAMES
    }
    stdout = (
        "compose diagnostic\n"
        f"{compose_runner.BROWSER_EVIDENCE_PREFIX}{json.dumps(payload)}"
    )
    monkeypatch.setattr(
        compose_runner.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        ),
    )

    compose_runner.export_browser_evidence(
        "sidecar-e2e-test",
        target=tmp_path,
        env={},
    )

    for filename in compose_runner.BROWSER_EVIDENCE_FILENAMES:
        destination = tmp_path / filename
        assert destination.read_bytes() == png
        assert destination.stat().st_mode & 0o777 == 0o600


def test_export_browser_evidence_rejects_non_png_payload(
    monkeypatch,
    tmp_path,
) -> None:
    payload = {
        filename: base64.b64encode(b"not-a-png").decode("ascii")
        for filename in compose_runner.BROWSER_EVIDENCE_FILENAMES
    }
    monkeypatch.setattr(
        compose_runner.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(f"{compose_runner.BROWSER_EVIDENCE_PREFIX}{json.dumps(payload)}"),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="not a valid PNG"):
        compose_runner.export_browser_evidence(
            "sidecar-e2e-test",
            target=tmp_path,
            env={},
        )


def test_export_e2e_test_reports_copies_from_daemon_namespace(
    monkeypatch,
    tmp_path,
) -> None:
    reports = {
        filename: f"<testsuite name='{filename}'/>".encode()
        for filename in compose_runner.E2E_TEST_REPORT_FILENAMES
    }
    payload = {
        filename: base64.b64encode(content).decode("ascii")
        for filename, content in reports.items()
    }
    monkeypatch.setattr(
        compose_runner.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                f"{compose_runner.E2E_TEST_REPORT_PREFIX}"
                f"{json.dumps(payload)}"
            ),
            stderr="",
        ),
    )

    compose_runner.export_e2e_test_reports(
        "sidecar-e2e-test",
        target=tmp_path,
        env={},
    )

    assert {
        filename: (tmp_path / filename).read_bytes()
        for filename in compose_runner.E2E_TEST_REPORT_FILENAMES
    } == reports


def test_prepare_dashboard_context_applies_overlay_and_retains_auth_shell(
    tmp_path,
) -> None:
    dashboard = tmp_path / "upstream" / "server" / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "package.json").write_text(
        '{"name":"mem0-dashboard","scripts":{"typecheck":"tsc --noEmit"}}'
    )
    (dashboard / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (dashboard / "pnpm-workspace.yaml").write_text("packages:\n  - '.'\n")
    root_app = dashboard / "src" / "app" / "(root)"
    root_app.mkdir(parents=True)
    (root_app / "clientLayout.tsx").write_text("AuthLoadingState\nTooltipProvider\n")
    (root_app / "dashboard-client-layout.tsx").write_text(
        "AuthProvider\n<ClientLayout>{children}</ClientLayout>\n"
    )

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
    assert "AuthLoadingState" in client_layout
    assert "TooltipProvider" in client_layout
    dashboard_client_layout = (
        prepared / "src" / "app" / "(root)" / "dashboard-client-layout.tsx"
    ).read_text()
    assert "AuthProvider" in dashboard_client_layout
    assert "<ClientLayout>{children}</ClientLayout>" in dashboard_client_layout
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


def test_resolve_mcp_context_prefers_explicit_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_E2E_MCP_CONTEXT", "/tmp/explicit-mcp")

    assert str(resolve_mcp_context()) == "/tmp/explicit-mcp"


def test_exact_source_revision_gate_binds_all_three_clean_sources(
    monkeypatch,
) -> None:
    contexts = {
        "sidecar": Path("/tmp/sidecar"),
        "core": Path("/tmp/core"),
        "mcp": Path("/tmp/mcp"),
    }
    revisions = {
        contexts["sidecar"]: "a" * 40,
        contexts["core"]: "b" * 40,
        contexts["mcp"]: "c" * 40,
    }
    monkeypatch.setattr(
        compose_runner,
        "_git_revision",
        lambda context: revisions[context],
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
    monkeypatch.setenv("MEM0_E2E_EXPECTED_SIDECAR_SHA", "a" * 40)
    monkeypatch.setenv("MEM0_E2E_EXPECTED_CORE_SHA", "b" * 40)
    monkeypatch.setenv("MEM0_E2E_EXPECTED_MCP_SHA", "c" * 40)

    observed = verify_source_revisions(
        sidecar_context=contexts["sidecar"],
        core_context=contexts["core"],
        mcp_context=contexts["mcp"],
    )

    assert observed == {
        "sidecar": "a" * 40,
        "core": "b" * 40,
        "mcp": "c" * 40,
    }


def test_exact_source_revision_gate_requires_all_expected_shas(
    monkeypatch,
) -> None:
    contexts = {
        "sidecar": Path("/tmp/sidecar"),
        "core": Path("/tmp/core"),
        "mcp": Path("/tmp/mcp"),
    }
    monkeypatch.setattr(
        compose_runner,
        "_git_revision",
        lambda _context: "a" * 40,
    )
    for variable in (
        "MEM0_E2E_EXPECTED_SIDECAR_SHA",
        "MEM0_E2E_EXPECTED_CORE_SHA",
        "MEM0_E2E_EXPECTED_MCP_SHA",
    ):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(
        RuntimeError,
        match="MEM0_E2E_EXPECTED_SIDECAR_SHA is required",
    ):
        verify_source_revisions(
            sidecar_context=contexts["sidecar"],
            core_context=contexts["core"],
            mcp_context=contexts["mcp"],
        )


def test_e2e_evidence_manifest_binds_sources_gates_and_screenshots(
    tmp_path,
) -> None:
    for filename in (
        *compose_runner.BROWSER_EVIDENCE_FILENAMES,
        *compose_runner.E2E_TEST_REPORT_FILENAMES,
    ):
        (tmp_path / filename).write_bytes(
            compose_runner.PNG_SIGNATURE + filename.encode("utf-8")
        )
    revisions = {
        "sidecar": "a" * 40,
        "core": "b" * 40,
        "mcp": "c" * 40,
    }
    gates = (
        "postgres_migration_smoke",
        "live_service_tests",
        "adoption_test",
        "destructive_browser",
        "mocked_browser",
    )

    compose_runner.write_e2e_evidence_manifest(
        target=tmp_path,
        revisions=revisions,
        completed_gates=gates,
    )

    manifest = json.loads((tmp_path / "e2e-manifest.json").read_text())
    assert manifest["sources"] == revisions
    assert manifest["completed_gates"] == list(gates)
    assert manifest["completed"] is True
    assert set(manifest["artifacts"]) == set(
        (
            *compose_runner.BROWSER_EVIDENCE_FILENAMES,
            *compose_runner.E2E_TEST_REPORT_FILENAMES,
        )
    )
    assert all(
        artifact["sha256"]
        for artifact in manifest["artifacts"].values()
    )


def test_compose_command_uses_e2e_file_and_isolated_project() -> None:
    command = compose_command("sidecar-e2e-test")

    assert command[:3] == ["docker", "compose", "-f"]
    assert command[3].endswith("docker/docker-compose.e2e.yml")
    assert command[-2:] == ["-p", "sidecar-e2e-test"]


def test_compose_up_command_starts_local_stack_detached() -> None:
    command = compose_up_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-10:] == [
        "up",
        "-d",
        "--build",
        "openai-stub",
        "postgres",
        "mem0",
        "sidecar",
        "mcp",
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
    commands = compose_runner.compose_cleanup_resource_commands("sidecar-e2e-test")

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


def test_e2e_compose_runs_hybrid_mcp_for_multi_token_revocation_gate() -> None:
    content = COMPOSE_FILE.read_text()
    mcp = _compose_service(content, "mcp")
    runner = _compose_service(content, "e2e-runner")

    for contract in (
        "context: ${MEM0_E2E_MCP_CONTEXT:?set MEM0_E2E_MCP_CONTEXT}",
        "MEM0_OSS_MCP_AUTH_MODE: hybrid",
        "MEM0_OSS_MCP_CLIENT_AUTH_URL: http://mem0:8000/auth/me",
        "MEM0_OSS_MCP_TOKEN: e2e-legacy-shared-mcp-key-00000001",
        'MEM0_SIDECAR_REQUIRED: "true"',
        "MEM0_SIDECAR_API_KEY: e2e-sidecar-operator-key-00000001",
        "http://127.0.0.1:8080/health",
    ):
        assert contract in mcp
    for contract in (
        "MEM0_E2E_MCP_URL: http://mcp:8080/mcp",
        "MEM0_E2E_SIDECAR_URL: http://sidecar:8765",
        "MEM0_E2E_MCP_LEGACY_TOKEN: e2e-legacy-shared-mcp-key-00000001",
    ):
        assert contract in runner


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
    assert (
        "label=com.docker.compose.project=sidecar-e2e-test"
        in (resource_commands["containers"])
    )
    assert (
        "label=com.docker.compose.project=sidecar-e2e-test"
        in (resource_commands["volumes"])
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


def _pin_compose_main_sources(monkeypatch) -> None:
    revision = "a" * 40
    for variable in (
        "MEM0_E2E_EXPECTED_SIDECAR_SHA",
        "MEM0_E2E_EXPECTED_CORE_SHA",
        "MEM0_E2E_EXPECTED_MCP_SHA",
    ):
        monkeypatch.setenv(variable, revision)
    monkeypatch.setattr(
        compose_runner,
        "_git_revision",
        lambda _context: revision,
    )
    monkeypatch.setattr(
        compose_runner,
        "write_e2e_evidence_manifest",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        compose_runner,
        "export_e2e_test_reports",
        lambda *args, **kwargs: None,
    )


def test_compose_main_runs_api_runners_real_gate_then_mocked_ui_smoke(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("MEM0_E2E_PROJECT_ID", "unique-project")
    monkeypatch.setenv("MEM0_E2E_COMPOSE_PROJECT", "unique-compose")
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/upstream")
    _pin_compose_main_sources(monkeypatch)
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
    evidence_export = ["export-browser-evidence"]
    report_export = ["export-test-reports"]
    monkeypatch.setattr(
        compose_runner,
        "export_browser_evidence",
        lambda *args, **kwargs: commands.append(evidence_export),
    )
    monkeypatch.setattr(
        compose_runner,
        "export_e2e_test_reports",
        lambda *args, **kwargs: commands.append(report_export),
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
    real_command = compose_runner.browser_destructive_smoke_command("unique-compose")
    assert mocked_command in commands
    assert real_command in commands
    assert commands.index(report_export) < commands.index(real_command)
    assert commands.index(real_command) < commands.index(evidence_export)
    assert commands.index(evidence_export) < commands.index(mocked_command)


def test_compose_main_reports_cleanup_failure_without_primary(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_E2E_PROJECT_ID", "unique-project")
    monkeypatch.setenv("MEM0_E2E_COMPOSE_PROJECT", "unique-compose")
    monkeypatch.setenv("MEM0_E2E_UPSTREAM_CONTEXT", "/tmp/upstream")
    _pin_compose_main_sources(monkeypatch)
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
        compose_runner,
        "export_browser_evidence",
        lambda *args, **kwargs: None,
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
    _pin_compose_main_sources(monkeypatch)
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
        compose_runner,
        "export_browser_evidence",
        lambda *args, **kwargs: None,
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
    _pin_compose_main_sources(monkeypatch)
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
    assert f"{cleanup_failure.rstrip('s')} cleanup failed" in (capsys.readouterr().err)


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
