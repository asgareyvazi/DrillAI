# مأموریت ۱۲ — بازیابی مهندسی و تضمین مستقل

**وضعیت کلی: NOT_ACCEPTED. صلاحیت میدانی: BLOCKED_BY_MISSING_AUTHORIZED_EVIDENCE.** این تحویل یک پیشرفت جزئی است، نه اتمام همه فازها و نه تأیید استفاده در چاه واقعی.

## اقدامات

- شاخه و workspace با HEAD تأییدشدهٔ دوردست بدون بازنویسی فایل‌ها تطبیق داده شد. manifest دقیق اولیه با مجوز محدود و خارج از Git نگهداری شد و فایل‌های untracked اولیه حفظ شدند. هر ۹ مدخل protected در مقایسه ثبت‌شده با ابتدای مأموریت، از جمله mtime، یکسان بودند. mtime آنها نسبت به پایان مأموریت ۱۱ در آغاز این مأموریت تفاوت داشت و علت آن معلوم نیست. جزئیات در [تعیین تکلیف workspace](MISSION12-WORKSPACE-DISPOSITION.json) و [مقایسه protected](MISSION12-PROTECTED-CHECK.json) آمده است.
- انتظار پس از خاتمهٔ ناموفق فرزند مستقیم OCR محدود شد. در آزمون مصنوعی، descendant با `setsid` از گروه فرایندی پیش‌فرض POSIX گریخت. حالت اختیاری `DRILLAI_OCR_PROCESS_ISOLATION=linux_pid_namespace` با `unshare` و supervisor در Linux اضافه شد و در نبود امکان اجرا بدون بازگشت به حالت ضعیف‌تر شکست می‌خورد. آزمون واقعی محلی فرزند گریزکرده را هم در timeout و هم بعد از خروج رهبر بررسی کرد. این نتیجه به معنای پشتیبانی اثبات‌شدهٔ Docker یا deployment، مهار Windows، یا deadline سخت برای کل سند نیست. حالت پیش‌فرض همچنان legacy و ضعیف‌تر است؛ حالت اختیاری به commissioning مستقل نیاز دارد.
- بازهٔ زمانی NPT در گزارش، API و خروجی JSON منتقل و اعتبارسنجی شد؛ caveat صریح اضافه شد. رفتار legacy ردیف هم‌پوشان را کامل حساب می‌کند و proration یا reconciliation مصوب وقایع نیست؛ سایر بخش‌های گزارش هم الزاماً محدود به همین بازه نیستند.
- فهرست ۳۰ الزام، ۳۱ فاز، ۱۶ engine و ۱۸ حوزهٔ تاریخی تهیه شد، اما موارد بررسی‌نشده `UNKNOWN` باقی ماندند. سه آزمون تحلیلی CP142 ممیزی کامل ۱۶ engine محسوب نمی‌شود. تعریف اصلی ۲۶ فاز پیشین از مستندات موجود بازیابی نشد.

## نتایج و محدودیت‌ها

آزمون متمرکز OCR/runtime/annotation: **۴۴ موفق** در این میزبان Linux، شامل شاخهٔ واقعی namespace. آزمون متمرکز NPT/API: **۷۸ موفق**؛ CP142: **۳ موفق**؛ frontend lint و typecheck موفق، **۴۲۳ آزمون واحد موفق** و inventory مرورگر **۱۳۰ تست در ۲۰ فایل**؛ guard مستندات موفق. پایلوت مصنوعی دوچاهی CP150: **۱ از ۱ موفق، بدون retry، ۲٫۱ دقیقه**. هیچ‌کدام اعتبارسنجی میدانی نیستند. آزمون‌های کامل SQLite و مرورگر پیش از تکمیل کد namespace شروع شدند؛ نتیجه نهایی آنها باید جداگانه بررسی شود و گواهی snapshot نهایی نیست.

CI مبنا [45345525273](https://github.com/asgareyvazi/DrillAI/actions/runs/45345525273) روی `d3ec562f6137ff99009711abb037df092928a8ee` در گیت واقعی OCR تصویر deployment شکست خورد، هرچند jobهای Windows موفق بودند. روی corpus ثابت، فیلدهای بحرانی **۳۰۸ از ۳۸۴**، فارسی **۱۱۸ از ۱۹۲** و انگلیسی **۱۹۰ از ۱۹۲** بودند؛ حداقل لازم ۹۰٪ کل و هر stratum است. مشکل کیفیت حل نشده و corpus، threshold، آزمون یا CI تضعیف نشده است. برای تأیید، CI کامل Linux و Windows باید روی همان SHA نهایی pushشده موفق باشد.

## اسناد و ادامه کار

[ماتریس الزامات](MISSION12-REQUIREMENT-MATRIX.json)، [دفتر فازها](MISSION12-PHASE-LEDGER.json)، [تضمین engine](MISSION12-ENGINE-ASSURANCE.json)، [تاریخچه](MISSION12-HISTORICAL-CAPABILITY-MATRIX.json)، [نتایج golden](MISSION12-GOLDEN-RESULTS.json)، [CI](MISSION12-CI-CERTIFICATION.json)، [ردیابی](MISSION12-SOURCE-TO-REPORT-TRACEABILITY.json) و [roadmap](MISSION12-CAPABILITY-ROADMAP.json) جزئیات را حفظ می‌کنند. اصلاح OCR deployment، مهار cross-platform و deadline سند، حسابداری مصوب NPT، ممیزی مستقل کامل، سناریوی golden کامل، CI دقیق SHA نهایی و شواهد مجاز میدانی همچنان بازند.
