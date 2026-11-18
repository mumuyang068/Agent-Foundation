from __future__ import annotations

from datetime import datetime, timezone
from itertools import islice
from math import isfinite

from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, ScopeRef, stable_fingerprint
from agent_foundation.modules.access.ports import AccessAuthorization

from .contracts import (
    GraphEdge,
    GraphEdgePage,
    GraphExecutionRecord,
    GraphNeighborhood,
    GraphNode,
    GraphNodePage,
    GraphOperation,
    GraphOperationReceipt,
    GraphPolicy,
    GraphProposal,
    GraphProposalPage,
    GraphQueryHit,
    GraphQueryResult,
    GraphSchema,
    GraphSchemaPage,
    GraphTemporalMetadata,
    GraphTemporalPath,
    GraphTemporalQueryResult,
)
from .ports import (
    GraphAuthorityReader,
    GraphCandidateAlgorithm,
    GraphCandidateSource,
    GraphControlAuthority,
    GraphProposalStore,
    GraphResourceCurrentness,
    GraphTemporalAlgorithm,
    GraphWriteAuthority,
)


class GraphApplication:
    def __init__(
        self,
        authority: GraphControlAuthority,
        access: AccessAuthorization,
        policy: GraphPolicy,
        profile: ExecutionProfile,
        algorithm: GraphCandidateAlgorithm,
        evidence: GraphResourceCurrentness,
        *,
        proposal_store: GraphProposalStore | None = None,
        write_authority: GraphWriteAuthority | None = None,
        reader: GraphAuthorityReader | None = None,
        candidate_source: GraphCandidateSource | None = None,
        temporal_algorithm: GraphTemporalAlgorithm | None = None,
    ) -> None:
        if profile.domain != "graph":
            raise FoundationError("execution_profile_domain_invalid", "Graph needs a Graph profile")
        if (
            algorithm.descriptor.algorithm_id != profile.algorithm_id
            or algorithm.descriptor.implementation_version != profile.implementation_version
        ):
            raise FoundationError("algorithm_unavailable", "Graph profile does not select this algorithm")
        self._authority = authority
        self._access = access
        self._policy = policy
        self._profile = profile
        self._algorithm = algorithm
        self._evidence = evidence
        self._proposal_store = proposal_store or authority
        if write_authority is None:
            raise FoundationError(
                "graph_write_authority_missing",
                "Graph control and write authorities must be injected explicitly",
            )
        self._write_authority = write_authority
        self._reader = reader or self._write_authority
        self._candidate_source = candidate_source or self._reader
        self._temporal_algorithm = temporal_algorithm

    def propose(
        self,
        token: str,
        *,
        operations: tuple[GraphOperation, ...],
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
        schema_id: str | None = None,
        schema_version: int | None = None,
    ) -> GraphProposal:
        operation, _grant, _trusted = self._authorize(token, "propose")
        digest = stable_fingerprint({
            "action": "propose",
            "scope": operation.scope.model_dump(mode="json"),
            "operations": [self._command_operation(item) for item in operations],
            "evidence_refs": [ref.model_dump(mode="json") for ref in evidence_refs],
            "schema_id": schema_id,
            "schema_version": schema_version,
        })
        replayed = self._replayed_proposal(operation.scope, operation_id, digest)
        if replayed is not None:
            return replayed
        if not evidence_refs or any(
            ref.domain != "evidence"
            or ref.scope != operation.scope
            or ref.revision is None
            or not self._evidence.is_current(ref)
            for ref in evidence_refs
        ):
            raise FoundationError("graph_evidence_invalid", "Graph proposal needs current scoped Evidence")
        for item in operations:
            resource = item.node if item.node is not None else item.edge
            if resource is not None and (
                resource.scope != operation.scope
                or resource.source_refs != evidence_refs
            ):
                raise FoundationError("graph_operation_scope_invalid", "Graph operation is outside proposal scope")
        proposal = GraphProposal(
            scope=operation.scope,
            operations=operations,
            evidence_refs=evidence_refs,
            proposed_by=operation.principal.principal_id,
            status="pending" if self._policy.require_review else "approved",
            schema_id=schema_id,
            schema_version=schema_version,
        )
        return self._proposal_store.save_proposal(
            proposal, operation_id=operation_id, command_digest=digest
        )

    def review(
        self,
        token: str,
        proposal_id: str,
        *,
        approve: bool,
        expected_revision: int,
        operation_id: str,
    ) -> GraphProposal:
        operation, _grant, _trusted = self._authorize(token, "review")
        digest = stable_fingerprint({
            "action": "review",
            "proposal_id": proposal_id,
            "approve": approve,
            "expected_revision": expected_revision,
        })
        replayed = self._replayed_proposal(operation.scope, operation_id, digest)
        if replayed is not None:
            return replayed
        current = self._required_proposal(operation.scope, proposal_id)
        if current.proposed_by == operation.principal.principal_id:
            raise FoundationError("graph_review_separation_required", "Proposer cannot review proposal")
        updated = current.model_copy(update={
            "revision": current.revision + 1,
            "status": "approved" if approve else "rejected",
            "reviewed_by": operation.principal.principal_id,
            "updated_at": datetime.now(timezone.utc),
        })
        return self._proposal_store.review_proposal(
            updated,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
        )

    def apply(
        self,
        token: str,
        proposal_id: str,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> GraphProposal:
        operation, grant, _trusted = self._authorize(token, "apply")
        digest = stable_fingerprint({
            "action": "apply",
            "proposal_id": proposal_id,
            "expected_revision": expected_revision,
        })
        replayed = self._replayed_proposal(operation.scope, operation_id, digest)
        if replayed is not None:
            return replayed
        current = self._required_proposal(operation.scope, proposal_id)
        if current.status != "approved":
            raise FoundationError("graph_proposal_not_approved", "Graph proposal is not approved")
        write_operation_id = "graph-write:" + stable_fingerprint({
            "scope": operation.scope.model_dump(mode="json"),
            "operation_id": operation_id,
        })
        write_digest = stable_fingerprint({
            "action": "graph_write",
            "scope": operation.scope.model_dump(mode="json"),
            "proposal_id": current.proposal_id,
            "proposal_revision": current.revision,
            "operations": [self._command_operation(item) for item in current.operations],
        })
        intent = self._proposal_store.get_apply_intent(operation.scope, proposal_id)
        write_receipt = self._write_authority.get_operation_receipt(
            operation.scope, write_operation_id
        )
        if intent is None:
            if write_receipt is not None:
                raise FoundationError(
                    "graph_apply_intent_missing",
                    "Graph write receipt has no matching control-side apply intent",
                )
            if not all(self._evidence.is_current(ref) for ref in current.evidence_refs):
                raise FoundationError("graph_evidence_stale", "Graph proposal Evidence is no longer current")
            self._validate_schema(current)
            self._authorize(token, "apply")
            intent = self._proposal_store.prepare_apply(
                operation.scope,
                proposal_id,
                expected_revision=expected_revision,
                operation_id=operation_id,
                command_digest=digest,
                write_operation_id=write_operation_id,
                write_command_digest=write_digest,
                applied_by=operation.principal.principal_id,
                grant_fingerprint=grant.fingerprint,
                policy_fingerprint=self._policy.fingerprint,
            )
        if (
            intent.operation_id != operation_id
            or intent.command_digest != digest
            or intent.write_operation_id != write_operation_id
            or intent.write_command_digest != write_digest
            or intent.expected_revision != expected_revision
        ):
            raise FoundationError(
                "graph_apply_intent_conflict", "Graph apply intent differs from this command"
            )
        if write_receipt is None:
            self._authorize(token, "apply")
            if not all(self._evidence.is_current(ref) for ref in current.evidence_refs):
                raise FoundationError("graph_evidence_stale", "Graph proposal Evidence is no longer current")
            self._validate_schema(current)
            write_receipt = self._write_authority.apply_operations(
                operation.scope,
                current.operations,
                operation_id=write_operation_id,
                command_digest=write_digest,
            )
        if (
            write_receipt.operation_id != write_operation_id
            or write_receipt.command_digest != write_digest
            or write_receipt.action != "write"
        ):
            raise FoundationError(
                "graph_write_receipt_invalid", "Graph write receipt does not match apply intent"
            )
        return self._proposal_store.complete_apply(
            operation.scope,
            proposal_id,
            expected_revision=expected_revision,
            operation_id=operation_id,
            command_digest=digest,
            write_receipt=write_receipt,
        )

    def get_proposal(self, token: str, proposal_id: str) -> GraphProposal:
        operation, _grant, _trusted = self._authorize(token, "read")
        return self._required_proposal(operation.scope, proposal_id)

    def list_proposals(
        self, token: str, *, states: tuple[str, ...] = ("pending",), limit: int = 8
    ) -> GraphProposalPage:
        operation, grant, _trusted = self._authorize(token, "read")
        allowed = {"pending", "approved", "rejected", "applied"}
        normalized = tuple(sorted(set(states)))
        self._check_page_limit(grant, limit)
        if not normalized or any(state not in allowed for state in normalized):
            raise FoundationError("graph_query_invalid", "Graph proposal states are invalid")
        return GraphProposalPage(
            items=self._proposal_store.list_proposals(
                operation.scope, states=normalized, limit=limit
            ),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def get_node(self, token: str, node_id: str) -> GraphNode:
        operation, _grant, _trusted = self._authorize(token, "read")
        node = self._reader.get_node(operation.scope, node_id)
        if not self._node_available(node, operation.scope):
            raise FoundationError("graph_node_not_found", "Graph node is unavailable")
        return node

    def list_nodes(self, token: str, *, limit: int = 8) -> GraphNodePage:
        operation, grant, _trusted = self._authorize(token, "read")
        self._check_page_limit(grant, limit)
        return GraphNodePage(
            items=tuple(
                node
                for node in self._reader.list_nodes(operation.scope, limit=limit)
                if self._node_available(node, operation.scope)
            ),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def get_node_revision(self, token: str, node_id: str, revision: int) -> GraphNode:
        operation, _grant, _trusted = self._authorize(token, "read")
        node = self._reader.get_node_revision(operation.scope, node_id, revision)
        if node is None:
            raise FoundationError("graph_node_not_found", "Graph node revision does not exist")
        return node

    def get_edge(self, token: str, edge_id: str) -> GraphEdge:
        operation, _grant, _trusted = self._authorize(token, "read")
        edge = self._current_edge(operation.scope, edge_id)
        if edge is None:
            raise FoundationError("graph_edge_not_found", "Graph edge is unavailable")
        return edge

    def get_edge_revision(self, token: str, edge_id: str, revision: int) -> GraphEdge:
        operation, _grant, _trusted = self._authorize(token, "read")
        edge = self._reader.get_edge_revision(operation.scope, edge_id, revision)
        if edge is None:
            raise FoundationError("graph_edge_not_found", "Graph edge revision does not exist")
        return edge

    def list_edges(self, token: str, *, limit: int = 8) -> GraphEdgePage:
        operation, grant, _trusted = self._authorize(token, "read")
        self._check_page_limit(grant, limit)
        return GraphEdgePage(
            items=tuple(
                current
                for edge in self._reader.list_edges(operation.scope, limit=limit)
                if (current := self._current_edge(
                    operation.scope, edge.edge_id, expected_revision=edge.revision
                )) is not None
            ),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def neighborhood(self, token: str, node_id: str) -> GraphNeighborhood:
        center = self.get_node(token, node_id)
        edges = tuple(
            current
            for edge in self._reader.incident_edges(center.scope, node_id)
            if (current := self._current_edge(
                center.scope, edge.edge_id, expected_revision=edge.revision
            )) is not None
        )
        neighbor_ids = {
            edge.to_node_id if edge.from_node_id == node_id else edge.from_node_id for edge in edges
        }
        nodes = tuple(self.get_node(token, neighbor_id) for neighbor_id in sorted(neighbor_ids))
        return GraphNeighborhood(
            center=center,
            nodes=nodes,
            edges=edges,
            authority_watermark=self._reader.authority_watermark(center.scope),
        )

    def query(self, token: str, *, query: str, limit: int = 8) -> GraphQueryResult:
        operation, grant, _trusted = self._authorize(token, "read")
        maximum = min(
            grant.limits.get("max_items", 1000),
            self._profile.budget.get("max_items", 1000),
            100,
        )
        if not 1 <= limit <= maximum:
            raise FoundationError("graph_budget_exceeded", "Graph limit exceeds budget")
        # The returned result count is not a candidate scan budget. Restrict the
        # scan separately, then remove ineligible dependencies before ranking.
        # A full prefix consumes the scan allowance; it does not prove that the
        # authority has no further eligible records.
        scan_limit = min(
            grant.limits.get("max_scan_items", 1000),
            self._profile.budget.get("max_scan_items", 1000),
            10_000,
        )
        if scan_limit < 1:
            raise FoundationError("graph_budget_exceeded", "Graph candidate scan budget must be positive")
        scanned = tuple(islice(
            self._candidate_source.candidates(
                operation.scope, query=query, limit=scan_limit
            ),
            scan_limit,
        ))
        eligible = {}
        for ref in scanned:
            if (
                not isinstance(ref, ResourceRef)
                or ref.domain != "graph"
                or ref.scope != operation.scope
                or ref.resource_type != "graph_node"
                or ref.revision is None
            ):
                continue
            node = self._reader.get_node(operation.scope, ref.resource_id)
            if (
                node is not None
                and node.ref() == ref
                and node.node_id not in eligible
                and self._node_available(node, operation.scope)
            ):
                eligible[node.node_id] = node
        bounded = tuple(eligible.values())
        ranked = self._algorithm.rank(bounded, query=query, limit=limit)
        try:
            ranked_items = iter(ranked)
        except TypeError as error:
            raise FoundationError("graph_algorithm_output_invalid", "Graph algorithm output is not iterable") from error
        # A lazy algorithm can do work between yields. Finish its bounded
        # execution before authorization/currentness checks, so a later yield
        # cannot invalidate an item already appended to the result.
        ranked_output = tuple(islice(ranked_items, scan_limit))
        self._authorize(token, "read")
        by_id = {node.node_id: node for node in bounded}
        hits = []
        seen = set()
        inspected = duplicates = invalid = stale = 0
        for item in ranked_output:
            inspected += 1
            if (
                not isinstance(item, (tuple, list)) or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool) or not isinstance(item[1], (int, float))
            ):
                invalid += 1
                continue
            node_id = item[0]
            try:
                score = float(item[1])
            except (OverflowError, ValueError):
                invalid += 1
                continue
            if not isfinite(score) or score < 0:
                invalid += 1
                continue
            if node_id in seen:
                duplicates += 1
                continue
            seen.add(node_id)
            candidate = by_id.get(node_id)
            if candidate is None:
                invalid += 1
                continue
            current = self._reader.get_node(operation.scope, node_id)
            if (
                current is None
                or current.revision != candidate.revision
                or not self._node_available(current, operation.scope)
            ):
                stale += 1
                continue
            hits.append(GraphQueryHit(node=current, score=score))
            if len(hits) == limit:
                break
        self._authorize(token, "read")
        execution = GraphExecutionRecord(
            request_id=operation.request_id,
            grant_fingerprint=grant.fingerprint,
            policy_fingerprint=self._policy.fingerprint,
            execution_profile_fingerprint=self._profile.fingerprint,
            output_refs=tuple(hit.node.ref() for hit in hits),
            diagnostics={
                "eligible": len(bounded), "algorithm_id": self._algorithm.descriptor.algorithm_id,
                "scan_limit": scan_limit, "scanned": len(scanned),
                "scan_limit_reached": len(scanned) == scan_limit,
                "algorithm_items_materialized": len(ranked_output),
                "algorithm_items_inspected": inspected,
                "algorithm_item_limit_reached": len(ranked_output) == scan_limit,
                "duplicate_algorithm_items": duplicates, "invalid_algorithm_items": invalid,
                "stale_algorithm_items": stale, "result_limit_reached": len(hits) == limit,
            },
        )
        return GraphQueryResult(
            hits=tuple(hits),
            execution=execution,
            algorithm=self._algorithm.descriptor,
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def query_temporal(
        self,
        token: str,
        *,
        query: str,
        at_time: datetime | None = None,
        max_hops: int = 1,
        max_items: int = 8,
    ) -> GraphTemporalQueryResult:
        operation, grant, _trusted = self._authorize(token, "read")
        if self._temporal_algorithm is None:
            raise FoundationError("algorithm_unavailable", "Temporal Graph algorithm is not configured")
        instant = at_time or datetime.now(timezone.utc)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise FoundationError("temporal_query_invalid", "Temporal query time must be timezone aware")
        if max_hops not in {1, 2}:
            raise FoundationError("temporal_query_invalid", "Temporal traversal is limited to one or two hops")
        maximum = min(
            grant.limits.get("max_items", 16),
            self._profile.budget.get("max_items", 16),
            16,
        )
        if not 1 <= max_items <= maximum:
            raise FoundationError("graph_budget_exceeded", "Temporal result budget exceeds authorization")
        scan_limit = min(
            grant.limits.get("max_scan_items", 1000),
            self._profile.budget.get("max_scan_items", 1000),
            10_000,
        )
        if scan_limit < 1:
            raise FoundationError("graph_budget_exceeded", "Temporal scan budget must be positive")
        scanned_nodes = tuple(islice(
            self._candidate_source.candidates(
                operation.scope, query=query, limit=scan_limit
            ),
            scan_limit,
        ))
        resolved_nodes: dict[str, GraphNode] = {}
        for ref in scanned_nodes:
            if (
                not isinstance(ref, ResourceRef)
                or ref.domain != "graph"
                or ref.scope != operation.scope
                or ref.resource_type != "graph_node"
                or ref.revision is None
            ):
                continue
            node = self._reader.get_node(operation.scope, ref.resource_id)
            if (
                node is not None
                and node.ref() == ref
                and self._node_available(node, operation.scope)
            ):
                resolved_nodes.setdefault(node.node_id, node)
        nodes = tuple(resolved_nodes.values())
        scanned_edges = tuple(islice(
            self._reader.list_edges(operation.scope, limit=scan_limit), scan_limit
        ))
        edges = tuple(
            current
            for edge in scanned_edges
            if (current := self._current_edge(
                operation.scope, edge.edge_id, expected_revision=edge.revision
            )) is not None
            and self._temporal_eligible(current, instant)
        )
        proposed = self._temporal_algorithm.traverse(
            nodes,
            edges,
            query=query,
            max_hops=max_hops,
            max_items=max_items,
        )
        try:
            proposed_paths = tuple(islice(iter(proposed), scan_limit))
        except TypeError as error:
            raise FoundationError(
                "graph_algorithm_output_invalid", "Temporal algorithm output is not iterable"
            ) from error
        self._authorize(token, "read")
        node_refs = {node.ref(): node for node in nodes}
        edge_refs = {edge.ref(): edge for edge in edges}
        accepted: list[GraphTemporalPath] = []
        seen: set[tuple[ResourceRef, ...]] = set()
        invalid = stale = 0
        for path in proposed_paths:
            if (
                not isinstance(path, GraphTemporalPath)
                or len(path.edge_refs) > max_hops
                or path.edge_refs in seen
                or not isfinite(path.score)
                or path.score < 0
                or any(ref not in edge_refs for ref in path.edge_refs)
                or any(ref not in node_refs for ref in path.node_refs)
            ):
                invalid += 1
                continue
            seen.add(path.edge_refs)
            if not self._temporal_path_current(operation.scope, path, instant):
                stale += 1
                continue
            accepted.append(path)
            if len(accepted) == max_items:
                break
        self._authorize(token, "read")
        output_refs = tuple(ref for path in accepted for ref in path.edge_refs)
        execution = GraphExecutionRecord(
            request_id=operation.request_id,
            grant_fingerprint=grant.fingerprint,
            policy_fingerprint=self._policy.fingerprint,
            execution_profile_fingerprint=self._profile.fingerprint,
            output_refs=output_refs,
            diagnostics={
                "retrieval_mode": "temporal_point",
                "fallback_reason": "no_eligible_path" if not accepted else None,
                "scan_limit": scan_limit,
                "scanned_nodes": len(scanned_nodes),
                "scanned_edges": len(scanned_edges),
                "eligible_edges": len(edges),
                "traversed_items": len(proposed_paths),
                "invalid_algorithm_items": invalid,
                "stale_algorithm_items": stale,
                "budget_exhausted": len(scanned_nodes) == scan_limit or len(scanned_edges) == scan_limit,
                "algorithm_id": self._temporal_algorithm.descriptor.algorithm_id,
            },
        )
        return GraphTemporalQueryResult(
            paths=tuple(accepted),
            at_time=instant,
            max_hops=max_hops,
            max_items=max_items,
            execution=execution,
            algorithm=self._temporal_algorithm.descriptor,
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def propose_entity_normalization(
        self,
        token: str,
        *,
        node_ids: tuple[str, ...],
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
    ) -> GraphProposal:
        if len(set(node_ids)) < 2:
            raise FoundationError("entity_normalization_invalid", "Normalization needs two nodes")
        nodes = tuple(self.get_node(token, node_id) for node_id in sorted(set(node_ids)))
        canonical_id = nodes[0].node_id
        aliases = sorted({node.label for node in nodes})
        operations = tuple(
            GraphOperation(
                action="put_node",
                expected_revision=node.revision,
                node=node.model_copy(update={
                    "properties": {
                        **node.properties,
                        "canonical_node_id": canonical_id,
                        "canonical_aliases": aliases,
                    },
                    "source_refs": evidence_refs,
                }),
            )
            for node in nodes
        )
        return self.propose(
            token,
            operations=operations,
            evidence_refs=evidence_refs,
            operation_id=operation_id,
        )

    def propose_temporal_relation(
        self,
        token: str,
        *,
        edge: GraphEdge,
        valid_from: datetime,
        valid_to: datetime | None,
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
        cardinality: str = "many",
        audit_reason: str | None = None,
    ) -> GraphProposal:
        operation, _grant, _trusted = self._authorize(token, "read")
        if edge.scope != operation.scope:
            raise FoundationError("graph_operation_scope_invalid", "Graph edge is outside scope")
        current = self._reader.get_edge(operation.scope, edge.edge_id)
        source_authority = self._temporal_source_authority(operation.principal.kind)
        try:
            metadata = GraphTemporalMetadata(
                valid_from=valid_from,
                valid_until=valid_to,
                source_authority=source_authority,
                lifecycle="confirmed",
                cardinality=cardinality,
                audit_reason=audit_reason,
            )
        except ValueError as error:
            raise FoundationError("temporal_relation_invalid", str(error)) from error
        temporal = edge.model_copy(update={
            "temporal": metadata,
            "source_refs": evidence_refs,
        })
        return self.propose(
            token,
            operations=(GraphOperation(
                action="put_edge",
                edge=temporal,
                expected_revision=current.revision if current is not None else None,
            ),),
            evidence_refs=evidence_refs,
            operation_id=operation_id,
        )

    def propose_temporal_invalidation(
        self,
        token: str,
        edge_id: str,
        *,
        evidence_refs: tuple[ResourceRef, ...],
        audit_reason: str,
        operation_id: str,
    ) -> GraphProposal:
        edge = self.get_edge(token, edge_id)
        if edge.temporal is None or edge.temporal.lifecycle != "confirmed":
            raise FoundationError(
                "temporal_relation_not_confirmed", "Only confirmed temporal relations can be invalidated"
            )
        invalidated = edge.model_copy(update={
            "temporal": edge.temporal.model_copy(update={
                "lifecycle": "invalidated", "audit_reason": audit_reason,
            }),
            "source_refs": evidence_refs,
        })
        return self.propose(
            token,
            operations=(GraphOperation(
                action="put_edge", edge=invalidated, expected_revision=edge.revision
            ),),
            evidence_refs=evidence_refs,
            operation_id=operation_id,
        )

    def propose_temporal_supersession(
        self,
        token: str,
        edge_id: str,
        *,
        successor: GraphEdge,
        valid_from: datetime,
        valid_to: datetime | None,
        evidence_refs: tuple[ResourceRef, ...],
        audit_reason: str,
        operation_id: str,
    ) -> GraphProposal:
        operation, _grant, _trusted = self._authorize(token, "read")
        current = self._current_edge(operation.scope, edge_id)
        if current is None or current.temporal is None or current.temporal.lifecycle != "confirmed":
            raise FoundationError(
                "temporal_relation_not_confirmed", "Only confirmed temporal relations can be superseded"
            )
        if successor.edge_id == edge_id or self._reader.get_edge(operation.scope, successor.edge_id) is not None:
            raise FoundationError("temporal_successor_invalid", "Temporal successor needs a new edge identity")
        if (
            successor.scope != operation.scope
            or successor.from_node_id != current.from_node_id
            or successor.relation_type != current.relation_type
        ):
            raise FoundationError(
                "temporal_successor_invalid",
                "Temporal successor must preserve scope, subject and relation type",
            )
        source_authority = self._temporal_source_authority(operation.principal.kind)
        try:
            successor_metadata = GraphTemporalMetadata(
                valid_from=valid_from,
                valid_until=valid_to,
                source_authority=source_authority,
                lifecycle="confirmed",
                cardinality=current.temporal.cardinality,
                predecessor_ref=current.ref(),
                audit_reason=audit_reason,
            )
        except ValueError as error:
            raise FoundationError("temporal_relation_invalid", str(error)) from error
        successor_edge = successor.model_copy(update={
            "revision": 1,
            "temporal": successor_metadata,
            "source_refs": evidence_refs,
        })
        superseded = current.model_copy(update={
            "temporal": current.temporal.model_copy(update={
                "lifecycle": "superseded",
                "successor_ref": successor_edge.ref(),
                "audit_reason": audit_reason,
            }),
            "source_refs": evidence_refs,
        })
        return self.propose(
            token,
            operations=(
                GraphOperation(action="put_edge", edge=superseded, expected_revision=current.revision),
                GraphOperation(action="put_edge", edge=successor_edge),
            ),
            evidence_refs=evidence_refs,
            operation_id=operation_id,
        )

    def propose_relation_reinforcement(
        self,
        token: str,
        *,
        edge_id: str,
        value: float,
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
    ) -> GraphProposal:
        if not -1 <= value <= 1:
            raise FoundationError("relation_feedback_invalid", "Relation feedback must be -1..1")
        edge = self.get_edge(token, edge_id)
        updated = edge.model_copy(update={
            "properties": {
                **edge.properties,
                "feedback_count": int(edge.properties.get("feedback_count", 0)) + 1,
                "feedback_total": float(edge.properties.get("feedback_total", 0.0)) + value,
            },
            "source_refs": evidence_refs,
        })
        return self.propose(
            token,
            operations=(GraphOperation(
                action="put_edge", edge=updated, expected_revision=edge.revision
            ),),
            evidence_refs=evidence_refs,
            operation_id=operation_id,
        )

    def propose_relation_evolution(
        self,
        token: str,
        *,
        edge_id: str,
        relation_type: str,
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
    ) -> GraphProposal:
        edge = self.get_edge(token, edge_id)
        updated = edge.model_copy(update={
            "relation_type": relation_type,
            "properties": {
                **edge.properties,
                "supersedes_relation_type": edge.relation_type,
            },
            "source_refs": evidence_refs,
        })
        return self.propose(
            token,
            operations=(GraphOperation(
                action="put_edge", edge=updated, expected_revision=edge.revision
            ),),
            evidence_refs=evidence_refs,
            operation_id=operation_id,
        )

    def propose_community_topology(
        self,
        token: str,
        *,
        evidence_refs: tuple[ResourceRef, ...],
        operation_id: str,
    ) -> GraphProposal:
        operation, _grant, _trusted = self._authorize(token, "read")
        nodes = self._reader.list_nodes(operation.scope, limit=10_000)
        if not nodes:
            raise FoundationError("graph_topology_empty", "Community topology needs nodes")
        adjacency = {node.node_id: set() for node in nodes}
        for node_id in adjacency:
            for edge in self._reader.incident_edges(operation.scope, node_id):
                if edge.from_node_id in adjacency and edge.to_node_id in adjacency:
                    adjacency[edge.from_node_id].add(edge.to_node_id)
                    adjacency[edge.to_node_id].add(edge.from_node_id)
        assignments: dict[str, str] = {}
        for start in sorted(adjacency):
            if start in assignments:
                continue
            pending = [start]
            component = []
            while pending:
                current = pending.pop()
                if current in component:
                    continue
                component.append(current)
                pending.extend(sorted(adjacency[current] - set(component)))
            community_id = f"community:{min(component)}"
            assignments.update({node_id: community_id for node_id in component})
        operations = tuple(
            GraphOperation(
                action="put_node",
                expected_revision=node.revision,
                node=node.model_copy(update={
                    "properties": {**node.properties, "community_id": assignments[node.node_id]},
                    "source_refs": evidence_refs,
                }),
            )
            for node in nodes
        )
        return self.propose(
            token,
            operations=operations,
            evidence_refs=evidence_refs,
            operation_id=operation_id,
        )

    def publish_schema(
        self, token: str, *, schema: GraphSchema, operation_id: str
    ) -> GraphSchema:
        operation, _grant, _trusted = self._authorize(token, "schema_publish")
        if schema.scope != operation.scope:
            raise FoundationError("graph_schema_scope_invalid", "Graph schema is outside scope")
        digest = stable_fingerprint({
            "action": "schema_publish",
            "schema": schema.model_dump(mode="json", exclude={"published_at"}),
        })
        return self._authority.publish_schema(
            schema, operation_id=operation_id, command_digest=digest
        )

    def get_schema(self, token: str, schema_id: str, version: int) -> GraphSchema:
        operation, _grant, _trusted = self._authorize(token, "read")
        schema = self._authority.get_schema(operation.scope, schema_id, version)
        if schema is None:
            raise FoundationError("graph_schema_not_found", "Graph schema does not exist")
        return schema

    def list_schemas(
        self, token: str, *, schema_id: str | None = None, limit: int = 8
    ) -> GraphSchemaPage:
        operation, grant, _trusted = self._authorize(token, "read")
        self._check_page_limit(grant, limit)
        return GraphSchemaPage(
            items=self._authority.list_schemas(
                operation.scope, schema_id=schema_id, limit=limit
            ),
            authority_watermark=self._reader.authority_watermark(operation.scope),
        )

    def get_operation_receipt(self, token: str, operation_id: str) -> GraphOperationReceipt:
        operation, _grant, _trusted = self._authorize(token, "read")
        receipt = self._proposal_store.get_operation_receipt(operation.scope, operation_id)
        if receipt is None:
            raise FoundationError("graph_receipt_not_found", "Graph operation receipt does not exist")
        return receipt

    def is_current(self, ref: ResourceRef) -> bool:
        if ref.domain != "graph" or ref.revision is None:
            return False
        if ref.resource_type == "graph_node":
            resource = self._reader.get_node(ref.scope, ref.resource_id)
            return bool(
                self._node_available(resource, ref.scope)
                and str(resource.revision) == ref.revision
            )
        elif ref.resource_type == "graph_edge":
            resource = self._current_edge(ref.scope, ref.resource_id)
        else:
            return False
        return bool(
            resource is not None
            and str(resource.revision) == ref.revision
        )

    def close(self) -> None:
        closed: set[int] = set()
        for component in (
            self._candidate_source,
            self._reader,
            self._write_authority,
            self._proposal_store,
            self._authority,
        ):
            if id(component) in closed:
                continue
            closed.add(id(component))
            close = getattr(component, "close", None)
            if close is not None:
                close()

    def _validate_schema(self, proposal: GraphProposal) -> None:
        if proposal.schema_id is None:
            if self._policy.require_schema:
                raise FoundationError("graph_schema_required", "Graph policy requires a schema")
            return
        schema = self._authority.get_schema(
            proposal.scope, proposal.schema_id, int(proposal.schema_version)
        )
        if schema is None:
            raise FoundationError("graph_schema_not_found", "Graph schema does not exist")
        for item in proposal.operations:
            if item.node is not None and item.node.node_type not in schema.node_types:
                raise FoundationError("graph_node_type_invalid", "Graph node type is outside schema")
            if item.edge is not None and item.edge.relation_type not in schema.relation_types:
                raise FoundationError("graph_relation_type_invalid", "Graph relation type is outside schema")

    @staticmethod
    def _command_operation(item: GraphOperation) -> dict:
        payload = item.model_dump(mode="json")
        for resource_name in ("node", "edge"):
            resource = payload.get(resource_name)
            if resource is not None:
                for generated in ("revision", "status", "created_at", "updated_at"):
                    resource.pop(generated, None)
        return payload

    def _required_proposal(self, scope, proposal_id: str) -> GraphProposal:
        proposal = self._proposal_store.get_proposal(scope, proposal_id)
        if proposal is None:
            raise FoundationError("graph_proposal_not_found", "Graph proposal does not exist")
        return proposal

    def _replayed_proposal(
        self, scope: ScopeRef, operation_id: str, command_digest: str
    ) -> GraphProposal | None:
        receipt = self._proposal_store.get_operation_receipt(scope, operation_id)
        if receipt is None:
            return None
        if receipt.command_digest != command_digest:
            raise FoundationError("operation_id_conflict", "Operation ID was used for another command")
        if not receipt.resource_ids:
            raise FoundationError("graph_receipt_invalid", "Graph proposal receipt is invalid")
        proposal = self._proposal_store.get_proposal(scope, receipt.resource_ids[0])
        if proposal is None:
            raise FoundationError("graph_proposal_not_found", "Graph proposal does not exist")
        return proposal

    def _sources_current(self, refs: tuple[ResourceRef, ...]) -> bool:
        return bool(refs) and all(self._evidence.is_current(ref) for ref in refs)

    def _node_available(self, node: GraphNode | None, scope: ScopeRef) -> bool:
        return bool(
            node is not None and node.scope == scope and node.status == "active"
            and all(ref.scope == scope for ref in node.source_refs)
            and self._sources_current(node.source_refs)
        )

    def _current_edge(
        self, scope: ScopeRef, edge_id: str, *, expected_revision: int | None = None
    ) -> GraphEdge | None:
        edge = self._reader.get_edge(scope, edge_id)
        if (
            edge is None or edge.scope != scope or edge.status != "active"
            or (expected_revision is not None and edge.revision != expected_revision)
            or any(ref.scope != scope for ref in edge.source_refs)
            or not self._sources_current(edge.source_refs)
        ):
            return None
        for node_id in (edge.from_node_id, edge.to_node_id):
            if not self._node_available(self._reader.get_node(scope, node_id), scope):
                return None
        return edge

    @staticmethod
    def _temporal_source_authority(principal_kind: str) -> str:
        if principal_kind == "agent":
            return "agent_inference"
        if principal_kind == "service":
            return "verified_tool"
        return "owner_explicit"

    @staticmethod
    def _temporal_eligible(edge: GraphEdge, at_time: datetime) -> bool:
        temporal = edge.temporal
        return bool(
            temporal is not None
            and temporal.lifecycle == "confirmed"
            and temporal.source_authority in {"owner_explicit", "verified_tool"}
            and temporal.valid_from <= at_time
            and (temporal.valid_until is None or at_time < temporal.valid_until)
        )

    def _temporal_path_current(
        self, scope: ScopeRef, path: GraphTemporalPath, at_time: datetime
    ) -> bool:
        expected_from: str | None = None
        for index, ref in enumerate(path.edge_refs):
            if ref.scope != scope or ref.revision is None:
                return False
            current = self._current_edge(scope, ref.resource_id)
            if (
                current is None
                or str(current.revision) != ref.revision
                or not self._temporal_eligible(current, at_time)
            ):
                return False
            if index == 0:
                expected_from = current.from_node_id
            elif current.from_node_id != path.node_refs[index].resource_id:
                return False
            if (
                path.node_refs[index].resource_id != current.from_node_id
                or path.node_refs[index + 1].resource_id != current.to_node_id
            ):
                return False
            expected_from = current.to_node_id
        return expected_from is not None and all(
            ref.scope == scope
            and ref.revision is not None
            and (current := self._reader.get_node(scope, ref.resource_id)) is not None
            and str(current.revision) == ref.revision
            and self._node_available(current, scope)
            for ref in path.node_refs
        )

    @staticmethod
    def _check_page_limit(grant, limit: int) -> None:
        if not 1 <= limit <= min(grant.limits.get("max_items", 1000), 100):
            raise FoundationError("graph_budget_exceeded", "Graph page exceeds budget")

    def _authorize(self, token: str, action: str):
        return self._access.authorize_token(
            token,
            domain="graph",
            action=action,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
            execution_profile_id=self._profile.profile_id,
            execution_profile_version=self._profile.version,
        )
