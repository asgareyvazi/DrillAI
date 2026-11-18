# Synthetic OCR diagnosis with a local native library

**Status: NOT_ACCEPTED.** This is a development diagnostic on the previously tuned CP135 v3 synthetic corpus, not an independent holdout, deployed-image result, field sample, production adapter replacement, or proof of the deployment failure's cause. The existing global/stratum thresholds and CI gate were not changed.

## Method and reproducibility

A separate temporary environment installed the public `tesserocr` 2.13.0 wheel (wheel SHA-256 `f56150f5a9eded87a942a13eea358a4339690e968959c40e67b5c40c5a487323`). Its bundled native library reports Tesseract 5.5.4; it is **not** the deployed Tesseract 5.5.0 CLI. `scripts/diagnose_synthetic_ocr_tesserocr.py` wraps that library in a temporary local CLI-protocol emulator (including a TSV header), then runs the real synthetic benchmark, PDFium parser, product line parser, script routing and column ordering. It records only pass language/PSM and per-field match booleans, never transcripts or images. It does not install or configure a production OCR dependency. No licensing or distribution approval of this wheel is claimed.

Run in a separate authorized Linux/POSIX environment after independently installing backend `[dev,ocr]` and `tesserocr==2.13.0`, and obtaining the two language files into a local directory:

```sh
python scripts/diagnose_synthetic_ocr_tesserocr.py --language-dir /path/to/models --output /path/to/new-report.json
```

The output path must not exist. This completed-but-failing diagnostic exits **2**, not success. The wrapper's internal `--shim` mode accepts the synthetic benchmark's rendered PNG on stdin; it is not a supported general-purpose OCR command and must not be used on customer data. The source corpus is the same pinned `31aca80b4323fe470d34705e36038296ef620adb7525373f5196c9f561b6735d`, PDFium 5.15.2 at 200 DPI. The model bytes matched the model hashes recorded by the deployment benchmark: English `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`; Persian `db1c0a91208aff00d3cf1ed2c1d23f76419afd5f024688b4f71adc3f2ce4a505`. Matching traineddata alone does not make the native library, emulated CLI, operating system, parser environment or observed result the same as the deployment image.

A first local prototype was invalid: its emulated TSV omitted the required header, so 0/64 cases completed; no metrics from that run are claimed. After correcting the emulator, an initial 64-case run and the reproducible script produced identical error and match counts. The final transcript-free [per-case evidence](CONTINUOUS-TESSEROCR-SYNTHETIC-EVIDENCE.json) is SHA-256 `d0bb51e9e4506cfaf9c3796ce42ed1e9179fdbda352863cadaf33960184eca18`. It contains pass and field attribution only when every case completes and the per-field booleans reproduce the benchmark's unmodified aggregate score. Two unit tests exercise safe attribution and rejected traces; no fixture language is used to select the recognition pass.

A second isolated run used `tesserocr` 2.9.1 (wheel SHA-256 `dca9f3ddf18f51b9400c1023bad8b4d4a169e4988bac847652c6f2354ad579c2`), reporting native Tesseract 5.5.1 and Leptonica 1.85.0, against the same model bytes and corpus. It again failed all required overall acceptance: its 64 per-case character/word error counts, critical-field booleans and routing/PSM sequences exactly matched the 5.5.4 emulator run. Its [separate transcript-free evidence](CONTINUOUS-TESSEROCR-551-SYNTHETIC-EVIDENCE.json) hashes to `6f5c0cfa88578506df5750f5d6013d8dc06a3526b9030756c85f7729c1cf45d9`. These two library/emulator builds are closer in version to, but still not, the deployed CLI; identical local counts do not establish the cause of the deployment discrepancy.

## Results and localization

| Stratum | Cases | CER | WER | Critical exact |
|---|---:|---:|---:|---:|
| Overall | 64 | 0.107024 | 0.154639 | 0.809896 |
| English | 32 | 0.001594 | 0.009549 | 0.989583 |
| Persian | 32 | 0.227241 | 0.296769 | 0.630208 |
| Table, both languages | 16 | 0.114099 | 0.202749 | 0.510417 |
| Two-column, both languages | 16 | 0.146138 | 0.180412 | 0.895833 |

All 64 were evaluated, but the global critical floor and multiple language/layout/scan strata failed. The local instrumented run recorded one combined `eng+fas` pass for each English case and a combined pass plus `fas` rerun for each Persian case; PSM 11 was chosen for the 16 two-column fixtures and PSM 6 for the others. This establishes routing **only for this library/emulator run**. It neither proves nor disproves a routing problem in the deployment image.

The unchanged label-specific scorer matched the synthetic Persian sample-ID field **0/32**. Persian table cases matched **3/48** critical fields (all three depth fields); the remaining five field types matched 0/8 each. Persian two-column cases matched 38/48; single-column and compact each matched 40/48. These describe fixture-specific string matching, not semantic field accuracy in actual reports. They are useful places to investigate mixed-script recognition, layout segmentation and label/value extraction without guessing an identifier or silently merging conflicting values.

A separate four-case local probe compared `fas`, `fas+eng` and `eng+fas` on one Persian case per layout. Neither mixed ordering recovered the sample ID in those four cases; both mixed settings lost some matches compared with `fas` in three cases. This tiny, selected probe is not a complete comparison or a holdout, so no alternative was adopted. Earlier Tesseract.js raw-text PSM 6 routing exploration also failed its thresholds and differs further from the product path; it is not used as deployment evidence.

## Next validation

Capture comparable bounded pass/PSM provenance **inside the actual deployment image** and investigate why its Persian critical rate is 0/192 despite matching model bytes; the local library observations do not isolate the cause. Evaluate any correction on all unchanged strata, then an independently prepared holdout and reviewed real sources. No successful benchmark, repaired OCR, DDR field acceptance, or release readiness is asserted.

## Synthetic benchmark routing observability

The benchmark's sequential, benchmark-owned provider now temporarily observes the adapter's actual language/PSM calls and restores the original instance method even on failure. Only allowlisted pass identifiers and bounded per-language route counts are retained; unrecognized paths, stubs, failed cases and ambiguous invocations are unknown. Calls through this observer are serialized defensively, and it is not used by product ingestion. A bounded, strictly shaped numeric route summary is emitted in CI annotations; raw text is not added. This does not alter recognition decisions or quality thresholds. An emulator rerun evaluated 64 cases and its observer traces matched all 96 independently recorded emulator passes, while per-case errors and field matches matched the earlier 5.5.4 report. It remained **failed**. The actual deployment CLI's routing remains **NOT_MEASURED** until CI for the new commit runs; local observation cannot be transferred to it.

Local SQLite backend regression after instrumentation: **1,793 passed, 15 skipped**; test inventory verified. Focused OCR/annotation/diagnostic tests and Ruff passed. These local results are not Linux PostgreSQL, native-Windows or exact-commit deployment certification.

The actual deployment image was subsequently measured in [run 44296549169](https://github.com/asgareyvazi/DrillAI/actions/runs/44296549169) on `bb97059abf1fb02850056b1844b66dfd6e2be023`: 32/32 English cases were combined-only, 31/32 Persian cases had a `fas` rerun, and one Persian case was combined-only. All 64 were evaluated; CER 0.318719, WER 0.457904 and critical exact 191/384 still failed the unchanged gate, with Persian 0/192 critical matches. Therefore the local all-Persian-rerun observation must not be transferred wholesale to deployment; the missing rerun for one Persian case cannot by itself explain failures in the other 31. Root cause and independent holdout remain open.
