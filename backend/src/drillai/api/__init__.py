"""HTTP/WebSocket boundary.

The API layer is deliberately thin: it authenticates, authorizes, validates transport-level
input, and delegates to the same domain services the workflow runtime and the AI tools use.
No engineering logic lives here — if a rule matters, it belongs in a service or an engine so
that every entry point (HTTP, workflow, agent, CLI) is governed identically.
"""

from drillai.api.app import create_app

__all__ = ["create_app"]
