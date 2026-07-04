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
    assert (dashboard / "src/app/(root)/dashboard/categories/page.tsx").exists()
    assert (dashboard / "src/app/(root)/dashboard/export/page.tsx").exists()


def test_verify_dashboard_overlay_rejects_locked_pages(tmp_path):
    dashboard = tmp_path / "dashboard"
    categories = dashboard / "src/app/(root)/dashboard/categories"
    export = dashboard / "src/app/(root)/dashboard/export"
    categories.mkdir(parents=True)
    export.mkdir(parents=True)
    (categories / "page.tsx").write_text(
        'export default function Page() { return "LockedPage"; }\n'
    )
    (export / "page.tsx").write_text(NULL_PAGE)

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
    )

    assert result.returncode == 1
    assert "LockedPage" in result.stderr


def test_verify_dashboard_overlay_runs_typecheck_when_unlocked(tmp_path):
    dashboard = tmp_path / "dashboard"
    categories = dashboard / "src/app/(root)/dashboard/categories"
    export = dashboard / "src/app/(root)/dashboard/export"
    categories.mkdir(parents=True)
    export.mkdir(parents=True)
    (categories / "page.tsx").write_text(NULL_PAGE)
    (export / "page.tsx").write_text(NULL_PAGE)
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
