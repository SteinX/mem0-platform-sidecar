from scripts.run_live_e2e_compose import (
    build_runner_env,
    compose_command,
    compose_run_command,
    compose_up_command,
)


def test_build_runner_env_points_live_e2e_at_compose_service(monkeypatch) -> None:
    monkeypatch.setenv("MEM0_E2E_BASE_URL", "http://external.example")
    monkeypatch.setenv("MEM0_E2E_API_KEY", "external-key")

    env = build_runner_env(project_id="sidecar-local-e2e")

    assert env["MEM0_E2E_BASE_URL"] == "http://mem0:8000"
    assert env["MEM0_E2E_PROJECT_ID"] == "sidecar-local-e2e"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "MEM0_E2E_API_KEY" not in env


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
    assert command[-4:] == ["run", "--rm", "--build", "e2e-runner"]
