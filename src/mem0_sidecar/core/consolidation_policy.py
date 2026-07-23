from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast

ConsolidationMode = Literal["OBSERVE", "MANUAL", "AUTO_SAFE"]
RetentionAction = Literal["REVIEW", "SHADOW"]

ALLOWED_MODES = frozenset({"OBSERVE", "MANUAL", "AUTO_SAFE"})
ALLOWED_RETENTION_ACTIONS = frozenset({"REVIEW", "SHADOW"})
ALLOWED_SAFE_ACTIONS = frozenset({"EXACT_DUPLICATE", "RETENTION_EXPIRED"})


@dataclass(frozen=True, slots=True)
class RetentionRule:
    days: int | None
    action: RetentionAction


DEFAULT_RETENTION: Mapping[str, RetentionRule] = MappingProxyType(
    {
        "session_state": RetentionRule(days=90, action="SHADOW"),
        "compact_summary": RetentionRule(days=90, action="SHADOW"),
        "auto_capture": RetentionRule(days=30, action="SHADOW"),
        "debugging_note": RetentionRule(days=180, action="REVIEW"),
        "decision": RetentionRule(days=None, action="REVIEW"),
        "user_preference": RetentionRule(days=None, action="REVIEW"),
        "project_profile": RetentionRule(days=None, action="REVIEW"),
    }
)


def _default_retention() -> Mapping[str, RetentionRule]:
    return MappingProxyType(dict(DEFAULT_RETENTION))


def _normalize_type_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("retention type names must be strings")
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError("retention type names must not be empty")
    return normalized


def _bounded_int(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


def _retention_rule(value: object, *, type_name: str) -> RetentionRule:
    if isinstance(value, RetentionRule):
        days: object = value.days
        action: object = value.action
    elif isinstance(value, Mapping):
        unknown = set(value) - {"days", "action"}
        if unknown:
            raise ValueError(
                f"retention rule {type_name!r} has unknown fields: "
                + ", ".join(sorted(map(str, unknown)))
            )
        days = value.get("days")
        action = value.get("action", "REVIEW")
    else:
        raise ValueError(f"retention rule {type_name!r} must be a mapping")

    if days is not None:
        days = _bounded_int(
            days,
            field_name=f"retention.{type_name}.days",
            minimum=1,
            maximum=36_500,
        )
    if not isinstance(action, str):
        raise ValueError(f"retention.{type_name}.action must be REVIEW or SHADOW")
    normalized_action = action.strip().upper()
    if normalized_action not in ALLOWED_RETENTION_ACTIONS:
        raise ValueError(f"retention.{type_name}.action must be REVIEW or SHADOW")
    if normalized_action == "SHADOW" and days is None:
        raise ValueError(
            f"retention.{type_name} cannot SHADOW without a finite day limit"
        )
    return RetentionRule(
        days=cast(int | None, days),
        action=cast(RetentionAction, normalized_action),
    )


@dataclass(frozen=True, slots=True)
class ConsolidationPolicySpec:
    enabled: bool = False
    mode: ConsolidationMode = "OBSERVE"
    max_anchors_per_run: int = 100
    max_proposals_per_run: int = 200
    shadow_grace_days: int = 7
    min_new_memories: int = 20
    scan_interval_seconds: int = 86_400
    near_duplicate_threshold: float = 0.92
    policy_version: int = 1
    safe_actions: frozenset[str] = field(
        default_factory=lambda: ALLOWED_SAFE_ACTIONS
    )
    retention: Mapping[str, RetentionRule] = field(
        default_factory=_default_retention
    )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> ConsolidationPolicySpec:
        if not isinstance(value, Mapping):
            raise ValueError("consolidation policy must be a mapping")
        allowed_fields = {
            "enabled",
            "mode",
            "max_anchors_per_run",
            "max_proposals_per_run",
            "shadow_grace_days",
            "min_new_memories",
            "scan_interval_seconds",
            "near_duplicate_threshold",
            "policy_version",
            "safe_actions",
            "retention",
        }
        unknown = set(value) - allowed_fields
        if unknown:
            raise ValueError(
                "unknown consolidation policy fields: "
                + ", ".join(sorted(map(str, unknown)))
            )

        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")

        raw_mode = value.get("mode", "OBSERVE")
        if not isinstance(raw_mode, str):
            raise ValueError("mode must be OBSERVE, MANUAL, or AUTO_SAFE")
        mode = raw_mode.strip().upper()
        if mode not in ALLOWED_MODES:
            raise ValueError("mode must be OBSERVE, MANUAL, or AUTO_SAFE")

        threshold = value.get("near_duplicate_threshold", 0.92)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("near_duplicate_threshold must be a number")
        threshold = float(threshold)
        if threshold < 0.5 or threshold > 1.0:
            raise ValueError("near_duplicate_threshold must be between 0.5 and 1.0")

        raw_safe_actions = value.get("safe_actions", ALLOWED_SAFE_ACTIONS)
        if isinstance(raw_safe_actions, str) or not isinstance(
            raw_safe_actions, (list, tuple, set, frozenset)
        ):
            raise ValueError("safe_actions must be a collection")
        safe_actions: set[str] = set()
        for item in raw_safe_actions:
            if not isinstance(item, str):
                raise ValueError("safe_actions entries must be strings")
            safe_actions.add(item.strip().upper())
        if not safe_actions.issubset(ALLOWED_SAFE_ACTIONS):
            raise ValueError(
                "safe_actions may contain only EXACT_DUPLICATE and "
                "RETENTION_EXPIRED"
            )

        retention = dict(DEFAULT_RETENTION)
        raw_retention = value.get("retention", {})
        if not isinstance(raw_retention, Mapping):
            raise ValueError("retention must be a mapping")
        normalized_overrides: set[str] = set()
        for raw_type, raw_rule in raw_retention.items():
            type_name = _normalize_type_key(raw_type)
            if type_name in normalized_overrides:
                raise ValueError(
                    f"retention contains duplicate normalized type {type_name!r}"
                )
            normalized_overrides.add(type_name)
            retention[type_name] = _retention_rule(raw_rule, type_name=type_name)

        return cls(
            enabled=enabled,
            mode=cast(ConsolidationMode, mode),
            max_anchors_per_run=_bounded_int(
                value.get("max_anchors_per_run", 100),
                field_name="max_anchors_per_run",
                minimum=1,
                maximum=1_000,
            ),
            max_proposals_per_run=_bounded_int(
                value.get("max_proposals_per_run", 200),
                field_name="max_proposals_per_run",
                minimum=1,
                maximum=1_000,
            ),
            shadow_grace_days=_bounded_int(
                value.get("shadow_grace_days", 7),
                field_name="shadow_grace_days",
                minimum=7,
                maximum=365,
            ),
            min_new_memories=_bounded_int(
                value.get("min_new_memories", 20),
                field_name="min_new_memories",
                minimum=1,
                maximum=100_000,
            ),
            scan_interval_seconds=_bounded_int(
                value.get("scan_interval_seconds", 86_400),
                field_name="scan_interval_seconds",
                minimum=60,
                maximum=31_536_000,
            ),
            near_duplicate_threshold=threshold,
            policy_version=_bounded_int(
                value.get("policy_version", 1),
                field_name="policy_version",
                minimum=1,
                maximum=2_147_483_647,
            ),
            safe_actions=frozenset(safe_actions),
            retention=MappingProxyType(retention),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "max_anchors_per_run": self.max_anchors_per_run,
            "max_proposals_per_run": self.max_proposals_per_run,
            "shadow_grace_days": self.shadow_grace_days,
            "min_new_memories": self.min_new_memories,
            "scan_interval_seconds": self.scan_interval_seconds,
            "near_duplicate_threshold": self.near_duplicate_threshold,
            "policy_version": self.policy_version,
            "safe_actions": sorted(self.safe_actions),
            "retention": {
                type_name: {"days": rule.days, "action": rule.action}
                for type_name, rule in sorted(self.retention.items())
            },
        }
