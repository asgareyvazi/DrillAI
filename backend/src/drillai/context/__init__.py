"""Engineering context: the single common source of well truth for every consumer."""

from drillai.context.builder import (
    ContextBuilder,
    SectionProvider,
    default_context_builder,
    install_default_providers,
    register_section_provider,
    registered_section_keys,
)
from drillai.context.live_providers import install_live_providers
from drillai.context.model import (
    ContextBundle,
    ContextItem,
    ContextPurpose,
    ContextRequest,
    ContextScope,
    ContextSection,
    UnitSystemName,
)

# The live sections are registered after the domain sections they sit beside: reading the current state of
# a well is not the same question as reading what is on file about it, and both belong in the bundle.
install_live_providers()

__all__ = [
    "ContextBuilder",
    "ContextBundle",
    "ContextItem",
    "ContextPurpose",
    "ContextRequest",
    "ContextScope",
    "ContextSection",
    "SectionProvider",
    "UnitSystemName",
    "default_context_builder",
    "install_default_providers",
    "install_live_providers",
    "register_section_provider",
    "registered_section_keys",
]
