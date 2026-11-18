from __future__ import annotations

import hashlib
import math
import re
import unicodedata

from agent_foundation.kernel.errors import FoundationError


class DeterministicHashVectorizer:
    """Dependency-free lexical baseline for real, content-dependent recall.

    This establishes the vector index contract without making a model-quality
    claim. Hosts can inject another implementation without changing domains or
    Qdrant storage code.
    """

    def __init__(self, *, dimensions: int = 256) -> None:
        if not 16 <= dimensions <= 4096:
            raise FoundationError(
                "vectorizer_configuration_invalid",
                "Vector dimensions must be between 16 and 4096",
            )
        self.dimensions = dimensions

    def vectorize(self, text: str) -> tuple[float, ...]:
        normalized = unicodedata.normalize("NFKC", text).casefold().strip()
        if not normalized:
            raise FoundationError(
                "projection_text_empty", "Candidate projection text cannot be empty"
            )
        features: list[str] = []
        for token in re.findall(r"\w+", normalized, flags=re.UNICODE):
            features.append(f"w:{token}")
            compact = f"^{token}$"
            features.extend(
                f"c{width}:{compact[index:index + width]}"
                for width in (2, 3)
                for index in range(max(0, len(compact) - width + 1))
            )
        if not features:
            features = [f"u:{character}" for character in normalized if not character.isspace()]
        values = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            values[index] += sign
        magnitude = math.sqrt(sum(value * value for value in values))
        if magnitude == 0:
            raise FoundationError(
                "projection_vector_empty", "Candidate projection produced an empty vector"
            )
        return tuple(value / magnitude for value in values)
