import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "integrations" / "mem0-dashboard-overlay"
UPSTREAM_DASHBOARD = ROOT.parents[2] / "upstream" / "server" / "dashboard"
DASHBOARD_TYPESCRIPT = UPSTREAM_DASHBOARD / "node_modules" / "typescript"
VERIFY_DASHBOARD_OVERLAY = OVERLAY / "scripts" / "verify-dashboard-overlay"
NULL_PAGE = "export default function Page() { return null; }\n"


def write_dashboard_package(dashboard: Path) -> None:
    dashboard.mkdir()
    (dashboard / "package.json").write_text(
        json.dumps(
            {
                "name": "mem0-dashboard",
                "packageManager": "pnpm@10.34.2",
                "scripts": {"typecheck": "tsc --noEmit"},
            }
        )
    )


def applied_overlay(tmp_path: Path) -> Path:
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)
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
    return dashboard


def applied_upstream_overlay(tmp_path: Path) -> Path:
    dashboard = tmp_path / "dashboard"
    shutil.copytree(
        UPSTREAM_DASHBOARD,
        dashboard,
        ignore=shutil.ignore_patterns("node_modules", ".next"),
        symlinks=True,
    )
    (dashboard / "node_modules").symlink_to(
        UPSTREAM_DASHBOARD / "node_modules",
        target_is_directory=True,
    )
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
    return dashboard


def run_verify_without_typecheck(dashboard: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "verify-dashboard-overlay"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "MEM0_DASHBOARD_OVERLAY_SKIP_TYPECHECK": "1"},
    )


def assert_dashboard_tsx_transpiles(source_path: Path) -> None:
    result = subprocess.run(
        [
            "node",
            "-e",
            """
const fs = require("fs");
const ts = require(process.argv[1]);
const source = fs.readFileSync(process.argv[2], "utf8");
const result = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.Preserve, target: ts.ScriptTarget.ESNext },
  fileName: process.argv[2],
  reportDiagnostics: true,
});
if (result.diagnostics?.length) {
  process.stderr.write(ts.formatDiagnosticsWithColorAndContext(result.diagnostics, {
    getCanonicalFileName: (fileName) => fileName,
    getCurrentDirectory: () => process.cwd(),
    getNewLine: () => "\\n",
  }));
  process.exit(1);
}
""",
            str(DASHBOARD_TYPESCRIPT),
            str(source_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def assert_unmasked_category_field_row_is_accepted(content: str) -> None:
    verifier = runpy.run_path(str(VERIFY_DASHBOARD_OVERLAY))
    component_body = verifier["extract_named_component_body"](
        content, "CategoryFieldEditor"
    )
    assert component_body is not None
    field_maps = list(
        re.finditer(
            r"\bfields\.map\(\s*\(\s*field\s*,\s*index\s*\)\s*=>\s*\{",
            component_body,
        )
    )
    assert len(field_maps) >= 2
    callback_start = field_maps[0].end() - 1
    callback_body = verifier["extract_balanced"](
        component_body, callback_start, "{", "}"
    )
    assert callback_body is not None
    return_match = re.search(r"\breturn\s*\(", callback_body)
    assert return_match is not None
    field_row = verifier["extract_balanced"](
        callback_body, return_match.end() - 1, "(", ")"
    )
    assert field_row is not None

    group_tags = [
        tag
        for tag in verifier["extract_jsx_component_tags"](field_row, "div")
        if re.search(r'\bkey\s*=\s*\{\s*field\.id\s*\}', tag) is not None
        and re.search(r'\brole\s*=\s*"group"', tag) is not None
    ]
    assert len(group_tags) == 1
    assert re.search(r"\baria-invalid(?:\s*=|(?=\s|/|>))", group_tags[0]) is None
    assert verifier["has_field_error_description"](group_tags[0])

    field_key_inputs = [
        tag
        for tag in verifier["extract_jsx_component_tags"](field_row, "Input")
        if re.search(r"\bid\s*=\s*\{\s*`\$\{field\.id\}-key`\s*\}", tag)
        is not None
    ]
    assert len(field_key_inputs) == 1
    assert re.search(
        r"\baria-invalid\s*=\s*\{\s*Boolean\(errors\[field\.id\]\)\s*\}",
        field_key_inputs[0],
    )
    assert verifier["has_field_error_description"](field_key_inputs[0])
    assert verifier["has_rendered_category_field_error"](field_row)


def write_verify_fixture(dashboard: Path) -> None:
    for relative in [
        "src/app/(root)/dashboard/categories/page.tsx",
        "src/app/(root)/dashboard/categories/category-field-editor.tsx",
        "src/app/(root)/dashboard/categories/category-editor-drawer.tsx",
        "src/app/(root)/dashboard/memories/page.tsx",
        "src/app/(root)/dashboard/memories/memories-page.tsx",
        "src/app/(root)/dashboard/memories/memory-categories.tsx",
        "src/app/(root)/dashboard/memories/memory-detail-drawer.tsx",
        "src/app/(root)/dashboard/requests/page.tsx",
        "src/app/(root)/dashboard/requests/request-trace-drawer.tsx",
        "src/app/(root)/dashboard/entities/page.tsx",
        "src/app/(root)/dashboard/export/page.tsx",
        "src/app/api/sidecar/config/route.ts",
        "src/app/api/sidecar/[...path]/route.ts",
        "src/utils/sidecar-project.ts",
        "src/utils/sidecar-api.ts",
        "src/utils/sidecar-proxy.ts",
        "src/utils/category-schema.ts",
        "src/utils/category-editor-state.ts",
        "src/utils/explorer-query-state.ts",
        "src/utils/memory-explorer-state.ts",
        "src/utils/request-trace-state.ts",
        "src/types/dashboard-explorer.ts",
        "src/types/sidecar.ts",
        "src/components/self-hosted/explorer/date-range-filter.tsx",
        "src/components/self-hosted/explorer/filter-builder.tsx",
        "src/components/self-hosted/explorer/entity-badges.tsx",
        "src/components/self-hosted/explorer/explorer-component-state.ts",
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
    assert "src/app/api/sidecar/config/route.ts" in manifest["files"]
    assert "src/app/api/sidecar/[...path]/route.ts" in manifest["files"]
    assert "src/utils/sidecar-project.ts" in manifest["files"]
    assert "src/utils/sidecar-proxy.ts" in manifest["files"]
    assert "src/utils/category-editor-state.ts" in manifest["files"]


def test_dashboard_overlay_readme_documents_memory_explorer_operations():
    content = (OVERLAY / "README.md").read_text()

    for contract in (
        "Memory Explorer",
        "/dashboard/memories?memoryId=",
        "SIDECAR_APP_ID",
        "results",
        "has_more",
        "stale_skipped",
        "5000",
        "scanned",
        "indexed",
        "skipped_unscoped",
        "skipped_other_scope",
        "stale_marked",
        "MEM0_SIDECAR_ALLOW_ADOPT_UNSCOPED",
        "one-project migration",
        "shared upstream store",
        "AUTH_DISABLED",
        "apply-dashboard-overlay",
        "Remove the overlay",
    ):
        assert contract in content


def test_dashboard_overlay_includes_category_schema_builder_contract():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    schema_path = "src/utils/category-schema.ts"

    assert schema_path in manifest["files"]

    dashboard = OVERLAY / "overlays"
    schema_content = (dashboard / schema_path).read_text()
    for symbol in (
        "export type CategoryFieldType",
        "export type CategoryField",
        "export function createEmptyField",
        "export function schemaToEditor",
        "export function editorToSchema",
        "export function validateCategoryFields",
        "export function countSchemaFields",
    ):
        assert symbol in schema_content
    assert 'mode: "advanced"' in schema_content
    assert 'format: "date"' in schema_content


def test_dashboard_overlay_includes_category_editor_state_contract():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    state_path = "src/utils/category-editor-state.ts"

    assert state_path in manifest["files"]

    state_content = (OVERLAY / "overlays" / state_path).read_text()
    for symbol in (
        "export type CategoryDraft",
        "export function createCategoryDraft",
        "export function categoryDraftFingerprint",
        "export function activateAdvancedMode",
        "export function planBuilderTransition",
        "export function resetToEmptyBuilder",
    ):
        assert symbol in state_content

    harness = OVERLAY / "scripts" / "test-category-editor-state.cjs"
    assert harness.exists()


def test_dashboard_overlay_includes_explorer_query_state_contract():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    type_path = "src/types/dashboard-explorer.ts"
    state_path = "src/utils/explorer-query-state.ts"

    assert type_path in manifest["files"]
    assert state_path in manifest["files"]

    type_content = (OVERLAY / "overlays" / type_path).read_text()
    for symbol in (
        "export type ExplorerMatch",
        "export type ExplorerField",
        "export type ExplorerOperator",
        "export type ExplorerFilter",
        "export type ExplorerDateRange",
        "export type ExplorerQueryPayload",
    ):
        assert symbol in type_content
    assert "project_id" not in type_content

    state_content = (OVERLAY / "overlays" / state_path).read_text()
    for symbol in (
        "export function createExplorerFilter",
        "export function normalizeExplorerFilters",
        "export function datePresetRange",
        "export function readExplorerUrlState",
        "export function writeExplorerUrlState",
    ):
        assert symbol in state_content

    harness = OVERLAY / "scripts" / "test-explorer-query-state.cjs"
    assert harness.exists()


def test_dashboard_overlay_includes_shared_explorer_components():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    component_paths = [
        "src/components/self-hosted/explorer/date-range-filter.tsx",
        "src/components/self-hosted/explorer/filter-builder.tsx",
        "src/components/self-hosted/explorer/entity-badges.tsx",
    ]

    for relative in component_paths:
        assert relative in manifest["files"]
        component = OVERLAY / "overlays" / relative
        assert component.is_file()
        assert_dashboard_tsx_transpiles(component)

    state_path = (
        "src/components/self-hosted/explorer/explorer-component-state.ts"
    )
    assert state_path in manifest["files"]
    assert (OVERLAY / "overlays" / state_path).is_file()
    assert (OVERLAY / "scripts/test-explorer-components.cjs").is_file()


def test_dashboard_overlay_includes_memory_explorer_page_and_drawer_contracts():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    page_path = "src/app/(root)/dashboard/memories/page.tsx"
    screen_path = "src/app/(root)/dashboard/memories/memories-page.tsx"
    drawer_path = (
        "src/app/(root)/dashboard/memories/memory-detail-drawer.tsx"
    )
    categories_path = (
        "src/app/(root)/dashboard/memories/memory-categories.tsx"
    )
    state_path = "src/utils/memory-explorer-state.ts"

    for relative in (
        page_path,
        screen_path,
        drawer_path,
        categories_path,
        state_path,
    ):
        assert relative in manifest["files"]
        source = OVERLAY / "overlays" / relative
        assert source.is_file()
        assert_dashboard_tsx_transpiles(source)

    page_content = (OVERLAY / "overlays" / screen_path).read_text()
    for contract in (
        "Time",
        "Entities",
        "Memory Content",
        "Categories",
        "Action",
        "DateRangeFilter",
        "FilterBuilder",
        "DataTable",
        "stale_skipped",
        "Loading memories",
        "No memories found",
        "Retry",
        "Refresh",
        "Pagination",
        "memoryId",
        "line-clamp-2",
        "md:hidden",
        "aria-disabled",
        "tabIndex",
        "normalizeMemoryId",
        "shouldShowMemoryPagination",
        "MemoryCategories",
    ):
        assert contract in page_content

    drawer_content = (OVERLAY / "overlays" / drawer_path).read_text()
    for contract in (
        "SheetTitle",
        "SheetDescription",
        "Details",
        "Source & Updates",
        "Memory content",
        "Metadata JSON",
        "Expiration",
        "Source unavailable",
        "AlertDialog",
        "DeleteConfirmationModal",
        "Copy ID",
        "overflow-x-hidden",
        "sm:max-w-2xl",
        "await navigator.clipboard.writeText",
        "Failed to copy memory ID",
        "beginMemoryOperation",
        "canApplyMemoryOperation",
        "mutationGeneration",
        "mountedRef",
        "activeMemoryIdRef",
        "SidecarMemoryUpdateResponse",
        "response.memory",
    ):
        assert contract in drawer_content

    assert "CategoriesDisplay" not in page_content
    categories_content = (OVERLAY / "overlays" / categories_path).read_text()
    for contract in (
        "Popover",
        "PopoverTrigger",
        "PopoverContent",
        "aria-expanded",
        "Show ${remainingCount} more categories",
        "event.stopPropagation()",
        "mobile",
    ):
        assert contract in categories_content

    for contract in (
        "return () =>",
        "requestGeneration.current = nextMemoryRequestGeneration",
    ):
        assert contract in page_content
    for contract in (
        "if (isBusy)",
        "onMemoryIdChange(activeMemoryId)",
        "detailGeneration.current = nextMemoryRequestGeneration",
        "historyGeneration.current = nextMemoryRequestGeneration",
        "mutationGeneration.current = nextMemoryRequestGeneration",
    ):
        assert contract in drawer_content

    assert (OVERLAY / "scripts/test-memory-explorer-state.cjs").is_file()


def test_dashboard_overlay_includes_request_trace_page_and_drawer_contracts():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    page_path = "src/app/(root)/dashboard/requests/page.tsx"
    drawer_path = (
        "src/app/(root)/dashboard/requests/request-trace-drawer.tsx"
    )
    state_path = "src/utils/request-trace-state.ts"

    for relative in (page_path, drawer_path, state_path):
        assert relative in manifest["files"]
        source = OVERLAY / "overlays" / relative
        assert source.is_file()
        assert_dashboard_tsx_transpiles(source)

    sidecar_api = (
        OVERLAY / "overlays/src/utils/sidecar-api.ts"
    ).read_text()
    assert "options: Pick<RequestInit, \"signal\"> = {}" in sidecar_api
    assert "signal: options.signal" in sidecar_api

    page_content = (OVERLAY / "overlays" / page_path).read_text()
    for contract in (
        "Overview",
        "ADD",
        "SEARCH",
        "GET ALL",
        "Has Results",
        'aria-label="Has results filter"',
        "setRequestTraceOperation(query, operation)",
        "toggleRequestTraceHasResults(query)",
        "DateRangeFilter",
        "FilterBuilder",
        'operators: ["equals"]',
        "allowAnyMatch={false}",
        "ResponsiveContainer",
        "BarChart",
        "Bar",
        "XAxis",
        "Tooltip",
        "Request timeline summary",
        "No request activity for this range",
        "Time",
        "Type",
        "Entities",
        "Event",
        "Latency",
        "Status",
        "Loading requests",
        "No requests found",
        "Could not load requests",
        "Pagination",
        "requestId",
        "operation",
        "sidecarQuery<SidecarTracePage>",
        '"/v1/events/query"',
        "signal: controller.signal",
        "AbortController",
        "controller.abort()",
        "requestGeneration",
        "isRefreshing",
        "setPageData(response)",
        "page: 1",
        "const canonicalSearch = canonicalParams.toString()",
        "if (canonicalSearch !== search)",
        "md:hidden",
        "aria-disabled",
        "<TraceEventButton",
        "onOpen={(opener) => openRequestTrace(row.id, opener)}",
        "event.stopPropagation()",
        "Open request ${trace.id}",
    ):
        assert contract in page_content
    assert "window." not in page_content
    assert "document." not in page_content
    assert re.search(
        r"pageData\.timeline\.length\s*>\s*0\s*\?\s*\(",
        page_content,
    )

    drawer_content = (OVERLAY / "overlays" / drawer_path).read_text()
    for contract in (
        "SheetTitle",
        "SheetDescription",
        "EntityBadges",
        'detailEntityId(detail, "user")',
        'detailEntityId(detail, "agent")',
        'detailEntityId(detail, "app")',
        'detailEntityId(detail, "run")',
        "Request Payload",
        "Retrieved Memories",
        "Copy ID",
        "Copy JSON",
        "Show more",
        "Show less",
        "Result count",
        "No memories retrieved",
        "result_previews.slice(0, 20)",
        "result_previews_omitted",
        "result_previews_scan_truncated",
        "Raw error",
        "Loading request details",
        "Could not load request details",
        "Retry details",
        "AbortController",
        "controller.abort()",
        "activeRequestIdRef",
        "generation: requestGeneration.current",
        "const targetId = activeRequestIdRef.current",
        "targetId,",
        "canApplyTraceDetailRequest(\n        copyTarget,",
        "requestGeneration",
        "encodeURIComponent",
        "sidecarGet<SidecarTrace>",
        "signal: controller.signal",
        "navigator.clipboard",
        "document.execCommand",
        "Failed to copy",
        "overflow-x-auto",
        "overflow-x-hidden",
        "sm:max-w-2xl",
    ):
        assert contract in drawer_content

    assert (OVERLAY / "scripts/test-request-trace-state.cjs").is_file()


def test_dashboard_overlay_includes_entity_explorer_contracts():
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    page_path = "src/app/(root)/dashboard/entities/page.tsx"

    assert page_path in manifest["files"]
    page = OVERLAY / "overlays" / page_path
    assert page.is_file()
    assert_dashboard_tsx_transpiles(page)

    content = page.read_text()
    for contract in (
        'label: "USER", value: "user"',
        'label: "RUN", value: "run"',
        'label: "AGENT", value: "agent"',
        'label: "APP", value: "app"',
        'entity_type: "user"',
        "DateRangeFilter",
        "FilterBuilder",
        "query.filters.length",
        "Refresh",
        "Entities",
        "Updated On",
        "Memories",
        "Action",
        "Loading entities",
        "No entities found",
        "Could not load entities",
        "Pagination",
        "EntityBadges",
        "md:hidden",
        "aria-disabled",
        "tabIndex",
        "sidecarQuery<SidecarEntityPage>",
        '"/v1/entities/query"',
        "signal: controller.signal",
        "new AbortController()",
        "controller.abort()",
        "listGeneration",
        "pageDataRef.current",
        "entityType",
        "page: 1",
        "AlertDialogTitle",
        "AlertDialogDescription",
        "projected memory count",
        "confirmationText === selectedEntity.entity_id",
        "case-sensitive",
        "encodeURIComponent(entity.type)",
        "encodeURIComponent(entity.entity_id)",
        "sidecarGet<SidecarEntity>",
        "detailGeneration",
        "deleteGeneration",
        'result.status === "SUCCEEDED"',
        'result.status === "PARTIAL"',
        'result.status === "FAILED"',
        "deleted_count",
        "failed_count",
        "sanitizeDisplayedError",
        "onCloseAutoFocus",
        "opener?.isConnected",
        "pageHeadingRef.current",
        "router.push",
        'field: ENTITY_MEMORY_FIELDS[entity.type]',
        'operator: "equals"',
        "new URLSearchParams()",
        "View memories for ${entity.entity_id}",
    ):
        assert contract in content

    assert "SESSION" not in content
    assert "project_id" not in content
    assert not re.search(
        r"setPageData\([^\n]*(filter|results)",
        content,
    ), "entity deletion must not optimistically remove rows"


def test_entity_explorer_review_regression_contracts():
    page = (
        OVERLAY
        / "overlays/src/app/(root)/dashboard/entities/page.tsx"
    ).read_text()
    badges = (
        OVERLAY
        / "overlays/src/components/self-hosted/explorer/entity-badges.tsx"
    ).read_text()
    state = (
        OVERLAY
        / "overlays/src/components/self-hosted/explorer/explorer-component-state.ts"
    ).read_text()

    assert "<EntityBadges" in page
    assert "function EntityIdentity" not in page
    assert "Tooltip" not in page
    assert "entity?:" in badges
    assert "tabIndex={0}" in badges
    assert "identity.value" in badges

    assert page.count("normalizeEntityExplorerFilters(") >= 3
    assert "invalidateEntityDetailForQueryTransition" in page
    assert "canApplyExplorerDetailRequest(" in page
    assert "setRowsAreAuthoritative(false)" in page
    assert "rowsAreAuthoritative" in page

    assert "{failure.id}" in page
    assert "break-all" in page
    assert "sanitizeDisplayedError(failure.id" not in page
    assert "sanitizeExplorerError" in state


def test_dashboard_overlay_verifier_runs_explorer_query_state_contracts():
    verifier = runpy.run_path(str(VERIFY_DASHBOARD_OVERLAY))
    dashboard = Path("/tmp/upstream-dashboard")

    assert verifier["explorer_query_state_harness_command"](
        OVERLAY,
        dashboard,
    )[-2:] == [
        str(OVERLAY / "scripts/test-explorer-query-state.cjs"),
        str(dashboard),
    ]
    source = VERIFY_DASHBOARD_OVERLAY.read_text()
    assert re.search(
        r"subprocess\.run\(\s*explorer_query_state_harness_command\(",
        source,
    )


def test_explorer_query_state_harness_verifies_applied_dashboard(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)
    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-explorer-query-state.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "explorer query state harness: 11 contracts passed" in result.stdout


def test_explorer_component_harness_runs_entity_regressions(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)
    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-explorer-components.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "explorer components harness: 8 contracts passed" in result.stdout


def test_entity_explorer_verifier_enforces_runtime_contracts(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/entities/page.tsx"
    content = page.read_text()
    assert '"/v1/entities/query"' in content
    page.write_text(content.replace(
        '"/v1/entities/query"',
        '"/v1/entities/missing"',
        1,
    ))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "Entities page must query /v1/entities/query" in result.stderr


@pytest.mark.parametrize(
    ("before", "after", "error"),
    [
        (
            "sidecarGet<SidecarEntity>",
            "sidecarGet<MissingSidecarEntity>",
            "Entities delete confirmation must refresh entity detail",
        ),
        (
            "confirmationText === selectedEntity.entity_id",
            "confirmationText.toLowerCase() === selectedEntity.entity_id.toLowerCase()",
            "Entities delete confirmation must require the exact case-sensitive ID",
        ),
        (
            'result.status === "PARTIAL"',
            'result.status === "MISSING_PARTIAL"',
            "Entities deletion must handle partial results",
        ),
        (
            "new URLSearchParams()",
            "new URLSearchParams(search)",
            "Entities memory drill-down must start from clean query state",
        ),
        (
            "onCloseAutoFocus",
            "onMissingCloseAutoFocus",
            "Entities delete dialog must restore keyboard focus",
        ),
    ],
)
def test_entity_explorer_verifier_rejects_missing_safety_contracts(
    tmp_path, before, after, error
):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/entities/page.tsx"
    content = page.read_text()
    assert before in content
    page.write_text(content.replace(before, after, 1))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert error in result.stderr


def test_request_trace_state_harness_executes_applied_target(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)

    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-request-trace-state.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "request trace state harness: 5 contract groups passed" in result.stdout


def test_request_trace_focus_restoration_contracts():
    page = (
        OVERLAY
        / "overlays/src/app/(root)/dashboard/requests/page.tsx"
    ).read_text()
    drawer = (
        OVERLAY
        / "overlays/src/app/(root)/dashboard/requests/request-trace-drawer.tsx"
    ).read_text()

    for contract in (
        "const pageHeadingRef = useRef<HTMLHeadingElement>(null)",
        "const requestOpenerRef = useRef<HTMLElement | null>(null)",
        "const openerRequestIdRef = useRef<string | null>(null)",
        "openerRequestIdRef.current !== requestId",
        "requestOpenerRef.current = null",
        "onOpen={(opener) => openRequestTrace(row.id, opener)}",
        "openRequestTrace(row.id, null)",
        "openRequestTrace(trace.id, event.currentTarget)",
        "const opener = requestOpenerRef.current",
        "opener?.isConnected",
        "pageHeadingRef.current",
        "if (!mountedRef.current)",
        "target?.isConnected",
        "target.focus()",
        "ref={pageHeadingRef}",
        "tabIndex={-1}",
        "onRestoreFocus={restoreRequestFocus}",
    ):
        assert contract in page

    for contract in (
        "onRestoreFocus: () => void",
        "onCloseAutoFocus={(event) =>",
        "event.preventDefault();",
        "onRestoreFocus();",
    ):
        assert contract in drawer


def test_dashboard_overlay_verifier_runs_request_trace_state_contracts():
    verifier = runpy.run_path(str(VERIFY_DASHBOARD_OVERLAY))
    dashboard = Path("/tmp/upstream-dashboard")

    assert verifier["request_trace_harness_command"](
        OVERLAY,
        dashboard,
    )[-2:] == [
        str(OVERLAY / "scripts/test-request-trace-state.cjs"),
        str(dashboard),
    ]
    source = VERIFY_DASHBOARD_OVERLAY.read_text()
    assert re.search(
        r"subprocess\.run\(\s*request_trace_harness_command\(",
        source,
    )


def test_request_trace_verifier_rejects_missing_runtime_wiring(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/requests/page.tsx"
    content = page.read_text()
    assert '"/v1/events/query"' in content
    page.write_text(content.replace(
        '"/v1/events/query"',
        '"/v1/events/missing"',
        1,
    ))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "Requests page must query /v1/events/query" in result.stderr


@pytest.mark.parametrize(
    ("relative", "before", "after", "error"),
    [
        (
            "src/app/(root)/dashboard/requests/page.tsx",
            "setRequestTraceOperation(query, operation)",
            "setRequestTraceOperationMissing(query, operation)",
            "Requests page must consume the independent operation reducer",
        ),
        (
            "src/app/(root)/dashboard/requests/page.tsx",
            "toggleRequestTraceHasResults(query)",
            "toggleRequestTraceHasResultsMissing(query)",
            "Requests page must consume the independent result reducer",
        ),
        (
            "src/app/(root)/dashboard/requests/page.tsx",
            "<TraceEventButton",
            "<MissingTraceEventButton",
            "Requests table must expose a keyboard-accessible row action",
        ),
        (
            "src/app/(root)/dashboard/requests/request-trace-drawer.tsx",
            "<EntityBadges",
            "<MissingEntityBadges",
            "Request drawer must render shared entity badges",
        ),
        (
            "src/app/(root)/dashboard/requests/page.tsx",
            "onRestoreFocus={restoreRequestFocus}",
            "onRestoreFocus={missingRestoreRequestFocus}",
            "Requests page must pass focus restoration to the drawer",
        ),
        (
            "src/app/(root)/dashboard/requests/request-trace-drawer.tsx",
            "onCloseAutoFocus={(event) =>",
            "onMissingCloseAutoFocus={(event) =>",
            "Request drawer must restore focus on close",
        ),
    ],
)
def test_request_trace_verifier_rejects_missing_review_contracts(
    tmp_path, relative, before, after, error
):
    dashboard = applied_overlay(tmp_path)
    target = dashboard / relative
    content = target.read_text()
    assert before in content
    target.write_text(content.replace(before, after, 1))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert error in result.stderr


def test_dashboard_overlay_verifier_runs_memory_explorer_runtime_contracts():
    verifier = runpy.run_path(str(VERIFY_DASHBOARD_OVERLAY))
    dashboard = Path("/tmp/upstream-dashboard")

    assert verifier["memory_explorer_harness_command"](
        OVERLAY,
        dashboard,
    )[-2:] == [
        str(OVERLAY / "scripts/test-memory-explorer-state.cjs"),
        str(dashboard),
    ]
    source = VERIFY_DASHBOARD_OVERLAY.read_text()
    assert re.search(
        r"subprocess\.run\(\s*memory_explorer_harness_command\(",
        source,
    )


def test_memory_explorer_mutations_consume_target_scoped_runtime_guards():
    verifier = runpy.run_path(str(VERIFY_DASHBOARD_OVERLAY))
    drawer = (
        OVERLAY
        / "overlays/src/app/(root)/dashboard/memories/memory-detail-drawer.tsx"
    ).read_text()

    for function_name in ("saveMemory", "deleteMemory"):
        body = verifier["extract_named_arrow_function_body"](
            drawer,
            function_name,
        )
        assert body is not None
        assert "beginMemoryOperation(" in body
        assert body.count("canApplyMemoryOperation(") >= 4
        assert "activeMemoryIdRef.current" in body
        assert "mountedRef.current" in body

    save_body = verifier["extract_named_arrow_function_body"](
        drawer,
        "saveMemory",
    )
    delete_body = verifier["extract_named_arrow_function_body"](
        drawer,
        "deleteMemory",
    )
    assert save_body is not None and "response.memory" in save_body
    assert delete_body is not None and "onDeleted(targetMemoryId)" in delete_body

    component = verifier["extract_named_component_body"](
        drawer,
        "MemoryDetailDrawer",
    )
    assert component is not None
    effects = []
    for match in re.finditer(r"\buseEffect\s*\(", component):
        arguments = verifier["extract_balanced"](
            component,
            component.find("(", match.start()),
            "(",
            ")",
        )
        assert arguments is not None
        effects.append(arguments)
    assert any(
        "if (isBusy)" in effect
        and "onMemoryIdChange(activeMemoryId)" in effect
        for effect in effects
    )
    cleanup = next(
        effect for effect in effects
        if "mountedRef.current = false" in effect
    )
    for generation in (
        "detailGeneration.current",
        "historyGeneration.current",
        "mutationGeneration.current",
    ):
        assert generation in cleanup


def test_memory_explorer_harness_verifies_the_applied_dashboard(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)
    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-memory-explorer-state.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "memory explorer state harness: 11 contracts passed" in result.stdout


def test_memory_explorer_harness_rejects_tampered_applied_source(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)
    state = dashboard / "src/utils/memory-explorer-state.ts"
    state.write_text(
        state.read_text().replace(
            "filters: query.filters.map(({ id: _id, ...filter }) => filter)",
            "filters: query.filters",
            1,
        )
    )
    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-memory-explorer-state.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "memory explorer state harness failed" in result.stderr


def test_dashboard_overlay_verifier_runs_explorer_component_contracts():
    verifier = runpy.run_path(str(VERIFY_DASHBOARD_OVERLAY))
    dashboard = Path("/tmp/upstream-dashboard")

    assert verifier["explorer_components_harness_command"](
        OVERLAY,
        dashboard,
    ) == [
        shutil.which("node"),
        str(OVERLAY / "scripts/test-explorer-components.cjs"),
        str(dashboard),
    ]
    verifier_source = VERIFY_DASHBOARD_OVERLAY.read_text()
    assert re.search(
        r"subprocess\.run\(\s*explorer_components_harness_command\(",
        verifier_source,
    )


def test_explorer_component_harness_verifies_the_applied_dashboard(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)
    state_path = (
        dashboard
        / "src/components/self-hosted/explorer/explorer-component-state.ts"
    )
    content = state_path.read_text()
    deterministic_format = (
        "return Number.isFinite(date.getTime()) "
        "? date.toISOString().slice(0, 10) : value;"
    )
    assert deterministic_format in content
    state_path.write_text(content.replace(deterministic_format, 'return "BROKEN";'))

    result = subprocess.run(
        [sys.executable, str(VERIFY_DASHBOARD_OVERLAY), str(dashboard)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "explorer components harness failed" in result.stderr


def test_explorer_component_harness_reports_missing_applied_source(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)
    missing = (
        dashboard
        / "src/components/self-hosted/explorer/date-range-filter.tsx"
    )
    missing.unlink()

    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-explorer-components.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"missing applied dashboard source: {missing}" in result.stderr


def test_date_range_filter_has_draft_utc_and_responsive_contracts():
    component = (
        OVERLAY
        / "overlays/src/components/self-hosted/explorer/date-range-filter.tsx"
    ).read_text()

    for import_path in (
        "@/components/ui/button",
        "@/components/ui/calendar",
        "@/components/ui/popover",
        "@/utils/explorer-query-state",
    ):
        assert f'from "{import_path}"' in component
    for label in ("All time", "Last 24 hours", "Last 7 days", "Last 30 days"):
        assert label in component
    assert re.search(r"datePresetRange\(preset\)", component)
    assert re.search(r'mode\s*=\s*"range"', component)
    assert re.search(
        r"numberOfMonths\s*=\s*\{\s*isDesktop\s*\?\s*2\s*:\s*1\s*\}",
        component,
    )
    assert 'window.matchMedia("(min-width: 768px)")' in component
    assert re.search(r"addEventListener\(\s*\"change\"", component)
    assert re.search(r"removeEventListener\(\s*\"change\"", component)
    assert re.search(r"setDraftRange\(isoRangeToCalendarRange\(value\)\)", component)
    assert re.search(r"calendarRangeToUtcRange\(draftRange\)", component)
    assert re.search(r"setOpen\(false\)", component)
    assert re.search(
        r"aria-label=\{`Choose date range: \$\{rangeLabel\}`\}",
        component,
    )
    cancel_button = (
        '<Button type="button" variant="ghost" onClick={() => setOpen(false)}>'
    )
    assert cancel_button in component
    assert re.search(r">\s*Cancel\s*</Button>", component)
    assert re.search(
        r'<Button[^>]*type="button"[^>]*>\s*Apply\s*</Button>',
        component,
        re.S,
    )


def test_filter_builder_uses_isolated_normalized_drafts_and_accessible_editors():
    component = (
        OVERLAY / "overlays/src/components/self-hosted/explorer/filter-builder.tsx"
    ).read_text()

    for import_path in (
        "@/components/ui/button",
        "@/components/ui/checkbox",
        "@/components/ui/input",
        "@/components/ui/popover",
        "@/components/ui/select",
        "@/utils/explorer-query-state",
    ):
        assert f'from "{import_path}"' in component
    assert re.search(r"openFilterBuilderDraft\(match,\s*filters\)", component)
    assert re.search(r"cancelFilterBuilderDraft\(current\)", component)
    assert re.search(
        r"onApply\(applied\.match,\s*applied\.filters\)",
        component,
    )
    assert re.search(r"onRemoveAll\(removed\.filters\)", component)
    assert re.search(
        r"changeExplorerFilterField\(\s*current,\s*value as ExplorerField",
        component,
    )
    assert re.search(r"aria-label=\{`Remove filter \$\{index \+ 1\}`\}", component)
    for label in (
        "Match all",
        "Match any",
        "Add filter",
        "Remove filters",
        "Metadata key",
        "Metadata value",
        "Comma-separated IDs",
        "Cancel",
        "Apply",
    ):
        assert label in component
    assert re.search(r"<Checkbox\b", component)
    assert re.search(r"<Input[^>]+aria-label=\"Metadata key\"", component, re.S)
    assert re.search(r"<Input[^>]+aria-label=\"Metadata value\"", component, re.S)


def test_entity_badges_render_only_present_accessible_identities():
    component = (
        OVERLAY / "overlays/src/components/self-hosted/explorer/entity-badges.tsx"
    ).read_text()

    state_component = (
        OVERLAY
        / "overlays/src/components/self-hosted/explorer/explorer-component-state.ts"
    ).read_text()
    for field, label in (
        ("user_id", "User"),
        ("agent_id", "Agent"),
        ("app_id", "App"),
        ("run_id", "Run"),
    ):
        assert f'field: "{field}"' in state_component
        assert f'label: "{label}"' in state_component
    assert "createEntityBadgeItems" in component
    assert "entityBadgeClickPayload(identity)" in component
    assert re.search(r"title=\{identity\.value\}", component)
    assert re.search(r"truncateIdentity\(identity\.value\)", component)
    assert re.search(r"onBadgeClick\s*\?\s*\(", component)
    assert re.search(r"<Button\b", component)
    assert re.search(r"<span\b", component)


def test_apply_dashboard_overlay_copies_files(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)
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


def test_apply_dashboard_overlay_rejects_non_dashboard_target(tmp_path):
    target = tmp_path / "not-dashboard"
    target.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(OVERLAY / "scripts" / "apply-dashboard-overlay"),
            str(target),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "does not look like a Mem0 dashboard checkout" in result.stderr
    assert not (target / "src").exists()


def test_apply_dashboard_overlay_copies_sidecar_proxy_and_client_exports(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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
    config_route_content = (
        dashboard / "src/app/api/sidecar/config/route.ts"
    ).read_text()
    helper_content = (dashboard / "src/utils/sidecar-api.ts").read_text()
    proxy_content = (dashboard / "src/utils/sidecar-proxy.ts").read_text()
    project_helper_content = (dashboard / "src/utils/sidecar-project.ts").read_text()
    type_content = (dashboard / "src/types/sidecar.ts").read_text()

    assert "export async function GET()" in config_route_content
    assert "SIDECAR_PROJECT_ID" in config_route_content
    assert "export const GET = proxy;" in route_content
    assert "export const POST = proxy;" in route_content
    assert "export async function sidecarGet<T>" in helper_content
    assert "export async function sidecarPut<T>" in helper_content
    assert "export async function sidecarPost<T>" in helper_content
    assert "export async function sidecarQuery<T>" in helper_content
    assert "body: object" in helper_content
    assert "export async function sidecarPatch<T>" in helper_content
    assert 'return sidecarRequest<T>("POST", path, body);' in helper_content
    assert 'return sidecarRequest<T>("PATCH", path, body);' in helper_content
    assert "return parseResponse<T>(response);" in helper_content
    assert "response.url" not in helper_content
    assert "export async function proxySidecarRequest(" in proxy_content
    assert "export async function getSidecarProjectId()" in project_helper_content
    assert 'fetch("/api/sidecar/config"' in project_helper_content
    assert "NEXT_PUBLIC_MEM0_SIDECAR_PROJECT_ID" not in project_helper_content
    assert "export type SidecarCategory =" in type_content
    assert "export type SidecarExportJob =" in type_content
    for symbol in (
        "export type SidecarMemory =",
        "export type SidecarMemoryQuery =",
        "export type SidecarMemoryPage =",
        "export type SidecarMemoryHistoryEntry =",
        "export type SidecarEntity =",
        "export type SidecarEntityQuery =",
        "export type SidecarEntityPage =",
        "export type SidecarEntityDeleteResult =",
    ):
        assert symbol in type_content

    memory_query = re.search(
        r"export type SidecarMemoryQuery\s*=\s*\{(?P<body>.*?)\n\};",
        type_content,
        re.S,
    )
    assert memory_query is not None
    assert "project_id" not in memory_query.group("body")
    assert "app_id" not in memory_query.group("body")
    assert 'filters: Array<Omit<ExplorerFilter, "id">>;' in memory_query.group(
        "body"
    )

    for field in (
        "id: string;",
        "memory: string | null;",
        "metadata: Record<string, unknown>;",
        "categories: string[];",
        "user_id: string | null;",
        "agent_id: string | null;",
        "app_id: string | null;",
        "run_id: string | null;",
        "created_at: string | null;",
        "updated_at: string | null;",
        "expiration_date: string | null;",
    ):
        assert field in type_content

    for field in (
        "results: SidecarMemory[];",
        "page: number;",
        "page_size: number;",
        "total: number;",
        "has_more: boolean;",
        "stale_skipped: number;",
    ):
        assert field in type_content


def test_dashboard_overlay_includes_exact_entity_types():
    type_content = (OVERLAY / "overlays/src/types/sidecar.ts").read_text()

    entity = re.search(
        r"export type SidecarEntity\s*=\s*\{(?P<body>.*?)\n\};",
        type_content,
        re.S,
    )
    assert entity is not None
    compact_entity = re.sub(r"\s+", "", entity.group("body"))
    for field in (
        "id: string;",
        'type: "user" | "agent" | "app" | "run";',
        "entity_id: string;",
        "display_name: string | null;",
        "memory_count: number;",
        "last_seen_at: string | null;",
        "updated_at: string | null;",
    ):
        assert re.sub(r"\s+", "", field) in compact_entity

    query = re.search(
        r"export type SidecarEntityQuery\s*=\s*\{(?P<body>.*?)\n\};",
        type_content,
        re.S,
    )
    assert query is not None
    assert re.search(r"^\s*project_id\s*:", query.group("body"), re.M) is None
    assert re.search(r"^\s*app_id\s*:", query.group("body"), re.M) is None
    compact_query = re.sub(r"\s+", "", query.group("body"))
    for field in (
        'entity_type: "user" | "agent" | "app" | "run";',
        "match: ExplorerMatch;",
        'filters: Array<Omit<ExplorerFilter, "id">>;',
        "date_range: ExplorerDateRange;",
        "page: number;",
        "page_size: number;",
    ):
        assert re.sub(r"\s+", "", field) in compact_query

    page = re.search(
        r"export type SidecarEntityPage\s*=\s*\{(?P<body>.*?)\n\};",
        type_content,
        re.S,
    )
    assert page is not None
    for field in (
        "results: SidecarEntity[];",
        "page: number;",
        "page_size: number;",
        "total: number;",
        "has_more: boolean;",
    ):
        assert field in page.group("body")

    delete_result = re.search(
        r"export type SidecarEntityDeleteResult\s*=\s*\{(?P<body>.*?)\n\};",
        type_content,
        re.S,
    )
    assert delete_result is not None
    compact_delete = re.sub(r"\s+", "", delete_result.group("body"))
    for field in (
        'status: "SUCCEEDED" | "PARTIAL" | "FAILED";',
        "requested_count: number;",
        "deleted_count: number;",
        "failed_count: number;",
        "failed: Array<{ id: string; error: Record<string, unknown> }>;",
        "event_id: string;",
    ):
        assert re.sub(r"\s+", "", field) in compact_delete


def test_dashboard_overlay_includes_exact_request_trace_types():
    type_content = (
        OVERLAY / "overlays/src/types/sidecar.ts"
    ).read_text()

    for symbol in (
        "export type SidecarTrace =",
        "export type SidecarTraceQuery =",
        "export type SidecarTracePage =",
        "export type SidecarTraceTimelineBucket =",
    ):
        assert symbol in type_content

    trace = re.search(
        r"export type SidecarTrace\s*=\s*\{(?P<body>.*?)\n\};",
        type_content,
        re.S,
    )
    assert trace is not None
    compact_trace = re.sub(r"\s+", "", trace.group("body"))
    for field in (
        "id: string;",
        "correlation_id: string | null;",
        "operation: string;",
        'display_operation: | "ADD" | "SEARCH" | "GET ALL" | "UPDATE" | '
        '"DELETE" | "OTHER";',
        'status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";',
        'entities: Array<{ type: "user" | "agent" | "app" | "run"; id: string }>;',
        "request: Record<string, unknown>;",
        "response: Record<string, unknown>;",
        "error: Record<string, unknown>;",
        "result_count: number;",
        "has_results: boolean;",
        "latency_ms: number | null;",
        "requested_at: string | null;",
        "completed_at: string | null;",
        "result_previews: Array<Record<string, unknown>>;",
        "result_previews_omitted: number;",
        "result_previews_scan_truncated: boolean;",
    ):
        assert re.sub(r"\s+", "", field) in compact_trace

    query = re.search(
        r"export type SidecarTraceQuery\s*=\s*\{(?P<body>.*?)\n\};",
        type_content,
        re.S,
    )
    assert query is not None
    assert re.search(r"^\s*project_id\s*:", query.group("body"), re.M) is None
    assert re.search(r"^\s*app_id\s*:", query.group("body"), re.M) is None
    compact_query = re.sub(r"\s+", "", query.group("body"))
    for field in (
        'operation: "ADD" | "SEARCH" | "GET_ALL" | null;',
        'statuses: Array<"PENDING" | "RUNNING" | "SUCCEEDED" | '
        '"FAILED" | "CANCELLED">;',
        "has_results: boolean | null;",
        "date_range: ExplorerDateRange;",
        'entity_filters: Partial<Record<"user_id" | "agent_id" | '
        '"app_id" | "run_id", string>>;',
        "page: number;",
        "page_size: number;",
    ):
        assert re.sub(r"\s+", "", field) in compact_query

    for field in (
        "timestamp: string;",
        "count: number;",
        "results: SidecarTrace[];",
        "total: number;",
        "page: number;",
        "page_size: number;",
        "has_more: boolean;",
        "timeline: SidecarTraceTimelineBucket[];",
    ):
        assert field in type_content


def test_sidecar_proxy_harness_executes_the_applied_target(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)

    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-sidecar-proxy.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "sidecar proxy request harness: 40 contracts passed" in result.stdout


def test_sidecar_proxy_harness_rejects_stale_applied_target(tmp_path):
    dashboard = applied_upstream_overlay(tmp_path)
    proxy = dashboard / "src/utils/sidecar-proxy.ts"
    proxy.write_text(
        proxy.read_text().replace(
            'return jsonError("Sidecar route is not allowed", 403);',
            'return jsonError("BROKEN", 403);',
        )
    )

    result = subprocess.run(
        [
            "node",
            str(OVERLAY / "scripts/test-sidecar-proxy.cjs"),
            str(dashboard),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "BROKEN" in result.stderr


def test_apply_dashboard_overlay_normalizes_sidecar_paths_and_removes_patch_footgun(
    tmp_path,
):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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
    proxy_content = (dashboard / "src/utils/sidecar-proxy.ts").read_text()

    assert "function normalizeSidecarPath(path: string): string" in helper_content
    assert "return path.startsWith(\"/\") ? path : `/${path}`;" in helper_content
    assert "export async function sidecarPatch<T>" in helper_content
    assert "export async function sidecarDelete(" in helper_content
    assert (
        'const METHODS_WITH_BODY = new Set(["POST", "PUT", "PATCH"]);'
        in proxy_content
    )
    assert "export const PATCH = proxy;" in route_content
    assert "export const DELETE = proxy;" in route_content


def test_apply_dashboard_overlay_route_restricts_sidecar_paths(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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
    proxy_content = (dashboard / "src/utils/sidecar-proxy.ts").read_text()

    assert "function isAllowedSidecarRequest(" in proxy_content
    assert "function getConfiguredProjectId()" in route_content
    assert "function getConfiguredAppId()" in route_content
    assert "process.env.SIDECAR_APP_ID?.trim()" in route_content
    assert "configuredAppId: getConfiguredAppId()" in route_content
    assert "function scopedSidecarPath(" in proxy_content
    assert "function scopedJsonBody(" in proxy_content
    assert 'key !== "project_id"' in proxy_content
    assert 'url.searchParams.set("project_id", configuredProjectId);' in proxy_content
    assert "scopedPayload.project_id = configuredProjectId" in proxy_content
    assert 'return jsonError("Sidecar route is not allowed", 403);' in proxy_content
    assert "isProjectCategoriesPath" in proxy_content
    assert "isProjectCategoryItemPath" in proxy_content
    assert "categoryItemMatch" in proxy_content
    assert "isExportPath" in proxy_content
    assert "isMemoryQueryPath" in proxy_content
    assert "isMemoryItemPath" in proxy_content
    assert "isMemoryHistoryPath" in proxy_content
    assert "scopedPayload.project_id = configuredProjectId" in proxy_content
    assert "scopedPayload.app_id = configuredAppId" in proxy_content
    assert (
        'method === "DELETE" && /^\\/v1\\/exports\\/[^/]+$/.test(path)'
        not in proxy_content
    )


def test_apply_dashboard_overlay_route_handles_proxy_errors_explicitly(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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

    proxy_content = (dashboard / "src/utils/sidecar-proxy.ts").read_text()

    assert (
        "function jsonError(message: string, status: number): Response"
        in proxy_content
    )
    assert (
        'return jsonError("SIDECAR_INTERNAL_API_URL is not configured", 500);'
        in proxy_content
    )
    assert 'return jsonError("Sidecar upstream request failed", 502);' in proxy_content


def test_apply_dashboard_overlay_route_validates_dashboard_session(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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
    proxy_content = (dashboard / "src/utils/sidecar-proxy.ts").read_text()

    assert 'const COOKIE_NAME = "mem0_refresh_token";' in route_content
    assert "async function validateDashboardSession()" in route_content
    assert "function isAuthDisabled()" in route_content
    assert 'process.env.AUTH_DISABLED?.toLowerCase()' in route_content
    assert "if (isAuthDisabled()) {" in route_content
    assert 'return jsonError("Unauthorized", 401);' in proxy_content
    assert "AUTH_ENDPOINTS.REFRESH" in route_content
    assert "getServerApiUrl()" in route_content


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


def test_verify_rejects_categories_outside_memory_tools(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    memory_start = content.index("const MEMORY_TOOL_ITEMS")
    cloud_start = content.index("const CLOUD_FEATURE_ITEMS")
    memory_items = content[memory_start:cloud_start].replace(
        'title: "Categories"', 'title: "Webhooks"', 1
    )
    cloud_items = content[cloud_start:].replace(
        'title: "Webhooks"', 'title: "Categories"', 1
    )
    nav.write_text(content[:memory_start] + memory_items + cloud_items)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "MEMORY_TOOL_ITEMS" in result.stderr


def test_verify_requires_responsive_sidebar_collapse(tmp_path):
    dashboard = applied_overlay(tmp_path)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 0, result.stderr


def test_verify_rejects_category_field_editor_group_aria_invalid(tmp_path):
    dashboard = applied_overlay(tmp_path)
    field_editor = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-field-editor.tsx"
    )
    content = field_editor.read_text()
    group_invalid_attribute = "            aria-invalid={Boolean(errors[field.id])}\n"

    assert '            role="group"\n' + group_invalid_attribute not in content

    field_editor.write_text(content.replace(
        '            role="group"\n',
        '            role="group"\n' + group_invalid_attribute,
        1,
    ))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "Category field editor group must not use aria-invalid" in result.stderr


def test_verify_rejects_category_field_editor_input_without_aria_invalid(tmp_path):
    dashboard = applied_overlay(tmp_path)
    field_editor = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-field-editor.tsx"
    )
    input_invalid_attribute = (
        "                  aria-invalid={Boolean(errors[field.id])}\n"
    )
    content = field_editor.read_text()

    assert input_invalid_attribute in content
    field_editor.write_text(content.replace(input_invalid_attribute, "", 1))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert (
        "Category field editor Field key Input must use aria-invalid" in result.stderr
    )


@pytest.mark.parametrize(
    "replacement",
    (
        "",
        "                  aria-describedby={errors[field.id] ? "
        "`${errorId}-mismatch` : undefined}\n",
    ),
)
def test_verify_rejects_category_field_editor_input_without_matching_error_description(
    tmp_path,
    replacement,
):
    dashboard = applied_overlay(tmp_path)
    field_editor = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-field-editor.tsx"
    )
    input_description = (
        "                  aria-describedby={errors[field.id] ? errorId : undefined}\n"
    )
    content = field_editor.read_text()

    assert input_description in content
    field_editor.write_text(content.replace(input_description, replacement, 1))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert (
        "Category field editor Field key Input must describe its field error"
        in result.stderr
    )


def test_verify_rejects_category_field_editor_aria_decoy_outside_fields_map(tmp_path):
    dashboard = applied_overlay(tmp_path)
    field_editor = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-field-editor.tsx"
    )
    content = field_editor.read_text()
    input_id = "                  id={`${field.id}-key`}\n"
    input_invalid_attribute = (
        "                  aria-invalid={Boolean(errors[field.id])}\n"
    )
    input_description = (
        "                  aria-describedby={errors[field.id] ? errorId : undefined}\n"
    )

    assert input_id in content
    assert input_invalid_attribute in content
    assert input_description in content
    field_editor.write_text(
        content.replace(input_id, '                  id="decoy-field-key"\n', 1)
        .replace(input_invalid_attribute, "", 1)
        .replace(input_description, "", 1)
        + """

const CATEGORY_FIELD_EDITOR_ACCESSIBILITY_DECOY = `
  <Input
    id={`${field.id}-key`}
    aria-invalid={Boolean(errors[field.id])}
    aria-describedby={errors[field.id] ? errorId : undefined}
  />
`;
"""
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "Category field editor Field key Input is missing" in result.stderr


def test_verify_rejects_category_field_editor_template_string_map_decoy(tmp_path):
    dashboard = applied_overlay(tmp_path)
    field_editor = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-field-editor.tsx"
    )
    content = field_editor.read_text()
    input_invalid_attribute = (
        "                  aria-invalid={Boolean(errors[field.id])}\n"
    )
    category_field_editor_return = '  return (\n    <div className="space-y-3">\n'
    map_decoy = (
        '  const categoryFieldEditorMapDecoy = \'{fields.map((field, index) => {'
        ' const errorId = field.id; return (<div key={field.id} role="group"'
        ' aria-describedby={errors[field.id] ? errorId : undefined}><Input'
        ' id={`${field.id}-key`} aria-invalid={Boolean(errors[field.id])}'
        ' aria-describedby={errors[field.id] ? errorId : undefined}/>'
        '{errors[field.id] ? (<p id={errorId}>{errors[field.id]}</p>) : null}'
        '</div>); })}\';\n\n'
    )

    assert input_invalid_attribute in content
    assert category_field_editor_return in content
    mutated_content = (
        content.replace(input_invalid_attribute, "", 1).replace(
            category_field_editor_return,
            map_decoy + category_field_editor_return,
            1,
        )
    )
    first_map = mutated_content.index("fields.map((field, index) => {")
    second_map = mutated_content.index("fields.map((field, index) => {", first_map + 1)
    assert first_map < second_map
    field_editor.write_text(mutated_content)
    assert_dashboard_tsx_transpiles(field_editor)
    assert_unmasked_category_field_row_is_accepted(mutated_content)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert (
        "Category field editor Field key Input must use aria-invalid" in result.stderr
    )


def test_verify_accepts_escaped_quote_string_inside_main_nav(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    effect_start = "  React.useEffect(() => {"
    escaped_quote = '  const escapedQuote = "value: \\"}";\n'
    assert effect_start in content
    nav.write_text(content.replace(effect_start, escaped_quote + effect_start, 1))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 0, result.stderr


RESPONSIVE_SIDEBAR_DECOY = """
function ResponsiveSidebarDecoy() {
  React.useEffect(() => {
    const sidebarMediaQuery = window.matchMedia("(max-width: 767px)");
    const collapseSidebarOnNarrowViewport = () => {
      if (
        sidebarMediaQuery.matches &&
        !store.getState().layout.isSidebarCollapsed
      ) {
        dispatch(toggleSidebar());
      }
    };
    collapseSidebarOnNarrowViewport();
    sidebarMediaQuery.addEventListener("change", collapseSidebarOnNarrowViewport);
    return () => {
      sidebarMediaQuery.removeEventListener(
        "change",
        collapseSidebarOnNarrowViewport,
      );
    };
  }, [dispatch, store]);
}
"""


def test_verify_rejects_responsive_effect_decoy_outside_main_nav(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    assert '  React.useEffect(() => {' in content
    nav.write_text(
        content.replace('  React.useEffect(() => {', '  React.useMemo(() => {', 1)
        + RESPONSIVE_SIDEBAR_DECOY
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "MainNav" in result.stderr


def test_verify_rejects_missing_live_responsive_collapse_invocation(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    active_invocation = "    collapseSidebarOnNarrowViewport();\n"
    assert active_invocation in content
    nav.write_text(content.replace(active_invocation, "", 1) + RESPONSIVE_SIDEBAR_DECOY)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "invoke" in result.stderr


def test_verify_rejects_missing_live_responsive_change_listener(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    active_listener = (
        '    sidebarMediaQuery.addEventListener("change", '
        "collapseSidebarOnNarrowViewport);\n"
    )
    assert active_listener in content
    nav.write_text(content.replace(active_listener, "", 1) + RESPONSIVE_SIDEBAR_DECOY)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "listener" in result.stderr


def test_verify_rejects_json_first_categories_regression(tmp_path):
    dashboard = applied_overlay(tmp_path)
    drawer = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-editor-drawer.tsx"
    )
    drawer.write_text(drawer.read_text().replace("Generated schema", "Schema JSON", 1))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "Schema JSON" in result.stderr


def test_verify_rejects_missing_category_patch_proxy(tmp_path):
    dashboard = applied_overlay(tmp_path)
    route = dashboard / "src/app/api/sidecar/[...path]/route.ts"
    route.write_text(route.read_text().replace("export const PATCH = proxy;", ""))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "PATCH" in result.stderr


def test_verify_rejects_categories_table_without_mobile_fixed_layout(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/categories/page.tsx"
    content = page.read_text().replace(
        '<Table className="table-fixed sm:table-auto">', "<Table>", 1
    )
    page.write_text(
        (
            'const mobileTableDecoy = '
            '"<Table className=\\"table-fixed sm:table-auto\\">";\n'
        )
        + content
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "fixed mobile table layout" in result.stderr


def test_verify_rejects_categories_table_without_mobile_wrapping_and_status(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/categories/page.tsx"
    content = page.read_text().replace(
        "<Table>", '<Table className="table-fixed sm:table-auto">', 1
    )
    content = content.replace('className="break-words"', 'className=""', 1)
    content = content.replace(
        (
            'className="whitespace-normal break-words text-xs '
            'text-onSurface-default-secondary sm:max-w-xl sm:truncate"'
        ),
        'className="max-w-xl truncate text-xs text-onSurface-default-secondary"',
        1,
    )
    content = content.replace('className="sm:hidden"', 'className="hidden"', 1)
    content = content.replace(
        '<TableHead className="hidden sm:table-cell">Status</TableHead>',
        "<TableHead>Status</TableHead>",
        1,
    )
    content = content.replace(
        '<TableCell className="hidden sm:table-cell">\n                      <span',
        '<TableCell>\n                      <span',
        1,
    )
    page.write_text(
        (
            'const mobileCategoryDecoy = '
            '"break-words whitespace-normal sm:hidden hidden sm:table-cell";\n'
        )
        + content
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "mobile Category cell" in result.stderr


@pytest.mark.parametrize(
    ("format_name", "replacement"),
    [
        ("csv", ""),
        ("csv", "disabled={false}"),
        ("csv", "disabled={futureFormatsDisabled}"),
        ("csv", "data-disabled"),
        ("pydantic", ""),
        ("pydantic", "disabled={false}"),
        ("pydantic", "disabled={futureFormatsDisabled}"),
        ("pydantic", "data-disabled"),
    ],
)
def test_verify_rejects_invalid_future_format_disabled_attribute(
    tmp_path, format_name, replacement
):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/export/page.tsx"
    page.write_text(
        page.read_text().replace(
            f'value="{format_name}" id="export-format-{format_name}" disabled',
            f'value="{format_name}" id="export-format-{format_name}" {replacement}',
        )
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert format_name.upper() in result.stderr


@pytest.mark.parametrize(
    ("required", "weakened"),
    [
        (
            '<div className="w-full min-w-0 space-y-6">',
            '<div className="w-full space-y-6">',
        ),
        (
            '<div className="flex min-w-0 flex-col items-start gap-4 '
            'sm:flex-row sm:justify-between">',
            '<div className="flex items-start justify-between gap-4">',
        ),
        (
            '<p className="break-words text-sm text-onSurface-default-secondary">\n'
            '            Export scoped memories from project {projectId ?? "..."}.',
            '<p className="text-sm text-onSurface-default-secondary">\n'
            '            Export scoped memories from project {projectId ?? "..."}.',
        ),
        (
            '<p className="break-all font-mono text-sm sm:truncate" '
            'title={projectId ?? undefined}>',
            '<p className="truncate font-mono text-sm" title={projectId ?? undefined}>',
        ),
        (
            '<Card className="w-full min-w-0 border-memBorder-primary">',
            '<Card className="border-memBorder-primary">',
        ),
        (
            '<CardContent className="w-full min-w-0 space-y-5 p-5">',
            '<CardContent className="space-y-5 p-5">',
        ),
        (
            '<div className="w-full min-w-0 space-y-1 sm:w-auto sm:min-w-44">',
            '<div className="min-w-44 space-y-1">',
        ),
        (
            '<fieldset className="w-full min-w-0 space-y-3">',
            '<fieldset className="space-y-3">',
        ),
        (
            'className="grid w-full min-w-0 gap-2 sm:grid-cols-3"',
            'className="grid gap-2 sm:grid-cols-3"',
        ),
        (
            'className="flex min-h-16 w-full min-w-0 cursor-pointer '
            'items-center gap-3 rounded-md border border-memBorder-primary '
            'px-3 py-2"',
            'className="flex min-h-16 cursor-pointer items-center gap-3 '
            'rounded-md border border-memBorder-primary px-3 py-2"',
        ),
        (
            '<span className="min-w-0 space-y-0.5">',
            '<span className="space-y-0.5">',
        ),
        (
            'className="grid w-full min-w-0 gap-4 sm:grid-cols-2 xl:grid-cols-4"',
            'className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"',
        ),
        (
            '<div className="min-w-0 space-y-2">\n'
            '              <Label htmlFor="export-app-id">',
            '<div className="space-y-2">\n'
            '              <Label htmlFor="export-app-id">',
        ),
        (
            '<Input className="w-full min-w-0" id="export-app-id"',
            '<Input id="export-app-id"',
        ),
        (
            '<section className="w-full min-w-0 space-y-3" '
            'aria-labelledby="recent-export-jobs">',
            '<section className="space-y-3" '
            'aria-labelledby="recent-export-jobs">',
        ),
        (
            '<p className="break-all text-xs '
            'text-onSurface-default-tertiary">{loadError}</p>',
            '<p className="text-xs '
            'text-onSurface-default-tertiary">{loadError}</p>',
        ),
        (
            '<CardContent className="grid w-full min-w-0 gap-4 p-4 '
            'lg:grid-cols-[minmax(0,1fr)_auto]">',
            '<CardContent className="grid gap-4 p-4 '
            'lg:grid-cols-[minmax(0,1fr)_auto]">',
        ),
        (
            '<span className="min-w-0 break-all font-mono text-xs" '
            'title={job.id}>{job.id}</span>',
            '<span className="truncate font-mono text-xs" '
            'title={job.id}>{job.id}</span>',
        ),
        (
            '<Badge className="max-w-full whitespace-normal break-all '
            'text-left" key={filter} variant="outline">{filter}</Badge>',
            '<Badge key={filter} variant="outline">{filter}</Badge>',
        ),
        (
            '<dl className="grid min-w-0 gap-x-5 gap-y-2 text-sm '
            'sm:grid-cols-2 xl:grid-cols-4">',
            '<dl className="grid gap-x-5 gap-y-2 text-sm '
            'sm:grid-cols-2 xl:grid-cols-4">',
        ),
        (
            '<dd className="break-words">{formatTime(job.created_at)}</dd>',
            '<dd>{formatTime(job.created_at)}</dd>',
        ),
        (
            '<dd className="break-words">{formatTime(job.completed_at)}</dd>',
            '<dd>{formatTime(job.completed_at)}</dd>',
        ),
        (
            '<p className="break-all text-sm '
            'text-onSurface-danger-primary">{errorSummary}</p>',
            '<p className="text-sm text-onSurface-danger-primary">{errorSummary}</p>',
        ),
        (
            'className="w-full sm:w-auto lg:self-start"\n'
            '          disabled={job.status !== "SUCCEEDED"}',
            'disabled={job.status !== "SUCCEEDED"}',
        ),
    ],
)
def test_verify_rejects_removed_mobile_export_layout_protection(
    tmp_path, required, weakened
):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/export/page.tsx"
    content = page.read_text()
    assert required in content
    content = content.replace(required, weakened, 1)
    content = content.replace(
        '"use client";',
        '"use client";\n\n'
        'const mobileExportLayoutDecoy = '
        '"w-full min-w-0 break-words break-all whitespace-normal sm:truncate";',
        1,
    )
    page.write_text(content)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "mobile Export" in result.stderr


def test_verify_rejects_export_input_wrapper_sibling_class_decoy(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/export/page.tsx"
    content = page.read_text()
    content = content.replace(
        '<div className="min-w-0 space-y-2">\n'
        '              <Label htmlFor="export-app-id">',
        '<div className="space-y-2">\n'
        '              <Label htmlFor="export-app-id">',
        1,
    )
    content = content.replace(
        '<Input className="w-full min-w-0" id="export-app-id"',
        '<div className="min-w-0" />\n'
        '              <Input className="w-full min-w-0" id="export-app-id"',
        1,
    )
    page.write_text(content)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "app input grid child" in result.stderr


def test_verify_accepts_export_input_wrapper_string_expression_decoy(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/export/page.tsx"
    content = page.read_text().replace(
        '<Input className="w-full min-w-0" id="export-app-id"',
        '{"<div>"}\n'
        '              <Input className="w-full min-w-0" id="export-app-id"',
        1,
    )
    page.write_text(content)
    assert_dashboard_tsx_transpiles(page)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 0, result.stderr


def test_verify_rejects_export_format_label_descendant_class_decoy(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/export/page.tsx"
    content = page.read_text()
    content = content.replace(
        '                className="flex min-h-16 w-full min-w-0 '
        'cursor-pointer items-center gap-3 rounded-md border '
        'border-memBorder-primary px-3 py-2"\n',
        "",
        1,
    )
    content = content.replace(
        '<span className="min-w-0 space-y-0.5">',
        '<span className="w-full min-w-0 space-y-0.5">',
        1,
    )
    page.write_text(content)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "JSON option must allow shrinking" in result.stderr


def test_verify_accepts_mobile_export_timestamps_with_word_wrapping(tmp_path):
    dashboard = applied_overlay(tmp_path)
    page = dashboard / "src/app/(root)/dashboard/export/page.tsx"
    content = page.read_text()
    assert '<dd className="break-words">{formatTime(job.created_at)}</dd>' in content
    assert '<dd className="break-words">{formatTime(job.completed_at)}</dd>' in content
    assert '<dd className="break-all">{formatTime(' not in content
    page.write_text(content)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 0, result.stderr


def test_verify_rejects_self_hosted_export_badge(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    nav.write_text(
        nav.read_text().replace(
            'title: "Export",',
            'title: "Export",\n                      badge: "SELF-HOSTED",',
            1,
        )
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "SELF-HOSTED" in result.stderr


def test_verify_rejects_badge_before_memory_tool_title(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    assert "const MEMORY_TOOL_ITEMS" in content
    nav.write_text(
        content.replace(
            '  {\n    title: "Categories",',
            '  {\n    badge: "SELF-HOSTED",\n    title: "Categories",',
            1,
        )
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "SELF-HOSTED" in result.stderr


def test_verify_ignores_comment_decoy_group_labels(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    nav.write_text(
        "// MEMORY TOOLS\n// CLOUD FEATURES\n// ACCOUNT\n" + nav.read_text()
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 0, result.stderr


def test_verify_rejects_swapped_rendered_navigation_item_bindings(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    assert "<NavigationItems" in content
    assert 'group="memory-tools"' in content
    assert 'items={MEMORY_TOOL_ITEMS}' in content
    assert 'group="cloud-features"' in content
    assert 'items={CLOUD_FEATURE_ITEMS}' in content
    swapped = content.replace("items={MEMORY_TOOL_ITEMS}", "items={TEMP_ITEMS}", 1)
    swapped = swapped.replace(
        "items={CLOUD_FEATURE_ITEMS}", "items={MEMORY_TOOL_ITEMS}", 1
    )
    nav.write_text(
        swapped.replace("items={TEMP_ITEMS}", "items={CLOUD_FEATURE_ITEMS}", 1)
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "memory-tools" in result.stderr


def test_verify_rejects_missing_webhooks_pro_badge(tmp_path):
    dashboard = applied_overlay(tmp_path)
    nav = dashboard / "src/app/(root)/dashboard/components/main-nav.tsx"
    content = nav.read_text()
    webhooks_start = content.index('title: "Webhooks"')
    analytics_start = content.index('title: "Analytics"', webhooks_start)
    webhooks_section = content[webhooks_start:analytics_start].replace(
        'badge: "PRO",', "", 1
    )
    nav.write_text(
        content[:webhooks_start] + webhooks_section + content[analytics_start:]
    )

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "Webhooks" in result.stderr


def test_verify_rejects_missing_category_delete_route_export(tmp_path):
    dashboard = applied_overlay(tmp_path)
    route = dashboard / "src/app/api/sidecar/[...path]/route.ts"
    route.write_text(route.read_text().replace("export const DELETE = proxy;", ""))

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert "DELETE" in result.stderr


@pytest.mark.parametrize("method", ("PATCH", "DELETE"))
def test_verify_rejects_missing_category_item_method_branch(tmp_path, method):
    dashboard = applied_overlay(tmp_path)
    proxy = dashboard / "src/utils/sidecar-proxy.ts"
    content = proxy.read_text()
    function_start = content.index("function isProjectCategoryItemPath")
    function_end = content.index("\n}\n", function_start) + 2
    item_function = content[function_start:function_end].replace(
        f'method === "{method}"', f'method === "{method}-MISSING"', 1
    )
    proxy.write_text(content[:function_start] + item_function + content[function_end:])

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert method in result.stderr


def test_verify_dashboard_overlay_runs_typecheck_when_unlocked(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)
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
    node_log = dashboard / "node.log"
    node = dashboard / "node"
    node.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {node_log}\n"
        "exit 0\n"
    )
    node.chmod(0o755)

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
    harness_args = node_log.read_text().splitlines()
    assert any("test-sidecar-proxy.cjs" in args for args in harness_args)
    assert any("test-category-schema.cjs" in args for args in harness_args)
    assert any("test-category-editor-state.cjs" in args for args in harness_args)
    assert any("test-memory-explorer-state.cjs" in args for args in harness_args)
    assert all(args.endswith(str(dashboard)) for args in harness_args)


def test_verify_dashboard_overlay_skip_typecheck_bypasses_node_tools(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)
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

    for command in ("pnpm", "node"):
        executable = dashboard / command
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o755)

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
            "PATH": f"{dashboard}:{Path('/usr/bin')}:{Path('/bin')}",
            "MEM0_DASHBOARD_OVERLAY_SKIP_TYPECHECK": "1",
        },
    )

    assert result.returncode == 0, result.stderr


def test_verify_dashboard_overlay_uses_npm_exec_pnpm_when_global_pnpm_missing(
    tmp_path,
) -> None:
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)
    (dashboard / "package.json").write_text(
        '{"name":"mem0-dashboard","packageManager":"pnpm@10.34.2",'
        '"scripts":{"typecheck":"tsc"}}\n'
    )
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
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)

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


def test_verify_dashboard_overlay_uses_pinned_default_when_package_manager_missing(
    tmp_path,
) -> None:
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)
    (dashboard / "package.json").write_text(
        '{"name":"mem0-dashboard","scripts":{"typecheck":"tsc"}}\n'
    )
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
    npm_log = tmp_path / "npm-default.log"
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > {npm_log}\n"
        "exit 0\n"
    )
    npm.chmod(0o755)
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)

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
    write_dashboard_package(dashboard)
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


def test_apply_dashboard_overlay_replaces_categories_with_productized_editor(
    tmp_path,
):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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
    page_content = (categories / "page.tsx").read_text()
    drawer_content = (categories / "category-editor-drawer.tsx").read_text()
    manifest = json.loads((OVERLAY / "manifest.json").read_text())

    assert '"use client";' in page_content
    assert "sidecarGet<SidecarCategoryResponse>" in page_content
    assert (
        'import { getSidecarProjectId } from "@/utils/sidecar-project";'
        in page_content
    )
    assert "await getSidecarProjectId()" in page_content
    assert "sidecarPost<SidecarCategory>" in drawer_content
    assert "sidecarPatch<SidecarCategory>" in drawer_content
    assert "sidecarDelete(" in drawer_content
    assert "CategoryEditorDrawer" in page_content
    assert "CategoryFieldEditor" in drawer_content
    assert "Create category" in page_content
    assert "Advanced schema" in drawer_content
    assert "Generated schema" in drawer_content
    assert "Discard changes?" in drawer_content
    assert "Delete category?" in drawer_content
    assert "Schema JSON" not in page_content
    assert "sidecarPut<SidecarCategoryResponse>" not in page_content
    assert "SELF-HOSTED" not in page_content
    assert "LockedPage" not in page_content
    assert (
        "src/app/(root)/dashboard/categories/category-field-editor.tsx"
        in manifest["files"]
    )
    assert (
        "src/app/(root)/dashboard/categories/category-editor-drawer.tsx"
        in manifest["files"]
    )


def test_apply_dashboard_overlay_wires_category_safety_and_context(tmp_path):
    dashboard = applied_overlay(tmp_path)
    drawer = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-editor-drawer.tsx"
    ).read_text()
    field_editor = (
        dashboard
        / "src/app/(root)/dashboard/categories/category-field-editor.tsx"
    ).read_text()
    page = (
        dashboard / "src/app/(root)/dashboard/categories/page.tsx"
    ).read_text()

    assert "resolveCategorySchemaForSave(" in drawer
    assert "planCategoryDisable(isDirty)" in drawer
    assert "setFieldDefaultEnabled(field, hasDefault)" in field_editor
    assert "setFieldType(field, value)" in field_editor
    assert "Project" in page
    assert "projectId ??" in page
    assert "categories.length" in page
    assert "setProjectId(resolvedProjectId);" in page
    assert "setProjectId(null);" not in page
    assert "Category total unavailable" in page


@pytest.mark.parametrize(
    ("relative_path", "required", "replacement", "expected_error"),
    [
        (
            "src/app/(root)/dashboard/categories/category-editor-drawer.tsx",
            "schema = resolveCategorySchemaForSave(",
            "schema = editorToSchema(",
            "preserve an untouched stored schema",
        ),
        (
            "src/app/(root)/dashboard/categories/category-editor-drawer.tsx",
            "planCategoryDisable(isDirty)",
            '"disable"',
            "dirty Disable confirmation",
        ),
        (
            "src/app/(root)/dashboard/categories/category-field-editor.tsx",
            "setFieldDefaultEnabled(field, hasDefault)",
            "{ ...field, hasDefault }",
            "Boolean default state",
        ),
        (
            "src/app/(root)/dashboard/categories/category-field-editor.tsx",
            "setFieldType(field, value)",
            "{ ...field, type: value }",
            "Boolean default state",
        ),
        (
            "src/app/(root)/dashboard/categories/page.tsx",
            'Project <span className="break-all font-mono">',
            'Scope <span className="break-all font-mono">',
            "read-only project context",
        ),
        (
            "src/app/(root)/dashboard/categories/page.tsx",
            "`${categories.length} ${categories.length === 1",
            "`${0} ${categories.length === 1",
            "category totals",
        ),
        (
            "src/app/(root)/dashboard/categories/page.tsx",
            "setProjectId(resolvedProjectId);",
            "setProjectId(null);",
            "retain resolved project context",
        ),
        (
            "src/app/(root)/dashboard/categories/page.tsx",
            '"Category total unavailable"',
            '"Loading categories..."',
            "unavailable total state",
        ),
    ],
)
def test_verify_rejects_removed_category_safety_wiring_with_string_decoy(
    tmp_path, relative_path, required, replacement, expected_error
):
    dashboard = applied_overlay(tmp_path)
    target = dashboard / relative_path
    content = target.read_text()
    assert required in content
    content = content.replace(required, replacement, 1)
    content = content.replace(
        '"use client";',
        '"use client";\n\n'
        'const categorySafetyDecoy = '
        '"resolveCategorySchemaForSave( planCategoryDisable(isDirty) '
        'setFieldDefaultEnabled(field, hasDefault) setFieldType(field, value) '
        'Project categories.length setProjectId(resolvedProjectId); '
        'Category total unavailable";',
        1,
    )
    target.write_text(content)

    result = run_verify_without_typecheck(dashboard)

    assert result.returncode == 1
    assert expected_error in result.stderr


def test_apply_dashboard_overlay_replaces_export_with_sidecar_export_page(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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
    assert 'import { getSidecarProjectId } from "@/utils/sidecar-project";' in content
    assert "await getSidecarProjectId()" in content
    assert 'const PROJECT_ID = "default";' not in content
    assert 'format: "json",' in content
    assert "Object.fromEntries(" in content
    assert "disabled={isCreating || !projectId}" in content
    assert 'toast({ title: "Export created", variant: "success" });' in content
    assert 'title: "Failed to load exports"' in content
    assert 'title: "Failed to create export"' in content
    assert 'title: "Failed to download export"' in content
    assert "job.status !== \"SUCCEEDED\"" in content
    assert "Create export" in content
    assert "Coming soon" in content
    assert 'value="json"' in content
    assert 'value="csv"' in content
    assert 'value="pydantic"' in content
    assert "disabled" in content
    assert "formatFilterSummary" in content
    assert "job.completed_at" in content
    assert "job.error" in content
    assert "exported_count" in content
    assert "skipped_count" in content
    assert "Create JSON Export" not in content
    assert "SELF-HOSTED" not in content
    assert "Download" in content
    assert "LockedPage" not in content


def test_apply_dashboard_overlay_export_page_uses_safe_blob_download_cleanup(tmp_path):
    dashboard = tmp_path / "dashboard"
    write_dashboard_package(dashboard)

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
    write_dashboard_package(dashboard)

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
