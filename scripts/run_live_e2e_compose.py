from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

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


def compose_run_command(
    project_name: str,
    *,
    service_name: str = "e2e-runner",
) -> list[str]:
    return [
        *compose_command(project_name),
        "run",
        "--rm",
        "--no-deps",
        service_name,
    ]


def compose_build_runner_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "build",
        "e2e-runner",
        "e2e-adoption-runner",
    ]


def compose_down_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "down",
        "-v",
        "--remove-orphans",
        "--rmi",
        "local",
    ]


def compose_cleanup_check_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "ps",
        "--all",
        "--quiet",
    ]


def compose_cleanup_resource_commands(
    project_name: str,
) -> dict[str, list[str]]:
    project_label = f"label=com.docker.compose.project={project_name}"
    return {
        "containers": [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            project_label,
        ],
        "networks": [
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            project_label,
        ],
        "volumes": [
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            project_label,
        ],
        "images": [
            "docker",
            "image",
            "ls",
            "--quiet",
            "--filter",
            f"reference={project_name}-*",
        ],
    }


def verify_compose_cleanup(project_name: str, *, env: dict[str, str]) -> None:
    remaining: list[str] = []
    for resource_type, command in compose_cleanup_resource_commands(
        project_name
    ).items():
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            diagnostic = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                "Could not verify Compose cleanup for "
                f"{resource_type}: {diagnostic or result.returncode}"
            )
        if resource_ids := result.stdout.strip():
            remaining.append(f"{resource_type}={resource_ids}")
    if remaining:
        raise RuntimeError(
            "Compose cleanup completed but project resources remain: "
            + "; ".join(remaining)
        )


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
            "e2e-adoption-runner",
        ],
        cwd=ROOT,
        env=env,
        check=False,
    )


def main() -> int:
    unique_suffix = f"{os.getpid()}-{uuid4().hex[:8]}"
    project_id = os.environ.get(
        "MEM0_E2E_PROJECT_ID",
        f"{DEFAULT_PROJECT_ID}-{unique_suffix}",
    )
    project_name = os.environ.get(
        "MEM0_E2E_COMPOSE_PROJECT",
        f"mem0-sidecar-e2e-{unique_suffix}",
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
            compose_build_runner_command(project_name),
            env=build_runner_env(project_id=project_id),
        )
        run(
            compose_run_command(project_name),
            env=build_runner_env(project_id=project_id),
        )
        run(
            compose_run_command(
                project_name,
                service_name="e2e-adoption-runner",
            ),
            env=build_runner_env(project_id=project_id),
        )
    except Exception:
        dump_diagnostics(base_compose, env=compose_env)
        raise
    finally:
        active_exception = sys.exc_info()[0] is not None
        cleanup_result = subprocess.run(
            compose_down_command(project_name),
            cwd=ROOT,
            env=compose_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if cleanup_result.returncode != 0:
            diagnostic = (
                cleanup_result.stderr.strip()
                or cleanup_result.stdout.strip()
                or str(cleanup_result.returncode)
            )
            print(f"Compose cleanup failed: {diagnostic}", file=sys.stderr)
            if not active_exception:
                raise RuntimeError(f"Compose cleanup failed: {diagnostic}")
        else:
            try:
                verify_compose_cleanup(project_name, env=compose_env)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                if not active_exception:
                    raise

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
