# تدقيق سجل إدارة الصفقات الموحد وTelegram

## مصدر القرار

- قرارات TP1 وTP2 وRunner وRANGE_SCALP وBreakeven وTrailing وThesis/Strict Exit تصدر من `TradeManagementBoard` ضمن `LiveTradeManager`.
- `ggp_management_events.py` سجل مراقبة فقط؛ لا يقرر تداولًا ولا يرسل أوامر.
- دوال الربح القديمة تبقى واجهات توافقية خاملة، و`council_exit()` مراقب فقط. لا يوجد مسار أرباح موازي معتمد.

## أيقونات وأحداث الإدارة

| الأيقونة | الحدث | معيار التسجيل |
|---|---|---|
| 🧠 | Management Decision | قرار اللوحة أو سبب HOLD بعد ضبط التكرار |
| 🥇 | TP1 Verified | تحقق خفض المركز إلى قرابة 50% من الكمية الأصلية |
| 🥈 | TP2 Verified | تأكيد أن المنصة أصبحت FLAT |
| ⚡ | Range Scalp | تأكيد إغلاق الكمية المتبقية عند تحقق قواعد السوق الضعيف/العرضي |
| 🛡️ | Breakeven+ | تحديث محلي للوقف؛ لا يعني تعديل Native SL على BingX |
| 📈 | Trailing | تحديث trailing محلي؛ لا يعني وقفًا أصليًا معدلًا على BingX |
| ⛔ | Strict Exit | إغلاق نهائي بعد تأكيد التنفيذ |
| 🧯 | Adaptive Runner Exit | إغلاق Runner بعد ضعف الاستمرارية وفق قواعد المدير |
| 🛑 | Stop Loss Classification | يعرض مؤكدًا أو مشتبهًا أو غير معروف حسب الأدلة |
| 🔎 | External Close | تسوية المركز المغلق من خارج البوت |
| ⚠️ / ❌ | Execution Uncertain / Close Failed | لا تعلن نجاح الإغلاق ولا تعيد إرسال أمر عشوائي |

## Dashboard API

- تمت إضافة `GET /decision`، ويعيد آخر 60 حدث إدارة، وسلطة القرار، والوضع، وحالة المركز النشط.
- الاستجابة لا تعتمد على وجود مركز نشط؛ عند عدم وجود أحداث تعيد قائمة فارغة بحالة HTTP 200.
- واجهة Dashboard تستهلك نفس سجل الأحداث، وتستخدم `textContent` لعرض النصوص الديناميكية.
- `/health` يعرض حالة إعداد Telegram دون إظهار Bot Token أو Chat ID.

## Telegram

- الإرسال غير متزامن عبر Queue محدودة بعامل واحد حتى لا يوقف شبكة Telegram حلقة التداول.
- يتحقق الإرسال من HTTP status ومن `ok=true` في استجابة Telegram.
- في حالة عدم وجود credentials أو امتلاء الطابور، لا تتوقف إدارة التداول؛ تسجل حالة الإخفاق/الإسقاط.
- الإشعارات تشمل بدء التشغيل، الدخول، TP1، الإغلاق النهائي، ووقف الخسارة الأصلي المؤكد.
- اختبارات Telegram تستخدم Fake response فقط؛ لم يتم إرسال رسالة حقيقية من بيئة البناء.

## حماية الأسرار

- `.env` و`.env.example` في الإصدار الجديد قوالب PAPER بمفاتيح BingX وTelegram فارغة.
- سجل الأحداث يخفي أسماء حقول token/secret/API key/authorization/password بغض النظر عن حالة الأحرف.
- لا يتم تضمين المفاتيح داخل Logs أو Dashboard أو Release ZIP.
- الحزمة السابقة التي أرسلها المستخدم تضمنت أسرار BingX وTelegram غير فارغة في `.env` و`.env.example`؛ يجب إبطال تلك القيم وإصدار بيانات جديدة.

## حدود التحقق

- `python -m pytest -q`: 81 passed.
- `python -m compileall -q .`: نجاح.
- لم يتم اختبار اتصال Telegram حقيقي أو معاملات BingX Demo أو LIVE.
- Full preflight لم يكتمل في بيئة البناء لغياب `ccxt` و`flask`.
