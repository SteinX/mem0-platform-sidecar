import pytest

from mem0_sidecar.core.scope import normalize_scope, validate_scope_id


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


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "app-a ",
        " app-a",
        "app\n-a",
        "app\x00-a",
        "app\u200b-a",
        "x" * 257,
    ],
)
def test_validate_scope_id_rejects_nonportable_identifiers(value: str) -> None:
    with pytest.raises(ValueError, match="^app_id must be a portable"):
        validate_scope_id(value, field_name="app_id")


def test_validate_scope_id_preserves_exact_portable_boundary() -> None:
    value = "x" * 256

    assert validate_scope_id(value, field_name="app_id") == value
    assert validate_scope_id(None, field_name="user_id", required=False) is None


def test_normalize_scope_reuses_portable_identifier_validation() -> None:
    with pytest.raises(ValueError, match="^user_id must be a portable"):
        normalize_scope(
            project_id="repo-a",
            user_id=" user",
            app_id="app-a",
            agent_id=None,
            run_id=None,
        )
