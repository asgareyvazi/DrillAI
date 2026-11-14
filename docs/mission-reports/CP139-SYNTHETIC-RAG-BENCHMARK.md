# CP139 — Scaled, Mode-Separated Synthetic RAG Benchmark

**Disposition:** the repository-executable benchmark work is implemented and locally measured. This is not a semantic-retrieval, production-corpus, provider, field, or customer acceptance result. No pass threshold is asserted for this development benchmark.

## Dataset and execution

The versioned generator `drillai-rag-synthetic-v4` builds an isolated SQLite corpus and a fixed query suite. Dataset SHA-256: `de60b80f03eb0c5699a3e6d505dfdf46b84ff35f6d564691c47c00ee14ec1678`.

- **508 chunks:** 499 in the primary synthetic well, 4 in a second same-tenant well, and 5 in a different synthetic tenant. The data includes 256 target chunks, 192 same-scope near-miss chunks sharing parameter tokens and explicit depth/time windows, unknown-location/time distractors, four quarantined chunks, and cross-well/cross-tenant distractors.
- **278 distinct query texts:** 260 positive queries and 18 no-relevant queries. The four generated positive-query strata each contain 64 queries (`identifier_lookup`, `record_phrase`, `parameter_phrase`, `canonical_value`); the generated `no_match` stratum contains 16. Six baseline cases cover report facts, conflicting values, program data, and unknown depth/time. Query text is represented by a SHA-256 digest in the per-query evidence; no query or chunk transcript is included in the JSON result.
- `K=5`. Metadata and lexical retrieval each run all 278 queries as distinct modes with separate outputs and metrics. The RAG/Advisor code paths and document-scope filters are the real application retrieval implementation; corpus and labels are synthetic fixtures.
- Semantic and hybrid are requested and probed separately. Both safely fall back to lexical because no authentic stored chunk embeddings exist. They are `not_evaluated`, have **no semantic/hybrid score**, and are not counted as semantic results. The benchmark inserts no vectors, reports zero synthetic vectors and zero provider embedding calls, and a test sentinel raises if an embedding provider is called.

The reproducible machine-readable report, including both measured-mode per-query IDs/ranks, mode/stratum metrics, scope/citation evidence, and semantic/hybrid availability, is `CP139-SYNTHETIC-RAG-BENCHMARK-EVIDENCE.json` (SHA-256 `d5893c49a06c37f91c11446f8f01e98b60b7cf8e157c92433fd17222eb035dff`, 793,426 bytes). Corpus/query text is not stored in that measurement artifact; the versioned generator and dataset hash define its synthetic inputs.

## Measured local results

| Metric (`K=5`) | Lexical | Metadata-only |
| --- | ---: | ---: |
| Queries / positive / no relevant | 278 / 260 / 18 | 278 / 260 / 18 |
| Micro Recall@5 | 1.0000 | 1.0000 |
| Macro Recall@5, positive queries | 1.0000 | 1.0000 |
| Macro Precision@5, all queries | 0.18705 | 0.18705 |
| MRR, positive queries | 1.0000 | 0.50654 |
| Macro nDCG@5, all queries | 0.93525 | 0.58931 |
| No-relevant queries with any hit | 0 / 18 | 4 / 18 (22.22%) |
| Retrieved hits / non-relevant hits | 695 / 435 | 900 / 640 |
| Scope violations / forbidden-chunk retrievals | 0 / 0 | 0 / 0 |
| Citation / source-locator coverage | 100% / 100% | 100% / 100% |

Lexical retrieval ranks the positive fixture target first for each positive query, while hard negatives occupy much of the remaining top five: the resulting Precision@5 is **0.18705**, not a blanket quality pass. Metadata-only ranking is measured separately and is visibly worse on MRR/nDCG and no-relevant-query false positives. There are no tuned acceptance thresholds or field labels behind these numbers.

Scope coverage across the 278 queries includes 277 section-scoped, 274 depth-windowed, and 275 period-windowed cases. The 508 chunks include 4 quarantined, 4 cross-well and 5 cross-tenant distractors. Both measured modes returned zero out-of-scope hits, zero forbidden IDs, and complete citation/source-locator fields for all returned hits.

## Verification

- Complete `backend/tests/rag` selection, including the benchmark and retriever contract tests after branch reconciliation: **58 passed in 39.41s**.
- The benchmark test executes twice and asserts identical dataset hash, per-mode/per-stratum metrics, and per-query evidence. The benchmark assigns fixed creation times so metadata-order measurements are reproducible rather than depending on wall-clock insertion order.
- The semantic/hybrid test sentinel's `embed` method raises unconditionally; both probes remain unavailable without calling it.
- The CLI `python -m drillai.rag.benchmark --output /tmp/cp139-rag-benchmark-reconciled.json` completed after branch reconciliation and reproduced the checked-in evidence byte-for-byte (SHA-256 `d5893c49a06c37f91c11446f8f01e98b60b7cf8e157c92433fd17222eb035dff`). Backend Ruff and Python compilation, plus `git diff --check`, passed for this CP139 change.
- No benchmark corpus or report came from field documents, external vendors, customer content, provider responses, or an independent adjudication set.

## Evidence boundary / follow-on

This result establishes that a deterministic, DB-backed synthetic benchmark measures more than the prior 160-chunk/106-query lexical baseline and reports metadata and lexical modes separately. It does **not** establish semantic retrieval quality, live-model answers, evidence-grounded Advisor acceptance, field/engineering accuracy, production readiness, or customer acceptance. Semantic and hybrid are explicitly unavailable until genuine, authorized embeddings are supplied and stored; future measurement must not substitute lexical fallback or fabricated vectors. CP140 remains responsible for evidence-grounded Advisor claims and safe abstention; CP151 owns exact-final-SHA cross-platform certification.
