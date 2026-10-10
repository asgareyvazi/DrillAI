"""Agent and skill registries.

An *agent* here is a declaration, not a code path: a named role composed of skills, tools, engines,
a context profile, a model profile and a guardrail set — all of which must already exist in their
own registries. The platform keeps the distinction the mission insists on:

* **Skill** — a reusable unit of work with an input/output contract (``analyse_hydraulics``).
* **Agent** — a composition of skills plus a scope and an autonomy ceiling (``drilling_engineer``).
* **Tool** — something the platform can *do* (``search_documents``); owned by ``ai.tools``.
* **Engine** — deterministic engineering maths; owned by ``engines``.
* **Workflow** — an executed graph; an agent may *start* one, never bypass its node gates.

Two properties are enforced at registration time rather than trusted:

1. every referenced tool, engine, skill, context section and context purpose must exist — a typo
   fails at import/registration, never as an empty tool list at runtime;
2. an agent's autonomy ceiling is at most ``L2`` (draft). Anything that executes, sends or writes
   must be a workflow node or a human action with its own approval — an agent cannot be registered
   with L4/L5 authority, which is what "unsafe autonomy off by default" means mechanically.

Nothing in the application code mentions a specific drilling agent: the defaults below are seed
data registered through the same public API a domain pack would use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from drillai.core.errors import ValidationFailed
from drillai.security.actions import ActionLevel

__all__ = [
    "AgentSpec",
    "SkillSpec",
    "agent_catalogue",
    "agent_spec",
    "install_default_agents",
    "register_agent",
    "register_skill",
    "registered_agents",
    "registered_skills",
    "skill_spec",
    "validate_agent_references",
]

#: An agent may never be registered with more authority than "draft" — see module docstring.
AGENT_MAX_AUTONOMY = ActionLevel.DRAFT


@dataclass(frozen=True)
class SkillSpec:
    """A reusable capability declaration (contract + provenance), not an implementation."""

    key: str
    name: str
    description: str
    version: str = "1.0.0"
    kind: str = "analysis"  # analysis | retrieval | generation | review | conversation
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    context_sections: tuple[str, ...] = ()
    required_permission: str = "agent.run"
    action_level: ActionLevel = ActionLevel.ADVISE
    limitations: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "kind": self.kind,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "tools": list(self.tools),
            "engines": list(self.engines),
            "context_sections": list(self.context_sections),
            "required_permission": self.required_permission,
            "action_level": self.action_level.value,
            "limits": list(self.limitations),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class AgentSpec:
    """A role: composition, scope, model profile and guardrails."""

    key: str
    name: str
    description: str
    version: str = "1.0.0"
    role_hint: str | None = None  # the human role this agent supports, if any
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    context_purpose: str = "agent"
    context_sections: tuple[str, ...] = ()
    llm_profile: str = "generate"  # TaskProfile name, resolved by the model router
    required_permission: str = "agent.run"
    max_action_level: ActionLevel = ActionLevel.ADVISE
    guardrails: tuple[str, ...] = ("ground_numbers", "require_citations", "scan_for_injection")
    prompt_template_key: str | None = None
    limitations: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "role_hint": self.role_hint,
            "skills": list(self.skills),
            "tools": list(self.tools),
            "engines": list(self.engines),
            "context_purpose": self.context_purpose,
            "context_sections": list(self.context_sections),
            "llm_profile": self.llm_profile,
            "required_permission": self.required_permission,
            "max_action_level": self.max_action_level.value,
            "guardrails": list(self.guardrails),
            "prompt_template_key": self.prompt_template_key,
            "limits": list(self.limitations),
            "tags": list(self.tags),
        }


_SKILLS: dict[str, SkillSpec] = {}
_AGENTS: dict[str, AgentSpec] = {}


def validate_agent_references(*, tools: tuple[str, ...] = (), engines: tuple[str, ...] = (),
                             skills: tuple[str, ...] = (), context_sections: tuple[str, ...] = ()) -> list[str]:
    """Return the list of dangling references (empty when everything resolves)."""
    from drillai.ai.tools import registered_tools
    from drillai.context.builder import registered_section_keys
    from drillai.engines.registry import load_default_engines

    problems: list[str] = []
    known_tools = set(registered_tools())
    known_engines = set(load_default_engines().keys())
    known_skills = set(_SKILLS)
    known_sections = set(registered_section_keys())
    for key in tools:
        if key not in known_tools:
            problems.append(f"tool {key!r} is not registered")
    for key in engines:
        if key not in known_engines:
            problems.append(f"engine {key!r} is not registered")
    for key in skills:
        if key not in known_skills:
            problems.append(f"skill {key!r} is not registered")
    for key in context_sections:
        if key not in known_sections:
            problems.append(f"context section {key!r} is not registered")
    return problems


def register_skill(spec: SkillSpec, *, replace: bool = False) -> SkillSpec:
    if spec.key in _SKILLS and not replace:
        raise ValidationFailed(f"skill {spec.key!r} is already registered")
    problems = validate_agent_references(tools=spec.tools, engines=spec.engines, context_sections=spec.context_sections)
    if problems:
        raise ValidationFailed(
            f"skill {spec.key!r} references unknown capabilities", details={"problems": problems}
        )
    _SKILLS[spec.key] = spec
    return spec


def register_agent(spec: AgentSpec, *, replace: bool = False) -> AgentSpec:
    if spec.key in _AGENTS and not replace:
        raise ValidationFailed(f"agent {spec.key!r} is already registered")
    if spec.max_action_level.rank > AGENT_MAX_AUTONOMY.rank:
        raise ValidationFailed(
            "an agent may not be registered with execution authority: side effects belong to "
            "workflow nodes (with their own approvals) or to people",
            details={
                "agent": spec.key,
                "requested": spec.max_action_level.value,
                "limit": AGENT_MAX_AUTONOMY.value,
            },
        )
    problems = validate_agent_references(
        tools=spec.tools, engines=spec.engines, skills=spec.skills, context_sections=spec.context_sections
    )
    from drillai.ai.router import TaskProfile

    if spec.context_purpose not in {purpose.value for purpose in _context_purposes()}:
        problems.append(f"context purpose {spec.context_purpose!r} is not known")
    if spec.llm_profile not in {profile.value for profile in TaskProfile}:
        problems.append(f"llm profile {spec.llm_profile!r} is not a known task profile")
    if problems:
        raise ValidationFailed(
            f"agent {spec.key!r} references unknown capabilities", details={"problems": problems}
        )
    _AGENTS[spec.key] = spec
    return spec


def _context_purposes() -> list[Any]:
    from drillai.context.model import ContextPurpose

    return list(ContextPurpose)


def registered_skills() -> dict[str, SkillSpec]:
    return dict(_SKILLS)


def registered_agents() -> dict[str, AgentSpec]:
    return dict(_AGENTS)


def skill_spec(key: str) -> SkillSpec:
    skill = _SKILLS.get(key)
    if skill is None:
        raise ValidationFailed(f"skill {key!r} is not registered", details={"known": sorted(_SKILLS)})
    return skill


def agent_spec(key: str) -> AgentSpec:
    agent = _AGENTS.get(key)
    if agent is None:
        raise ValidationFailed(f"agent {key!r} is not registered", details={"known": sorted(_AGENTS)})
    return agent


def agent_catalogue() -> dict[str, list[dict[str, Any]]]:
    return {
        "agents": [spec.describe() for _, spec in sorted(_AGENTS.items())],
        "skills": [spec.describe() for _, spec in sorted(_SKILLS.items())],
        "max_agent_autonomy": AGENT_MAX_AUTONOMY.value,
    }


# --------------------------------------------------------------------------- defaults
# Seed data, registered through the public API. A domain pack (completion, integrity,
# intervention) adds its own skills and agents the same way; nothing here is special-cased in
# application logic.


def install_default_agents() -> None:
    """Register the platform's default skills and agents (idempotent)."""
    if _AGENTS:
        return

    from drillai.ai.tools import install_default_tools

    install_default_tools()

    skills = (
        SkillSpec(
            key="well_context_review",
            name="Well context review",
            description="Assemble the engineering context for a well/section and summarise what is on file, with citations.",
            kind="retrieval",
            inputs={"well_id": "well to review", "section_id": "optional hole section scope"},
            outputs={"summary": "markdown summary with citations", "gaps": "list of missing data classes"},
            context_sections=("well_identity", "well_sections", "twin_state", "documents"),
            tags=("context", "review"),
        ),
        SkillSpec(
            key="document_evidence_search",
            name="Document evidence search",
            description="Retrieve the passages that answer a scoped question and return them with page-level citations.",
            kind="retrieval",
            inputs={"query": "natural-language question", "section_id": "hole section scope"},
            outputs={"hits": "ranked passages with citations"},
            tools=("search_documents",),
            context_sections=("documents", "evidence"),
            tags=("rag", "evidence"),
        ),
        SkillSpec(
            key="hydraulics_check",
            name="Hydraulics check",
            description="Run the laminar hydraulics engine on explicit inputs and explain the result inside its limits.",
            kind="analysis",
            inputs={"inputs": "canonical SI engine inputs"},
            outputs={"result": "engine outputs with units, warnings and violations"},
            tools=("run_engine",),
            engines=("hydraulics.laminar",),
            limitations=("laminar model only; turbulent flow requires the appropriate engine",),
            tags=("hydraulics", "engine"),
        ),
        SkillSpec(
            key="trajectory_check",
            name="Trajectory check",
            description="Compute minimum-curvature survey results and report dogleg severity against the plan.",
            kind="analysis",
            tools=("run_engine",),
            engines=("trajectory.minimum_curvature",),
            tags=("trajectory", "engine"),
        ),
        SkillSpec(
            key="offset_comparison",
            name="Offset comparison",
            description="Rank offset wells by similarity, then explain the differences that matter for the current well.",
            kind="analysis",
            tools=("run_engine",),
            engines=("offsets.similarity", "optimization.pareto"),
            limitations=("similarity is computed from the declared attributes; it is not a guarantee of performance",),
            tags=("offsets", "benchmarking"),
        ),
    )
    for skill in skills:
        register_skill(skill, replace=True)

    agents = (
        AgentSpec(
            key="drilling_engineer",
            name="Drilling engineer assistant",
            description="Answers well questions from the engineering context, runs the relevant engines and drafts recommendations.",
            role_hint="engineer",
            skills=("well_context_review", "document_evidence_search", "trajectory_check", "hydraulics_check"),
            tools=("search_documents", "run_engine", "list_wells"),
            engines=("trajectory.minimum_curvature", "hydraulics.laminar", "torque_drag.soft_string"),
            context_sections=(
                "well_identity",
                "well_sections",
                "trajectory",
                "twin_state",
                "documents",
                "evidence",
                "engine_results",
            ),
            tags=("drilling", "advisor"),
        ),
        AgentSpec(
            key="offset_analyst",
            name="Offset analyst",
            description="Compares the well against offset wells and quantifies the differences, without inventing performance numbers.",
            role_hint="engineer",
            skills=("offset_comparison", "document_evidence_search"),
            tools=("search_documents", "run_engine"),
            engines=("offsets.similarity", "optimization.sweep", "optimization.pareto"),
            context_sections=("well_identity", "well_sections", "formations", "documents", "lessons"),
            tags=("offsets",),
        ),
        AgentSpec(
            key="engines_review",
            name="Engine run reviewer",
            description="Re-runs a declared engine on stated inputs and explains the assumptions, limits and violations.",
            role_hint="engineer",
            skills=("hydraulics_check", "trajectory_check"),
            tools=("run_engine",),
            tags=("engines", "review"),
        ),
        AgentSpec(
            key="data_steward",
            name="Data quality steward",
            description="Reports ingestion status, extraction confidence and evidence coverage for a well.",
            role_hint="data_manager",
            skills=("well_context_review", "document_evidence_search"),
            tools=("search_documents", "list_wells"),
            context_sections=("documents", "evidence", "extracted_records"),
            tags=("data", "quality"),
        ),
    )
    for agent in agents:
        register_agent(agent, replace=True)
