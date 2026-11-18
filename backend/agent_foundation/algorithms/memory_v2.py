from __future__ import annotations

from agent_foundation.algorithms.lexical_v2 import lexical_terms
from agent_foundation.modules.memory.contracts import MemoryAlgorithmIdentity, MemoryEntry


class LexicalMemoryCandidates:
    descriptor = MemoryAlgorithmIdentity(
        algorithm_id="memory.lexical-v2",
        implementation_version="1",
    )

    def rank(
        self,
        entries: tuple[MemoryEntry, ...],
        *,
        query: str,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        terms = lexical_terms(query)
        ranked = []
        for entry in entries:
            words = lexical_terms(entry.content)
            score = len(terms & words) / max(len(terms), 1)
            if score:
                ranked.append((entry.memory_id, score))
        return tuple(sorted(ranked, key=lambda item: (-item[1], item[0]))[:limit])


class RecencyMemoryCandidates:
    descriptor = MemoryAlgorithmIdentity(
        algorithm_id="memory.recency-v2",
        implementation_version="1",
    )

    def rank(
        self,
        entries: tuple[MemoryEntry, ...],
        *,
        query: str,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        del query
        ordered = sorted(entries, key=lambda item: (item.updated_at, item.memory_id), reverse=True)
        return tuple((item.memory_id, 1.0 / index) for index, item in enumerate(ordered[:limit], start=1))
