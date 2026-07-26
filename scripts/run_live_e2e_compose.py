from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
BROWSER_EVIDENCE_FILENAMES = (
    "client-keys-unauthenticated-desktop.png",
    "client-keys-create-dialog-desktop.png",
    "client-keys-created-copied-desktop.png",
    "client-keys-list-desktop.png",
    "client-keys-list-compact.png",
    "client-keys-revoke-pending-desktop.png",
)
BROWSER_EVIDENCE_PREFIX = "MEM0_E2E_EVIDENCE_JSON="
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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


def resolve_mcp_context() -> Path:
    override = os.environ.get("MEM0_E2E_MCP_CONTEXT")
    if override:
        return Path(override).expanduser().resolve()
    return (ROOT.parents[1] / "mem0-oss-mcp").resolve()


def _git_revision(context: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=context,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def verify_source_revisions(
    *,
    sidecar_context: Path,
    core_context: Path,
    mcp_context: Path,
) -> dict[str, str]:
    sources = {
        "sidecar": (
            sidecar_context,
            "MEM0_E2E_EXPECTED_SIDECAR_SHA",
        ),
        "core": (
            core_context,
            "MEM0_E2E_EXPECTED_CORE_SHA",
        ),
        "mcp": (
            mcp_context,
            "MEM0_E2E_EXPECTED_MCP_SHA",
        ),
    }
    revisions: dict[str, str] = {}
    for name, (context, expected_variable) in sources.items():
        revision = _git_revision(context)
        revisions[name] = revision
        expected = os.environ.get(expected_variable)
        if expected is None:
            continue
        if revision != expected:
            raise RuntimeError(
                f"{name} source revision {revision} did not match "
                f"{expected_variable}={expected}"
            )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=context,
            capture_output=True,
            text=True,
            check=True,
        )
        if status.stdout.strip():
            raise RuntimeError(
                f"{name} source has uncommitted changes; exact-SHA E2E refused"
            )
    print(
        "MEM0_E2E_SOURCE_REVISIONS=" + json.dumps(revisions, sort_keys=True),
        flush=True,
    )
    return revisions


def build_runner_env(*, project_id: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "MEM0_E2E_API_KEY"}
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
        "sidecar",
        "mcp",
        "dashboard",
        "browser",
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
        "browser-smoke",
    ]


def postgres_smoke_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "run",
        "--rm",
        "--no-deps",
        "e2e-runner",
        "python",
        "/app/scripts/run_postgres_migration_smoke.py",
        "--database-url=postgresql+psycopg://postgres:e2e-postgres@postgres/postgres",
    ]


def mocked_browser_smoke_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "run",
        "--rm",
        "--no-deps",
        "browser-smoke",
        "node",
        "/app/run-browser-smoke.cjs",
    ]


def browser_destructive_smoke_command(project_name: str) -> list[str]:
    return [
        *compose_command(project_name),
        "run",
        "--rm",
        "--no-deps",
        "browser-smoke",
        "node",
        "/app/run-browser-destructive-e2e.cjs",
    ]


def browser_evidence_export_command(project_name: str) -> list[str]:
    filenames = json.dumps(BROWSER_EVIDENCE_FILENAMES)
    source = (
        'const fs=require("node:fs");'
        f"const names={filenames};"
        "const payload=Object.fromEntries(names.map((name)=>["
        'name,fs.readFileSync(`/evidence/${name}`).toString("base64")'
        "]));"
        f'process.stdout.write("{BROWSER_EVIDENCE_PREFIX}"+JSON.stringify(payload));'
    )
    return [
        *compose_command(project_name),
        "run",
        "--rm",
        "--no-deps",
        "browser-smoke",
        "node",
        "-e",
        source,
    ]


def export_browser_evidence(
    project_name: str,
    *,
    target: Path,
    env: dict[str, str],
) -> None:
    result = subprocess.run(
        browser_evidence_export_command(project_name),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Could not export browser evidence from the Compose daemon: "
            f"{diagnostic or result.returncode}"
        )

    evidence_line = next(
        (
            line
            for line in reversed(result.stdout.splitlines())
            if line.startswith(BROWSER_EVIDENCE_PREFIX)
        ),
        None,
    )
    if evidence_line is None:
        raise RuntimeError("Browser evidence export returned no evidence payload")
    try:
        encoded_files = json.loads(evidence_line.removeprefix(BROWSER_EVIDENCE_PREFIX))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Browser evidence export returned invalid JSON") from exc
    if not isinstance(encoded_files, dict):
        raise RuntimeError("Browser evidence export returned a non-object payload")
    if set(encoded_files) != set(BROWSER_EVIDENCE_FILENAMES):
        raise RuntimeError(
            f"Browser evidence export returned the wrong files: {sorted(encoded_files)}"
        )

    target.mkdir(parents=True, exist_ok=True)
    for filename in BROWSER_EVIDENCE_FILENAMES:
        try:
            data = base64.b64decode(encoded_files[filename], validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Browser evidence {filename} was not valid base64"
            ) from exc
        if not data.startswith(PNG_SIGNATURE):
            raise RuntimeError(f"Browser evidence {filename} was not a valid PNG")
        destination = target / filename
        destination.write_bytes(data)
        destination.chmod(0o600)


def prepare_dashboard_context(upstream_context: Path, target: Path) -> Path:
    source = upstream_context / "server" / "dashboard"
    if not (source / "package.json").is_file():
        raise FileNotFoundError(f"Dashboard checkout not found at {source}")
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "node_modules",
            ".next",
            "tsconfig.tsbuildinfo",
        ),
    )
    apply_script = (
        ROOT
        / "integrations"
        / "mem0-dashboard-overlay"
        / "scripts"
        / "apply-dashboard-overlay"
    )
    subprocess.run([str(apply_script), str(target)], cwd=ROOT, check=True)
    shutil.copy2(
        ROOT
        / "integrations"
        / "mem0-dashboard-overlay"
        / "docker"
        / "Dockerfile.browser-dashboard",
        target / "Dockerfile.e2e",
    )
    return target


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
            "sidecar",
            "mcp",
            "postgres",
            "openai-stub",
            "e2e-runner",
            "e2e-adoption-runner",
            "dashboard",
            "browser",
            "browser-smoke",
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
    upstream_context = resolve_upstream_context()
    mcp_context = resolve_mcp_context()
    verify_source_revisions(
        sidecar_context=ROOT,
        core_context=upstream_context,
        mcp_context=mcp_context,
    )
    dashboard_temp = tempfile.TemporaryDirectory(prefix="mem0-dashboard-smoke-")
    try:
        dashboard_context = prepare_dashboard_context(
            upstream_context,
            Path(dashboard_temp.name) / "dashboard",
        )
    except Exception:
        dashboard_temp.cleanup()
        raise
    compose_env = os.environ.copy()
    compose_env["MEM0_E2E_UPSTREAM_CONTEXT"] = str(upstream_context)
    compose_env["MEM0_E2E_MCP_CONTEXT"] = str(mcp_context)
    compose_env["MEM0_E2E_DASHBOARD_CONTEXT"] = str(dashboard_context)
    compose_env["MEM0_E2E_PROJECT_ID"] = project_id
    evidence_target = Path(
        os.environ.get(
            "MEM0_E2E_EVIDENCE_DIR",
            "/tmp/mem0-sidecar-e2e-evidence",
        )
    ).expanduser()
    runner_env = build_runner_env(project_id=project_id)
    runner_env["MEM0_E2E_DASHBOARD_CONTEXT"] = str(dashboard_context)
    runner_env["MEM0_E2E_UPSTREAM_CONTEXT"] = str(upstream_context)
    runner_env["MEM0_E2E_MCP_CONTEXT"] = str(mcp_context)
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
            env=runner_env,
        )
        run(postgres_smoke_command(project_name), env=compose_env)
        run(
            compose_run_command(project_name),
            env=runner_env,
        )
        run(
            compose_run_command(
                project_name,
                service_name="e2e-adoption-runner",
            ),
            env=runner_env,
        )
        print("\n=== real destructive browser acceptance gate ===")
        run(browser_destructive_smoke_command(project_name), env=compose_env)
        export_browser_evidence(
            project_name,
            target=evidence_target,
            env=compose_env,
        )
        print("\n=== mocked UI behavior smoke (not deployed-proxy acceptance) ===")
        run(mocked_browser_smoke_command(project_name), env=compose_env)
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
        dashboard_temp.cleanup()
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
