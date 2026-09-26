"""The platform action catalogue.

Every capability that can be *invoked* (by a human, a workflow node, an agent tool or an
integration) is registered here with its action level and required permission. Registration is
the contract: an unregistered action resolves to ``L4`` at authorization time, so a missing
entry fails closed rather than silently granting autonomy.

Domain packs register their own actions (``register_action``) when they are loaded; this module
covers the platform-level capabilities every deployment has.
"""

from __future__ import annotations

from drillai.security.actions import ActionLevel, ActionRef, register_action, registered_actions

__all__ = ["PLATFORM_ACTIONS", "ensure_platform_actions_registered"]

PLATFORM_ACTIONS: tuple[tuple[str, ActionLevel, str, str], ...] = (
    # --- observe -------------------------------------------------------------------------
    ("context.read", ActionLevel.OBSERVE, "Assemble and read the engineering context.", "context.read"),
    ("twin.read", ActionLevel.OBSERVE, "Read digital well twin state, aspects and snapshots.", "twin.read"),
    ("document.read", ActionLevel.OBSERVE, "Read documents, pages, regions and extracted records.", "document.read"),
    ("evidence.read", ActionLevel.OBSERVE, "Traverse evidence and provenance chains.", "evidence.read"),
    ("run.read", ActionLevel.OBSERVE, "Inspect workflow runs, node runs and events.", "workflow.read"),
    ("rag.search", ActionLevel.OBSERVE, "Search the corpus (lexical, semantic, metadata and SQL modes).", "document.read"),
    # --- advise --------------------------------------------------------------------------
    ("recommendation.create", ActionLevel.ADVISE, "Produce a recommendation with rationale and evidence.", "recommendation.create"),
    ("engine.run", ActionLevel.DRAFT, "Execute an engineering engine and persist its run.", "engine.run"),
    ("offset.analyze", ActionLevel.DRAFT, "Run offset similarity/comparison analysis.", "offset.analyze"),
    ("optimization.run", ActionLevel.DRAFT, "Run an optimization / what-if study.", "optimization.run"),
    ("twin.update", ActionLevel.DRAFT, "Write a computed aspect or snapshot into the twin.", "twin.update"),
    # --- draft ---------------------------------------------------------------------------
    ("artifact.draft", ActionLevel.DRAFT, "Create or edit a candidate (non-approved) artefact version.", "artifact.draft"),
    ("project.create", ActionLevel.DRAFT, "Create a project (the top-level asset container).", "project.write"),
    ("well.create", ActionLevel.DRAFT, "Create a well, wellbore or section record.", "well.create"),
    ("document.ingest", ActionLevel.DRAFT, "Upload and ingest a document into the data fabric.", "document.write"),
    ("workflow.draft", ActionLevel.DRAFT, "Draft or edit a workflow definition.", "workflow.draft"),
    ("scenario.create", ActionLevel.DRAFT, "Create a what-if scenario or twin snapshot.", "scenario.create"),
    # --- propose -------------------------------------------------------------------------
    ("workflow.publish", ActionLevel.PROPOSE, "Publish a workflow version so it can be triggered.", "workflow.publish"),
    ("artifact.submit", ActionLevel.PROPOSE, "Submit an artefact version for review/approval.", "action:artifact.submit"),
    ("requirement.verify", ActionLevel.PROPOSE, "Record verification of a requirement with evidence.", "requirement.verify"),
    ("integrity.verify", ActionLevel.PROPOSE, "Record a well-integrity verification result.", "action:integrity.verify"),
    ("change.propose", ActionLevel.PROPOSE, "Open a change request against a plan, schedule or setting.", "action:change.propose"),
    # --- execute with approval -------------------------------------------------------------
    ("artifact.approve", ActionLevel.EXECUTE_WITH_APPROVAL, "Approve an artefact version (human authority).", "action:artifact.approve"),
    # Starting a run is a draft-level act: the definition is versioned and every node carries its
    # own level, so side effects are gated where they happen (an L4 node suspends the run and
    # raises an approval request naming exactly what will be done). Gating the run itself at L4
    # would demand approval for read-only analyses and could not name the action being approved.
    ("workflow.run", ActionLevel.DRAFT, "Start a run of a saved workflow definition.", "workflow.run"),
    (
        "approval.decide",
        ActionLevel.EXECUTE_WITH_APPROVAL,
        "Decide a pending approval request (the decision is itself the human gate).",
        "action:approval.decide",
    ),
    ("message.send", ActionLevel.EXECUTE_WITH_APPROVAL, "Send a message through an integration (email/Telegram/WhatsApp).", "action:message.send"),
    ("export.create", ActionLevel.EXECUTE_WITH_APPROVAL, "Export data or a document out of the platform.", "action:export.create"),
    # --- authorized automation (off unless an org enables the envelope) --------------------
    ("monitor.notify", ActionLevel.AUTHORIZED_AUTOMATION, "Notify on a validated condition inside an agreed envelope.", "action:monitor.notify"),
    ("report.distribute", ActionLevel.AUTHORIZED_AUTOMATION, "Distribute a generated report to the agreed distribution list.", "action:report.distribute"),
    ("rig.write", ActionLevel.AUTHORIZED_AUTOMATION, "Write a setpoint to a rig/edge system inside an approved envelope.", "action:rig.write"),
)

_REGISTERED = False


def ensure_platform_actions_registered() -> dict[str, ActionRef]:
    """Register the platform catalogue once (idempotent)."""
    global _REGISTERED
    if not _REGISTERED:
        for key, level, description, permission in PLATFORM_ACTIONS:
            register_action(
                key,
                level,
                description,
                permission=permission,
                # Deciding an approval must not require a prior approval to exist.
                self_authorizing=key == "approval.decide",
                replace=True,
            )
        _REGISTERED = True
    return registered_actions()


ensure_platform_actions_registered()
