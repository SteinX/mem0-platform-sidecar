from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker" / "docker-compose.e2e.yml"
DEFAULT_PROJECT_ID = "sidecar-e2e"
INTERNAL_MEM0_BASE_URL = "http://mem0:8000"
MEM0_READY_CHECK = (
    "import urllib.request; "
    "urllib.request.urlopen('http://127.0.0.1:8000/docs', timeout=2)"
)


def compose_command(project_name: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-p",
        project_name,
    ]


def resolve_upstream_context() -> Path:
    override = os.environ.get("MEM0_E2E_UPSTREAM_CONTEXT")
    if override:
        return Path(override).expanduser().resolve()

    git_common_dir_result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    git_common_dir = Path(git_common_dir_result.stdout.strip())
    if not git_common_dir.is_absolute():
        git_common_dir = (ROOT / git_common_dir).resolve()
    main_checkout_root = git_common_dir.parent
    return (main_checkout_root.parent / "upstream").resolve()


def build_runner_env(*, project_id: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "MEM0_E2E_API_KEY"
    }
    env["MEM0_E2E_BASE_URL"] = INTERNAL_MEM0_BASE_URL
    env["MEM0_E2E_PROJECT_ID"] = project_id
    env["MEM0_E2E_UPSTREAM_CONTEXT"] = str(resolve_upstream_context())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def compose_up_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "up",
        "-d",
        "--build",
        "openai-stub",
        "postgres",
        "mem0",
    ]


def compose_run_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "run",
        "--rm",
        "--build",
        "e2e-runner",
    ]


def wait_for_mem0_ready(
    project_name: str,
    *,
    timeout_seconds: int,
    env: dict[str, str],
) -> None:
    deadline = time.monotonic() + timeout_seconds
    base_compose = compose_command(project_name)
    last_error = "service did not report readiness"
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                *base_compose,
                "exec",
                "-T",
                "mem0",
                "python",
                "-c",
                MEM0_READY_CHECK,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"readiness command exited {result.returncode}"
        )
        time.sleep(2)

    raise TimeoutError(f"Timed out waiting for Mem0 readiness: {last_error}")


def run(command: list[str], *, env: dict[str, str]) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def dump_diagnostics(base_compose: list[str], *, env: dict[str, str]) -> None:
    print("\n=== docker compose ps ===", file=sys.stderr)
    subprocess.run([*base_compose, "ps"], cwd=ROOT, env=env, check=False)
    print("\n=== docker compose logs ===", file=sys.stderr)
    subprocess.run(
        [
            *base_compose,
            "logs",
            "--no-color",
            "--tail=240",
            "mem0",
            "postgres",
            "openai-stub",
            "e2e-runner",
        ],
        cwd=ROOT,
        env=env,
        check=False,
    )


def main() -> int:
    project_id = os.environ.get("MEM0_E2E_PROJECT_ID", DEFAULT_PROJECT_ID)
    project_name = os.environ.get(
        "MEM0_E2E_COMPOSE_PROJECT",
        f"mem0-sidecar-e2e-{os.getpid()}",
    )
    timeout_seconds = int(os.environ.get("MEM0_E2E_STARTUP_TIMEOUT", "180"))
    compose_env = os.environ.copy()
    compose_env["MEM0_E2E_UPSTREAM_CONTEXT"] = str(resolve_upstream_context())
    base_compose = compose_command(project_name)

    try:
        run(compose_up_command(project_name), env=compose_env)
        wait_for_mem0_ready(
            project_name,
            timeout_seconds=timeout_seconds,
            env=compose_env,
        )
        run(
            compose_run_command(project_name),
            env=build_runner_env(project_id=project_id),
        )
    except Exception:
        dump_diagnostics(base_compose, env=compose_env)
        raise
    finally:
        subprocess.run(
            [*base_compose, "down", "-v", "--remove-orphans"],
            cwd=ROOT,
            env=compose_env,
            check=False,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
