"""Context providers. Add one by subclassing ContextProvider and registering it
in registry.PROVIDERS (order = detection precedence)."""

from .base import ContextProvider
from .indexed import IndexedProvider
from .plain import PlainProvider

__all__ = ["ContextProvider", "IndexedProvider", "PlainProvider"]
