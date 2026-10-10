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
    (
        "document.read",
        ActionLevel.OBSERVE,
        "Read documents, pages, regions and extracted records.",
        "document.read",
    ),
    ("evidence.read", ActionLevel.OBSERVE, "Traverse evidence and provenance chains.", "evidence.read"),
    (
        "operation.read",
        ActionLevel.OBSERVE,
        "Read operations: the work the well was drilled with.",
        "operation.read",
    ),
    ("event.read", ActionLevel.OBSERVE, "Read events: what happened on a well.", "operation.read"),
    (
        "timeseries.read",
        ActionLevel.OBSERVE,
        "Read telemetry channels, windows and latest values.",
        "timeseries.read",
    ),
    (
        "connector.read",
        ActionLevel.OBSERVE,
        "Read telemetry connectors, health, channel mappings and run history.",
        "connector.read",
    ),
    ("alert.read", ActionLevel.OBSERVE, "Read alerts and their evidence.", "alert.read"),
    (
        "live.read",
        ActionLevel.OBSERVE,
        "Subscribe to the live operational feed for a well.",
        "live.read",
    ),
    ("run.read", ActionLevel.OBSERVE, "Inspect workflow runs, node runs and events.", "workflow.read"),
    (
        "rag.search",
        ActionLevel.OBSERVE,
        "Search the corpus (lexical, semantic, metadata and SQL modes).",
        "document.read",
    ),
    # --- advise --------------------------------------------------------------------------
    (
        "recommendation.create",
        ActionLevel.ADVISE,
        "Produce a recommendation with rationale and evidence.",
        "recommendation.create",
    ),
    ("engine.run", ActionLevel.DRAFT, "Execute an engineering engine and persist its run.", "engine.run"),
    ("offset.analyze", ActionLevel.DRAFT, "Run offset similarity/comparison analysis.", "offset.analyze"),
    ("optimization.run", ActionLevel.DRAFT, "Run an optimization / what-if study.", "optimization.run"),
    ("twin.update", ActionLevel.DRAFT, "Write a computed aspect or snapshot into the twin.", "twin.update"),
    # --- draft ---------------------------------------------------------------------------
    (
        "artifact.draft",
        ActionLevel.DRAFT,
        "Create or edit a candidate (non-approved) artefact version.",
        "artifact.draft",
    ),
    (
        "project.create",
        ActionLevel.DRAFT,
        "Create a project (the top-level asset container).",
        "project.write",
    ),
    (
        "project.update",
        ActionLevel.DRAFT,
        "Edit a project's own master data (name, code, operator, phase).",
        "project.write",
    ),
    ("well.create", ActionLevel.DRAFT, "Create a well (the asset root of the hierarchy).", "well.create"),
    # Master-data editing is its own capability, separate from creating: correcting a well's name,
    # operator or rig, or re-scoping it to another field, is a different act from bringing a well into
    # existence, and an organization may wish to grant one without the other.
    (
        "well.update",
        ActionLevel.DRAFT,
        "Edit well master data (rename, re-scope, re-datum, identifiers).",
        "well.update",
    ),
    (
        "well.assign_rig",
        ActionLevel.DRAFT,
        "Attach or detach the rig working a well, keeping the rig's own pointer true.",
        "well.update",
    ),
    # A lifecycle transition moves a well between operational states (spudded, producing, suspended,
    # abandoned, plugged). It is draft-level like the rest of master data, but carries its own
    # permission because state changes gate other automation and are worth withholding on their own.
    ("well.lifecycle", ActionLevel.DRAFT, "Transition a well between lifecycle states.", "well.lifecycle"),
    # One entry per level of the hierarchy rather than one shared key: the ledger names the act that
    # happened ("who created this wellbore?"), and an audit trail whose entries cannot be told apart
    # is not evidence of anything.
    (
        "wellbore.create",
        ActionLevel.DRAFT,
        "Create a wellbore (a hole; a sidetrack names its parent).",
        "well.create",
    ),
    (
        "wellbore.update",
        ActionLevel.DRAFT,
        "Edit wellbore master data (name, purpose, lineage, TDs, datum).",
        "well.update",
    ),
    (
        "wellbore.activate",
        ActionLevel.DRAFT,
        "Make one wellbore the hole currently being drilled.",
        "well.lifecycle",
    ),
    (
        "wellbore.lifecycle",
        ActionLevel.DRAFT,
        "Transition a wellbore between lifecycle states.",
        "well.lifecycle",
    ),
    ("section.create", ActionLevel.DRAFT, "Create a hole section within a wellbore.", "well.create"),
    (
        "section.update",
        ActionLevel.DRAFT,
        "Edit hole-section master data: the plan, or a recorded as-drilled measurement.",
        "well.update",
    ),
    (
        "section.lifecycle",
        ActionLevel.DRAFT,
        "Transition a hole section between lifecycle states.",
        "well.lifecycle",
    ),
    ("field.create", ActionLevel.DRAFT, "Create a field (master data scoped to a project).", "field.create"),
    (
        "field.update",
        ActionLevel.DRAFT,
        "Edit field master data (name, aliases, location, notes).",
        "field.update",
    ),
    (
        "document.ingest",
        ActionLevel.DRAFT,
        "Upload and ingest a document into the data fabric.",
        "document.write",
    ),
    (
        "document.process",
        ActionLevel.DRAFT,
        "Promote an ingested document into structured drilling data.",
        "document.write",
    ),
    (
        "operation.create",
        ActionLevel.DRAFT,
        "Record an operation on a wellbore section.",
        "operation.write",
    ),
    (
        "operation.update",
        ActionLevel.DRAFT,
        "Correct a field on a recorded operation, with a reason and the version it corrects.",
        "operation.write",
    ),
    (
        "operation.transition",
        ActionLevel.DRAFT,
        "Move an operation through its status life cycle.",
        "operation.write",
    ),
    (
        "operation.link_predecessor",
        ActionLevel.DRAFT,
        "Set or clear the operation another one follows in the sequence.",
        "operation.write",
    ),
    (
        "operation.link_document",
        ActionLevel.DRAFT,
        "Attach the source document an operation was read from.",
        "operation.write",
    ),
    ("event.create", ActionLevel.DRAFT, "Record an event against a well.", "operation.write"),
    (
        "event.update",
        ActionLevel.DRAFT,
        "Correct an event, including its cause and the basis for that cause.",
        "operation.write",
    ),
    ("event.transition", ActionLevel.DRAFT, "Move an event through its status life cycle.", "operation.write"),
    (
        "event.link_document",
        ActionLevel.DRAFT,
        "Attach the source document an event was reported in.",
        "operation.write",
    ),
    (
        "timeseries.create",
        ActionLevel.DRAFT,
        "Register a telemetry channel on a well, wellbore or operation.",
        "timeseries.create",
    ),
    (
        "timeseries.append",
        ActionLevel.DRAFT,
        "Append measurements to a channel.",
        "timeseries.append",
    ),
    (
        "alert.rule_manage",
        ActionLevel.DRAFT,
        "Define or edit a deterministic alert rule.",
        "alert.manage",
    ),
    (
        "alert.acknowledge",
        ActionLevel.DRAFT,
        "Acknowledge an alert: a person has seen it and owns the response.",
        "alert.acknowledge",
    ),
    ("workflow.draft", ActionLevel.DRAFT, "Draft or edit a workflow definition.", "workflow.draft"),
    ("scenario.create", ActionLevel.DRAFT, "Create a what-if scenario or twin snapshot.", "scenario.create"),
    (
        "alert.clear",
        ActionLevel.DRAFT,
        "Clear an alert: the condition it described no longer holds.",
        "alert.clear",
    ),
    (
        "alert.cancel",
        ActionLevel.DRAFT,
        "Cancel an alert: it is not worth acting on, with a reason.",
        "alert.clear",
    ),
    (
        "timeseries.manage",
        ActionLevel.DRAFT,
        "Retire or re-scope a telemetry channel.",
        "timeseries.manage",
    ),
    (
        "connector.test",
        ActionLevel.DRAFT,
        "Test connector connectivity or preview channel descriptors without persistent ingestion.",
        "connector.test",
    ),
    (
        "connector.manage",
        ActionLevel.DRAFT,
        "Create or update a telemetry connector configuration and channel mappings.",
        "connector.manage",
    ),
    (
        "connector.control",
        ActionLevel.DRAFT,
        "Start, stop, restart or disable a telemetry connector.",
        "connector.control",
    ),
    # --- propose -------------------------------------------------------------------------
    (
        "workflow.publish",
        ActionLevel.PROPOSE,
        "Publish a workflow version so it can be triggered.",
        "workflow.publish",
    ),
    (
        "artifact.submit",
        ActionLevel.PROPOSE,
        "Submit an artefact version for review/approval.",
        "action:artifact.submit",
    ),
    (
        "requirement.verify",
        ActionLevel.PROPOSE,
        "Record verification of a requirement with evidence.",
        "requirement.verify",
    ),
    (
        "integrity.verify",
        ActionLevel.PROPOSE,
        "Record a well-integrity verification result.",
        "action:integrity.verify",
    ),
    (
        "change.propose",
        ActionLevel.PROPOSE,
        "Open a change request against a plan, schedule or setting.",
        "action:change.propose",
    ),
    # --- execute with approval -------------------------------------------------------------
    (
        "artifact.approve",
        ActionLevel.EXECUTE_WITH_APPROVAL,
        "Approve an artefact version (human authority).",
        "action:artifact.approve",
    ),
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
    (
        "message.send",
        ActionLevel.EXECUTE_WITH_APPROVAL,
        "Send a message through an integration (email/Telegram/WhatsApp).",
        "action:message.send",
    ),
    (
        "export.create",
        ActionLevel.EXECUTE_WITH_APPROVAL,
        "Export data or a document out of the platform.",
        "action:export.create",
    ),
    # --- authorized automation (off unless an org enables the envelope) --------------------
    (
        "monitor.notify",
        ActionLevel.AUTHORIZED_AUTOMATION,
        "Notify on a validated condition inside an agreed envelope.",
        "action:monitor.notify",
    ),
    (
        "report.distribute",
        ActionLevel.AUTHORIZED_AUTOMATION,
        "Distribute a generated report to the agreed distribution list.",
        "action:report.distribute",
    ),
    (
        "rig.write",
        ActionLevel.AUTHORIZED_AUTOMATION,
        "Write a setpoint to a rig/edge system inside an approved envelope.",
        "action:rig.write",
    ),
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
