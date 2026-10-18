"""CP20 Integration Completeness & External Acceptance Suite.

Verifies:
1. CP20-A: Real PostgreSQL + `pgvector` (`<=>` cosine distance) semantic & hybrid retrieval
   with `DeterministicEmbeddingProvider`, tenant (`org_id`) and section (`section_id`) isolation,
   and graceful fallback to lexical BM25 when embeddings are unavailable.
2. CP20-B: Outbound notification adapters (`WebhookNotificationAdapter`, `EmailSmtpNotificationAdapter`,
   `InAppNotificationAdapter`), HMAC-SHA256 secret-ref signing, idempotency suppression,
   retryable (`503`) vs terminal (`400` / egress block) classification, and bounded delivery ledger.
3. CP20-B (Workflow L4 Gate): `output.notify` in a workflow pauses at `waiting_approval` (`L4`)
   before any external delivery occurs, and dispatches upon human approval + resume.
4. CP20-C: `scripts/verify_external_vendor_interop.py` records `network_calls_made: 0`,
   `external_vendor_verified: False`, `BLOCKED_BY_EXTERNAL_EVIDENCE`, and the 4-step
   commissioning checklist.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from drillai.ai.providers import (
    DeterministicEmbeddingProvider,
    bootstrap_default_providers,
    provider_registry,
)
from drillai.db.models import (
    ApprovalRequest,
    Document,
    DocumentChunk,
    Organization,
    OutboundMessage,
    Project,
    Well,
    Wellbore,
    WellSection,
)
from drillai.db.session import Database
from drillai.rag.retriever import (
    RetrievalQuery,
    RetrieveMode,
    index_chunk_embeddings,
    retrieve,
)
from drillai.security.actions import ActionLevel, Principal
from drillai.telemetry.connectors import reset_egress_policy
from drillai.workflow.graph import GraphNode, WorkflowGraph
from drillai.workflow.notifications import dispatch_outbound_message
from drillai.workflow.runtime import RunScope, WorkflowRuntime
from drillai.workflow.service import WorkflowService

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.mark.postgres
async def test_cp20_01_pgvector_nearest_neighbor_and_hybrid_retrieval_on_postgres(
    postgres_url: str,
) -> None:
    """Verify real PostgreSQL pgvector (<=> cosine distance) semantic and hybrid retrieval,
    tenant/section scope isolation, and graceful lexical fallback."""
    await asyncio.to_thread(command.upgrade, _alembic_config(postgres_url), "head")

    db = Database(postgres_url)
    reg = provider_registry()
    emb_provider = DeterministicEmbeddingProvider(dimensions=32)
    reg.register(emb_provider, replace=True)
    try:
        async with db.session() as session:
            org_a = Organization(slug="cp20-pgvec-a", name="CP20 Org A", kind="operator")
            org_b = Organization(slug="cp20-pgvec-b", name="CP20 Org B", kind="operator")
            session.add_all([org_a, org_b])
            await session.flush()

            proj_a = Project(org_id=org_a.id, code="PRJ-VEC-A", name="Vector Project A")
            proj_b = Project(org_id=org_b.id, code="PRJ-VEC-B", name="Vector Project B")
            session.add_all([proj_a, proj_b])
            await session.flush()

            well_a = Well(org_id=org_a.id, project_id=proj_a.id, name="Well A")
            well_b = Well(org_id=org_b.id, project_id=proj_b.id, name="Well B")
            session.add_all([well_a, well_b])
            await session.flush()

            wb_a = Wellbore(
                org_id=org_a.id,
                well_id=well_a.id,
                name="Mainbore A",
                purpose="original",
                sequence=1,
                is_active=True,
            )
            session.add(wb_a)
            await session.flush()

            sec_85 = WellSection(
                org_id=org_a.id,
                wellbore_id=wb_a.id,
                sequence=2,
                name="8-1/2 in Reservoir",
                kind="production",
                hole_diameter_si=0.2159,
                planned_top_md_si=2000.0,
                planned_bottom_md_si=3200.0,
            )
            sec_1225 = WellSection(
                org_id=org_a.id,
                wellbore_id=wb_a.id,
                sequence=1,
                name="12-1/4 in Intermediate",
                kind="intermediate",
                hole_diameter_si=0.31115,
                planned_top_md_si=800.0,
                planned_bottom_md_si=2000.0,
            )
            session.add_all([sec_85, sec_1225])
            await session.flush()

            doc_a = Document(
                org_id=org_a.id,
                project_id=proj_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                doc_type="ddr",
                title="Well A DDR",
                status="extracted",
            )
            doc_b = Document(
                org_id=org_b.id,
                project_id=proj_b.id,
                well_id=well_b.id,
                doc_type="ddr",
                title="Well B DDR",
                status="extracted",
            )
            session.add_all([doc_a, doc_b])
            await session.flush()

            chk_85_lcm = DocumentChunk(
                org_id=org_a.id,
                document_id=doc_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                section_id=sec_85.id,
                doc_type="ddr",
                chunk_index=0,
                text="8-1/2 inch reservoir section experienced lost circulation cured with calcium carbonate LCM pill",
            )
            chk_85_bha = DocumentChunk(
                org_id=org_a.id,
                document_id=doc_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                section_id=sec_85.id,
                doc_type="ddr",
                chunk_index=1,
                text="Rotary steerable BHA directional survey inclination 28 degrees azimuth 115 degrees",
            )
            chk_1225_lcm = DocumentChunk(
                org_id=org_a.id,
                document_id=doc_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                section_id=sec_1225.id,
                doc_type="ddr",
                chunk_index=2,
                text="12-1/4 inch intermediate section lost circulation cured with calcium carbonate LCM pill",
            )
            chk_foreign = DocumentChunk(
                org_id=org_b.id,
                document_id=doc_b.id,
                well_id=well_b.id,
                doc_type="ddr",
                chunk_index=0,
                text="8-1/2 inch reservoir section experienced lost circulation cured with calcium carbonate LCM pill",
            )
            all_chunks = [chk_85_lcm, chk_85_bha, chk_1225_lcm, chk_foreign]
            session.add_all(all_chunks)
            await session.flush()

            indexed_count = await index_chunk_embeddings(session, all_chunks, provider=emb_provider)
            assert indexed_count == 4

            # Verify rows exist in PostgreSQL `document_chunk_vectors` table
            vec_rows = (
                await session.execute(text("SELECT COUNT(*) FROM document_chunk_vectors"))
            ).scalar_one()
            assert int(vec_rows) == 4

            # 1. Semantic search via PostgreSQL pgvector scoped to 8-1/2" section
            sem_res = await retrieve(
                session,
                org_a.id,
                RetrievalQuery(
                    text="lost circulation calcium carbonate LCM pill in reservoir",
                    mode=RetrieveMode.SEMANTIC,
                    well_id=well_a.id,
                    section_id=sec_85.id,
                    min_score=-1.0,
                    limit=5,
                ),
            )
            assert sem_res.mode_used == "semantic"
            assert any("pgvector" in note for note in sem_res.notes)
            assert len(sem_res.hits) == 2
            assert sem_res.hits[0].chunk_id == chk_85_lcm.id
            assert sem_res.hits[0].explain["vector_backend"] == "pgvector"
            assert sem_res.hits[0].score > sem_res.hits[1].score
            # Must never return 12-1/4" section chunk or foreign org chunk
            returned_ids = {h.chunk_id for h in sem_res.hits}
            assert chk_1225_lcm.id not in returned_ids
            assert chk_foreign.id not in returned_ids

            # 2. Hybrid search (BM25 + pgvector RRF)
            hyb_res = await retrieve(
                session,
                org_a.id,
                RetrievalQuery(
                    text="lost circulation calcium carbonate LCM pill",
                    mode=RetrieveMode.HYBRID,
                    well_id=well_a.id,
                    section_id=sec_85.id,
                    limit=5,
                ),
            )
            assert hyb_res.mode_used == "hybrid"
            assert hyb_res.hits[0].chunk_id == chk_85_lcm.id
            assert hyb_res.hits[0].explain["vector_backend"] == "pgvector"
            assert hyb_res.hits[0].explain["semantic_rank"] == 1
            assert hyb_res.hits[0].explain["lexical_rank"] == 1

            # 3. Graceful fallback when no embedding provider is registered
            reg._providers.pop(emb_provider.name, None)
            fallback_res = await retrieve(
                session,
                org_a.id,
                RetrievalQuery(
                    text="lost circulation calcium carbonate",
                    mode=RetrieveMode.HYBRID,
                    well_id=well_a.id,
                    section_id=sec_85.id,
                ),
            )
            assert fallback_res.mode_used == "lexical"
            assert any("vector search unavailable" in note for note in fallback_res.notes)
            assert fallback_res.hits[0].chunk_id == chk_85_lcm.id
    finally:
        reg._providers.pop(emb_provider.name, None)
        bootstrap_default_providers()
        await db.dispose()


class _LocalWebhookServer:
    """Minimal async HTTP receiver for testing WebhookNotificationAdapter."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response_queue: list[tuple[int, dict[str, Any]]] = []
        self._server: asyncio.AbstractServer | None = None
        self.port: int = 0

    async def __aenter__(self) -> _LocalWebhookServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/notify"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            lines = raw_head.decode("latin-1").split("\r\n")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            content_len = int(headers.get("content-length", "0"))
            body = (
                await asyncio.wait_for(reader.readexactly(content_len), timeout=5.0)
                if content_len > 0
                else b""
            )
            self.requests.append({"headers": headers, "body": body})
            if self.response_queue:
                code, resp_obj = self.response_queue.pop(0)
            else:
                code, resp_obj = 200, {"id": f"wh_msg_{len(self.requests)}"}
            resp_bytes = json.dumps(resp_obj).encode("utf-8")
            reason = {200: "OK", 400: "Bad Request", 503: "Service Unavailable"}.get(code, "Status")
            reply = (
                f"HTTP/1.1 {code} {reason}\r\n"
                f"Content-Type: application/json\r\n"
                f"X-Provider-Message-Id: {resp_obj.get('id', '')}\r\n"
                f"Content-Length: {len(resp_bytes)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("latin-1") + resp_bytes
            writer.write(reply)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)


class _LocalSmtpServer:
    """Minimal async RFC 5321 SMTP server for testing EmailSmtpNotificationAdapter."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self.port: int = 0

    async def __aenter__(self) -> _LocalSmtpServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)

    @property
    def url(self) -> str:
        return f"smtp://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"220 localhost ESMTP DrillAI-Test\r\n")
            await writer.drain()
            in_data = False
            buf: list[str] = []
            while not reader.at_eof():
                line_bytes = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                if in_data:
                    if line.strip() == ".":
                        in_data = False
                        self.messages.append("".join(buf))
                        buf.clear()
                        writer.write(b"250 2.0.0 OK: queued\r\n")
                        await writer.drain()
                    else:
                        buf.append(line)
                    continue
                cmd = line.strip().upper()
                if cmd.startswith(("EHLO", "HELO")):
                    writer.write(b"250 localhost\r\n")
                elif cmd.startswith(("MAIL FROM", "RCPT TO", "RSET", "NOOP")):
                    writer.write(b"250 OK\r\n")
                elif cmd.startswith("DATA"):
                    in_data = True
                    writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                elif cmd.startswith("QUIT"):
                    writer.write(b"221 Bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"250 OK\r\n")
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)


async def test_cp20_02_outbound_webhook_and_smtp_notification_adapters_idempotency_and_retries(
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Webhook and SMTP adapters, HMAC signing via secret_ref, idempotency suppression,
    retryable (503) -> dead_letter vs terminal (400 / egress block) classification, and ledger."""
    reset_egress_policy()
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    monkeypatch.setenv("DRILLAI_NOTIFY_SECRET_RIG_WEBHOOK_KEY", "top-secret-hmac-key-2026")

    org = Organization(slug="cp20-notify-org", name="CP20 Notify Org", kind="operator")
    session.add(org)
    await session.flush()

    async with _LocalWebhookServer() as wh_srv, _LocalSmtpServer() as smtp_srv:
        # 1. Successful webhook delivery + HMAC-SHA256 verification + idempotency suppression
        msg1 = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient=wh_srv.url,
            subject="Mud Weight Alert",
            body="ECD reached 1420 kg/m3 at 2850 m MD",
            payload={"secret_ref": "rig_webhook_key", "timeout_seconds": 5.0},
            idempotency_key="idem-cp20-msg-01",
            evidence_refs=["doc_ddr_01"],
        )
        session.add(msg1)
        await session.flush()

        res1 = await dispatch_outbound_message(session, msg1)
        assert res1["status"] == "sent"
        assert res1["duplicate_suppressed"] is False
        assert res1["external_provider_verified"] is False
        assert res1["provider_message_id"] == "wh_msg_1"
        assert len(wh_srv.requests) == 1

        req0 = wh_srv.requests[0]
        expected_sig = hmac.new(
            b"top-secret-hmac-key-2026", req0["body"], hashlib.sha256
        ).hexdigest()
        assert req0["headers"]["x-drillai-signature-256"] == f"sha256={expected_sig}"
        assert req0["headers"]["idempotency-key"] == "idem-cp20-msg-01"

        # Re-dispatching duplicate with same idempotency_key must NOT make a network call
        msg1_dup = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient=wh_srv.url,
            subject="Mud Weight Alert Duplicate",
            body="ECD reached 1420 kg/m3 at 2850 m MD",
            idempotency_key="idem-cp20-msg-01",
        )
        session.add(msg1_dup)
        await session.flush()
        res_dup = await dispatch_outbound_message(session, msg1_dup)
        assert res_dup["status"] == "sent"
        assert res_dup["duplicate_suppressed"] is True
        assert res_dup["provider_message_id"] == "wh_msg_1"
        assert len(wh_srv.requests) == 1  # Still 1 request!

        # 2. Retryable HTTP 503 -> failed_retryable on attempt 1, dead_letter on attempt 2 (max_attempts=2)
        wh_srv.response_queue.extend([(503, {"error": "temporarily overloaded"}), (503, {"error": "still overloaded"})])
        msg_retry = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient=wh_srv.url,
            subject="Transient Test",
            body="Testing retry classification",
            max_attempts=2,
            idempotency_key="idem-cp20-retry",
        )
        session.add(msg_retry)
        await session.flush()

        r_try1 = await dispatch_outbound_message(session, msg_retry)
        assert r_try1["status"] == "failed_retryable"
        assert r_try1["attempts"] == 1
        assert msg_retry.next_attempt_at is not None

        r_try2 = await dispatch_outbound_message(session, msg_retry)
        assert r_try2["status"] == "dead_letter"
        assert r_try2["attempts"] == 2
        assert msg_retry.next_attempt_at is None
        assert len(msg_retry.attributes["delivery_ledger"]) == 2

        # 3. Terminal HTTP 400 -> failed_terminal immediately on attempt 1
        wh_srv.response_queue.append((400, {"error": "bad payload schema"}))
        msg_term = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient=wh_srv.url,
            subject="Terminal 400 Test",
            body="Bad request",
            max_attempts=3,
            idempotency_key="idem-cp20-term",
        )
        session.add(msg_term)
        await session.flush()

        r_term = await dispatch_outbound_message(session, msg_term)
        assert r_term["status"] == "failed_terminal"
        assert r_term["attempts"] == 1
        assert msg_term.next_attempt_at is None

        # 4. Egress policy block when loopback is disabled -> failed_terminal with zero network requests
        monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "false")
        req_count_before = len(wh_srv.requests)
        msg_egress = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient=wh_srv.url,
            subject="Blocked Loopback",
            body="Should be blocked by SSRF guard",
            idempotency_key="idem-cp20-egress",
        )
        session.add(msg_egress)
        await session.flush()
        r_egress = await dispatch_outbound_message(session, msg_egress)
        assert r_egress["status"] == "failed_terminal"
        assert len(wh_srv.requests) == req_count_before
        monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")

        # 5. SMTP Email adapter delivery against local RFC 5321 server
        msg_email = OutboundMessage(
            org_id=org.id,
            channel="email",
            recipient="company.man@rig.example.com",
            subject="Daily Drilling Summary",
            body="24h footage: 185 m in 8-1/2 in section.",
            payload={"smtp_url": smtp_srv.url, "from_address": "drillai@rig.example.com"},
            idempotency_key="idem-cp20-email-01",
        )
        session.add(msg_email)
        await session.flush()
        r_email = await dispatch_outbound_message(session, msg_email)
        assert r_email["status"] == "sent"
        assert len(smtp_srv.messages) == 1
        assert "X-DrillAI-Idempotency-Key: idem-cp20-email-01" in smtp_srv.messages[0]
        assert "24h footage: 185 m" in smtp_srv.messages[0]


async def test_cp20_03_workflow_output_notify_requires_l4_human_approval_before_dispatch(
    session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that a workflow containing `output.notify` pauses for L4 human approval before
    sending any notification, and dispatches through WebhookNotificationAdapter once approved."""
    reset_egress_policy()
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")

    org = Organization(slug="cp20-wf-org", name="CP20 Workflow Org", kind="operator")
    session.add(org)
    await session.flush()
    proj = Project(org_id=org.id, name="CP20 WF Project")
    session.add(proj)
    await session.flush()
    well = Well(org_id=org.id, project_id=proj.id, name="CP20-WF-1")
    session.add(well)
    await session.flush()

    supervisor = Principal(
        id="usr_cp20_supervisor",
        kind="user",
        role_keys=("drilling_supervisor",),
        permissions=frozenset({"**"}),
        max_action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
    )

    async with _LocalWebhookServer() as wh_srv:
        graph = WorkflowGraph(
            nodes=[
                GraphNode(
                    id="notify_rig",
                    type="output.notify",
                    name="Notify Rig Supervisor",
                    config={
                        "channel": "webhook",
                        "recipient": wh_srv.url,
                        "subject": "Kill Sheet Ready",
                        "body": "Kill mud weight 1320 kg/m3 verified.",
                        "dispatch_immediately": True,
                    },
                )
            ]
        )
        wf_svc = WorkflowService(session, org_id=org.id, principal=supervisor)
        wf = await wf_svc.create(key="cp20_notify_wf", name="CP20 L4 Notification Workflow", graph=graph)
        ver = await wf_svc.get_version(wf.id)
        runtime = WorkflowRuntime(session, org_id=org.id, principal=supervisor)

        # 1. Start workflow without approval -> pauses at waiting_approval, 0 HTTP requests made
        run_waiting = await runtime.start(
            graph,
            workflow_id=wf.id,
            workflow_version_id=ver.id,
            workflow_key=wf.key,
            scope=RunScope(project_id=proj.id, well_id=well.id),
        )
        assert run_waiting.status == "waiting_approval"
        assert len(wh_srv.requests) == 0

        approval = (
            await session.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == run_waiting.pending_approval_id)
            )
        ).scalar_one()
        assert approval.action_level == "L4"
        assert approval.status == "pending"

        # 2. Approve L4 request and resume workflow -> dispatches webhook notification
        approval.status = "approved"
        approval.decided_by = supervisor.id
        approval.decided_at = dt.datetime.now(tz=dt.UTC)
        await session.flush()

        run_done = await runtime.resume(
            run_waiting.id,
            graph,
            approval_id=approval.id,
            decided_by=supervisor.id,
        )
        assert run_done.status == "succeeded"
        assert len(wh_srv.requests) == 1

        obm = (
            await session.execute(
                select(OutboundMessage).where(OutboundMessage.workflow_run_id == run_done.id)
            )
        ).scalar_one()
        assert obm.status == "sent"
        assert obm.provider_message_id == "wh_msg_1"


def test_cp20_04_external_vendor_interop_commissioning_contract() -> None:
    """Verify scripts/verify_external_vendor_interop.py reports network_calls_made=0,
    external_vendor_verified=False, BLOCKED_BY_EXTERNAL_EVIDENCE, and commissioning_checklist."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_external_vendor_interop.py"), "--mode", "harness"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["network_calls_made"] == 0
    assert payload["external_vendor_verified"] is False
    assert payload["external_gate_status"] == "BLOCKED_BY_EXTERNAL_EVIDENCE"
    assert len(payload["commissioning_checklist"]) >= 4
