import json
from datetime import UTC, datetime, timedelta

from mem0_sidecar.core.consolidation_analysis import ConsolidationAnalyzer
from mem0_sidecar.core.consolidation_policy import ConsolidationPolicySpec
from mem0_sidecar.store.models import MemoryIndex

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
POLICY = ConsolidationPolicySpec.from_mapping({})


def memory(
    memory_id: str,
    *,
    content_hash: str = "same-hash",
    normalized_type: str = "decision",
    user_id: str | None = "root",
    agent_id: str | None = None,
    source: str | None = "manual",
    confidence: object = 0.5,
    content_length: int = 20,
    created_at: datetime = NOW - timedelta(days=10),
    pinned: int = 0,
) -> MemoryIndex:
    return MemoryIndex(
        id=f"row-{memory_id}",
        project_id="project",
        app_id="app",
        mem0_memory_id=memory_id,
        user_id=user_id,
        agent_id=agent_id,
        content_hash=content_hash,
        content_length=content_length,
        normalized_type=normalized_type,
        source=source,
        pinned=pinned,
        metadata_projection_json=json.dumps({"confidence": confidence}),
        consolidation_state="ACTIVE",
        created_at=created_at,
        updated_at=created_at,
        last_observed_at=created_at,
    )


def test_exact_duplicate_never_crosses_user_subjects() -> None:
    drafts = ConsolidationAnalyzer().analyze_scope(
        "project",
        "app",
        POLICY,
        [memory("alice", user_id="alice"), memory("bob", user_id="bob")],
        NOW,
    )

    assert drafts == []


def test_exact_duplicate_never_crosses_agent_or_type_boundaries() -> None:
    drafts = ConsolidationAnalyzer().analyze_scope(
        "project",
        "app",
        POLICY,
        [
            memory("agent-a", user_id=None, agent_id="agent-a"),
            memory("agent-b", user_id=None, agent_id="agent-b"),
            memory("type-a", normalized_type="decision"),
            memory("type-b", normalized_type="project_profile"),
        ],
        NOW,
    )

    assert drafts == []


def test_exact_duplicate_canonical_choice_is_deterministic_and_text_free() -> None:
    auto = memory(
        "auto",
        source="opencode",
        confidence="0.99",
        content_length=100,
        created_at=NOW - timedelta(days=20),
    )
    manual = memory(
        "manual",
        source="manual",
        confidence="0.7",
        content_length=30,
        created_at=NOW - timedelta(days=5),
    )
    lower = memory(
        "lower",
        source="manual",
        confidence=0.6,
        content_length=200,
        created_at=NOW - timedelta(days=30),
    )

    draft = ConsolidationAnalyzer().analyze_scope(
        "project", "app", POLICY, [auto, lower, manual], NOW
    )[0]

    assert draft.kind == "EXACT_DUPLICATE"
    assert draft.canonical_id == "manual"
    assert draft.source_ids == ("auto", "lower", "manual")
    assert draft.evidence == {
        "hash_prefix": "same-hash"[:12],
        "normalized_type": "decision",
        "subject_kind": "user",
        "count": 3,
        "selection_reasons": [
            "non_auto_source",
            "higher_confidence",
            "longer_content",
            "older_created_at",
            "lexical_memory_id",
        ],
        "safe_action": True,
    }
    assert {"memory", "text", "data"}.isdisjoint(draft.evidence)


def test_pinned_memories_are_excluded_from_all_proposals() -> None:
    drafts = ConsolidationAnalyzer().analyze_scope(
        "project",
        "app",
        POLICY,
        [memory("pinned", pinned=1), memory("active")],
        NOW,
    )

    assert drafts == []


def test_retention_expiry_marks_only_shadow_rules_auto_safe() -> None:
    drafts = ConsolidationAnalyzer().analyze_scope(
        "project",
        "app",
        POLICY,
        [
            memory(
                "expired-auto",
                normalized_type="auto_capture",
                content_hash="auto",
                created_at=NOW - timedelta(days=31),
            ),
            memory(
                "fresh-auto",
                normalized_type="auto_capture",
                content_hash="fresh",
                created_at=NOW - timedelta(days=29),
            ),
            memory(
                "old-debug",
                normalized_type="debugging_note",
                content_hash="debug",
                created_at=NOW - timedelta(days=181),
            ),
            memory(
                "old-decision",
                normalized_type="decision",
                content_hash="decision",
                created_at=NOW - timedelta(days=500),
            ),
        ],
        NOW,
    )

    assert [(draft.source_ids, draft.evidence["safe_action"]) for draft in drafts] == [
        (("expired-auto",), True),
        (("old-debug",), False),
    ]


def test_proposal_keys_are_stable_and_output_is_bounded() -> None:
    policy = ConsolidationPolicySpec.from_mapping(
        {"max_proposals_per_run": 1}
    )
    anchors = [
        memory(
            "expired-b",
            normalized_type="auto_capture",
            content_hash="b",
            created_at=NOW - timedelta(days=31),
        ),
        memory(
            "expired-a",
            normalized_type="auto_capture",
            content_hash="a",
            created_at=NOW - timedelta(days=31),
        ),
    ]
    analyzer = ConsolidationAnalyzer()

    forward = analyzer.analyze_scope("project", "app", policy, anchors, NOW)
    reverse = analyzer.analyze_scope(
        "project", "app", policy, list(reversed(anchors)), NOW
    )

    assert len(forward) == 1
    assert forward[0].proposal_key == reverse[0].proposal_key
    assert forward[0].source_ids == ("expired-a",)
