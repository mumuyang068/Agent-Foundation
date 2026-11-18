from __future__ import annotations

from agent_foundation.modules.context.contracts import (
    ContextAlgorithmIdentity,
    ContextCandidate,
)


class ScoreContextCombination:
    descriptor = ContextAlgorithmIdentity(
        algorithm_id="context.score-v2", implementation_version="1"
    )

    def rank(self, candidates, *, query: str, limit: int):
        del query
        ranked = sorted(
            candidates,
            key=lambda item: (-item.score, item.ref.domain, item.ref.resource_id, item.ref.revision or ""),
        )
        return tuple((item.ref, item.score) for item in ranked[:limit])


class InterleavedContextCombination:
    descriptor = ContextAlgorithmIdentity(
        algorithm_id="context.interleaved-v2", implementation_version="1"
    )

    def rank(self, candidates: tuple[ContextCandidate, ...], *, query: str, limit: int):
        del query
        buckets: dict[str, list[ContextCandidate]] = {}
        for item in sorted(candidates, key=lambda value: (-value.score, value.ref.resource_id)):
            buckets.setdefault(item.ref.domain, []).append(item)
        output: list[tuple] = []
        while buckets and len(output) < limit:
            for domain in sorted(tuple(buckets)):
                values = buckets[domain]
                item = values.pop(0)
                output.append((item.ref, item.score))
                if not values:
                    del buckets[domain]
                if len(output) == limit:
                    break
        return tuple(output)
