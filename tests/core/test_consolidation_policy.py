from types import MappingProxyType

import pytest

from mem0_sidecar.core.consolidation_policy import (
    ConsolidationPolicySpec,
    RetentionRule,
)


def test_policy_defaults_are_observe_only_and_bounded() -> None:
    policy = ConsolidationPolicySpec.from_mapping({})

    assert policy.enabled is False
    assert policy.mode == "OBSERVE"
    assert policy.max_anchors_per_run == 100
    assert policy.max_proposals_per_run == 200
    assert policy.shadow_grace_days == 7
    assert policy.retention["session_state"].days == 90
    assert policy.retention["auto_capture"].days == 30
    assert policy.retention["decision"].days is None
    assert policy.safe_actions == frozenset(
        {"EXACT_DUPLICATE", "RETENTION_EXPIRED"}
    )
    assert isinstance(policy.retention, MappingProxyType)


def test_policy_normalizes_mode_type_names_and_retention_overrides() -> None:
    policy = ConsolidationPolicySpec.from_mapping(
        {
            "enabled": True,
            "mode": "manual",
            "retention": {
                "Debug Note": {"days": 14, "action": "review"},
                "AUTO-CAPTURE": RetentionRule(days=10, action="SHADOW"),
            },
        }
    )

    assert policy.enabled is True
    assert policy.mode == "MANUAL"
    assert policy.retention["debug_note"] == RetentionRule(
        days=14, action="REVIEW"
    )
    assert policy.retention["auto_capture"] == RetentionRule(
        days=10, action="SHADOW"
    )
    assert policy.retention["decision"].days is None


@pytest.mark.parametrize("mode", ["AUTO", "SAFE", "", 3, True])
def test_policy_rejects_invalid_modes(mode: object) -> None:
    with pytest.raises(ValueError, match="mode"):
        ConsolidationPolicySpec.from_mapping({"mode": mode})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_anchors_per_run", 0),
        ("max_anchors_per_run", 1001),
        ("max_proposals_per_run", 0),
        ("max_proposals_per_run", 1001),
        ("shadow_grace_days", 6),
        ("shadow_grace_days", True),
        ("min_new_memories", 0),
        ("scan_interval_seconds", 59),
    ],
)
def test_policy_rejects_invalid_integer_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        ConsolidationPolicySpec.from_mapping({field: value})


@pytest.mark.parametrize(
    "rule",
    [
        {"days": 0, "action": "SHADOW"},
        {"days": True, "action": "SHADOW"},
        {"days": None, "action": "SHADOW"},
        {"days": 30, "action": "DELETE"},
    ],
)
def test_policy_rejects_unsafe_retention_rules(rule: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="retention"):
        ConsolidationPolicySpec.from_mapping(
            {"retention": {"temporary": rule}}
        )


def test_policy_rejects_non_allowlisted_safe_actions() -> None:
    with pytest.raises(ValueError, match="safe_actions"):
        ConsolidationPolicySpec.from_mapping(
            {"safe_actions": ["EXACT_DUPLICATE", "NEAR_DUPLICATE"]}
        )


def test_policy_is_immutable() -> None:
    policy = ConsolidationPolicySpec.from_mapping({})

    with pytest.raises(TypeError):
        policy.retention["decision"] = RetentionRule(1, "REVIEW")
