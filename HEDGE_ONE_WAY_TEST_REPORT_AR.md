# تقرير توافق Hedge / One-Way في الاستعادة

**التاريخ:** 2026-11-09

## One-Way

- وضع الحساب يجب قراءته من BingX، وليس استنتاجه من `.env` أو من سجل قديم فقط.
- في وضع One-Way، يُستخدم `positionSide=BOTH` حيث يتطلبه محول BingX.
- جانب الصفقة BUY/SELL يظل منفصلًا عن قيمة Position Side.
- إذا أظهر السجل LONG/SHORT مع One-Way، أو لم يمكن إثبات وضع الحساب، يتم Hold.

## Hedge

- Long يطابق `BUY` مع Position Side `LONG`.
- Short يطابق `SELL` مع Position Side `SHORT`.
- لا يسمح باستبدال LONG/SHORT بـBOTH.
- لا يُسمح بتمرير افتراضات One-Way إلى Hedge.
- لا يتم إرسال تغيير تلقائي لوضع الحساب.

## قراءة CCXT

وثائق CCXT الخاصة بـBingX توثق `fetchPositionMode(symbol)` كواجهة لقراءة نمط المركز، وتعيد قيمة `hedged`، وتذكر أن الرمز مطلوب للأسواق الخطية. <https://docs.ccxt.com/docs/exchanges/bingx>

محول BingX في CCXT يربط وضع Dual Side بحقل `dualSidePosition`، كما يوثق معاملات الأوامر الخاصة بـ`positionSide` و`reduceOnly` والوقف الشرطي. يجب تثبيت نسخة CCXT ومراجعة إصدار المحول الذي يشغّله المشروع. <https://github.com/ccxt/ccxt/blob/master/php/bingx.php>

## نتيجة الاختبار المحلي

اختبارات الموديل تغطي الحالات التالية: One-Way/ Hedge، الجانب المطابق وغير المطابق، وضع UNKNOWN، وتغيير وضع الحساب بعد حفظ الصفقة.

هذه اختبارات منطقية وليست اختبارًا حيًا لـBingX Demo. يجب إكمال التحقق الخارجي على حساب Demo؛ خصوصًا معاملات Stop/ReduceOnly وقراءة أوامر الحماية.
