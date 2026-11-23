# Mission 9 continuation: OCR process containment

**NOT_ACCEPTED.** This is a bounded reliability improvement, not OCR quality or end-to-end deadline certification. The original Mission 9 reports describe the state before this continuation.

## Change and verification

On POSIX, each recognition invocation now starts in a new session and its process group is killed on timeout, output overflow, and after leader exit before pipe threads are joined. This addresses ordinary descendants that inherit OCR pipes, including after a nominally successful leader exit. Process creation time is deducted from the existing recognition budget before waiting. Existing output and incomplete-input checks remain in place. The frozen quality corpus, scorer, language routing and segmentation choices are unchanged.

The existing POSIX executable contract test now starts real descendants in both timeout and successful-leader-exit scenarios, verifies their start and checks that they cannot write a delayed survival marker. A platform-independent fake-process scenario checks that a slow spawn gets no renewed wait budget. The focused OCR file passed 28 tests locally; Ruff passed. The first local test attempt failed because the new test requested an invalid 0.7-second configured timeout; it was corrected to the existing one-second minimum and rerun. This is not a native Windows process-tree test, a deployment-image OCR benchmark, or field evidence.

This is **not a hard end-to-end deadline**: synchronous layout analysis and PDF parsing/rasterization cannot currently be interrupted, diagnostics are not in this containment, spawn and cleanup may block, and a POSIX descendant that deliberately creates a new session can escape the process group. On Windows only the direct recognition child is stopped; a Windows Job Object or equivalent containment remains necessary. Process-group teardown does not prove termination of hostile escaped descendants. No claim is made that recognition, layout, extraction or engineering correctness has improved.

## Previously completed exact-SHA CI

The previous pushed SHA `76bef500a85c09f5362b1011ed42055a0aac07c1` completed [CI 45023680246](https://github.com/asgareyvazi/DrillAI/actions/runs/45023680246) with Linux failure at deployment-image OCR quality and native Windows certification and independent diagnostics passing. The unchanged 64-case deployment corpus produced CER 0.1083959490, WER 0.1550687285, and critical 308/384, below the required 0.90 overall and in several strata; Persian critical was 118/192. Full Windows authoring file and three independent full frontend suites passed, with 421 tests per suite. This is evidence for that earlier SHA only, not this continuation's final SHA. Diagnostic artifact ID `14755080801`; checkout artifact ID `14754946364`.

The overall mission still needs the hard deadline and cross-platform containment, qualifying OCR on the unchanged deployment benchmark, governed source-linked workflows and engineering audit described in the original reports, and successful Linux and Windows CI on the exact final pushed SHA. A partial containment fix cannot change the release decision.

## ادامهٔ فارسی

**NOT_ACCEPTED — پذیرفته نشده.** در POSIX پردازهٔ OCR در نشست جدا اجرا می‌شود و گروه پردازه هنگام timeout و پس از خروج پردازهٔ اصلی خاتمه می‌یابد؛ آزمون محلی با فرزند واقعی و آزمون زمان ایجاد پردازه انجام شد. این تغییر بهبود دقت OCR یا تحقق مهلت سراسری نیست. تحلیل layout و rasterization هنوز قابل قطع نیستند؛ در ویندوز فقط پردازهٔ اصلی متوقف می‌شود و فرزند جداشده در POSIX نیز ممکن است از گروه خارج شود. معیار ۶۴ نمونه‌ای و scorer ثابت مانده‌اند. اجرای پیشین روی SHA ذکرشده در لینوکس به علت کیفیت OCR شکست خورد و ویندوز موفق بود؛ نتیجهٔ آن برای SHA جدید قابل تعمیم نیست. سایر الزامات مهندسی و CI نهایی همچنان باز هستند.

## Continuation workspace evidence

The restored sandbox initially presented stale HEAD `bfea530602a23bcc08efca85a136240f61c44a0d` and a divergent-looking index. The authorized remote tree at `76bef500a85c09f5362b1011ed42055a0aac07c1` was compared against all 764 worktree entries: zero content/mode mismatches, with only the five protected files extra. The ref and index were reconciled without altering worktree files. Raw starting status, index, untracked and ignored paths, and the comparison are retained as `MISSION9-CONTINUATION-*` evidence. Protected entries match continuation-start type, mode, size, mtime and content hashes. Historical Mission 7-to-8 mtime differences remain disclosed, not repaired.

The continuation workspace manifest records precommit tracked/untracked files, excluding itself, Git internals and ignored files; precommit Git status separately records ignored paths in collapsed directory form. It cannot contain its own final hash or the future commit SHA. Local test caches remain ignored and are not deleted; the isolated test environment and log are in volatile `/tmp/drillai-resume`, not a durable workspace artifact. CI checkout artifacts, if generated, capture the tested commit, not the local protected files.

The subsequent isolated local full SQLite backend run exited zero with 1,890 passing dots and 15 PostgreSQL skips in its quiet log (`MISSION9-CONTINUATION-BACKEND-REGRESSION.txt`). It was started before the additional slow-spawn assertion was written; that assertion was separately covered by the successful focused rerun. PostgreSQL and native Windows are not covered by this local run.
