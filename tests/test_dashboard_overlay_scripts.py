import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "integrations" / "mem0-dashboard-overlay"
NULL_PAGE = "export default function Page() { return null; }\n"


def write_verify_fixture(dashboard: Path) -> None:
    for relative in [
        "src/app/(root)/dashboard/categories/page.tsx",
        "src/app/(root)/dashboard/export/page.tsx",
        "src/app/api/sidecar/[...path]/route.ts",
        "src/utils/sidecar-api.ts",
        "src/types/sidecar.ts",
        "src/app/(root)/dashboard/components/main-nav.tsx",
    ]:
        target = dashboard / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(NULL_PAGE)


def test_dashboard_overlay_manifest_lists_phase1_files():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())

    assert "src/app/(root)/dashboard/categories/page.tsx" in manifest["files"]
    assert "src/app/(root)/dashboard/export/page.tsx" in manifest["files"]
    assert "src/app/(root)/dashboard/components/main-nav.tsx" in manifest["files"]
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


def test_apply_dashboard_overlay_normalizes_sidecar_paths_and_removes_patch_footgun(
    tmp_path,
):
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

    assert "function normalizeSidecarPath(path: string): string" in helper_content
    assert "return path.startsWith(\"/\") ? path : `/${path}`;" in helper_content
    assert "METHODS_WITH_BODY = new Set([\"POST\", \"PUT\"]);" in route_content
    assert "export const PATCH = proxy;" not in route_content


def test_apply_dashboard_overlay_route_handles_proxy_errors_explicitly(tmp_path):
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

    assert (
        "function jsonError(message: string, status: number): Response"
        in route_content
    )
    assert (
        'return jsonError("SIDECAR_INTERNAL_API_URL is not configured", 500);'
        in route_content
    )
    assert 'return jsonError("Sidecar upstream request failed", 502);' in route_content


def test_verify_dashboard_overlay_rejects_locked_pages(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_verify_fixture(dashboard)
    locked_page = dashboard / "src/app/(root)/dashboard/categories/page.tsx"
    main_nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    locked_page.write_text(
        "import { LockedPage } from '@/components/self-hosted/locked-page';"
    )
    main_nav.write_text(
        'badge: "SELF-HOSTED"\n'
        'title: "Categories"\n'
        'title: "Webhooks"\n'
        'badge: "PRO"\n'
        'title: "Analytics"\n'
        'badge: "PRO"\n'
        'title: "Export"\n'
        'badge: "SELF-HOSTED"\n'
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
        env={
            **os.environ,
            "MEM0_DASHBOARD_OVERLAY_SKIP_TYPECHECK": "1",
        },
    )

    assert result.returncode == 1
    assert "still imports or renders LockedPage" in result.stderr


def test_verify_dashboard_overlay_rejects_incorrect_navigation_badges(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_verify_fixture(dashboard)
    main_nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    main_nav.write_text(
        'title: "Categories"\n'
        'badge: "PRO"\n'
        'title: "Webhooks"\n'
        'badge: "PRO"\n'
        'title: "Analytics"\n'
        'badge: "PRO"\n'
        'title: "Export"\n'
        'badge: "SELF-HOSTED"\n'
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
        env={
            **os.environ,
            "MEM0_DASHBOARD_OVERLAY_SKIP_TYPECHECK": "1",
        },
    )

    assert result.returncode == 1
    assert 'Categories badge mismatch: expected "SELF-HOSTED"' in result.stderr


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


def test_verify_dashboard_overlay_uses_npm_exec_pnpm_when_global_pnpm_missing(
    tmp_path,
):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "package.json").write_text('{"packageManager":"pnpm@10.34.2"}\n')
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

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm_log = tmp_path / "npm.log"
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > {npm_log}\n"
        "exit 0\n"
    )
    npm.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{Path('/usr/bin')}:{Path('/bin')}"

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
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert npm_log.read_text().strip() == "exec --yes pnpm@10.34.2 -- typecheck"


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


def test_apply_dashboard_overlay_replaces_categories_with_editable_sidecar_page(
    tmp_path,
):
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

    content = (dashboard / "src/app/(root)/dashboard/categories/page.tsx").read_text()

    assert '"use client";' in content
    assert "sidecarGet<SidecarCategoryResponse>" in content
    assert "sidecarPut<SidecarCategoryResponse>" in content
    assert 'const PROJECT_ID = "default";' in content
    assert 'toast({ title: "Categories saved", variant: "success" });' in content
    assert "JSON.parse(category.schemaText)" in content
    assert "type EditableCategory = {" in content
    assert "id: string;" in content
    assert "crypto.randomUUID()" in content
    assert "key={category.id}" in content
    assert 'key={`${category.name}-${index}`}' not in content
    assert "const isEditorDisabled = isLoading || isSaving || !hasLoaded;" in content
    assert "disabled={isEditorDisabled}" in content
    assert (
        "onCheckedChange={(enabled) => updateCategory(index, { enabled })}"
        in content
    )
    assert "disabled={isEditorDisabled}" in content
    assert 'title: "Failed to load categories"' in content
    assert "Retry load" in content
    assert "void loadCategories();" in content
    assert "LockedPage" not in content


def test_apply_dashboard_overlay_replaces_export_with_sidecar_export_page(tmp_path):
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

    content = (dashboard / "src/app/(root)/dashboard/export/page.tsx").read_text()

    assert '"use client";' in content
    assert "sidecarGet<SidecarExportListResponse>" in content
    assert 'await sidecarPost<SidecarExportJob>("/v1/exports"' in content
    assert '`/v1/exports/${job.id}/download`' in content
    assert (
        "function downloadJson(filename: string, payload: SidecarExportDownload)"
        in content
    )
    assert 'const PROJECT_ID = "default";' in content
    assert 'format: "json",' in content
    assert "Object.fromEntries(" in content
    assert 'toast({ title: "Export created", variant: "success" });' in content
    assert 'title: "Failed to load exports"' in content
    assert 'title: "Failed to create export"' in content
    assert 'title: "Failed to download export"' in content
    assert (
        "formatDistanceToNow(new Date(job.created_at), { addSuffix: true })"
        in content
    )
    assert "job.status !== \"SUCCEEDED\"" in content
    assert "Create JSON Export" in content
    assert "Download" in content
    assert "LockedPage" not in content


def test_apply_dashboard_overlay_export_page_uses_safe_blob_download_cleanup(tmp_path):
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

    content = (dashboard / "src/app/(root)/dashboard/export/page.tsx").read_text()

    assert "document.body.appendChild(anchor);" in content
    assert "anchor.click();" in content
    assert "window.setTimeout(() => {" in content
    assert "document.body.removeChild(anchor);" in content
    assert "URL.revokeObjectURL(url);" in content


def test_apply_dashboard_overlay_export_page_includes_loading_error_and_empty_states(
    tmp_path,
):
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

    content = (dashboard / "src/app/(root)/dashboard/export/page.tsx").read_text()

    assert 'const [loadError, setLoadError] = useState<string | null>(null);' in content
    assert "Loading export jobs..." in content
    assert "Failed to load export jobs." in content
    assert "Retry load" in content
    assert "No exports yet." in content
