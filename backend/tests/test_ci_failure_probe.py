"""TEMPORARY PROBE — deleted by the commit that follows this one.

This file exists to certify one property of the pipeline that cannot be certified by reading YAML:
that a failing *required* gate fails the workflow, rather than being swallowed, skipped or reported as
a warning. It fails on purpose, on the branch, for exactly one CI run; the next commit removes it.

It is deliberately a backend test rather than a frontend one: the backend gate runs *after* the
frontend gates, so a red run also proves that the remaining steps (the end-to-end suite among them)
are skipped rather than executed against a tree that has already failed certification.
"""


def test_ci_fails_when_a_required_gate_fails() -> None:
    raise AssertionError(
        "deliberate failure: certifying that a required gate failure fails the CI workflow"
    )
