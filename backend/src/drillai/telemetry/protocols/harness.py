"""Non-production local WITSML 1.4.1.1 SOAP and ETP 1.2 WebSocket protocol harness manager.

Starts real local TCP servers on ``127.0.0.1`` for automated integration and Playwright E2E testing
when ``settings.is_production`` is ``False``. Unconditionally refused in production.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Any

from drillai.core.clock import utc_now
from drillai.core.config import get_settings
from drillai.core.errors import PermissionDenied
from drillai.telemetry.protocols.etp import EtpSamplePoint, LocalEtpWebSocketServer
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer, WitsmlSampleRow


@dataclass
class LocalProtocolHarnessManager:
    """Lifecycle manager for in-process TCP WITSML SOAP and ETP WebSocket test servers."""

    witsml_server: LocalWitsmlSoapServer | None = field(default=None, init=False)
    etp_server: LocalEtpWebSocketServer | None = field(default=None, init=False)

    def _assert_non_production(self) -> None:
        settings = get_settings()
        if settings.is_production:
            raise PermissionDenied(
                "local protocol test harness is disabled in production",
                details={"reason": "production_forbidden"},
            )

    async def ensure_started(self) -> dict[str, Any]:
        self._assert_non_production()
        os.environ.setdefault("DRILLAI_SECRET_HARNESS_WITSML_USER", "harness_witsml")
        os.environ.setdefault("DRILLAI_SECRET_HARNESS_WITSML_PASS", "HarnessWitsmlSecret123!")
        os.environ.setdefault("DRILLAI_SECRET_HARNESS_ETP_TOKEN", "HarnessEtpBearerToken123!")

        now = utc_now()
        if self.witsml_server is None or self.witsml_server._server is None:
            self.witsml_server = LocalWitsmlSoapServer(
                host="127.0.0.1",
                port=0,
                expected_username="harness_witsml",
                expected_password="HarnessWitsmlSecret123!",
            )
            self.witsml_server.seed_default_rows(
                start=now - dt.timedelta(seconds=20), count=5, step_seconds=4.0
            )
            await self.witsml_server.start()

        if self.etp_server is None or self.etp_server._server is None:
            self.etp_server = LocalEtpWebSocketServer(
                host="127.0.0.1",
                port=0,
                expected_bearer_token="HarnessEtpBearerToken123!",
            )
            self.etp_server.seed_default_points(
                start=now - dt.timedelta(seconds=20), count=5, step_seconds=4.0
            )
            await self.etp_server.start()

        return self.status()

    def status(self) -> dict[str, Any]:
        self._assert_non_production()
        return {
            "witsml": {
                "running": bool(self.witsml_server and self.witsml_server._server is not None),
                "endpoint_url": self.witsml_server.endpoint_url if self.witsml_server else None,
                "fault_mode": self.witsml_server.fault_mode if self.witsml_server else "ok",
                "requests_received": self.witsml_server.requests_received if self.witsml_server else 0,
                "row_count": len(self.witsml_server.rows) if self.witsml_server else 0,
                "secret_refs": {
                    "username": "env:DRILLAI_SECRET_HARNESS_WITSML_USER",
                    "password": "env:DRILLAI_SECRET_HARNESS_WITSML_PASS",
                },
            },
            "etp": {
                "running": bool(self.etp_server and self.etp_server._server is not None),
                "endpoint_url": self.etp_server.endpoint_url if self.etp_server else None,
                "fault_mode": self.etp_server.fault_mode if self.etp_server else "ok",
                "sessions_opened": self.etp_server.sessions_opened if self.etp_server else 0,
                "point_count": len(self.etp_server.points) if self.etp_server else 0,
                "secret_refs": {
                    "bearer_token": "env:DRILLAI_SECRET_HARNESS_ETP_TOKEN",
                },
            },
        }

    async def configure(
        self,
        *,
        protocol: str,
        fault_mode: str | None = None,
        append_values: dict[str, float | None] | None = None,
        depth_md: float = 2500.0,
        quality: str = "good",
        reset_rows: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_started()
        now_iso = utc_now().isoformat()
        proto = protocol.strip().lower()
        if proto == "witsml" and self.witsml_server is not None:
            if fault_mode is not None:
                self.witsml_server.fault_mode = fault_mode
            if reset_rows:
                self.witsml_server.rows.clear()
            if append_values:
                self.witsml_server.rows.append(
                    WitsmlSampleRow(
                        timestamp=now_iso,
                        depth_md=depth_md,
                        values={k.upper(): v for k, v in append_values.items()},
                        quality=quality,
                    )
                )
        elif proto == "etp" and self.etp_server is not None:
            if fault_mode is not None:
                self.etp_server.fault_mode = fault_mode
            if reset_rows:
                self.etp_server.points.clear()
            if append_values:
                ch_map = {ch.mnemonic.upper(): ch.channel_id for ch in self.etp_server.channels}
                for mnem, val in append_values.items():
                    mnem_up = mnem.upper()
                    self.etp_server.points.append(
                        EtpSamplePoint(
                            channel_id=ch_map.get(mnem_up, 1),
                            mnemonic=mnem_up,
                            timestamp=now_iso,
                            value=val,
                            depth_md=depth_md,
                            quality=quality,
                        )
                    )
        return self.status()

    async def stop(self) -> None:
        if self.witsml_server is not None:
            await self.witsml_server.stop()
            self.witsml_server = None
        if self.etp_server is not None:
            await self.etp_server.stop()
            self.etp_server = None
