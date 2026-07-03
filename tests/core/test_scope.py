from mem0_sidecar.core.scope import normalize_scope


def test_normalize_scope_preserves_first_class_fields() -> None:
    scope = normalize_scope(
        project_id="repo-a",
        user_id="root",
        app_id=None,
        agent_id="codex",
        run_id="session-1",
    )

    assert scope.project_id == "repo-a"
    assert scope.user_id == "root"
    assert scope.app_id == "repo-a"
    assert scope.agent_id == "codex"
    assert scope.run_id == "session-1"
    assert scope.as_filter_dict() == {
        "user_id": "root",
        "agent_id": "codex",
        "app_id": "repo-a",
        "run_id": "session-1",
    }
