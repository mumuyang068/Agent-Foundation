from __future__ import annotations

from agent_foundation.algorithms.lexical_v2 import lexical_terms
from agent_foundation.modules.evidence.contracts import EvidenceAlgorithmIdentity, EvidenceRevision


class LexicalEvidenceCandidates:
    descriptor = EvidenceAlgorithmIdentity(
        algorithm_id="evidence.lexical-v2",
        implementation_version="1",
    )

    def rank(
        self,
        revisions: tuple[EvidenceRevision, ...],
        *,
        query: str,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        terms = lexical_terms(query)
        ranked = []
        for revision in revisions:
            words = lexical_terms(revision.content)
            score = len(terms & words) / max(len(terms), 1)
            if score:
                ranked.append((revision.revision_id, score))
        return tuple(sorted(ranked, key=lambda item: (-item[1], item[0]))[:limit])
