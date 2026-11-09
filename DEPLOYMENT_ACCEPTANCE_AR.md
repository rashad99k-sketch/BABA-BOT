# قائمة قبول الإصدار — PAPER ثم BingX Demo

## قبل التشغيل المحلي

1. فك ZIP كاملًا في مجلد مستقل.
2. أنشئ `.env` من `.env.example` عند الحاجة.
3. اترك `PAPER_MODE=true` و`ENABLE_LIVE_TRADING=false`.
4. شغّل `RUN.bat` من جذر المشروع.
5. تحقق من ظهور رسائل `[DASHBOARD]` ومنفذي 8000 و8080.
6. تحقق من `/health` و`/data` ومن حالة Scanner 1 وScanner 2.

## ما يجب مراقبته في PAPER

- تزايد عدد الرموز التي وصلت إلى Unified Institutional Watchlist.
- ظهور BUY وSELL hypotheses لكل رمز.
- ظهور أسباب WAITING / REJECTED / NO_CHASE بدل انتظار Cross فقط.
- وجود OB/FVG/Liquidity/Structure مع مصدر وتوقيت بيانات.
- عدم وجود `ENTRY_READY` دون Event ID وStructural SL.
- عدم وجود أي أمر حقيقي على BingX.

## قبول BingX Demo

اختبر منفصلًا:

- تفعيل 10x والتحقق من قراءة الرافعة مرة أخرى.
- 40% / 30% كهامش مستهدف مع Risk cap 1% عند SL.
- دقة العقد وcontractSize والحد الأدنى للكمية.
- Native SL coverage.
- TP1 50%، ثم TP2 للكمية المتبقية.
- RANGE_SCALP فقط عند تحقق أدلة الضعف/السوق العرضي أو السيولة الرقيقة.
- الإغلاق الخارجي وضرب SL وتحديد السبب من سجل BingX.
- Restart بعد الدخول وبعد TP1 وأثناء أمر إغلاق غير محسوم.
- استعادة SL وTP stages وTrailing والكمية المتبقية.
- انقطاع API وعودة الاتصال دون أوامر مكررة.

## حظر LIVE

لا يُعلن الإصدار LIVE READY قبل نجاح Windows runtime وBingX Demo والاختبارات التشغيلية الطويلة. نجاح اختبارات الوحدة وحده لا يثبت الربحية أو سلامة التنفيذ الحي.


## قبول سجل الإدارة وTelegram

- افتح `/decision` وتأكد من استجابة JSON بحالة HTTP 200 حتى عندما لا توجد أحداث.
- نفّذ دورة PAPER: Entry ثم TP1؛ يجب أن يظهر 🥇 TP1 VERIFIED بعد محاكاة/تأكيد التنفيذ فقط.
- أكمل TP2 أو Strict Exit؛ يجب أن يظهر 🥈 أو ⛔ مع حالة PAPER_SIMULATED أو EXCHANGE_VERIFIED، لا أن يُسجل كتنفيذ LIVE أثناء PAPER.
- عند غياب Telegram credentials، يجب أن يستمر البوت دون تنبيه ولا خطأ تداول. عند وضع credentials الجديدة، افحص Telegram test message/notification مع عدم عرض الأسرار في `/health` أو Logs.
- لا تعتمد على نجاح unit tests كإثبات لإرسال Telegram فعلي أو تنفيذ BingX فعلي.


## 2026-11-09 — حالة بوابات التنفيذ

الحالة ما زالت **PAPER / Demo Preparation — NOT LIVE READY**. اجتازت الحزمة 96 اختبارًا محليًا وPreflight، وثُبت CCXT على `4.5.92`. أضيف محول قراءة وضع الحساب ومسار تنفيذ موحد وتحقق Native STOP_MARKET ومهاجرات SQLite.

لم تُنفذ أوامر BingX حقيقية. القبول المتبقي قبل LIVE هو BingX Demo/VST، Hedge وOne-Way، الرافعة وNative SL، partial fill، restart، انقطاع الشبكة، وتشغيل Windows فعلي.
