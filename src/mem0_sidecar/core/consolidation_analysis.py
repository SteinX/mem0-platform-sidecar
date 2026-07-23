from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from mem0_sidecar.core.consolidation_policy import ConsolidationPolicySpec
from mem0_sidecar.store.models import MemoryIndex

_AUTO_SOURCES = frozenset(
    {"auto", "auto_capture", "opencode", "periodic_capture"}
)


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    kind: str
    source_ids: tuple[str, ...]
    canonical_id: str | None
    score: float | None
    evidence: dict[str, object]
    proposal_key: str


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    project_id: str
    app_id: str
    memory_id: str
    user_id: str | None
    agent_id: str | None
    normalized_type: str
    content_hash: str
    content_length: int
    text: str


_NEGATION_TOKENS = frozenset({"not", "no", "never", "without", "false"})
_DATE_PATTERN = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b")
_VERSION_PATTERN = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b", re.IGNORECASE)


def _tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return tuple(re.findall(r"[a-z0-9]+", normalized))


def _replacement_conflict(
    left: str,
    right: str,
    pattern: re.Pattern[str],
) -> bool:
    left_values = pattern.findall(left)
    right_values = pattern.findall(right)
    if not left_values or not right_values or left_values == right_values:
        return False
    return pattern.sub("<value>", left.casefold()) == pattern.sub(
        "<value>", right.casefold()
    )


def _contradiction_reason(left: str, right: str) -> str | None:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    left_negated = any(token in _NEGATION_TOKENS for token in left_tokens)
    right_negated = any(token in _NEGATION_TOKENS for token in right_tokens)
    left_base = tuple(
        token for token in left_tokens if token not in _NEGATION_TOKENS
    )
    right_base = tuple(
        token for token in right_tokens if token not in _NEGATION_TOKENS
    )
    if left_negated != right_negated and left_base == right_base:
        return "negation_flip"
    if _replacement_conflict(left, right, _DATE_PATTERN):
        return "date_change"
    if _replacement_conflict(left, right, _VERSION_PATTERN):
        return "version_change"
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _metadata(memory: MemoryIndex) -> dict[str, object]:
    try:
        value = json.loads(memory.metadata_projection_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _confidence(memory: MemoryIndex) -> float:
    value = _metadata(memory).get("confidence", 0.0)
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _subject_identity(memory: MemoryIndex) -> tuple[str, str, str]:
    if memory.user_id is not None and memory.agent_id is not None:
        return "user_agent", memory.user_id, memory.agent_id
    if memory.user_id is not None:
        return "user", memory.user_id, ""
    if memory.agent_id is not None:
        return "agent", memory.agent_id, ""
    return "unscoped", "", ""


def _canonical_sort_key(memory: MemoryIndex) -> tuple[object, ...]:
    source = (memory.source or "").strip().lower()
    is_auto = 1 if source in _AUTO_SOURCES else 0
    return (
        is_auto,
        -_confidence(memory),
        -(memory.content_length or 0),
        _as_utc(memory.created_at),
        memory.mem0_memory_id,
    )


def _proposal_key(
    *,
    project_id: str,
    app_id: str,
    kind: str,
    source_ids: tuple[str, ...],
    policy_version: int,
) -> str:
    payload = json.dumps(
        {
            "project_id": project_id,
            "app_id": app_id,
            "kind": kind,
            "source_ids": sorted(source_ids),
            "policy_version": policy_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConsolidationAnalyzer:
    def classify_neighbor(
        self,
        anchor: MemorySnapshot,
        candidate: MemorySnapshot,
        score: float,
        *,
        policy_version: int = 1,
    ) -> ProposalDraft | None:
        if (
            anchor.memory_id == candidate.memory_id
            or anchor.project_id != candidate.project_id
            or anchor.app_id != candidate.app_id
            or anchor.user_id != candidate.user_id
            or anchor.agent_id != candidate.agent_id
            or anchor.normalized_type != candidate.normalized_type
            or anchor.content_hash == candidate.content_hash
            or not math.isfinite(score)
        ):
            return None
        anchor_terms = set(_tokens(anchor.text))
        candidate_terms = set(_tokens(candidate.text))
        shared_terms = anchor_terms & candidate_terms
        reason = _contradiction_reason(anchor.text, candidate.text)
        if reason is not None:
            kind = "CONTRADICTION"
            reason_codes = [reason]
        elif len(shared_terms) >= 2:
            kind = "NEAR_DUPLICATE"
            reason_codes = ["high_similarity_shared_terms"]
        else:
            return None
        source_ids = tuple(sorted((anchor.memory_id, candidate.memory_id)))
        return ProposalDraft(
            kind=kind,
            source_ids=source_ids,
            canonical_id=None,
            score=score,
            evidence={
                "score": score,
                "shared_term_count": len(shared_terms),
                "normalized_type": anchor.normalized_type,
                "content_hash_prefixes": sorted(
                    {anchor.content_hash[:12], candidate.content_hash[:12]}
                ),
                "content_lengths": sorted(
                    [anchor.content_length, candidate.content_length]
                ),
                "reason_codes": reason_codes,
                "safe_action": False,
            },
            proposal_key=_proposal_key(
                project_id=anchor.project_id,
                app_id=anchor.app_id,
                kind=kind,
                source_ids=source_ids,
                policy_version=policy_version,
            ),
        )

    def analyze_scope(
        self,
        project_id: str,
        app_id: str,
        policy: ConsolidationPolicySpec,
        anchors: Sequence[MemoryIndex],
        now: datetime,
    ) -> list[ProposalDraft]:
        eligible = sorted(
            (
                memory
                for memory in anchors
                if memory.project_id == project_id
                and memory.app_id == app_id
                and memory.deleted_at is None
                and memory.consolidation_state == "ACTIVE"
                and not memory.pinned
                and memory.content_hash is not None
            ),
            key=lambda memory: memory.mem0_memory_id,
        )
        drafts: list[ProposalDraft] = []
        exact_groups: dict[
            tuple[str, str, str, str, str], list[MemoryIndex]
        ] = defaultdict(list)
        for memory in eligible:
            subject_kind, subject_id, secondary_subject_id = _subject_identity(memory)
            exact_groups[
                (
                    subject_kind,
                    subject_id,
                    secondary_subject_id,
                    memory.normalized_type or "unknown",
                    memory.content_hash or "",
                )
            ].append(memory)

        exact_member_ids: set[str] = set()
        for group_key in sorted(exact_groups):
            group = exact_groups[group_key]
            if len(group) < 2 or len(group) > 100:
                continue
            canonical = min(group, key=_canonical_sort_key)
            source_ids = tuple(
                sorted(memory.mem0_memory_id for memory in group)
            )
            exact_member_ids.update(source_ids)
            subject_kind, _subject_id, _secondary, normalized_type, content_hash = (
                group_key
            )
            drafts.append(
                ProposalDraft(
                    kind="EXACT_DUPLICATE",
                    source_ids=source_ids,
                    canonical_id=canonical.mem0_memory_id,
                    score=None,
                    evidence={
                        "hash_prefix": content_hash[:12],
                        "normalized_type": normalized_type,
                        "subject_kind": subject_kind,
                        "count": len(group),
                        "selection_reasons": [
                            "non_auto_source",
                            "higher_confidence",
                            "longer_content",
                            "older_created_at",
                            "lexical_memory_id",
                        ],
                        "safe_action": (
                            "EXACT_DUPLICATE" in policy.safe_actions
                        ),
                    },
                    proposal_key=_proposal_key(
                        project_id=project_id,
                        app_id=app_id,
                        kind="EXACT_DUPLICATE",
                        source_ids=source_ids,
                        policy_version=policy.policy_version,
                    ),
                )
            )

        for memory in eligible:
            if memory.mem0_memory_id in exact_member_ids:
                continue
            normalized_type = memory.normalized_type or "unknown"
            rule = policy.retention.get(normalized_type)
            if rule is None or rule.days is None:
                continue
            age_days = (_as_utc(now) - _as_utc(memory.created_at)).total_seconds() / (
                24 * 60 * 60
            )
            if age_days <= rule.days:
                continue
            source_ids = (memory.mem0_memory_id,)
            safe_action = (
                rule.action == "SHADOW"
                and "RETENTION_EXPIRED" in policy.safe_actions
            )
            drafts.append(
                ProposalDraft(
                    kind="RETENTION_EXPIRED",
                    source_ids=source_ids,
                    canonical_id=None,
                    score=None,
                    evidence={
                        "normalized_type": normalized_type,
                        "age_days": int(age_days),
                        "retention_days": rule.days,
                        "retention_action": rule.action,
                        "safe_action": safe_action,
                    },
                    proposal_key=_proposal_key(
                        project_id=project_id,
                        app_id=app_id,
                        kind="RETENTION_EXPIRED",
                        source_ids=source_ids,
                        policy_version=policy.policy_version,
                    ),
                )
            )

        drafts.sort(key=lambda draft: (draft.kind, draft.source_ids))
        return drafts[: policy.max_proposals_per_run]
