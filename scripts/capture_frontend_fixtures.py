"""Capture the frontend's API fixtures from a real API.

Everything under ``frontend/src/test/fixtures`` is written here — the well cockpit's payloads and the
run-monitor and approval payloads — because the README of that directory says so, and a capture path
the README promises but the script does not implement is a fixture that quietly goes stale by hand.

Run from the repository root:

    backend/.venv/bin/python scripts/capture_frontend_fixtures.py

Why capture instead of writing fixtures by hand: a hand-written fixture encodes what the author
believed the server sends, which is how a screen ends up reading fields that do not exist. Everything
written here came back from a seeded instance of the real application over HTTP, so a component test
built on these files fails when the server's shapes move.

Trimming: the seeded engineering context is delivered as a ~52 KB prompt string that appears in
several places in one payload (the run's variables, the context node's outputs, the approval's
upstream node runs). A fixture that repeats it six times is 200 KB of text no test reads. Strings
longer than `MAX_STRING` are therefore replaced by a marker naming how much was removed. Structure,
keys, types, numbers and short values are untouched — the parts a client contract is made of.

The script is idempotent: it reseeds into a throwaway database under /tmp and never touches a
developer's data.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
OUT = REPO / "frontend/src/test/fixtures"
DATA = pathlib.Path("/tmp/drillai-fixture-capture")
PORT = 8093
BASE = f"http://127.0.0.1:{PORT}/api/v1"
DB_URL = f"sqlite+aiosqlite:///{DATA / 'capture.db'}"
MAX_STRING = 2000
MAX_LIST_ITEMS = 5

SUPERVISOR = {"X-Dev-Roles": "drilling_supervisor", "Content-Type": "application/json"}
MANAGER = {"X-Dev-Roles": "well_manager", "Content-Type": "application/json"}


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    for key, value in (headers or SUPERVISOR).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.URLError:
        return 0, None  # the port is not open yet; the caller retries
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        try:
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, payload


def trim(node):
    """Replace over-long strings and over-long lists, keeping every key and every type."""
    if isinstance(node, str):
        if len(node) > MAX_STRING:
            return f"⟪trimmed: {len(node)} characters of generated context text⟫"
        return node
    if isinstance(node, dict):
        return {key: trim(value) for key, value in node.items()}
    if isinstance(node, list):
        if len(node) > MAX_LIST_ITEMS and all(not isinstance(item, (dict, list)) for item in node):
            return [trim(item) for item in node[:MAX_LIST_ITEMS]] + [
                f"⟪trimmed: {len(node) - MAX_LIST_ITEMS} more entries⟫"
            ]
        return [trim(item) for item in node]
    return node


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    seeded = subprocess.run(
        [
            str(BACKEND / ".venv/bin/python"),
            str(REPO / "scripts/seed_demo.py"),
            "--reset",
            "--database-url",
            DB_URL,
            "--blob-dir",
            str(DATA / "blobs"),
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    if seeded.returncode != 0:
        print(seeded.stdout[-2000:], seeded.stderr[-2000:], file=sys.stderr)
        return 1
    fixtures = json.loads(seeded.stdout[seeded.stdout.index("{") :])
    workflow_id, well_id = fixtures["workflow_id"], fixtures["well_id"]

    log = (DATA / "uvicorn.log").open("w")
    server = subprocess.Popen(
        [
            str(BACKEND / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "--factory",
            "drillai.api.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND,
        stdout=log,
        stderr=subprocess.STDOUT,
        env={
            **os.environ,
            "DRILLAI_ENVIRONMENT": "development",
            "DRILLAI_AUTH_ENABLED": "false",
            "DRILLAI_DATABASE_URL": DB_URL,
            "DRILLAI_BLOB_BACKEND": "filesystem",
            "DRILLAI_BLOB_ROOT": str(DATA / "blobs"),
            "DRILLAI_SCHEDULER_ENABLED": "false",
            "PYTHONPATH": str(BACKEND / "src"),
        },
    )
    try:
        for _ in range(120):
            if call("GET", "/health")[0] == 200:
                break
            time.sleep(0.5)
        else:
            log.flush()
            print("api did not come up:\n" + (DATA / "uvicorn.log").read_text()[-3000:], file=sys.stderr)
            return 1

        started = call(
            "POST",
            f"/workflows/{workflow_id}/runs",
            {"well_id": well_id, "inputs": {"plan_date": "2026-03-15"}, "trigger_type": "manual"},
        )
        if started[0] != 201:
            print("run start failed:", started, file=sys.stderr)
            return 1
        run_id, approval_id = started[1]["id"], started[1]["pending_approval_id"]

        captured = {
            # The well cockpit's payloads. Captured before the run so the fixtures describe the well
            # as it is at the start of the synthetic day the run belongs to.
            "well-state.json": call("GET", f"/wells/{well_id}/state")[1],
            "well-npt.json": call("GET", f"/wells/{well_id}/npt")[1],
            "well-timeline.json": call("GET", f"/wells/{well_id}/timeline")[1],
            "well-twin.json": call("GET", f"/wells/{well_id}/twin")[1],
            "well-audit.json": call("GET", f"/wells/{well_id}/audit")[1],
            # The operational workspace's payloads. The limits are the ones the fixtures were
            # reviewed at: 25 timeline entries is under the seeded well's 30, so `next_cursor` is
            # populated and the tests exercise the "more entries follow" state from a real page.
            "operations-list.json": call("GET", f"/operations?well_id={well_id}&limit=20")[1],
            "events-list.json": call("GET", f"/events?well_id={well_id}&limit=20")[1],
            "operations-timeline.json": call("GET", f"/wells/{well_id}/timeline?limit=25")[1],
            "run-waiting-approval.json": call("GET", f"/runs/{run_id}")[1],
            "approval-pending.json": call("GET", f"/approvals/{approval_id}")[1],
            "approvals-list.json": call("GET", "/approvals?status=pending")[1],
            "runs-list.json": call("GET", "/runs?limit=50")[1],
        }

        decided = call(
            "POST",
            f"/approvals/{approval_id}/decide",
            {
                "decision": "approved",
                "note": "Reviewed the NPT attribution against the DDR of 2026-03-15.",
                "conditions": ["Re-issue the summary if the 06:00 report revises NPT."],
                "resume": True,
            },
            MANAGER,
        )
        if decided[0] != 200:
            print("decision failed:", decided, file=sys.stderr)
            return 1
        captured["run-succeeded.json"] = call("GET", f"/runs/{run_id}")[1]
        captured["approvals-list-any.json"] = call("GET", "/approvals?status=any")[1]

        # A fixture written from a refusal would look like a contract and describe nothing. Every
        # capture above must have come back as a 200 with a body; anything else stops the run.
        for name, payload in captured.items():
            if payload is None:
                print(f"{name} was not captured (the request failed)", file=sys.stderr)
                return 1

        for name, payload in captured.items():
            (OUT / name).write_text(
                json.dumps(trim(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "approval_id": approval_id,
                    "statuses": {
                        "waiting": captured["run-waiting-approval.json"]["run"]["status"],
                        "final": captured["run-succeeded.json"]["run"]["status"],
                    },
                    "operations": captured["operations-list.json"]["total"],
                    "events": captured["events-list.json"]["total"],
                    "timeline_entries": captured["operations-timeline.json"]["count"],
                    "written": {name: (OUT / name).stat().st_size for name in captured},
                },
                indent=2,
            )
        )
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged server
            server.kill()
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
