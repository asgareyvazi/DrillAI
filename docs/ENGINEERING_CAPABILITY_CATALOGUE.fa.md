# فهرست اولیه قابلیت‌های مهندسی

این فهرست متصل به منبع، ممیزی مستقل یا فهرست جامع تاریخی و محصول نیست. اعتبارسنجی میدانی، پذیرش مشتری و تأیید سازنده اثبات نشده‌اند.

| موتور | نسخه | وضعیت | منبع |
|---|---|---|---|
| `drilling.mse` | 1.1.1 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/mechanics.py` |
| `hydraulics.laminar` | 1.7.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/fluids.py` |
| `hydraulics.power_law_pipe` | 1.0.0 | `INDEPENDENT_SOFTWARE_REFERENCE_CHECKED` | `backend/src/drillai/engines/power_law.py` |
| `integrity.barrier_envelope` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/integrity.py` |
| `offsets.similarity` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/offsets.py` |
| `optimization.pareto` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/optimization.py` |
| `optimization.sweep` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/optimization.py` |
| `readiness.assessment` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/readiness.py` |
| `schematic.well_model` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/schematic.py` |
| `torque_drag.soft_string` | 1.1.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/mechanics.py` |
| `trajectory.geometric_proximity` | 1.0.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/proximity.py` |
| `trajectory.minimum_curvature` | 1.3.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/trajectory.py` |
| `tubulars.api_5c3` | 1.1.0 | `NEEDS_FIELD_VALIDATION` | `backend/src/drillai/engines/tubulars.py` |
| `twin.reconciliation` | 1.2.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/twin.py` |
| `well_control.kill_sheet` | 1.2.0 | `NEEDS_FIELD_VALIDATION` | `backend/src/drillai/engines/wellcontrol.py` |
| `wellbore.capacity` | 1.4.0 | `IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED` | `backend/src/drillai/engines/fluids.py` |

## نقاط تاریخی منتخب
- `bfa066b`: آغاز مخزن؛ نشانه توانایی فعلی نیست.
- `09bef03`: بسته اولیه محاسبات مسیر و سیالات.
- `80aee9f`: آزمون‌های تحلیلی نرم‌افزاری؛ نه اعتبارسنجی میدانی.
- `658c4f0`: منشأ مبنا و زمان برداشت مسیر.
- `d7526a1`: محصولات مهندسی دارای حاکمیت و شواهد OCR.
- `11e6cd2`: اصلاح OCR بدون احراز معیار پذیرش.
- `85396bc` و `e212fa1`: محافظت عددی و هندسی ظرفیت و هیدرولیک.
- `0735fc1`: ترتیب قطعی داده آزمون شواهد.

## وضعیت گردش‌کار
- ظرفیت و گردش: ناقص؛ ورود ساخت‌یافته و ذخیره اجرا وجود دارد، نسخه تأییدشده و گزارش کامل وجود ندارد.
- هیدرولیک و ECD: ناقص؛ اتصال معتبر برداشت، مقایسه و گزارش کامل وجود ندارد.
- ضدبرخورد: فقط `GEOMETRIC_PROXIMITY_ONLY`؛ بدون عدم‌قطعیت و ضریب جدایش.
- OCR فارسی: پذیرفته نشده؛ آزمون مستقل holdout موجود نیست.

## حوزه‌های آینده
هیدرولیک آشفته، رئولوژی پیشرفته، پاک‌سازی چاه، عدم‌قطعیت ضدبرخورد، گشتاور و درگ، surge/swab، جداری، سیمان‌کاری، پایداری چاه، کنترل فشار، MPD، اقتصاد چاه و بهینه‌سازی پیشرفته.

جزئیات ورودی، خروجی، واحد، فرض‌ها و منابع ادعاشده در JSON آمده است. هش منبع با نرمال‌سازی CRLF به LF محاسبه می‌شود.
