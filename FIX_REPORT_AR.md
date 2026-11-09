# تقرير إصلاح RECOVERY_HOLD واستعادة إدارة الصفقة

**المشروع المستهدف:** JGB / GGP Institutional Unified Trade Log  
**المشروع المرجعي:** RORO — ملف `hj.py`  
**التاريخ:** 2026-11-09  
**الحالة:** Recovery Patch — مرشح اختبار PAPER/DEMO، وليس تصريح LIVE

## 1. معنى الخطأ

الرسالة:

```text
[RECOVERY_HOLD] Exposure is tracked but full management state is incomplete; no new entries or fabricated TP/SL actions
```

ليست دليلًا على فشل اكتشاف المركز. معناها أن النظام اكتشف تعرضًا مفتوحًا (Exposure) أو احتفظ به محليًا، لكنه لا يستطيع إثبات اكتمال الحالة اللازمة لإدارته بأمان. لذلك يمنع الدخول الجديد، ويمنع اختراع وقف أو أهداف ربح.

**لا يمكن تحديد السبب الأصلي للحادث من سطر الرسالة وحده.** في النسخة المعدلة أضيف `recovery_reason` محدد إلى السجل، مثل:

- `journal_row_not_found`
- `multiple_journal_rows_match_exposure`
- `exchange_position_mode_unknown`
- `position_mode_changed_since_entry`
- `actual_filled_quantity` مفقودة
- `exchange_remaining_quantity_mismatch_requires_fill_reconciliation`
- `persisted_native_sl_price_missing`
- `native_sl_missing` أو `native_sl_unknown`
- `structural_sl_wrong_side` أو أهداف TP غير متناسقة

## 2. المشاكل التي عالجها التعديل

1. كان GGP يستطيع تتبع مركز ظاهر على BingX دون إعادة محاولة ربطه بسجل SQLite بعد فشل أولي مؤقت؛ وهذا قد يبقيه في Hold.
2. مسار الاستعادة القديم كان يسمح بقيم افتراضية غير آمنة مثل `BOTH` عند غياب دليل موثوق على Position Side.
3. كانت حالة المدير المركزي في GGP أقل تشددًا من مرجع RORO عند تفعيل إدارة الصفقة بعد المصالحة؛ لم يكن اشتراط حماية Native SL واضحًا في كل مسارات LIVE.
4. كان حقل `remaining_qty` في سجل التنفيذ يختلط أحيانًا مع `remaining_qty` الخاص بأمر الدخول غير المنفذ، بدل كمية المركز المفتوح. تم فصل `order_remaining_qty` عن كمية المركز.
5. استعادة حجم المركز من الكمية الحالية وحدها قد تعيد احتساب TP1 بصورة خاطئة؛ أصبح `actual_filled_qty` هو أساس الكمية الأصلية، وكمية BingX الحالية هي أساس الكمية المتبقية.
6. إذا كانت كمية BingX الحالية أصغر من الكمية المتبقية المسجلة، فقد يشير ذلك إلى إغلاق جزئي خارجي أو حدث لم تتم مصالحة تعبئته. أصبح هذا الاختلاف سبب Hold صريحًا بدل إعادة تشغيل TP1 على حجم غير موثوق.
7. بعد نجاح التحقق من Native SL، لم يكن GGP يثبت دائمًا `STATE.native_sl_verified=True` قبل تسليم الحالة لمدير الصفقات.
8. أزيلت إعادة التحقق الزائدة من الوقف بعد أن تكون خدمة التنفيذ قد أجرت التحقق بالفعل؛ مع إبقاء التحقق مطلوبًا إذا لم تقدم الخدمة إثباتًا.
9. في بعض قواعد البيانات القديمة قد تكون أعمدة الترحيل الجديدة صفرًا بينما يحتوي `state_json` على القيم الموثوقة؛ أصبحت دوال الاستعادة تتجاوز الصفر وتواصل البحث في قيم الـSnapshot بدل حجبها.

## 3. مسار الاستعادة الجديد

```text
EXCHANGE POSITION SNAPSHOT
        ↓
UNIQUE JOURNAL MATCH (SYMBOL + SIDE)
        ↓
READ POSITION MODE FROM BINGX
        ↓
VALIDATE POSITION SIDE
        ↓
RESTORE ACTUAL FILLED QTY + ENTRY + TP1/TP2 + TRAILING STATE
        ↓
COMPARE PERSISTED REMAINING QTY WITH LIVE EXCHANGE QTY
        ↓
VERIFY EXISTING NATIVE STOP-MARKET (PRICE + SIDE + QTY + STATUS)
        ↓
PERSIST RECOVERY CHECKPOINT
        ↓
REACTIVATE THE EXISTING LiveTradeManager
```

- توجد إعادة محاولة دورية قابلة للضبط عبر `RECOVERY_RETRY_INTERVAL_SEC` (الافتراضي 20 ثانية، والحد الأدنى 10 ثوانٍ).
- إعادة المحاولة لا ترسل أمر دخول جديدًا ولا تعيد إرسال MARKET.
- عند عدم وجود سجل وحيد مطابق، يعرض النظام بيانات التعرض التي قرأها من BingX فقط، ويضع القيم الأصلية والأهداف غير المعروفة على صفر، ويظل في Hold.
- لا يتم اختراع سعر دخول أو SL أو TP أو كمية تعبئة أصلية.
- لا يتم إنشاء/تعديل وقف تلقائيًا عند الاستعادة. يلزم أولًا إثبات ما هو قائم على المنصة؛ غياب الإثبات يبقي الحاجز مغلقًا.

## 4. مرجع RORO وما تم اقتباسه

تمت مراجعة `hj.py` في RORO، خصوصًا:

- `_on_reconciled()` عند الأسطر 3299–3324 تقريبًا: لا يفعّل مدير الصفقة الحي قبل مطابقة الرمز والاتجاه وتأكيد Native SL في LIVE.
- `start_trade()` عند الأسطر 3326–3344 تقريبًا: يصفر مؤقتات المصالحة والحساب الثقيل حتى لا يرث المدير حالة الصفقة السابقة.

نُقلت هذه المبادئ إلى GGP مع إضافة قيود GGP الخاصة بالـJournal وRecovery. لم يتم نسخ استراتيجية RORO أو استبدال استراتيجية GGP. ملف RORO لم يُدمج داخل مشروع GGP.

## 5. ما لم يتغير

- استراتيجية الدخول والـScanner والـWatchlist.
- قواعد حجم المركز والمخاطرة والرافعة المستهدفة الموجودة في نسخة GGP المرفوعة.
- محرك إدارة الأرباح المركزي الموجود؛ لم تتم إضافة Profit Manager موازٍ.
- أهداف TP1 وTP2 ومراحل الربح المعتمدة في المشروع.
- واجهات Dashboard وTelegram وبنية Flask ومسارات API.
- لا يتم تغيير Position Mode تلقائيًا.

## 6. القيود والقبول

اختبارات الوحدة والمحاكاة المحلية لا تثبت تفاعل حساب BingX الفعلي مع كل شكل لأوامر Conditional/Stop. لم يتم تنفيذ اتصال بحساب Demo/VST أو Live في بيئة البناء. يلزم اختبار Demo على الحساب نفسه، وبالأخص: قراءة Position Mode، قراءة المركز، ظهور أمر STOP_MARKET في Open Orders، مطابقة الكمية/السعر، واستعادة Restart.

**القرار:** الإصلاح يعالج مسارات الاستعادة والحالة غير المتسقة محليًا، لكنه لا يجعل النسخة LIVE READY قبل اختبار منصة Demo.
