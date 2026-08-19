from pathlib import Path

REQUESTS_PAGE = (
    Path(__file__).parents[1]
    / "integrations/mem0-dashboard-overlay/overlays/src/app/(root)"
    / "dashboard/requests/page.tsx"
)


def test_requests_page_constrains_compact_viewport_width() -> None:
    content = REQUESTS_PAGE.read_text()

    assert (
        'className="w-full min-w-0 max-w-full [contain:inline-size] space-y-5"'
        in content
    )
