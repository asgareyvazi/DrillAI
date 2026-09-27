# End-to-end assets

`synthetic_ddr_upload.txt` is the upload used by the document journey. It is copied **verbatim** from
the backend's own ingestion fixture (`backend/tests/fixtures/synthetic_ddr.py::SYNTHETIC_DDR_TEXT`),
so the browser uploads exactly the text the backend's extractors are tested against. If that fixture
changes, recopy this file in the same commit — the journey asserts that ingestion produces extraction
records, so a mismatch shows up as a failing test rather than as silent drift.

Every line of the file is synthetic test data; it contains no well, operator or field data.
