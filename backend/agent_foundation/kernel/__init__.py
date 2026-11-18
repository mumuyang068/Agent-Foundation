"""Transport-neutral identity, scope, resource and error contracts."""

from .errors import FoundationError
from .primitives import ContractModel, DomainName, OperationContext, Principal, ResourceRef, ScopeRef
from .ports import DocumentRepository, ExecutionRecordRepository, VersionedDocument

__all__ = [
    "ContractModel",
    "DocumentRepository",
    "ExecutionRecordRepository",
    "FoundationError",
    "DomainName",
    "OperationContext",
    "Principal",
    "ResourceRef",
    "ScopeRef",
    "VersionedDocument",
]
