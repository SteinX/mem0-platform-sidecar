from pathlib import Path

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
    assert command[-6:] == ["up", "-d", "--build", "openai-stub", "postgres", "mem0"]


def test_compose_run_command_executes_pytest_inside_compose_network() -> None:
    command = compose_run_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-4:] == ["run", "--rm", "--no-deps", "e2e-runner"]
    assert "--build" not in command


def test_compose_build_runner_command_builds_only_runner() -> None:
    command = compose_build_runner_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-2:] == ["build", "e2e-runner"]


def test_compose_down_command_removes_local_test_images() -> None:
    command = compose_down_command("sidecar-e2e-test")

    assert command[:5] == ["docker", "compose", "-f", command[3], "-p"]
    assert command[-5:] == ["down", "-v", "--remove-orphans", "--rmi", "local"]


def test_e2e_postgres_healthcheck_waits_for_final_server() -> None:
    compose_file = ROOT / "docker" / "docker-compose.e2e.yml"

    content = compose_file.read_text()

    assert "cat /proc/1/comm" in content
    assert "pg_isready -q -d postgres -U postgres" in content
