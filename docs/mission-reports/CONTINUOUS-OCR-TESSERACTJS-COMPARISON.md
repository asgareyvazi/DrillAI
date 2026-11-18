# Continuous mission — offline Tesseract.js synthetic comparison

**Status: NOT_ACCEPTED.** This is an exploratory alternative runtime comparison, not a production OCR adapter, an independent holdout, deployment-image measurement, field test or acceptance of CP135. The existing deployment-image quality gate and its thresholds are unchanged.

## Method

The checked-in synthetic CP135 v3 corpus (`31aca80b4323fe470d34705e36038296ef620adb7525373f5196c9f561b6735d`, 64 cases) was rendered with the existing `_raster_pdf` fixture and PDFium 5.15.2 at 200 DPI. Tesseract.js 7.0.0 / tesseract.js-core 7.0.0 was run offline on Node 22.22.3 using a **single combined `eng+fas` recognition pass, PSM 6** for every case; fixture language was not used to select a model. This path consumes raw Tesseract.js text and does **not** use DrillAI's production PDF parser, TSV adapter, RTL routing or two-column ordering. Differences from the deployment-image benchmark include runtime, traineddata and extraction behavior, so direct claims of improvement are not warranted. The corpus has previously been used in tuning and is not a holdout.

The local npm packages were installed outside the repository solely for this comparison. The compressed model SHA-256 values were `45b4cb346724ac1774f1c36f42f182b887bcdb28ebe63e6fff90ac41f3fcff91` (eng) and `b5847360e25f646c55449f1fe93eee57d53e406a265ead2374e7320bf0b82025` (fas). The decompressed bytes hash to `5dc5d8d640a212c9d6184921ba103b186f50e0fed9ee716c53e6b312b400d747` and `ff6a6743a643ebd93717346ae6526aed110aad4e08ca10240279787fc567522c`, respectively; these are different from the deployment model hashes. npm package license metadata is not a complete distribution/license review. No model or new mandatory dependency was added to the product.

## Results

All 64 synthetic cases produced a recognized result. Using the existing `error_rates` and label-specific `_critical_field_matches` scoring functions:

| Stratum | Cases | CER | WER | Critical exact |
|---|---:|---:|---:|---:|
| Overall | 64 | 0.242404 | 0.356529 | 0.588542 |
| English | 32 | 0.155506 | 0.186632 | 0.781250 |
| Persian | 32 | 0.341491 | 0.522959 | 0.395833 |
| Two-column | 16 | 0.653445 | 0.807560 | 0.125000 |

All unchanged CP135 global thresholds fail; every layout/scan stratum also fails the critical exact floor. This raw-text experiment must not be used as a passing gate or a replacement for the deployed benchmark. Transcript-free per-case and all-strata metrics are retained in [CONTINUOUS-TESSERACTJS-EXPLORATORY-EVIDENCE.json](CONTINUOUS-TESSERACTJS-EXPLORATORY-EVIDENCE.json), SHA-256 `ed96016a5275c6ac58a245477b813ed5dac6fca39fbd4211cd59b75e7a2fcf7f`.

## Reproduction and limits

`scripts/compare_synthetic_ocr_tesseractjs.py` and `scripts/compare_ocr_tesseractjs.cjs` perform the comparison on the pinned synthetic corpus only. Provide a preinstalled local Node package prefix containing `tesseract.js` and `tesseract.js-core`, plus a local directory containing `eng.traineddata.gz` and `fas.traineddata.gz`; install the backend OCR extras separately. No dependency or model is downloaded by the scripts:

```sh
python scripts/compare_synthetic_ocr_tesseractjs.py \
  --module-prefix /path/to/local/prefix \
  --language-dir /path/to/local/languages \
  --output /path/to/new/metrics.json
```

The runner uses a fresh temporary synthetic image directory, local paths only, captures transcripts in process memory and writes no transcript to its metrics report. It refuses to overwrite the requested output. An independent prototype and this reusable runner both evaluated 64 cases and produced the same displayed aggregate metrics. Node syntax and Python Ruff checks passed. Runtime resource/latency profiling, package supply-chain review, a holdout corpus, real data evaluation and integration as an OCR provider remain unperformed. OCR remains `NOT_ACCEPTED`.
