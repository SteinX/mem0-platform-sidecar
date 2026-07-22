from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from mem0_sidecar.core.consolidation_analysis import (
    ConsolidationAnalyzer,
    MemorySnapshot,
    ProposalDraft,
)
from mem0_sidecar.core.consolidation_policy import ConsolidationPolicySpec
from mem0_sidecar.core.exports import ExportService
from mem0_sidecar.core.memory_ops import (
    MemoryService,
    extract_memory_id,
    extract_memory_ids,
    memory_content_fingerprint,
)
from mem0_sidecar.mem0_client.client import Mem0UpstreamError
from mem0_sidecar.store.models import (
    ConsolidationProposal,
    ConsolidationRun,
    MemoryIndex,
)
from mem0_sidecar.store.repositories import (
    ConsolidationLineageRepository,
    ConsolidationPolicyRepository,
    ConsolidationProposalRepository,
    ConsolidationRunRepository,
    ExportJobRepository,
    JobRepository,
    MemoryIndexRepository,
    ProjectRepository,
    ServiceCapabilityRepository,
)


class ConsolidationConflictError(RuntimeError):
    pass


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_payload(run: ConsolidationRun) -> dict[str, object]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "app_id": run.app_id,
        "mode": run.mode,
        "status": run.status,
        "scan_cutoff": run.scan_cutoff,
        "counts": _json_object(run.counts_json),
        "error_code": run.error_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def _proposal_payload(proposal: ConsolidationProposal) -> dict[str, object]:
    try:
        source_ids = json.loads(proposal.source_ids_json)
    except (TypeError, ValueError):
        source_ids = []
    return {
        "id": proposal.id,
        "run_id": proposal.run_id,
        "kind": proposal.kind,
        "status": proposal.status,
        "source_ids": source_ids if isinstance(source_ids, list) else [],
        "canonical_id": proposal.canonical_memory_id,
        "score": proposal.score,
        "evidence": _json_object(proposal.evidence_json),
        "created_at": proposal.created_at,
    }


class ConsolidationService:
    def __init__(
        self,
        *,
        session: Session,
        mem0: Any | None = None,
        source_snapshot_checker: Callable[[str, str], bool] | None = None,
        bridge_routing_ready: bool = False,
        hard_delete_enabled: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.mem0 = mem0
        self.source_snapshot_checker = source_snapshot_checker or (
            lambda _project_id, _app_id: True
        )
        self.bridge_routing_ready = bridge_routing_ready
        self.hard_delete_enabled = hard_delete_enabled
        self.now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _proposal_sources(
        self,
        proposal: ConsolidationProposal,
        *,
        expected_hashes: dict[str, str],
        required_state: str,
    ) -> list[MemoryIndex] | None:
        repository = ConsolidationProposalRepository(self.session)
        source_ids = repository.source_ids(proposal)
        if set(expected_hashes) != set(source_ids):
            return None
        memories = MemoryIndexRepository(self.session).list_memories_by_ids(
            project_id=proposal.project_id,
            app_id=proposal.app_id,
            mem0_memory_ids=source_ids,
            include_deleted=True,
            include_shadowed=True,
        )
        by_id = {memory.mem0_memory_id: memory for memory in memories}
        if set(by_id) != set(source_ids):
            return None
        if any(
            memory.deleted_at is not None
            or memory.pinned == 1
            or memory.content_hash != expected_hashes[memory.mem0_memory_id]
            or memory.consolidation_state != required_state
            for memory in memories
        ):
            return None
        if proposal.kind == "EXACT_DUPLICATE" and (
            proposal.canonical_memory_id is None
            or proposal.canonical_memory_id not in by_id
        ):
            return None
        return memories

    def _mark_stale(self, proposal: ConsolidationProposal) -> dict[str, object]:
        ConsolidationProposalRepository(self.session).set_status(proposal, "STALE")
        return {"proposal_id": proposal.id, "status": "STALE"}

    async def approve_proposal(
        self,
        proposal_id: str,
        *,
        expected_status: str,
        expected_source_hashes: Mapping[str, str],
        canonical_id: str | None = None,
        replacement_text: str | None = None,
    ) -> dict[str, object]:
        proposals = ConsolidationProposalRepository(self.session)
        proposal = proposals.get(proposal_id)
        ProjectRepository(self.session).lock_for_mutation(proposal.project_id)
        self.session.expire(proposal)
        proposal = proposals.get(proposal_id)
        if proposal.status != expected_status:
            raise ConsolidationConflictError("proposal status changed")
        policy_row = ConsolidationPolicyRepository(self.session).get(
            proposal.project_id, proposal.app_id
        )
        if policy_row is None:
            raise ConsolidationConflictError("consolidation policy is missing")
        policy = ConsolidationPolicyRepository(self.session).spec(policy_row)
        if not policy.enabled or policy.mode == "OBSERVE":
            raise ConsolidationConflictError("policy does not allow approval")
        if not self.bridge_routing_ready:
            raise ConsolidationConflictError("bridge routing is not verified")
        semantic = proposal.kind in {"NEAR_DUPLICATE", "CONTRADICTION"}
        if semantic:
            if policy.mode != "MANUAL" or expected_status != "REVIEW_REQUIRED":
                raise ConsolidationConflictError(
                    "semantic proposals require manual review"
                )
            if (canonical_id is None) == (replacement_text is None):
                raise ConsolidationConflictError(
                    "choose one existing canonical or replacement text"
                )
        elif proposal.kind not in policy.safe_actions:
            raise ConsolidationConflictError("proposal action is not allow-listed")
        elif replacement_text is not None:
            raise ConsolidationConflictError(
                "replacement text is only valid for semantic proposals"
            )
        elif canonical_id is not None and canonical_id != proposal.canonical_memory_id:
            raise ConsolidationConflictError("canonical memory does not match proposal")
        normalized_hashes = {
            key: value
            for key, value in expected_source_hashes.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if normalized_hashes != dict(expected_source_hashes) or (
            sources := self._proposal_sources(
            proposal,
            expected_hashes=normalized_hashes,
            required_state="ACTIVE",
            )
        ) is None:
            return self._mark_stale(proposal)
        source_ids = proposals.source_ids(proposal)
        if canonical_id is not None and canonical_id not in source_ids:
            raise ConsolidationConflictError(
                "canonical memory is not a proposal source"
            )
        if replacement_text is not None:
            if not replacement_text.strip() or len(replacement_text) > 20_000:
                raise ConsolidationConflictError("replacement text is invalid")
            source = min(sources, key=lambda item: item.mem0_memory_id)
            replacement_type = source.normalized_type or "unknown"
            replacement_user = source.user_id
            replacement_agent = source.agent_id
            project_id = proposal.project_id
            app_id = proposal.app_id
            self.session.commit()
            replacement_payload: dict[str, Any] = {
                "text": replacement_text,
                "app_id": app_id,
                "metadata": {
                    "type": replacement_type,
                    "source": "consolidation_replacement",
                    "consolidation_proposal_id": proposal_id,
                },
            }
            if replacement_user is not None:
                replacement_payload["user_id"] = replacement_user
            if replacement_agent is not None:
                replacement_payload["agent_id"] = replacement_agent
            added = await MemoryService(
                session=self.session,
                mem0=self.mem0,
            ).add_memory(
                project_id=project_id,
                payload=replacement_payload,
                idempotency_key=f"consolidation-replacement-{proposal_id}",
            )
            memory_payload = added.get("memory")
            if not isinstance(memory_payload, dict):
                raise ConsolidationConflictError("replacement memory add failed")
            canonical_id = extract_memory_id(memory_payload)
            ProjectRepository(self.session).lock_for_mutation(project_id)
            proposal = proposals.get(proposal_id)
            if self._proposal_sources(
                proposal,
                expected_hashes=normalized_hashes,
                required_state="ACTIVE",
            ) is None:
                return self._mark_stale(proposal)
        proposal.expected_hashes_json = json.dumps(
            normalized_hashes,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if semantic:
            proposal.canonical_memory_id = canonical_id
            evidence = _json_object(proposal.evidence_json)
            evidence["operator_decision"] = (
                "replacement_created"
                if replacement_text is not None
                else "existing_canonical"
            )
            proposal.evidence_json = json.dumps(
                evidence,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        proposals.set_status(proposal, "APPROVED")
        return {"proposal_id": proposal.id, "status": "APPROVED"}

    @staticmethod
    def _record_is_pinned(record: dict[str, Any]) -> bool:
        metadata = record.get("metadata")
        if isinstance(metadata, dict) and metadata.get("pinned") is True:
            return True
        text = record.get("memory", record.get("text"))
        return isinstance(text, str) and text.lstrip().startswith("[PINNED]")

    async def _upstream_sources_match(
        self,
        source_ids: tuple[str, ...],
        expected_hashes: dict[str, str],
    ) -> bool:
        if self.mem0 is None:
            raise ConsolidationConflictError("Mem0 client is unavailable")
        for memory_id in source_ids:
            try:
                response = await self.mem0.get_memory(memory_id)
            except Mem0UpstreamError as exc:
                if exc.status_code == 404:
                    return False
                raise
            if not isinstance(response, dict) or memory_id not in extract_memory_ids(
                response
            ):
                return False
            content_hash, _length = memory_content_fingerprint(response)
            if (
                content_hash != expected_hashes[memory_id]
                or self._record_is_pinned(response)
            ):
                return False
        return True

    async def shadow_approved(self, proposal_id: str) -> dict[str, object]:
        proposals = ConsolidationProposalRepository(self.session)
        proposal = proposals.get(proposal_id)
        ProjectRepository(self.session).lock_for_mutation(proposal.project_id)
        self.session.expire(proposal)
        proposal = proposals.get(proposal_id)
        if proposal.status != "APPROVED":
            raise ConsolidationConflictError("proposal is not approved")
        if not self.bridge_routing_ready:
            raise ConsolidationConflictError("bridge routing is not verified")
        policy_row = ConsolidationPolicyRepository(self.session).get(
            proposal.project_id, proposal.app_id
        )
        if policy_row is None:
            raise ConsolidationConflictError("consolidation policy is missing")
        policy = ConsolidationPolicyRepository(self.session).spec(policy_row)
        expected_hashes = proposals.expected_hashes(proposal)
        if self._proposal_sources(
            proposal,
            expected_hashes=expected_hashes,
            required_state="ACTIVE",
        ) is None:
            result = self._mark_stale(proposal)
            self.session.commit()
            return result
        source_ids = proposals.source_ids(proposal)
        self.session.commit()

        if not await self._upstream_sources_match(source_ids, expected_hashes):
            proposal = proposals.get(proposal_id)
            result = self._mark_stale(proposal)
            self.session.commit()
            return result

        export = await ExportService(
            exports=ExportJobRepository(self.session),
            memories=MemoryIndexRepository(self.session),
            mem0=self.mem0,
        ).create_export(
            project_id=proposal.project_id,
            export_format="json",
            filters={"app_id": proposal.app_id, "memory_ids": list(source_ids)},
            release_before_upstream=True,
        )
        self.session.commit()

        ProjectRepository(self.session).lock_for_mutation(proposal.project_id)
        proposal = proposals.get(proposal_id)
        proposal.export_job_id = str(export["id"])
        complete_export = (
            export["status"] == "SUCCEEDED"
            and export["total_count"] == len(source_ids)
            and export["exported_count"] == len(source_ids)
            and export["skipped_count"] == 0
        )
        if not complete_export:
            status = "STALE" if export["status"] == "SUCCEEDED" else "FAILED"
            proposals.set_status(proposal, status)
            result = {"proposal_id": proposal.id, "status": status}
            self.session.commit()
            return result
        if self._proposal_sources(
            proposal,
            expected_hashes=expected_hashes,
            required_state="ACTIVE",
        ) is None:
            result = self._mark_stale(proposal)
            self.session.commit()
            return result

        target_ids = set(source_ids)
        if proposal.canonical_memory_id in target_ids:
            target_ids.remove(str(proposal.canonical_memory_id))
        sources = MemoryIndexRepository(self.session).list_memories_by_ids(
            project_id=proposal.project_id,
            app_id=proposal.app_id,
            mem0_memory_ids=target_ids,
            include_shadowed=True,
        )
        if len(sources) != len(target_ids):
            result = self._mark_stale(proposal)
            self.session.commit()
            return result
        for source in sources:
            source.consolidation_state = "SHADOWED"
            source.shadowed_by_proposal_id = proposal.id
        proposal.not_before = self.now() + timedelta(days=policy.shadow_grace_days)
        proposals.set_status(proposal, "SHADOWED")
        result = {
            "proposal_id": proposal.id,
            "status": "SHADOWED",
            "shadowed_count": len(target_ids),
            "not_before": proposal.not_before.isoformat(),
        }
        self.session.commit()
        return result

    def rollback_shadowed(self, proposal_id: str) -> dict[str, object]:
        proposals = ConsolidationProposalRepository(self.session)
        proposal = proposals.get(proposal_id)
        ProjectRepository(self.session).lock_for_mutation(proposal.project_id)
        self.session.expire(proposal)
        proposal = proposals.get(proposal_id)
        if proposal.status != "SHADOWED":
            raise ConsolidationConflictError("proposal is not shadowed")
        sources = MemoryIndexRepository(self.session).list_memories_by_ids(
            project_id=proposal.project_id,
            app_id=proposal.app_id,
            mem0_memory_ids=proposals.source_ids(proposal),
            include_deleted=True,
            include_shadowed=True,
        )
        restored = 0
        for source in sources:
            if (
                source.deleted_at is None
                and source.consolidation_state == "SHADOWED"
                and source.shadowed_by_proposal_id == proposal.id
            ):
                source.consolidation_state = "ACTIVE"
                source.shadowed_by_proposal_id = None
                restored += 1
        proposal.not_before = None
        proposals.set_status(proposal, "ROLLED_BACK")
        result = {
            "proposal_id": proposal.id,
            "status": "ROLLED_BACK",
            "restored_count": restored,
        }
        self.session.commit()
        return result

    async def _verify_upstream_deleted(self, memory_id: str) -> None:
        try:
            await self.mem0.get_memory(memory_id)
        except Mem0UpstreamError as exc:
            if exc.status_code == 404:
                return
            raise
        raise ConsolidationConflictError("hard-delete verification failed")

    async def finalize_shadowed(
        self,
        proposal_id: str,
        *,
        now: datetime,
    ) -> dict[str, object]:
        proposals = ConsolidationProposalRepository(self.session)
        proposal = proposals.get(proposal_id)
        if proposal.status != "SHADOWED":
            raise ConsolidationConflictError("proposal is not shadowed")
        if proposal.not_before is None or self._as_utc(now) < self._as_utc(
            proposal.not_before
        ):
            raise ConsolidationConflictError("shadow grace period has not elapsed")
        if not self.hard_delete_enabled:
            return {"proposal_id": proposal.id, "status": "SHADOWED"}
        if not proposal.export_job_id:
            raise ConsolidationConflictError("export checkpoint is missing")
        expected_hashes = proposals.expected_hashes(proposal)
        source_ids = proposals.source_ids(proposal)
        target_ids = list(source_ids)
        if proposal.canonical_memory_id in target_ids:
            target_ids.remove(str(proposal.canonical_memory_id))

        lineages = ConsolidationLineageRepository(self.session)
        for memory_id in target_ids:
            if lineages.get_source(
                proposal_id=proposal.id,
                source_memory_id=memory_id,
            ) is not None:
                continue
            projection = MemoryIndexRepository(self.session).get_memory(
                project_id=proposal.project_id,
                mem0_memory_id=memory_id,
                app_id=proposal.app_id,
                include_deleted=True,
            )
            if projection is None:
                proposals.set_status(proposal, "FAILED")
                self.session.commit()
                raise ConsolidationConflictError("shadowed projection is missing")
            if projection.deleted_at is None:
                if (
                    projection.consolidation_state != "SHADOWED"
                    or projection.shadowed_by_proposal_id != proposal.id
                    or projection.content_hash != expected_hashes[memory_id]
                    or projection.pinned == 1
                ):
                    proposals.set_status(proposal, "FAILED")
                    self.session.commit()
                    raise ConsolidationConflictError("shadowed source changed")
                await MemoryService(session=self.session, mem0=self.mem0).delete_memory(
                    project_id=proposal.project_id,
                    memory_id=memory_id,
                    request_app_id=proposal.app_id,
                )
            try:
                await self._verify_upstream_deleted(memory_id)
            except ConsolidationConflictError:
                proposal = proposals.get(proposal_id)
                proposals.set_status(proposal, "FAILED")
                self.session.commit()
                raise

            ProjectRepository(self.session).lock_for_mutation(proposal.project_id)
            proposal = proposals.get(proposal_id)
            lineages.create(
                proposal=proposal,
                source_memory_id=memory_id,
                canonical_memory_id=proposal.canonical_memory_id,
                action=f"{proposal.kind}_DELETE",
                source_content_hash=expected_hashes[memory_id],
                export_job_id=proposal.export_job_id,
                applied_at=self._as_utc(now),
            )
            self.session.commit()

        proposal = proposals.get(proposal_id)
        proposal.applied_at = self._as_utc(now)
        proposals.set_status(proposal, "APPLIED")
        result = {
            "proposal_id": proposal.id,
            "status": "APPLIED",
            "deleted_count": len(target_ids),
        }
        self.session.commit()
        return result

    @staticmethod
    def _snapshot_header(memory: MemoryIndex) -> MemorySnapshot:
        return MemorySnapshot(
            project_id=memory.project_id,
            app_id=memory.app_id or "",
            memory_id=memory.mem0_memory_id,
            user_id=memory.user_id,
            agent_id=memory.agent_id,
            normalized_type=memory.normalized_type or "unknown",
            content_hash=memory.content_hash or "",
            content_length=memory.content_length or 0,
            text="",
        )

    @staticmethod
    def _record_text(record: dict[str, Any]) -> str | None:
        for key in ("memory", "text", "content"):
            value = record.get(key)
            if isinstance(value, str):
                return value
        return None

    async def _semantic_drafts(
        self,
        *,
        policy: ConsolidationPolicySpec,
        anchor_headers: list[MemorySnapshot],
    ) -> list[ProposalDraft]:
        search = getattr(self.mem0, "search_memories", None)
        get_memory = getattr(self.mem0, "get_memory", None)
        if not callable(search) or not callable(get_memory):
            return []
        semaphore = asyncio.Semaphore(8)

        async def hydrate(header: MemorySnapshot) -> MemorySnapshot | None:
            async with semaphore:
                try:
                    response = await get_memory(header.memory_id)
                except Mem0UpstreamError as exc:
                    if exc.status_code == 404:
                        return None
                    raise
            if (
                not isinstance(response, dict)
                or header.memory_id not in extract_memory_ids(response)
                or self._record_is_pinned(response)
            ):
                return None
            content_hash, content_length = memory_content_fingerprint(response)
            text = self._record_text(response)
            if content_hash != header.content_hash or text is None:
                return None
            return MemorySnapshot(
                project_id=header.project_id,
                app_id=header.app_id,
                memory_id=header.memory_id,
                user_id=header.user_id,
                agent_id=header.agent_id,
                normalized_type=header.normalized_type,
                content_hash=content_hash,
                content_length=content_length or 0,
                text=text,
            )

        hydrated_anchors = [
            snapshot
            for snapshot in await asyncio.gather(
                *(hydrate(header) for header in anchor_headers)
            )
            if snapshot is not None
        ]

        async def neighbors(
            anchor: MemorySnapshot,
        ) -> tuple[MemorySnapshot, list[dict[str, Any]]]:
            payload: dict[str, Any] = {
                "query": anchor.text,
                "app_id": anchor.app_id,
                "top_k": 10,
                "threshold": policy.near_duplicate_threshold,
                "filters": {
                    "normalized_type": anchor.normalized_type,
                    "sidecar_app_id": anchor.app_id,
                    "sidecar_project_id": anchor.project_id,
                },
            }
            if anchor.user_id is not None:
                payload["user_id"] = anchor.user_id
            if anchor.agent_id is not None:
                payload["agent_id"] = anchor.agent_id
            async with semaphore:
                response = await search(payload)
            results = response.get("results") if isinstance(response, dict) else None
            return (
                anchor,
                [item for item in results if isinstance(item, dict)]
                if isinstance(results, list)
                else [],
            )

        search_results = await asyncio.gather(
            *(neighbors(anchor) for anchor in hydrated_anchors)
        )
        candidate_refs: list[tuple[tuple[str, str], MemorySnapshot, str, float]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for anchor, results in search_results:
            for item in results[:10]:
                ids = extract_memory_ids(item)
                if not ids or ids[0] == anchor.memory_id:
                    continue
                score_value = item.get("score")
                if isinstance(score_value, bool):
                    continue
                try:
                    score = float(score_value)
                except (TypeError, ValueError):
                    continue
                if (
                    not math.isfinite(score)
                    or score < policy.near_duplicate_threshold
                ):
                    continue
                pair = tuple(sorted((anchor.memory_id, ids[0])))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                candidate_refs.append((pair, anchor, ids[0], score))

        candidate_ids = list(
            dict.fromkeys(ref[2] for ref in sorted(candidate_refs))
        )
        candidate_projections = MemoryIndexRepository(
            self.session
        ).list_memories_by_ids(
            project_id=anchor_headers[0].project_id if anchor_headers else "",
            app_id=anchor_headers[0].app_id if anchor_headers else "",
            mem0_memory_ids=candidate_ids,
        )
        candidate_headers = {
            memory.mem0_memory_id: self._snapshot_header(memory)
            for memory in candidate_projections
            if not memory.pinned
        }
        self.session.rollback()

        valid_refs = [
            ref
            for ref in sorted(candidate_refs)
            if (header := candidate_headers.get(ref[2])) is not None
            and header.project_id == ref[1].project_id
            and header.app_id == ref[1].app_id
            and header.user_id == ref[1].user_id
            and header.agent_id == ref[1].agent_id
            and header.normalized_type == ref[1].normalized_type
            and header.content_hash != ref[1].content_hash
        ]
        hydration_ids = list(dict.fromkeys(ref[2] for ref in valid_refs))[:20]
        anchor_cache = {anchor.memory_id: anchor for anchor in hydrated_anchors}
        missing_headers = [
            candidate_headers[memory_id]
            for memory_id in hydration_ids
            if memory_id not in anchor_cache
        ]
        candidate_cache = dict(anchor_cache)
        for snapshot in await asyncio.gather(
            *(hydrate(header) for header in missing_headers)
        ):
            if snapshot is not None:
                candidate_cache[snapshot.memory_id] = snapshot

        drafts: list[ProposalDraft] = []
        analyzer = ConsolidationAnalyzer()
        for _pair, anchor, candidate_id, score in valid_refs:
            if candidate_id not in hydration_ids:
                continue
            candidate = candidate_cache.get(candidate_id)
            if candidate is None:
                continue
            draft = analyzer.classify_neighbor(
                anchor,
                candidate,
                score,
                policy_version=policy.policy_version,
            )
            if draft is not None:
                drafts.append(draft)
        return drafts

    async def run_scan(self, run_id: str) -> dict[str, object]:
        runs = ConsolidationRunRepository(self.session)
        policies = ConsolidationPolicyRepository(self.session)
        proposals = ConsolidationProposalRepository(self.session)
        memories = MemoryIndexRepository(self.session)
        run = runs.get(run_id)
        project_id = run.project_id
        if run.status == "SUCCEEDED":
            return {
                "run_id": run.id,
                "status": run.status,
                "proposal_count": proposals.count_for_run(run.id),
            }

        scan_cutoff = self.now()
        runs.mark_running(run.id, scan_cutoff=scan_cutoff)
        if not self.source_snapshot_checker(run.project_id, run.app_id):
            runs.mark_failed(
                run.id,
                error_code="INCOMPLETE_SOURCE",
                error={"error_type": "IncompleteSourceSnapshot"},
                completed_at=self.now(),
            )
            self.session.commit()
            return {"run_id": run.id, "status": "FAILED", "proposal_count": 0}

        policy_row = policies.get(run.project_id, run.app_id)
        if policy_row is None:
            runs.mark_failed(
                run.id,
                error_code="POLICY_NOT_FOUND",
                error={"error_type": "PolicyNotFound"},
                completed_at=self.now(),
            )
            self.session.commit()
            return {"run_id": run.id, "status": "FAILED", "proposal_count": 0}

        try:
            policy = policies.spec(policy_row)
            anchors = memories.list_dirty_anchors(
                project_id=run.project_id,
                app_id=run.app_id,
                limit=policy.max_anchors_per_run,
            )
            expanded = {}
            for anchor in anchors:
                expanded[anchor.mem0_memory_id] = anchor
                for peer in memories.list_exact_group_peers(anchor, limit=100):
                    expanded[peer.mem0_memory_id] = peer
            drafts = ConsolidationAnalyzer().analyze_scope(
                run.project_id,
                run.app_id,
                policy,
                list(expanded.values()),
                scan_cutoff,
            )
            for draft in drafts:
                status = (
                    "PENDING"
                    if draft.evidence.get("safe_action") is True
                    else "REVIEW_REQUIRED"
                )
                proposals.create(
                    run=run,
                    proposal_key=draft.proposal_key,
                    kind=draft.kind,
                    source_ids=draft.source_ids,
                    canonical_id=draft.canonical_id,
                    score=draft.score,
                    evidence=draft.evidence,
                    status=status,
                )
            remaining = max(policy.max_proposals_per_run - len(drafts), 0)
            if remaining and anchors:
                covered_ids = {
                    memory_id
                    for draft in drafts
                    for memory_id in draft.source_ids
                }
                anchor_headers = [
                    self._snapshot_header(anchor)
                    for anchor in anchors
                    if anchor.mem0_memory_id not in covered_ids
                ]
            else:
                anchor_headers = []
            if anchor_headers:
                self.session.commit()
                semantic_drafts = await self._semantic_drafts(
                    policy=policy,
                    anchor_headers=anchor_headers,
                )
                ProjectRepository(self.session).lock_for_mutation(project_id)
                run = runs.get(run_id)
                if run.status != "RUNNING":
                    raise ConsolidationConflictError("scan run is no longer active")
                for draft in semantic_drafts[:remaining]:
                    proposals.create(
                        run=run,
                        proposal_key=draft.proposal_key,
                        kind=draft.kind,
                        source_ids=draft.source_ids,
                        canonical_id=draft.canonical_id,
                        score=draft.score,
                        evidence=draft.evidence,
                        status="REVIEW_REQUIRED",
                    )
            counts = proposals.summary_for_run(run.id)
            runs.mark_succeeded(
                run.id,
                counts=counts,
                completed_at=self.now(),
            )
            result = {
                "run_id": run.id,
                "status": "SUCCEEDED",
                "proposal_count": counts["total"],
            }
            self.session.commit()
            return result
        except Exception as exc:
            self.session.rollback()
            runs.mark_failed(
                run_id,
                error_code="SCAN_FAILED",
                error={"error_type": type(exc).__name__},
                completed_at=self.now(),
            )
            self.session.commit()
            raise

    def get_status(self, project_id: str, app_id: str) -> dict[str, object]:
        policies = ConsolidationPolicyRepository(self.session)
        runs = ConsolidationRunRepository(self.session)
        proposals = ConsolidationProposalRepository(self.session)
        policy_row = policies.get(project_id, app_id)
        policy = (
            policies.spec(policy_row)
            if policy_row is not None
            else ConsolidationPolicySpec.from_mapping({})
        )
        current = runs.active(project_id, app_id)
        last = runs.last_succeeded(project_id, app_id)
        dirty_count = MemoryIndexRepository(self.session).count_dirty_anchors(
            project_id=project_id,
            app_id=app_id,
        )
        bridge_status = ServiceCapabilityRepository(
            self.session
        ).bridge_routing_status(project_id)
        return {
            "project_id": project_id,
            "app_id": app_id,
            "policy": policy.to_mapping(),
            "configured": policy_row is not None,
            "dirty_count": dirty_count,
            "current_run": _run_payload(current) if current else None,
            "last_run": _run_payload(last) if last else None,
            "proposal_counts": proposals.counts_for_scope(project_id, app_id),
            "bridge_routing_ready": (
                self.bridge_routing_ready or bool(bridge_status["ready"])
            ),
            "bridge": bridge_status,
            "worker": JobRepository(self.session).consolidation_status(
                project_id=project_id,
                app_id=app_id,
            ),
        }

    def get_run(
        self, project_id: str, app_id: str, run_id: str
    ) -> dict[str, object]:
        return _run_payload(
            ConsolidationRunRepository(self.session).get(
                run_id, project_id=project_id, app_id=app_id
            )
        )

    def list_proposals(
        self,
        *,
        project_id: str,
        app_id: str,
        run_id: str,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        ConsolidationRunRepository(self.session).get(
            run_id, project_id=project_id, app_id=app_id
        )
        repository = ConsolidationProposalRepository(self.session)
        items = repository.list_for_run(
            project_id=project_id,
            app_id=app_id,
            run_id=run_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return {
            "results": [_proposal_payload(item) for item in items],
            "page": page,
            "page_size": page_size,
            "total": repository.count_for_run(run_id),
        }
