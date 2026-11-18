"""Transport-only Agent Foundation client."""

from .client import FoundationClient, FoundationClientError
from .routes import OPERATIONS

__all__ = ["FoundationClient", "FoundationClientError", "OPERATIONS"]
__version__ = "1.0.0a4"
