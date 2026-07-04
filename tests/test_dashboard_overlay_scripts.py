import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "integrations" / "mem0-dashboard-overlay"
NULL_PAGE = "export default function Page() { return null; }\n"


def test_dashboard_overlay_manifest_lists_phase1_files():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())

    assert "src/app/(root)/dashboard/categories/page.tsx" in manifest["files"]
    assert "src/app/(root)/dashboard/export/page.tsx" in manifest["files"]
    assert "src/app/api/sidecar/[...path]/route.ts" in manifest["files"]


def test_apply_dashboard_overlay_copies_files(tmp_path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    manifest = json.loads((OVERLAY / "manifest.json").read_text())

    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "apply-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for relative in manifest["files"]:
        assert (dashboard / relative).exists(), relative


def test_apply_dashboard_overlay_copies_sidecar_proxy_and_client_exports(tmp_path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "apply-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    route_content = (dashboard / "src/app/api/sidecar/[...path]/route.ts").read_text()
    helper_content = (dashboard / "src/utils/sidecar-api.ts").read_text()
    type_content = (dashboard / "src/types/sidecar.ts").read_text()

    assert "export const GET = proxy;" in route_content
    assert "export const POST = proxy;" in route_content
    assert "export async function sidecarGet<T>" in helper_content
    assert "export async function sidecarPut<T>" in helper_content
    assert "export async function sidecarPost<T>" in helper_content
    assert "export type SidecarCategory =" in type_content
    assert "export type SidecarExportJob =" in type_content


def test_verify_dashboard_overlay_rejects_locked_pages(tmp_path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "apply-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    categories = dashboard / "src/app/(root)/dashboard/categories"
    (categories / "page.tsx").write_text(
        'export default function Page() { return "LockedPage"; }\n'
    )

    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "verify-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": f"{dashboard}:{Path('/usr/bin')}:{Path('/bin')}"},
    )

    assert result.returncode == 1
    assert "LockedPage" in result.stderr


def test_verify_dashboard_overlay_runs_typecheck_when_unlocked(tmp_path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "apply-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    pnpm = dashboard / "pnpm"
    pnpm.write_text("#!/bin/sh\nexit 0\n")
    pnpm.chmod(0o755)

    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "verify-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": f"{dashboard}:{Path('/usr/bin')}:{Path('/bin')}"},
    )

    assert result.returncode == 0, result.stderr


def test_verify_dashboard_overlay_rejects_missing_manifest_file(tmp_path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    pnpm = dashboard / "pnpm"
    pnpm.write_text("#!/bin/sh\nexit 0\n")
    pnpm.chmod(0o755)

    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "apply-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": f"{dashboard}:{Path('/usr/bin')}:{Path('/bin')}"},
    )

    assert result.returncode == 0, result.stderr

    missing = dashboard / manifest["files"][2]
    missing.unlink()

    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "verify-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": f"{dashboard}:{Path('/usr/bin')}:{Path('/bin')}"},
    )

    assert result.returncode == 1
    assert manifest["files"][2] in result.stderr
