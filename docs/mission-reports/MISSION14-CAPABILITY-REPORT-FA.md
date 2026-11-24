# گزارش موقت قابلیت‌های مهندسی مأموریت ۱۴

**NOT_ACCEPTED.** این فهرست گواهی میدانی یا ممیزی کامل از منبع تا گزارش نیست. [گزارش مأموریت](MISSION14-REPORT-FA.md)، [دفتر فازها](MISSION14-PHASE-LEDGER.json) و [نامزدهای محاسبات غیرregistry](MISSION14-NONREGISTRY-CALCULATION-INVENTORY.json).

## engineهای ثبت‌شده

| engine | منبع | وضعیت شاهد |
|---|---|---|
| `drilling.mse` | `backend/src/drillai/engines/mechanics.py` | بردار مستقل مصنوعی محدود از مأموریت قبل؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `hydraulics.laminar` | `backend/src/drillai/engines/fluids.py` | بردار مستقل مصنوعی محدود از مأموریت قبل؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `hydraulics.power_law_pipe` | `backend/src/drillai/engines/power_law.py` | بردار مستقل مصنوعی محدود از مأموریت قبل؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `integrity.barrier_envelope` | `backend/src/drillai/engines/integrity.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `offsets.similarity` | `backend/src/drillai/engines/offsets.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `optimization.pareto` | `backend/src/drillai/engines/optimization.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `optimization.sweep` | `backend/src/drillai/engines/optimization.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `readiness.assessment` | `backend/src/drillai/engines/readiness.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `schematic.well_model` | `backend/src/drillai/engines/schematic.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `torque_drag.soft_string` | `backend/src/drillai/engines/mechanics.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `trajectory.geometric_proximity` | `backend/src/drillai/engines/proximity.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `trajectory.minimum_curvature` | `backend/src/drillai/engines/trajectory.py` | بردار مستقل مصنوعی محدود از مأموریت قبل؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `tubulars.api_5c3` | `backend/src/drillai/engines/tubulars.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `twin.reconciliation` | `backend/src/drillai/engines/twin.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `well_control.kill_sheet` | `backend/src/drillai/engines/wellcontrol.py` | بردار مستقل مصنوعی محدود از مأموریت قبل؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |
| `wellbore.capacity` | `backend/src/drillai/engines/fluids.py` | بردار مستقل اثبات نشده؛ ممیزی کامل مدل و یکپارچگی انجام نشده است. |

## سایر قابلیت‌ها

DDR، NPT، OCR، chronology، trajectory، anti-collision، capacity و hydraulics، طراحی چاه، برنامه‌ها، Advisor/RAG، telemetry/twin و گزارش‌ها به کار و تضمین بیشتر نیاز دارند. [تاریخچه ۱۸ حوزه](MISSION14-HISTORICAL-CAPABILITY-MATRIX.json) تاریخ نخستین قابلیت عملیاتی را نامعلوم نگه می‌دارد. [roadmap](MISSION14-CAPABILITY-ROADMAP.json) ۲۲ مورد برنامه‌ریزی‌شده بدون ادعای پیاده‌سازی یا مرجع انتخاب‌شده دارد. سناریوی گسترده دوچاهی پیاده‌سازی نشده است.

بردارهای مصنوعی تنها بررسی نرم‌افزاری‌اند؛ شاهد میدانی مجاز در دسترس نبود.
