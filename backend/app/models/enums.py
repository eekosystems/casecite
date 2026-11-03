"""Python enums used across database models."""

from enum import Enum as PyEnum

# UserRole's canonical definition lives in app.services.auth (the auth domain
# owns the role model). Re-exported here so model code and tests keep importing
# it from app.models.enums without a second, drift-prone definition.
from app.services.auth import UserRole  # noqa: F401


class DocumentStatus(str, PyEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    # A delete was requested but a step (search index, derived rows, stored
    # file) failed. The document stays listed with the error until a retry
    # completes; metadata["deletion"] records which steps are done.
    DELETE_FAILED = "delete_failed"
