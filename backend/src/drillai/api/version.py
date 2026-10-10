"""Public contract version of the HTTP surface.

Kept in its own module so routers can report it without importing the application factory
(which would be a circular import).
"""

from __future__ import annotations

#: Bumped when the HTTP contract changes in a way a client must know about.
API_VERSION = "1.0.0"

__all__ = ["API_VERSION"]
