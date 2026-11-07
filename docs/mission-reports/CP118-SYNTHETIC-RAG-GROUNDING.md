# CP118 — Synthetic RAG Scope, Provenance, and Unknown-State Benchmark

## Disposition

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Offline lexical benchmark expansion | **DONE** | Version `drillai-rag-synthetic-v2`, 13 manifest-in-code synthetic chunks and six fixed development queries, run through production `retrieve` in an isolated SQLite database. |
| Tenant/well/section/type/depth/time/quarantine behavior | **DONE (software tests)** | Dataset includes same-text cross-well and cross-tenant distractors, wrong-section passages, a quarantined artifact, explicit/unknown depth and period values, and conflicting synthetic source values. No forbidden result or scope violation occurred in the measured run. |
| Citation and source-locator plumbing | **DONE (fixture contract)** | All retrieved hits in the benchmark carried citations and exact synthetic source locators. This verifies API plumbing, not source truth. |
| Semantic, hybrid, LLM, or operational quality | **NOT ESTABLISHED** | The benchmark uses deterministic lexical BM25 only, hand-authored labels, no embeddings or LLM, and no real/independently adjudicated documents. `Echo` and deterministic lexical results are not live AI or semantic-quality evidence. |

## Dataset and measurement contract

`backend/src/drillai/rag/benchmark.py` pins version, corpus, query scope and relevance identifiers into a deterministic SHA-256. The synthetic set contains 13 chunks across fictional wells/tenants and the surface/intermediate/program scopes. It includes a same-tenant other-well and other-tenant lexical twin, a quarantined document, a passage whose words mention the wrong section, a conflicting mud-value note, a chunk with unknown depth and period, and chunks outside explicit depth/time windows.

Six queries exercise four positive source lookups and two expected no-result windows. Filters are passed as explicit well/wellbore/section/document type, SI-depth bounds and timezone-aware source-period bounds. Unknown depth or period does not satisfy a bounded query; the database-generated `created_at` is never used as a substitute for source period. The report exposes expected and retrieved chunk IDs, explicit filters, disallowed-result checks, scope violations, and citation/source-locator coverage. It emits no chunk transcript text.

The report labels the corpus `synthetic`, records query count, dataset version and hash, and states that relevance labels are not independently adjudicated. Precision@5 uses the fixed cutoff denominator even when fewer than five matches are returned; positive-query recall and MRR exclude the two no-relevant cases, which are instead reported as expected empty results.

## Local evidence

- `backend/.venv/bin/python -m drillai.rag.benchmark`: **success**, version `drillai-rag-synthetic-v2`, 13 chunks / 6 queries, SHA-256 `02eb21e778c497dbfc2575922abb3a27f39996f888406e52ab557d5b8289c61d`.
- Measured development-fixture metrics: micro Recall@5 **1.0**; macro Recall@5 over positive queries **1.0**; MRR over positive queries **1.0**; macro Precision@5 across all queries **0.133333…**; two expected empty-result cases; zero forbidden chunk retrievals; zero scope violations; citation coverage **1.0**; source-locator coverage **1.0**.
- `backend/.venv/bin/python -m pytest backend/tests/rag backend/tests/drilling/test_advisor.py -q`: **70 passed**. This includes existing retrieval access, tenant-scope, section-boundary, quarantine, explicit-window and Advisor narration/safety tests plus the updated benchmark assertions.
- Targeted Ruff and `compileall` passed.

## Limits

These metrics do not estimate retrieval quality on operational reports, establish semantic relevance, prove a natural-language answer is entailed by its citations, validate an LLM or embedding provider, or authorize engineering/field decisions. P@5's low value on this small fixture is disclosed rather than masked by the perfect positive-query recall. Any real-corpus evaluation requires authorized records and independently reviewed relevance labels. Unknown or conflicting evidence must remain explicit for a human reviewer. No external model, provider, endpoint, or network call was made.
