"""Reference resolution and condition evaluation for workflows.

Two languages, both deliberately tiny and *not* Python:

``${node_id.field.path}``   reference into a previous node's output, a run variable, the run
                            metadata, or the workflow inputs. Resolution is a dictionary walk —
                            attribute access, calls and subscripts are impossible by construction.
``<value> <op> <value>``    comparisons (``== != > >= < <= in "not in"``) combined with
                            ``and`` / ``or`` / ``not`` and parentheses.

Using :func:`eval` here would hand arbitrary workflow authors (and anything they copy from a
document) the ability to execute code inside the platform. This module exists so that can never
happen, and so conditions are deterministically testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from drillai.core.errors import WorkflowDefinitionInvalid

__all__ = ["ExpressionError", "UnresolvedReference", "evaluate_condition", "resolve_references", "resolve_value"]

_REFERENCE_RE = re.compile(r"\$\{([^}]+)\}")
_MISSING = object()


class ExpressionError(WorkflowDefinitionInvalid):
    """Raised when an expression cannot be parsed or evaluated."""


class UnresolvedReference(ExpressionError):
    """Raised when a ``${...}`` reference does not exist in the run state.

    This is deliberately fatal. Substituting an empty string for a missing reference is how a
    workflow silently produces a report that says "TVD at TD:  m" and nobody notices; the failure
    has to surface where the mistake is.
    """


def _resolve_or_raise(reference: str, context: Any, *, strict: bool) -> Any:
    """Resolve one reference; on failure say *which level* failed and what was available there."""
    path = _split_path(reference)
    resolved = _Reference(tuple(path)).evaluate(context)
    if resolved is not _MISSING and resolved is not None:
        return resolved
    if not strict:
        return None

    head = path[0]
    if head in context.node_outputs:
        current: Any = context.node_outputs[head]
        label = f"node {head!r}"
    elif head in {"vars", "inputs", "run"}:
        current = {"vars": context.variables, "inputs": context.inputs}.get(head, {})
        label = f"{head}"
    else:
        detail = (
            f"no node {head!r} has produced output yet "
            f"(nodes seen: {', '.join(sorted(context.node_outputs)) or 'none'})"
        )
        raise UnresolvedReference(f"reference ${{{reference}}} does not resolve: {detail}")

    for part in path[1:]:
        if isinstance(current, dict) and part in current and current[part] is not None:
            current = current[part]
            label = f"{label}.{part}"
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            label = f"{label}[{part}]"
            continue
        available = ", ".join(sorted(current)) if isinstance(current, dict) else type(current).__name__
        raise UnresolvedReference(
            f"reference ${{{reference}}} does not resolve: {label} has no {part!r} (available: {available or 'nothing'})"
        )
    raise UnresolvedReference(f"reference ${{{reference}}} does not resolve: {label} is empty")


@dataclass(frozen=True)
class _Literal:
    value: Any

    def evaluate(self, context: Any) -> Any:
        return self.value


@dataclass(frozen=True)
class _Reference:
    path: tuple[str, ...]

    def evaluate(self, context: Any) -> Any:
        head = self.path[0]
        if head == "vars":
            current: Any = context.variables
            rest = self.path[1:]
        elif head == "inputs":
            current = context.inputs
            rest = self.path[1:]
        elif head == "run":
            current = {"id": context.run_id, "node_id": context.node_id, "loop_index": context.loop_index}
            rest = self.path[1:]
        else:
            current = context.node_outputs.get(head, {})
            rest = self.path[1:]
        for part in rest:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return None
        return current


@dataclass(frozen=True)
class _Comparison:
    left: Any
    operator: str
    right: Any

    def evaluate(self, context: Any) -> bool:
        left = _resolve_node(self.left, context)
        if left is _MISSING:
            left = None
        right = _resolve_node(self.right, context)
        if right is _MISSING:
            right = None
        return _compare(left, self.operator, right)


@dataclass(frozen=True)
class _BooleanOp:
    operator: str
    operands: list[Any]

    def evaluate(self, context: Any) -> bool:
        if self.operator == "and":
            return all(_truthy(_resolve_node(operand, context)) for operand in self.operands)
        return any(_truthy(_resolve_node(operand, context)) for operand in self.operands)


@dataclass(frozen=True)
class _NotOp:
    operand: Any

    def evaluate(self, context: Any) -> bool:
        return not _truthy(_resolve_node(self.operand, context))


def _resolve_node(node: Any, context: Any) -> Any:
    if isinstance(node, (_Literal, _Reference, _Comparison, _BooleanOp, _NotOp)):
        return node.evaluate(context)
    return node


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no", "none", "null"}
    return bool(value)


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator in {"==", "!="}:
        result = _loose_equal(left, right)
        return result if operator == "==" else not result
    if operator == "in":
        try:
            return left in right  # type: ignore[operator]
        except TypeError:
            return False
    if operator == "not in":
        try:
            return left not in right  # type: ignore[operator]
        except TypeError:
            return True
    left_number, right_number = _as_number(left), _as_number(right)
    if left_number is None or right_number is None:
        # Comparing incomparable types is an error, not a silent False: a workflow that cannot
        # evaluate its own condition is broken and must say so.
        raise ExpressionError(f"cannot compare {left!r} {operator} {right!r}")
    if operator == ">":
        return left_number > right_number
    if operator == ">=":
        return left_number >= right_number
    if operator == "<":
        return left_number < right_number
    if operator == "<=":
        return left_number <= right_number
    raise ExpressionError(f"unsupported operator {operator!r}")


def _loose_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, bool):
        return _truthy(left) is right
    if isinstance(right, str) and isinstance(left, bool):
        return _truthy(right) is left
    left_number, right_number = _as_number(left), _as_number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return left == right


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- resolution


def resolve_references(value: Any, context: Any, *, strict: bool = True) -> Any:
    """Replace ``${...}`` references inside a JSON-like structure.

    A string that is exactly one reference keeps the referenced type (so a list stays a list);
    a string that *contains* references is interpolated into text. With ``strict`` (the default) a
    reference that cannot be resolved raises :class:`UnresolvedReference`; pass ``strict=False``
    only where a missing value is genuinely acceptable.
    """
    if isinstance(value, str):
        matches = list(_REFERENCE_RE.finditer(value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return _resolve_or_raise(matches[0].group(1), context, strict=strict)

        def substitute(match: re.Match[str]) -> str:
            resolved = _resolve_or_raise(match.group(1), context, strict=strict)
            return "" if resolved is None else str(resolved)

        return _REFERENCE_RE.sub(substitute, value)
    if isinstance(value, dict):
        return {key: resolve_references(item, context, strict=strict) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_references(item, context) for item in value]
    return value


def resolve_value(value: Any, context: Any) -> Any:
    """Alias kept for callers that only resolve (no parsing)."""
    return resolve_references(value, context)


def _split_path(reference: str) -> list[str]:
    parts = [part.strip() for part in reference.strip().split(".") if part.strip()]
    if not parts:
        raise ExpressionError(f"empty reference {{{reference}}}")
    return parts


# --------------------------------------------------------------------------- condition parser


class _Parser:
    """Recursive descent: or → and → not → comparison → primary."""

    def __init__(self, text: str) -> None:
        self.tokens = _tokenize(text)
        self.position = 0

    def parse(self) -> Any:
        node = self.parse_or()
        if self.position != len(self.tokens):
            raise ExpressionError(f"unexpected token {self.tokens[self.position]!r}")
        return node

    def parse_or(self) -> Any:
        operands = [self.parse_and()]
        while self._peek() == "or":
            self._next()
            operands.append(self.parse_and())
        return operands[0] if len(operands) == 1 else _BooleanOp("or", operands)

    def parse_and(self) -> Any:
        operands = [self.parse_not()]
        while self._peek() == "and":
            self._next()
            operands.append(self.parse_not())
        return operands[0] if len(operands) == 1 else _BooleanOp("and", operands)

    def parse_not(self) -> Any:
        if self._peek() == "not":
            self._next()
            return _NotOp(self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        left = self.parse_primary()
        token = self._peek()
        if token in {"==", "!=", ">", ">=", "<", "<=", "in", "not in"}:
            self._next()
            right = self.parse_primary()
            return _Comparison(left, token, right)
        return left

    def parse_primary(self) -> Any:
        token = self._next()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        if token == "(":
            node = self.parse_or()
            if self._next() != ")":
                raise ExpressionError("missing closing parenthesis")
            return node
        return _parse_literal(token)

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _next(self) -> str | None:
        token = self._peek()
        self.position += 1
        return token


_TOKEN_RE = re.compile(
    r"""
    \$\{[^}]*\}                 # reference
  | "(?:[^"\\]|\\.)*"           # double-quoted string
  | '(?:[^'\\]|\\.)*'           # single-quoted string
  | >=|<=|==|!=|>|<           # comparison operators
  | \( | \)
  | [A-Za-z_][A-Za-z0-9_]*    # bare word (true/false/none/and/or/not/in/null)
  | -?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?
  | \S+                       # anything else (rejected later, but tokenised)
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if token in {"&&"}:
            tokens.append("and")
        elif token in {"||"}:
            tokens.append("or")
        elif token == "!":
            tokens.append("not")
        else:
            tokens.append(token)
    return _merge_not_in(tokens)


def _merge_not_in(tokens: list[str]) -> list[str]:
    merged: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index] == "not" and index + 1 < len(tokens) and tokens[index + 1] == "in":
            merged.append("not in")
            index += 2
            continue
        merged.append(tokens[index])
        index += 1
    return merged


def _parse_literal(token: str | None) -> Any:
    if token is None:
        raise ExpressionError("unexpected end of expression")
    if token.startswith("${") and token.endswith("}"):
        return _Reference(tuple(_split_path(token[2:-1])))
    if token.startswith(("'", '"')):
        return _Literal(token[1:-1].encode().decode("unicode_escape"))
    lowered = token.lower()
    if lowered == "true":
        return _Literal(True)
    if lowered == "false":
        return _Literal(False)
    if lowered in {"none", "null"}:
        return _Literal(None)
    try:
        return _Literal(float(token))
    except ValueError:
        pass
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        # A bare word in a boolean position: treat as a string literal ("approved").
        return _Literal(token)
    raise ExpressionError(f"cannot parse token {token!r}")


def evaluate_condition(expression: str, context: Any) -> bool:
    """Evaluate a workflow condition against a node context."""
    if not expression or not expression.strip():
        raise ExpressionError("empty condition")
    parser = _Parser(expression)
    tree = parser.parse()
    return _truthy(_resolve_node(tree, context))
