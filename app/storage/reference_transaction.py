"""Commit evidence for private run references; legacy archives remain readable."""
from app.domain.errors import ExecutionError


def require_committed_reference(group) -> None:
    version = group.attrs.get("reference_transaction_version")
    if version is not None and version != 1:
        raise ExecutionError("Unsupported reference transaction version.")
    if not bool(group.attrs.get("complete", version is None)):
        raise ExecutionError("Reference is not committed.")
