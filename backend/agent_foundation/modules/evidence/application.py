from __future__ import annotations

from itertools import islice
from math import isfinite

from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, stable_fingerprint
from agent_foundation.modules.access.ports import AccessAuthorization

from .contracts import (
    EvidenceHit,
    EvidenceOperationReceipt,
    EvidencePolicy,
    EvidenceRetrievalStatus,
    EvidenceRevision,
    EvidenceRevisionPage,
    EvidenceSearchResult,
    EvidenceSupport,
    SourceRange,
)
from .ports import (
    EvidenceAuthority,
    EvidenceAuthorityReader,
    EvidenceCandidateAlgorithm,
    EvidenceCandidateSource,
)


class EvidenceApplication:
    def __init__(
        self,
        authority: EvidenceAuthority,
        access: AccessAuthorization,
        policy: EvidencePolicy,
        profile: ExecutionProfile,
        algorithm: EvidenceCandidateAlgorithm,
        *,
        reader: EvidenceAuthorityReader | None = None,
        candidate_source: EvidenceCandidateSource | None = None,
    ) -> None:
        if profile.domain != "evidence":
            raise FoundationError("execution_profile_domain_invalid", "Evidence needs an Evidence profile")
        if (
            algorithm.descriptor.algorithm_id != profile.algorithm_id
            or algorithm.descriptor.implementation_version != profile.implementation_version
        ):
            raise FoundationError("algorithm_unavailable", "Evidence profile does not select this algorithm")
        self._authority = authority
        self._access = access
        self._policy = policy
        self._profile = profile
        self._algorithm = algorithm
        self._reader = reader or authority
        self._candidate_source = candidate_source or authority

    def admit(
        self,
        token: str,
        *,
        source_id: str,
        content: str,
        source_range: SourceRange | None = None,
        operation_id: str,
    ) -> EvidenceRevision:
        operation, _grant, _trusted = self._authorize(token, "admit")
        if len(content) > self._policy.max_content_chars:
            raise FoundationError("evidence_content_too_large", "Evidence exceeds policy size")
        digest = stable_fingerprint({
            "action": "admit",
            "scope": operation.scope.model_dump(mode="json"),
            "source_id": source_id,
            "content": content,
            "source_range": source_range.model_dump(mode="json") if source_range else None,
        })
        return self._authority.admit(
            operation.scope,
            source_id=source_id,
            content=content,
            source_range=source_range,
            operation_id=operation_id,
            command_digest=digest,
        )

    def get(self, token: str, revision_id: str) -> EvidenceRevision:
        operation, _grant, _trusted = self._authorize(token, "read")
        revision = self._reader.get_revision(operation.scope, revision_id)
        if revision is None or revision.status == "revoked":
            raise FoundationError("evidence_not_found", "Evidence revision is unavailable")
        return revision

    def list(
        self,
        token: str,
        *,
        states: tuple[str, ...] = ("active",),
        source_id: str | None = None,
        limit: int = 8,
        cursor: str | None = None,
    ) -> EvidenceRevisionPage:
        operation, grant, _trusted = self._authorize(token, "read")
        allowed_states = {"active", "superseded", "revoked"}
        normalized_states = tuple(sorted(set(states)))
        if not normalized_states or any(state not in allowed_states for state in normalized_states):
            raise FoundationError("evidence_query_invalid", "Evidence states are invalid")
        max_items = min(grant.limits.get("max_items", 1000), 100)
        if not 1 <= limit <= max_items:
            raise FoundationError("evidence_budget_exceeded", "Evidence page exceeds the authorized budget")
        return self._reader.list_revisions(
            operation.scope,
            states=normalized_states,
            source_id=source_id,
            limit=limit,
            cursor=cursor,
        )

    def get_operation_receipt(self, token: str, operation_id: str) -> EvidenceOperationReceipt:
        operation, _grant, _trusted = self._authorize(token, "read")
        receipt = self._authority.get_operation_receipt(operation.scope, operation_id)
        if receipt is None:
            raise FoundationError("evidence_receipt_not_found", "Evidence operation receipt does not exist")
        return receipt

    def retrieval_status(self, token: str) -> EvidenceRetrievalStatus:
        operation, _grant, _trusted = self._authorize(token, "search")
        return EvidenceRetrievalStatus(
            algorithm=self._algorithm.descriptor,
            policy_fingerprint=self._policy.fingerprint,
            execution_profile_fingerprint=self._profile.fingerprint,
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def retrieval_watermark(self, token: str) -> int:
        operation, _grant, _trusted = self._authorize(token, "read")
        return self._reader.authority_watermark(operation.scope)

    def search(
        self,
        token: str,
        *,
        query: str,
        limit: int = 8,
        source_ids: tuple[str, ...] = (),
        revision_ids: tuple[str, ...] = (),
    ) -> EvidenceSearchResult:
        operation, grant, _trusted = self._authorize(token, "search")
        max_items = min(
            grant.limits.get("max_items", 1000),
            self._profile.budget.get("max_items", 1000),
        )
        if not 1 <= limit <= max_items:
            raise FoundationError("evidence_budget_exceeded", "Evidence limit exceeds the authorized budget")
        selected_sources = tuple(sorted(set(source_ids)))
        selected_revisions = tuple(sorted(set(revision_ids)))
        scan_limit = min(max(limit * 4, limit), max_items * 4, 4_000)
        proposed = self._candidate_source.candidates(
            operation.scope,
            query=query,
            source_ids=selected_sources,
            revision_ids=selected_revisions,
            limit=scan_limit,
        )
        # Candidate implementations only return untrusted references. Resolve
        # exact current authority records before an algorithm sees content.
        bounded_by_id: dict[str, EvidenceRevision] = {}
        for ref in islice(iter(proposed), scan_limit):
            if (
                not isinstance(ref, ResourceRef)
                or ref.domain != "evidence"
                or ref.scope != operation.scope
                or ref.resource_type != "evidence_revision"
                or ref.revision is None
            ):
                continue
            candidate = self._reader.get_revision(operation.scope, ref.resource_id)
            if (
                candidate is None
                or candidate.ref() != ref
                or not self._reader.is_current(ref)
                or (selected_sources and candidate.source_id not in selected_sources)
                or (selected_revisions and candidate.revision_id not in selected_revisions)
            ):
                continue
            bounded_by_id.setdefault(candidate.revision_id, candidate)
        bounded = tuple(bounded_by_id.values())
        ranked = self._algorithm.rank(bounded, query=query, limit=limit)
        try:
            ranked_output = tuple(islice(iter(ranked), scan_limit))
        except TypeError as error:
            raise FoundationError(
                "evidence_algorithm_output_invalid", "Evidence algorithm output is not iterable"
            ) from error
        # Authorization and authority are deliberately re-read after the untrusted candidate algorithm.
        self._authorize(token, "search")
        hits: list[EvidenceHit] = []
        seen: set[str] = set()
        for item in ranked_output:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], (int, float))
            ):
                continue
            revision_id = item[0]
            try:
                score = float(item[1])
            except (OverflowError, ValueError):
                continue
            if revision_id in seen or not isfinite(score) or score < 0:
                continue
            seen.add(revision_id)
            candidate = bounded_by_id.get(revision_id)
            if candidate is None:
                continue
            current = self._reader.get_revision(operation.scope, revision_id)
            if current is None or current.ref() != candidate.ref() or not self._reader.is_current(current.ref()):
                continue
            # Return the authority record rather than candidate-controlled content.
            hits.append(EvidenceHit(revision=current, score=score))
            if len(hits) == limit:
                break
        self._authorize(token, "search")
        return EvidenceSearchResult(
            hits=tuple(hits),
            policy_fingerprint=self._policy.fingerprint,
            execution_profile_fingerprint=self._profile.fingerprint,
            algorithm=self._algorithm.descriptor,
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def revoke(self, token: str, revision_id: str, *, operation_id: str) -> EvidenceRevision:
        operation, _grant, _trusted = self._authorize(token, "revoke")
        digest = stable_fingerprint({
            "action": "revoke", "scope": operation.scope.model_dump(mode="json"), "revision_id": revision_id,
        })
        return self._authority.revoke(
            operation.scope, revision_id, operation_id=operation_id, command_digest=digest
        )

    def revalidate(self, token: str, revision_id: str, *, operation_id: str) -> EvidenceRevision:
        operation, _grant, _trusted = self._authorize(token, "revalidate")
        digest = stable_fingerprint({
            "action": "revalidate", "scope": operation.scope.model_dump(mode="json"), "revision_id": revision_id,
        })
        return self._authority.revalidate(
            operation.scope, revision_id, operation_id=operation_id, command_digest=digest
        )

    def support(
        self,
        token: str,
        *,
        target: ResourceRef,
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
    ) -> EvidenceSupport:
        operation, _grant, _trusted = self._authorize(token, "support")
        if target.scope != operation.scope or any(
            ref.domain != "evidence" or ref.scope != operation.scope for ref in evidence_refs
        ):
            raise FoundationError("evidence_support_scope_invalid", "Support references are outside scope")
        if any(not self._reader.is_current(ref) for ref in evidence_refs):
            raise FoundationError("evidence_support_stale", "Support Evidence is not current")
        support = EvidenceSupport(scope=operation.scope, target=target, evidence_refs=evidence_refs)
        digest = stable_fingerprint({"action": "support", "support": support.model_dump(mode="json")})
        return self._authority.put_support(support, operation_id=operation_id, command_digest=digest)

    def get_support(self, token: str, support_id: str) -> EvidenceSupport:
        operation, _grant, _trusted = self._authorize(token, "read")
        support = self._reader.get_support(operation.scope, support_id)
        if support is None or support.status != "active":
            raise FoundationError("evidence_support_not_found", "Evidence support is unavailable")
        if self._policy.require_current_supports and any(
            not self._reader.is_current(ref) for ref in support.evidence_refs
        ):
            raise FoundationError("evidence_support_stale", "Evidence support dependencies are unavailable")
        return support

    def is_current(self, ref: ResourceRef) -> bool:
        return self._reader.is_current(ref)

    def close(self) -> None:
        try:
            if self._candidate_source is not self._authority:
                close = getattr(self._candidate_source, "close", None)
                if close is not None:
                    close()
        finally:
            self._authority.close()

    def _authorize(self, token: str, action: str):
        return self._access.authorize_token(
            token,
            domain="evidence",
            action=action,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
            execution_profile_id=self._profile.profile_id,
            execution_profile_version=self._profile.version,
        )
