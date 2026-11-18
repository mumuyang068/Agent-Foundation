from __future__ import annotations

from agent_foundation.kernel.errors import FoundationError

from .contracts import ExecutionProfile
from .ports import CandidateAlgorithm


class AlgorithmCatalog:
    def __init__(self) -> None:
        self._algorithms: dict[tuple[str, str], CandidateAlgorithm] = {}

    def register(self, algorithm: CandidateAlgorithm) -> None:
        key = (algorithm.descriptor.algorithm_id, algorithm.descriptor.implementation_version)
        current = self._algorithms.get(key)
        if current is not None and current.descriptor != algorithm.descriptor:
            raise FoundationError("algorithm_conflict", "Algorithm identity has different content")
        self._algorithms[key] = algorithm

    def resolve(self, profile: ExecutionProfile) -> CandidateAlgorithm:
        algorithm = self._algorithms.get((profile.algorithm_id, profile.implementation_version))
        if algorithm is None or profile.domain not in algorithm.descriptor.domains:
            raise FoundationError("algorithm_unavailable", "Execution Profile references an unavailable algorithm")
        return algorithm


class ExecutionProfileCatalog:
    def __init__(self) -> None:
        self._profiles: dict[tuple[str, int], ExecutionProfile] = {}

    def put(self, profile: ExecutionProfile) -> ExecutionProfile:
        key = (profile.profile_id, profile.version)
        current = self._profiles.get(key)
        if current is not None and current != profile:
            raise FoundationError("execution_profile_conflict", "Execution Profile identity has different content")
        self._profiles[key] = profile
        return profile

    def get(self, profile_id: str, version: int) -> ExecutionProfile:
        try:
            return self._profiles[(profile_id, version)]
        except KeyError as error:
            raise FoundationError("execution_profile_not_found", "Execution Profile does not exist") from error
