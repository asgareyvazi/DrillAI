"""Runtime-preflight contract tests; software presence is not OCR-quality evidence."""

from __future__ import annotations

import json

import pytest

from drillai.core.config import Settings
from drillai.ingestion import ocr_runtime


class StubProvider:
    name = "tesseract"
    adapter_version = "tesseract-cli-tsv-v5"
    version = "tesseract-cli-tsv-v5; tesseract 5.4.1"

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def diagnostics(self):
        return {
            "available": True,
            "runtime_version": "tesseract 5.4.1",
            "configured_languages": ["eng", "fas"],
            "available_languages": ["eng", "fas", "osd"],
            "missing_languages": [],
            "reason": None,
        }


def test_ocr_language_configuration_is_normalized_and_invalid_values_are_rejected() -> None:
    assert Settings(_env_file=None, ocr_languages="eng, fas").ocr_languages == "eng+fas"
    with pytest.raises(ValueError, match="Tesseract language"):
        Settings(_env_file=None, ocr_languages="eng;injected")


def test_disabled_ocr_does_not_probe_or_block_readiness(monkeypatch) -> None:
    def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("disabled OCR must not run runtime probes on every readiness request")

    monkeypatch.setattr(ocr_runtime, "TesseractOcrProvider", unexpected_provider)
    report = ocr_runtime.inspect_ocr_runtime(
        Settings(_env_file=None, ocr_enabled=False), probe_when_disabled=False
    )
    assert report["enabled"] is False
    assert report["status"] == "disabled"
    assert report["runtime_status"] == "not_checked"
    assert report["required_ready"] is True
    assert report["quality_evaluated"] is False


def test_enabled_runtime_requires_tesseract_languages_and_pdfium(monkeypatch) -> None:
    ocr_runtime._runtime_snapshot.cache_clear()
    monkeypatch.setattr(ocr_runtime, "TesseractOcrProvider", StubProvider)
    monkeypatch.setattr(ocr_runtime, "_pdfium_details", lambda: (True, "4.30.0"))
    settings = Settings(
        _env_file=None,
        ocr_enabled=True,
        ocr_languages="eng+fas",
        ocr_max_concurrency=3,
    )

    report = ocr_runtime.inspect_ocr_runtime(settings)

    assert report["status"] == "ready"
    assert report["runtime_status"] == "ready"
    assert report["runtime_ready"] is True
    assert report["required_ready"] is True
    assert report["runtime_version"] == "tesseract 5.4.1"
    assert report["available_languages"] == ["eng", "fas", "osd"]
    assert report["pdf_renderer_version"] == "4.30.0"
    assert report["configuration"]["max_concurrent_tesseract_processes"] == 3
    assert report["configuration"]["page_segmentation_mode"] == 6
    assert report["configuration"]["two_column_page_segmentation_mode"] == 11
    assert report["configuration"]["layout_detection"] == "visible-central-divider-only; otherwise-unclassified"
    assert report["quality_evaluated"] is False
    assert "/opt" not in str(report)


def test_enabled_runtime_probe_is_cached_and_returned_without_shared_mutable_state(monkeypatch) -> None:
    ocr_runtime._runtime_snapshot.cache_clear()
    calls = 0

    class CountingProvider(StubProvider):
        def diagnostics(self):
            nonlocal calls
            calls += 1
            return super().diagnostics()

    monkeypatch.setattr(ocr_runtime, "TesseractOcrProvider", CountingProvider)
    monkeypatch.setattr(ocr_runtime, "_pdfium_details", lambda: (True, "4.30.0"))
    settings = Settings(_env_file=None, ocr_enabled=True)

    first = ocr_runtime.inspect_ocr_runtime(settings)
    second = ocr_runtime.inspect_ocr_runtime(settings)
    second["available_languages"].append("tampered")

    assert calls == 1
    assert first["available_languages"] == ["eng", "fas", "osd"]


def test_enabled_runtime_is_blocked_if_renderer_is_missing(monkeypatch) -> None:
    ocr_runtime._runtime_snapshot.cache_clear()
    monkeypatch.setattr(ocr_runtime, "TesseractOcrProvider", StubProvider)
    monkeypatch.setattr(ocr_runtime, "_pdfium_details", lambda: (False, None))
    report = ocr_runtime.inspect_ocr_runtime(Settings(_env_file=None, ocr_enabled=True))
    assert report["status"] == "blocked"
    assert report["runtime_status"] == "blocked"
    assert report["required_ready"] is False
    assert report["reason"] == "PDFium renderer is not installed or could not be loaded"


def test_runtime_cli_nonzero_exit_is_reserved_for_required_but_missing_dependencies(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ocr_runtime, "get_settings", lambda: Settings(_env_file=None, ocr_enabled=False))
    monkeypatch.setattr(
        ocr_runtime,
        "inspect_ocr_runtime",
        lambda **_kwargs: {
            "enabled": False,
            "runtime_ready": False,
            "required_ready": True,
            "status": "disabled",
            "quality_evaluated": False,
        },
    )
    assert ocr_runtime.main([]) == 0
    assert '"quality_evaluated": false' in capsys.readouterr().out
    assert ocr_runtime.main(["--require-runtime"]) == 1


def test_synthetic_runtime_canary_checks_a_generated_image_without_emitting_ocr_text(monkeypatch) -> None:
    expected_text = "SYNTHETIC OCR CANARY MUST NOT LEAK"

    class CanaryProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def availability(self):
            return True, None

        def recognize(self, image_png):
            assert image_png.startswith(b"\x89PNG\r\n\x1a\n")
            assert len(image_png) <= 2 * 1024 * 1024
            return [type("Line", (), {"text": expected_text})()]

    monkeypatch.setattr(ocr_runtime, "TesseractOcrProvider", CanaryProvider)
    report = ocr_runtime.run_synthetic_runtime_canary(
        Settings(_env_file=None, ocr_enabled=False, ocr_languages="eng+fas")
    )

    assert report["case_id"] == "drillai-ocr-runtime-canary-v1"
    assert report["source"] == "generated_in_memory"
    assert report["status"] == "passed"
    assert report["line_count"] == 1
    assert report["recognized_text_emitted"] is False
    assert report["quality_evaluated"] is False
    assert expected_text not in json.dumps(report)
    assert "does not score transcription accuracy" in report["note"]


def test_cli_self_test_runs_only_after_full_runtime_preflight_and_fails_closed(monkeypatch, capsys) -> None:
    settings = Settings(_env_file=None, ocr_enabled=False)
    monkeypatch.setattr(ocr_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(
        ocr_runtime,
        "inspect_ocr_runtime",
        lambda **_kwargs: {
            "enabled": False,
            "runtime_ready": False,
            "required_ready": True,
            "status": "disabled",
            "reason": "PDFium renderer is not installed or could not be loaded",
            "quality_evaluated": False,
        },
    )

    assert ocr_runtime.main(["--self-test"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["synthetic_canary"]["status"] == "blocked"
    assert report["synthetic_canary"]["quality_evaluated"] is False

    monkeypatch.setattr(
        ocr_runtime,
        "inspect_ocr_runtime",
        lambda **_kwargs: {
            "enabled": False,
            "runtime_ready": True,
            "required_ready": True,
            "status": "disabled",
            "reason": None,
            "quality_evaluated": False,
        },
    )
    monkeypatch.setattr(
        ocr_runtime,
        "run_synthetic_runtime_canary",
        lambda _settings: {
            "case_id": "drillai-ocr-runtime-canary-v1",
            "source": "generated_in_memory",
            "status": "passed",
            "recognized_text_emitted": False,
            "quality_evaluated": False,
            "reason": None,
            "note": "synthetic runtime canary only",
        },
    )
    assert ocr_runtime.main(["--self-test"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["synthetic_canary"]["status"] == "passed"
    assert report["quality_evaluated"] is False
