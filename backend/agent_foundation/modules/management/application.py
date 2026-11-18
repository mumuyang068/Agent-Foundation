from __future__ import annotations

from typing import Any

from agent_foundation.execution.extractors import ExtractionProposalSink, ExtractorRuntime, ExtractorSpec
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, stable_fingerprint
from agent_foundation.modules.access.ports import AccessAuthorization

from .contracts import (
    EvaluationReport,
    ExtractorCertificate,
    ExtractorCertificateRevocation,
    ExtractorExecutionReceipt,
    ExtractorFixture,
    ExtractorFixtureResult,
    IndexGeneration,
    ManagedRelease,
    ManagementPolicy,
    TransferPlan,
)
from .ports import IndexEffect, ManagementAuthority, ReleaseEffect, TransferEffect


class ManagementApplication:
    def __init__(
        self,
        authority: ManagementAuthority,
        access: AccessAuthorization,
        policy: ManagementPolicy,
        *,
        release_effects: dict[str, ReleaseEffect],
        index_effect: IndexEffect,
        transfer_effect: TransferEffect,
        extractor_runtime: ExtractorRuntime | None = None,
        extraction_sink: ExtractionProposalSink | None = None,
    ) -> None:
        self._authority = authority
        self._access = access
        self._policy = policy
        self._release_effects = dict(release_effects)
        self._index_effect = index_effect
        self._transfer_effect = transfer_effect
        self._extractor_runtime = extractor_runtime
        self._extraction_sink = extraction_sink

    def certify_extractor(
        self,
        token: str,
        release_id: str,
        *,
        fixtures: tuple[ExtractorFixture, ...],
        reason: str,
        operation_id: str,
    ) -> ExtractorCertificate:
        operation = self._authorize(token, "extractor_certify")[0]
        try:
            fixtures = tuple(ExtractorFixture.model_validate(item) for item in fixtures)
        except ValueError as error:
            raise FoundationError(
                "extractor_fixture_invalid", "Extractor certification fixture is invalid"
            ) from error
        if self._extractor_runtime is None:
            raise FoundationError("extractor_runtime_unavailable", "Extractor runtime is not configured")
        release = self._required_active_extractor(operation.scope, release_id)
        if not 2 <= len(fixtures) <= 8:
            raise FoundationError("extractor_fixture_count_invalid", "Certification needs 2..8 fixtures")
        fixture_ids = tuple(item.fixture_id for item in fixtures)
        if len(set(fixture_ids)) != len(fixture_ids):
            raise FoundationError("extractor_fixture_duplicate", "Fixture IDs must be unique")
        if {item.expected_status for item in fixtures} != {"candidate", "no_candidate"}:
            raise FoundationError(
                "extractor_fixture_coverage_incomplete",
                "Certification needs candidate and no_candidate fixtures",
            )
        extractor = self._extractor_spec(release)
        results = []
        for fixture in fixtures:
            observed_status, value, _size = self._extractor_runtime.execute_value(
                extractor, fixture.input_payload
            )
            expected_fingerprint = (
                stable_fingerprint({"value": fixture.expected_value})
                if fixture.expected_status == "candidate" else None
            )
            observed_fingerprint = (
                stable_fingerprint({"value": value}) if observed_status == "candidate" else None
            )
            results.append(ExtractorFixtureResult(
                fixture_id=fixture.fixture_id,
                input_fingerprint=stable_fingerprint(fixture.input_payload),
                expected_status=fixture.expected_status,
                observed_status=observed_status,
                expected_value_fingerprint=expected_fingerprint,
                observed_value_fingerprint=observed_fingerprint,
                passed=(
                    observed_status == fixture.expected_status
                    and (fixture.expected_status != "candidate"
                         or expected_fingerprint == observed_fingerprint)
                ),
            ))
        result_tuple = tuple(results)
        certificate = ExtractorCertificate(
            scope=operation.scope,
            release_id=release.release_id,
            release_content_digest=release.content_digest,
            schema_id=extractor.schema_id,
            schema_version=extractor.schema_version,
            implementation_id=extractor.implementation_id,
            status="conformant" if all(item.passed for item in result_tuple) else "nonconformant",
            fixture_results=result_tuple,
            fixture_set_fingerprint=stable_fingerprint({
                "fixtures": [item.model_dump(mode="json") for item in fixtures]
            }),
            reason=reason,
            certified_by=operation.principal.principal_id,
        )
        digest = stable_fingerprint({
            "action": "extractor_certify",
            "release_id": release_id,
            "release_content_digest": release.content_digest,
            "fixtures": [item.model_dump(mode="json") for item in fixtures],
            "reason": reason,
        })
        return self._authority.save_extractor_certificate(
            certificate, operation_id=operation_id, command_digest=digest
        )

    def revoke_extractor_certificate(
        self,
        token: str,
        certificate_id: str,
        *,
        expected_fingerprint: str,
        reason: str,
        operation_id: str,
    ) -> ExtractorCertificateRevocation:
        operation = self._authorize(token, "extractor_revoke")[0]
        certificate = self._authority.get_extractor_certificate(operation.scope, certificate_id)
        if certificate is None:
            raise FoundationError("extractor_certificate_not_found", "Extractor certificate does not exist")
        if certificate.fingerprint != expected_fingerprint:
            raise FoundationError("extractor_certificate_stale", "Extractor certificate fingerprint changed")
        revocation = ExtractorCertificateRevocation(
            scope=operation.scope,
            certificate_id=certificate.certificate_id,
            certificate_fingerprint=certificate.fingerprint,
            release_id=certificate.release_id,
            release_content_digest=certificate.release_content_digest,
            reason=reason,
            revoked_by=operation.principal.principal_id,
        )
        digest = stable_fingerprint({
            "action": "extractor_revoke",
            "certificate_id": certificate_id,
            "expected_fingerprint": expected_fingerprint,
            "reason": reason,
        })
        return self._authority.revoke_extractor_certificate(
            revocation, operation_id=operation_id, command_digest=digest
        )

    def execute_extractor(
        self,
        token: str,
        release_id: str,
        *,
        event_id: str,
        schema_id: str,
        schema_version: int,
        input_payload: dict[str, Any],
        source_refs: tuple[ResourceRef, ...],
        operation_id: str,
    ) -> ExtractorExecutionReceipt:
        operation = self._authorize(token, "extractor_execute")[0]
        if operation_id != event_id:
            raise FoundationError(
                "extractor_idempotency_mismatch", "Operation ID must equal the extraction event ID"
            )
        existing = self._authority.get_extractor_execution(operation.scope, event_id)
        if existing is not None:
            certificate = self._authority.get_extractor_certificate(
                operation.scope, existing.certificate_id
            )
            release = self._required_release(operation.scope, existing.release_id)
            if certificate is None:
                raise FoundationError(
                    "extractor_execution_corrupt", "Execution certificate is missing"
                )
            replay_digest = stable_fingerprint({
                "action": "extractor_execute",
                "event_id": event_id,
                "release_id": release_id,
                "release_content_digest": release.content_digest,
                "certificate_fingerprint": certificate.fingerprint,
                "schema_id": schema_id,
                "schema_version": schema_version,
                "input_payload": input_payload,
                "source_refs": [ref.model_dump(mode="json") for ref in source_refs],
            })
            stored_digest = self._authority.get_extractor_execution_digest(
                operation.scope, event_id
            )
            if stored_digest != replay_digest:
                raise FoundationError(
                    "operation_id_conflict", "Extractor event ID has different content"
                )
            return existing
        if self._extractor_runtime is None:
            raise FoundationError("extractor_runtime_unavailable", "Extractor runtime is not configured")
        release = self._required_active_extractor(operation.scope, release_id)
        extractor = self._extractor_spec(release)
        if extractor.schema_id != schema_id or extractor.schema_version != schema_version:
            raise FoundationError("extractor_schema_mismatch", "Extractor schema binding differs")
        if any(ref.scope != operation.scope for ref in source_refs):
            raise FoundationError("extractor_source_scope_invalid", "Extractor sources are outside scope")
        certificate = self._authority.get_release_certificate(operation.scope, release_id)
        if certificate is None or certificate.release_content_digest != release.content_digest:
            raise FoundationError(
                "extractor_not_conformant", "Extractor needs a current conformant certificate"
            )
        status, value, input_bytes = self._extractor_runtime.execute_value(extractor, input_payload)
        digest = stable_fingerprint({
            "action": "extractor_execute",
            "event_id": event_id,
            "release_id": release_id,
            "release_content_digest": release.content_digest,
            "certificate_fingerprint": certificate.fingerprint,
            "schema_id": schema_id,
            "schema_version": schema_version,
            "input_payload": input_payload,
            "source_refs": [ref.model_dump(mode="json") for ref in source_refs],
        })
        proposal_ref = None
        public_status = status
        if status == "candidate":
            if self._extraction_sink is None:
                raise FoundationError(
                    "extractor_proposal_sink_unavailable", "No domain proposal sink is configured"
                )
            self._authorize(token, "extractor_execute")
            current_release = self._required_active_extractor(operation.scope, release_id)
            current_certificate = self._authority.get_release_certificate(operation.scope, release_id)
            if (
                current_release.content_digest != release.content_digest
                or current_certificate is None
                or current_certificate.fingerprint != certificate.fingerprint
            ):
                raise FoundationError("extractor_eligibility_changed", "Extractor eligibility changed")
            proposal_ref = self._extraction_sink.propose(
                token,
                extractor,
                event_id=event_id,
                value=value,
                source_refs=source_refs,
            )
            public_status = "proposal_created"
        receipt = ExtractorExecutionReceipt(
            event_id=event_id,
            scope=operation.scope,
            release_id=release_id,
            certificate_id=certificate.certificate_id,
            status=public_status,
            input_bytes=input_bytes,
            candidate_count=1 if public_status == "proposal_created" else 0,
            value_fingerprint=(
                stable_fingerprint({"value": value}) if status in {"candidate", "invalid"} else None
            ),
            proposal_ref=proposal_ref,
        )
        return self._authority.save_extractor_execution(
            receipt, operation_id=operation_id, command_digest=digest
        )

    def stage_release(
        self, token: str, *, kind: str, artifact_id: str, version: int,
        payload: dict, operation_id: str,
    ) -> ManagedRelease:
        operation = self._authorize(token, "release_stage")[0]
        release = ManagedRelease(
            scope=operation.scope, kind=kind, artifact_id=artifact_id, version=version,
            payload=payload, staged_by=operation.principal.principal_id,
        )
        effect = self._effect(release.kind)
        effect.validate(release)
        digest = stable_fingerprint({
            "action": "release_stage", "scope": operation.scope.model_dump(mode="json"),
            "kind": kind, "artifact_id": artifact_id, "version": version, "payload": payload,
        })
        return self._authority.stage_release(
            release, operation_id=operation_id, command_digest=digest
        )

    def approve_release(
        self, token: str, release_id: str, *, expected_revision: int, operation_id: str
    ) -> ManagedRelease:
        operation = self._authorize(token, "release_approve")[0]
        release = self._required_release(operation.scope, release_id)
        if self._policy.require_distinct_approver and release.staged_by == operation.principal.principal_id:
            raise FoundationError("management_approver_not_distinct", "Release needs a distinct approver")
        digest = stable_fingerprint({
            "action": "release_approve", "release_id": release_id,
            "expected_revision": expected_revision,
        })
        return self._authority.approve_release(
            operation.scope, release_id, expected_revision=expected_revision,
            approved_by=operation.principal.principal_id,
            operation_id=operation_id, command_digest=digest,
        )

    def activate_release(
        self, token: str, release_id: str, *, expected_revision: int, operation_id: str
    ) -> ManagedRelease:
        operation = self._authorize(token, "release_activate")[0]
        release = self._required_release(operation.scope, release_id)
        command_digest = stable_fingerprint({
            "action": "release_activate", "release_id": release_id,
            "content_digest": release.content_digest,
        })
        if self._authority.get_receipt(operation.scope, operation_id) is not None:
            return self._authority.activate_release(
                operation.scope, release_id, expected_revision=release.revision,
                activated_by=operation.principal.principal_id,
                effect_receipt=release.effect_receipt, operation_id=operation_id,
                command_digest=command_digest,
            )
        if release.revision != expected_revision or release.status != "approved":
            raise FoundationError("management_release_stale", "Only the expected approved release can activate")
        effect = self._effect(release.kind)
        try:
            effect.validate(release)
            receipt = effect.activate(release, operation_id=operation_id)
            self._authorize(token, "release_activate")
            current = self._required_release(operation.scope, release_id)
            return self._authority.activate_release(
                operation.scope, release_id, expected_revision=current.revision,
                activated_by=operation.principal.principal_id, effect_receipt=receipt,
                operation_id=operation_id,
                command_digest=command_digest,
            )
        except FoundationError as error:
            current = self._required_release(operation.scope, release_id)
            if current.status == "approved":
                self._authority.fail_release(
                    operation.scope, release_id, expected_revision=current.revision,
                    error_code=error.code,
                )
            raise

    def get_release(self, token: str, release_id: str) -> ManagedRelease:
        operation = self._authorize(token, "read")[0]
        return self._required_release(operation.scope, release_id)

    def get_extractor_certificate(
        self, token: str, certificate_id: str
    ) -> ExtractorCertificate:
        operation = self._authorize(token, "read")[0]
        certificate = self._authority.get_extractor_certificate(
            operation.scope, certificate_id
        )
        if certificate is None:
            raise FoundationError(
                "extractor_certificate_not_found", "Extractor certificate does not exist"
            )
        return certificate

    def get_extractor_execution(
        self, token: str, event_id: str
    ) -> ExtractorExecutionReceipt:
        operation = self._authorize(token, "read")[0]
        receipt = self._authority.get_extractor_execution(operation.scope, event_id)
        if receipt is None:
            raise FoundationError(
                "extractor_execution_not_found", "Extractor execution does not exist"
            )
        return receipt

    def list_releases(self, token: str, *, kind=None, status=None, limit: int = 20):
        operation, grant, _trusted = self._authorize(token, "read")
        self._check_limit(grant, limit)
        return {"items": self._authority.list_releases(
            operation.scope, kind=kind, status=status, limit=limit
        )}

    def get_active_release(self, token: str, *, kind: str, artifact_id: str) -> ManagedRelease:
        operation = self._authorize(token, "read")[0]
        release = self._authority.get_active_release(operation.scope, kind, artifact_id)
        if release is None:
            raise FoundationError("management_active_release_not_found", "Active release does not exist")
        return release

    def begin_index(
        self, token: str, *, domain: str, execution_profile_fingerprint: str,
        source_revision: str, artifact_path: str, artifact_digest: str, operation_id: str,
    ) -> IndexGeneration:
        operation = self._authorize(token, "index_build")[0]
        generation = IndexGeneration(
            scope=operation.scope, domain=domain,
            execution_profile_fingerprint=execution_profile_fingerprint,
            source_revision=source_revision, artifact_path=artifact_path,
            artifact_digest=artifact_digest, planned_by=operation.principal.principal_id,
        )
        digest = stable_fingerprint({
            "action": "index_begin", "scope": operation.scope.model_dump(mode="json"),
            "domain": domain, "profile": execution_profile_fingerprint,
            "source_revision": source_revision, "artifact_path": artifact_path,
            "artifact_digest": artifact_digest,
        })
        return self._authority.begin_index(
            generation, operation_id=operation_id, command_digest=digest
        )

    def verify_index(
        self, token: str, generation_id: str, *, expected_revision: int, operation_id: str
    ) -> IndexGeneration:
        operation = self._authorize(token, "index_verify")[0]
        generation = self._required_index(operation.scope, generation_id)
        if generation.revision != expected_revision or generation.status != "building":
            raise FoundationError("index_generation_stale", "Expected building generation is required")
        try:
            receipt = self._index_effect.verify(generation)
            return self._authority.verify_index(
                operation.scope, generation_id, expected_revision=expected_revision,
                effect_receipt=receipt, operation_id=operation_id,
                command_digest=stable_fingerprint({
                    "action": "index_verify", "generation_id": generation_id,
                    "artifact_digest": generation.artifact_digest,
                }),
            )
        except FoundationError as error:
            self._authority.fail_index(
                operation.scope, generation_id, expected_revision=expected_revision,
                error_code=error.code,
            )
            raise

    def activate_index(
        self, token: str, generation_id: str, *, expected_revision: int, operation_id: str
    ) -> IndexGeneration:
        operation = self._authorize(token, "index_activate")[0]
        generation = self._required_index(operation.scope, generation_id)
        command_digest = stable_fingerprint({
            "action": "index_activate", "generation_id": generation_id,
            "artifact_digest": generation.artifact_digest,
        })
        if self._authority.get_receipt(operation.scope, operation_id) is not None:
            return self._authority.activate_index(
                operation.scope, generation_id, expected_revision=generation.revision,
                activated_by=operation.principal.principal_id,
                effect_receipt=generation.effect_receipt, operation_id=operation_id,
                command_digest=command_digest,
            )
        if generation.revision != expected_revision or generation.status != "verified":
            raise FoundationError("index_generation_stale", "Expected verified generation is required")
        try:
            receipt = self._index_effect.activate(generation, operation_id=operation_id)
            self._authorize(token, "index_activate")
            current = self._required_index(operation.scope, generation_id)
            return self._authority.activate_index(
                operation.scope, generation_id, expected_revision=current.revision,
                activated_by=operation.principal.principal_id, effect_receipt=receipt,
                operation_id=operation_id,
                command_digest=command_digest,
            )
        except FoundationError as error:
            current = self._required_index(operation.scope, generation_id)
            if current.status == "verified":
                self._authority.fail_index(
                    operation.scope, generation_id, expected_revision=current.revision,
                    error_code=error.code,
                )
            raise

    def get_index(self, token: str, generation_id: str) -> IndexGeneration:
        operation = self._authorize(token, "read")[0]
        return self._required_index(operation.scope, generation_id)

    def list_indexes(self, token: str, *, domain=None, status=None, limit: int = 20):
        operation, grant, _trusted = self._authorize(token, "read")
        self._check_limit(grant, limit)
        return {"items": self._authority.list_indexes(
            operation.scope, domain=domain, status=status, limit=limit
        )}

    def get_active_index(self, token: str, domain: str) -> IndexGeneration:
        operation = self._authorize(token, "read")[0]
        generation = self._authority.get_active_index(operation.scope, domain)
        if generation is None:
            raise FoundationError("index_generation_active_not_found", "Active index generation does not exist")
        return generation

    def record_evaluation(
        self, token: str, *, subject_ref: str, cases: int, passed: int,
        metrics: dict[str, float], source_digest: str, operation_id: str,
    ) -> EvaluationReport:
        operation = self._authorize(token, "evaluate")[0]
        if passed > cases:
            raise FoundationError("evaluation_result_invalid", "Passed cases cannot exceed cases")
        report = EvaluationReport(
            scope=operation.scope, subject_ref=subject_ref, cases=cases, passed=passed,
            metrics=metrics, source_digest=source_digest,
            recorded_by=operation.principal.principal_id,
        )
        return self._authority.record_evaluation(
            report, operation_id=operation_id,
            command_digest=stable_fingerprint({
                "action": "evaluate", "scope": operation.scope.model_dump(mode="json"),
                "subject_ref": subject_ref, "cases": cases, "passed": passed,
                "metrics": metrics, "source_digest": source_digest,
            }),
        )

    def get_evaluation(self, token: str, evaluation_id: str) -> EvaluationReport:
        operation = self._authorize(token, "read")[0]
        report = self._authority.get_evaluation(operation.scope, evaluation_id)
        if report is None:
            raise FoundationError("evaluation_not_found", "Evaluation does not exist")
        return report

    def list_evaluations(self, token: str, *, subject_ref=None, limit: int = 20):
        operation, grant, _trusted = self._authorize(token, "read")
        self._check_limit(grant, limit)
        return {"items": self._authority.list_evaluations(
            operation.scope, subject_ref=subject_ref, limit=limit
        )}

    def propose_transfer(
        self, token: str, *, kind: str, destination: str,
        resource_refs: tuple[ResourceRef, ...], payload: dict, operation_id: str,
    ) -> TransferPlan:
        operation = self._authorize(token, "transfer_plan")[0]
        plan = TransferPlan(
            scope=operation.scope, kind=kind, destination=destination,
            resource_refs=resource_refs, payload=payload,
            planned_by=operation.principal.principal_id,
        )
        return self._authority.propose_transfer(
            plan, operation_id=operation_id,
            command_digest=stable_fingerprint({
                "action": "transfer_plan", "scope": operation.scope.model_dump(mode="json"),
                "kind": kind, "destination": destination,
                "resource_refs": [ref.model_dump(mode="json") for ref in resource_refs],
                "payload": payload,
            }),
        )

    def approve_transfer(
        self, token: str, transfer_id: str, *, expected_revision: int, operation_id: str
    ) -> TransferPlan:
        operation = self._authorize(token, "transfer_approve")[0]
        plan = self._required_transfer(operation.scope, transfer_id)
        if self._policy.require_distinct_approver and plan.planned_by == operation.principal.principal_id:
            raise FoundationError("management_approver_not_distinct", "Transfer needs a distinct approver")
        return self._authority.approve_transfer(
            operation.scope, transfer_id, expected_revision=expected_revision,
            approved_by=operation.principal.principal_id, operation_id=operation_id,
            command_digest=stable_fingerprint({
                "action": "transfer_approve", "transfer_id": transfer_id,
                "expected_revision": expected_revision,
            }),
        )

    def execute_transfer(
        self, token: str, transfer_id: str, *, expected_revision: int, operation_id: str
    ) -> TransferPlan:
        operation = self._authorize(token, "transfer_execute")[0]
        plan = self._required_transfer(operation.scope, transfer_id)
        command_digest = stable_fingerprint({
            "action": "transfer_execute", "transfer_id": transfer_id,
            "payload_digest": plan.payload_digest,
        })
        if self._authority.get_receipt(operation.scope, operation_id) is not None:
            return self._authority.complete_transfer(
                operation.scope, transfer_id, expected_revision=plan.revision,
                executed_by=operation.principal.principal_id,
                effect_receipt=plan.effect_receipt, operation_id=operation_id,
                command_digest=command_digest,
            )
        if plan.revision != expected_revision or plan.status != "approved":
            raise FoundationError("transfer_stale", "Expected approved transfer is required")
        try:
            receipt = self._transfer_effect.execute(plan, operation_id=operation_id)
            self._authorize(token, "transfer_execute")
            current = self._required_transfer(operation.scope, transfer_id)
            return self._authority.complete_transfer(
                operation.scope, transfer_id, expected_revision=current.revision,
                executed_by=operation.principal.principal_id, effect_receipt=receipt,
                operation_id=operation_id,
                command_digest=command_digest,
            )
        except FoundationError as error:
            current = self._required_transfer(operation.scope, transfer_id)
            if current.status == "approved":
                self._authority.fail_transfer(
                    operation.scope, transfer_id, expected_revision=current.revision,
                    error_code=error.code,
                )
            raise

    def get_transfer(self, token: str, transfer_id: str) -> TransferPlan:
        operation = self._authorize(token, "read")[0]
        return self._required_transfer(operation.scope, transfer_id)

    def list_transfers(self, token: str, *, kind=None, status=None, limit: int = 20):
        operation, grant, _trusted = self._authorize(token, "read")
        self._check_limit(grant, limit)
        return {"items": self._authority.list_transfers(
            operation.scope, kind=kind, status=status, limit=limit
        )}

    def diagnostics(self, token: str) -> dict:
        operation = self._authorize(token, "diagnose")[0]
        return self._authority.diagnostics(operation.scope)

    def get_operation_receipt(self, token: str, operation_id: str):
        operation = self._authorize(token, "read")[0]
        receipt = self._authority.get_receipt(operation.scope, operation_id)
        if receipt is None:
            raise FoundationError("management_receipt_not_found", "Operation receipt does not exist")
        return receipt

    def authorize(self, token: str, action: str):
        return self._authorize(token, action)[0]

    def close(self) -> None:
        self._authority.close()

    def _authorize(self, token: str, action: str):
        return self._access.authorize_token(
            token, domain="management", action=action,
            policy_id=self._policy.policy_id, policy_version=self._policy.version,
        )

    def _effect(self, kind: str) -> ReleaseEffect:
        try:
            return self._release_effects[kind]
        except KeyError as error:
            raise FoundationError("management_release_kind_unavailable", "Release kind is not configured") from error

    @staticmethod
    def _check_limit(grant, limit: int) -> None:
        maximum = min(grant.limits.get("max_items", 1000), 100)
        if not 1 <= limit <= maximum:
            raise FoundationError("management_budget_exceeded", "Management list limit exceeds budget")

    def _required_release(self, scope, release_id):
        value = self._authority.get_release(scope, release_id)
        if value is None:
            raise FoundationError("management_release_not_found", "Release does not exist")
        return value

    def _required_active_extractor(self, scope, release_id: str) -> ManagedRelease:
        release = self._required_release(scope, release_id)
        if release.kind != "extractor" or release.status != "active":
            raise FoundationError(
                "extractor_release_inactive", "An active extractor release is required"
            )
        active = self._authority.get_active_release(
            scope, release.kind, release.artifact_id
        )
        if active is None or active.release_id != release.release_id:
            raise FoundationError(
                "extractor_release_inactive", "Extractor release is no longer active"
            )
        return release

    @staticmethod
    def _extractor_spec(release: ManagedRelease) -> ExtractorSpec:
        try:
            return ExtractorSpec(
                release_id=release.release_id,
                scope=release.scope,
                content_digest=release.content_digest,
                schema_id=release.payload["schema_id"],
                schema_version=release.payload["schema_version"],
                implementation_id=release.payload["implementation_id"],
                implementation_digest=release.payload["implementation_digest"],
                max_input_bytes=release.payload["max_input_bytes"],
                configuration=release.payload.get("configuration", {}),
                output_schema=release.payload["output_schema"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FoundationError(
                "extractor_configuration_invalid",
                "Extractor release has an invalid execution contract",
            ) from error

    def _required_index(self, scope, generation_id):
        value = self._authority.get_index(scope, generation_id)
        if value is None:
            raise FoundationError("index_generation_not_found", "Index generation does not exist")
        return value

    def _required_transfer(self, scope, transfer_id):
        value = self._authority.get_transfer(scope, transfer_id)
        if value is None:
            raise FoundationError("transfer_not_found", "Transfer does not exist")
        return value
