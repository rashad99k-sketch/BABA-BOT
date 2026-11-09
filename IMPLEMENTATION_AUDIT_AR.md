# تقرير التدقيق والتنفيذ — GGP + RORO

## ما أُضيف أو عُدّل

- طبقة دمج Watchlist موحدة مع فصل مفتاح الرمز الداخلي عن رمز CCXT.
- تحليل مغلق الشموع للبنية والسيولة وOB وFVG والحجم.
- إثراء كل مرشح في Watchlist بمحركات GGP الموجودة (`SmartMoneyEngine`, `MomentumFlowEngine`, `ADXDIIntelligence`, `InstitutionalTrendEngine`, `TradeStateMachine`) وعرض نتائجها وأيقوناتها؛ دون السماح لها بتجاوز الشروط الهيكلية.
- محركا Reversal وTrend Continuation وسلطة قرار موحدة.
- Failed Auction منفصل عن Sweep مع متطلبات قبول خارج المستوى ثم Reclaim.
- إعادة تحليل المرشح الجاهز قبل التنفيذ لتفادي استخدام قرار قديم.
- تمرير الأوامر عبر `execute_entry()` الأصلي فقط.
- حفظ أحداث الدخول المستهلكة حتى Restart.
- دفتر تداول SQLite ومصالحة OPEN/FLAT/UNKNOWN واستعادة عند بدء التشغيل.
- تأكيد إعداد PAPER الافتراضي، مع شرط LIVE إضافي صريح `ENABLE_LIVE_TRADING=true` بجانب `PAPER_MODE=false` ووجود مفاتيح BingX.
- توافق Resource Manager مع Windows/Linux دون import إجباري لـ`resource`.
- إضافة Launchers لـWindows وفحص Preflight واختبارات.

## ما تم اختباره

- Python compilation: ناجح.
- Pytest بعد اكتمال تعديلات إدارة الأرباح والإغلاق والاستعادة: 42 passed.
- AST parse للوحدات الجذرية: ناجح.

## ما لم يتم التحقق منه

- استيراد المضيف كاملًا: فشل Preflight لأن `ccxt` غير مثبت؛ `flask` أيضًا غير مثبتة. تعذر تثبيت الحزم من بيئة البناء بسبب مشكلة DNS/الاتصال.
- Windows CMD: لم يتم تشغيل BAT على Windows حقيقي.
- BingX Demo/LIVE: لم يتم الاختبار.
- Backtest موثوق أو Soak Test لمدة 24 ساعة: لم يتم التنفيذ.

## الحالة

الحزمة هي إصدار تكاملي قابل للفحص والاختبار محليًا ويبدأ بإعداد PAPER. لا يُصنف LIVE READY. يجب تثبيت dependencies ثم اجتياز Preflight على Windows، وبعدها PAPER وBingX Demo ومراجعة Native SL وRestart Recovery قبل التفكير في LIVE.

## تحديث التدقيق — توحيد الأرباح والإغلاق

- `TradeManagementBoard` هو صاحب القرار الوحيد لإدارة TP1/TP2 وRunner وBreakeven وTrailing وThesis Failure. `apply_profit_engine()` و`apply_50_50_profit_engine()` و`manage_take_profit()` تبقى واجهات توافقية خاملة (HOLD)، و`council_exit()` لا يصدر أوامر.
- TP1 يستهدف بقاء 50% من إجمالي الكمية الأصلية المنفذة؛ TP2 يغلق الكمية المتبقية بعد تحقق التعبئة. لم يعد Score يبدل نسبة TP1 إلى 30% أو 60%.
- `RANGE_SCALP` ليس وضعًا افتراضيًا عند فقدان البيانات. يتطلب أدلة متعددة لسوق Range/ضعف/سيولة رقيقة، ويستخدم هدفًا واحدًا لإغلاق كامل الكمية المتبقية. نقص المؤشرات أو UNKNOWN يبقي الخطة القياسية 50/50.
- `TREND_RIDE` يتطلب اتجاهًا وDI ومحرك استمرار وزخمًا متوافقًا وبنية غير مكسورة. بعد TP1 تتم حماية Runner بـBE+ وATR trailing. TP2 البعيد لا يُمدد بلا نهاية إذا ضعف الاستمرار أو فُقدت البنية.
- الخروج الصارم يتطلب Structural Invalidation أو Failure Score ≥70 أو Failed Thesis مع دليلين شديدين مستقلين. التحذير المتوسط في صفقة رابحة يطلب حماية عند Breakeven بدل تصفية غير مبررة.
- كل الإغلاقات اليدوية والآلية وEmergency Kill Switch تستخدم نفس المسار المتحقق. لا يتم إغلاق الحالة المحلية لمجرد وجود Order object؛ يلزم إثبات تغير كمية المركز أو تأكيد FLAT من المنصة.
- تم تحسين تشخيص سبب الإغلاق الخارجي: Native SL المؤكد يتطلب تطابق معرف الوقف المحفوظ بعد التحقق وتعبئة أمر خروج فعلية، أو تطابقًا قويًا بين سعر Trigger ومستوى SL. قرب سعر التنفيذ من SL فقط ينتج STOP_LOSS_SUSPECTED بدرجة ثقة منخفضة، وليس تأكيدًا.
- إذا لم توفر BingX بيانات Realized PnL موثوقة، يسجل النظام `REALIZED_PNL_UNAVAILABLE` ولا يخترع ربحًا من سعر السوق أو كمية العقد.
- Paper startup recovery يقرأ الصفقات غير المسواة من SQLite ويعيد بناء الحالة وLiveTradeManager والكمية المتبقية ومراحل الربح؛ كما يكمل Finalization بعد Restart إذا كان إغلاق PAPER قد تأكد قبل توقف العملية.
- تم تصحيح مشكلة لوحة Dashboard عند عدم توفر PnL، ومنع عد الصفقة مرتين إذا كان السجل قد تمت تسويته مسبقًا.

## تحقق الاختبارات

- `py_compile` لجميع ملفات Python في الجذر والاختبارات: ناجح.
- `PYTHONPATH=. python -m pytest -q`: 42 passed.
- `preflight.py`: فشل استيراد المضيف في بيئة البناء لغياب `ccxt` و`flask`.
- لم يتم تشغيل Windows فعلي أو BingX Demo أو Soak 24h؛ تبقى هذه بوابات تحقق لازمة.

## تحديث مراجعة الرافعة وحجم المركز — 2026-11-08

- `LEVERAGE` أصبح 10 ثابتًا في GGP.
- LIVE يطلب BingX one-way mode ويضبط `set_leverage(10, symbol, {side: "both"})` ثم يقرأ النتيجة عبر `fetch_leverage`; عدم توفر القراءة أو عدم تطابقها يمنع الدخول.
- 30% مخصصة فقط لفرصة قوية موثقة من Unified Watchlist: score ≥85 وvolume confirmation وبنية مؤكدة وOB أو FVG صالحة. مجرد label مثل SNIPER أو REVERSAL لا يمنح هذه النسبة.
- 20% هدف الهامش لبقية الفرص المقبولة.
- تم فرض سياسة الأهلية داخل `execute_entry()` أيضًا، لتجنب منح 40% من مسار يدوي أو قديم يمرر اسم تصنيف فقط.
- سقف المخاطرة الافتراضي 1% من الرصيد المتاح عند الوقف، إضافة إلى buffer سعري 0.05%. هذه تقديرات لا تشمل يقينًا الرسوم والتمويل والانزلاق غير المتوقع.
- الكمية تراعي contractSize وprecision وحدود العقد. تم تعطيل scale-ins القديمة مؤقتًا لأنها لا تعيد حساب ميزانية المخاطرة الإجمالية.
- بقي حساب BingX Demo مطلوبًا للتحقق من سلوك `fetch_leverage` ومواصفات العقد والهامش وأمر Native SL على كل سوق مستهدف.

## مراجعة Dashboard والماسحين — 2026-11-08

- سبب غياب Dashboard في نسخة GGP السابقة أن Flask routes كانت معرفة دون بدء HTTP server. النسخة الحالية تبدأ خادم Werkzeug قبل `safe_main_loop()`.
- Windows local: منفذا 8000 و8080؛ hosted: `0.0.0.0:$PORT` فقط إذا وفرت المنصة `PORT`.
- Scanner 1 يشغّل RF وSmart Scanner V2 بالتتابع على نفس Universe ويستخدم `_SCANNER_FRAME_CACHE` لمشاركة OHLCV snapshots. Radar يطبق على أعلى المرشحين فقط.
- Scanner 2 يعمل في تدوير Unified Institutional Watchlist ويحسب EMA9/VWAP proximity من نفس DataFrame الذي تم تحليله مؤسسيًا. لا ينتج Entry Ready ولا يصبح Cross شرط دخول.
- عند ظهور مركز مفتوح تتوقف مهام الاكتشاف؛ تظل إدارة الصفقة ومزامنة BingX أعلى أولوية.
- `MIN_API_INTERVAL` الافتراضي 0.35 ثانية مع قفل Thread وجدولة retry/backoff، كما أن إعدادات المسح والـTimeout محدودة بحدود آمنة.
- الاختبارات: 61 passed. لم يتم التحقق من فتح منافذ Dashboard فعليًا من بيئة البناء بسبب عدم توفر Flask وCCXT فيها.

## المراجعة الأخيرة لاستراتيجية RORO والماسح الاحترافي — 2026-11-08

- الملفات المصدرية التي تم استلامها هي `roro.py` و`hj.py`؛ الملف `hj.py` هو فرع RF Liquidity Engine v28.1 ويحتوي Professional Lightweight Scan وGlobal Discovery وWatchlist Rotation. لم يصل ZIP مستقل ثالث باسم HyoT.
- أضيف `ggp_discovery_scanner.py` إلى Scanner 1 ويستخدم `_get_scanner_frame()` ومخزن `_SCANNER_FRAME_CACHE` لتجنب جلب OHLCV مكرر لنفس الرمز ضمن دورة المسح.
- Scanner 1 يجمع V2 وProfessional Scan وRF ثم Radar محدودًا بأعلى المرشحين؛ لا يرسل أي أمر تداول.
- Scanner 2 يحلل قائمة Watchlist بالتناوب، ويحسب EMA9/VWAP proximity من شموع مغلقة كعامل ترتيب فقط.
- `update_unified_watchlist()` يختبر BUY وSELL منفصلين حتى إن جاء رمز من Scanner باتجاه واحد. إذا أصبح الاتجاهان ENTRY_READY في الوقت نفسه يرفض التعارض ولا يخمن الجانب.
- ترتيب المرشحين يحسب حالة Setup السابقة وتوافق Golden Zone وقرب OB وثقة السيولة ودرجة التحليل وحداثة البيانات، وليس Score الخام وحده.
- تم إصلاح توقيع استدعاء `MomentumFlowEngine.analyze_momentum_flow()` ليتوافق مع GGP؛ الدالة لا تستقبل وسيط `side`.
- تم تحديث حالة امتلاء OB بعد التكوين إلى FRESH / TESTED / MITIGATED / INVALIDATED، مع إصلاح المسافة ATR عندما تكون صفرًا تمامًا.
- تم إضافة اختبارات Static تمنع رجوع تمرير `side` غير المدعوم، وتثبت عقد منافذ Dashboard.

### نتائج التحقق المحدثة

- `python -m pytest -q`: 72 passed.
- `python -m compileall -q .`: ناجح.
- `python preflight.py`: لم يكتمل؛ المضيف فشل في الاستيراد لأن `ccxt` غير مثبتة في بيئة البناء (`No module named 'ccxt'`).
- لم يتم تشغيل Windows فعلي أو BingX Demo/LIVE أو Backtest أو اختبار Soak لمدة 24 ساعة من هذه البيئة.

الحالة تظل PAPER / Demo Preparation وليست LIVE READY.


## تحديث سجل إدارة الصفقات وTelegram — 2026-11-08

- أضيف `ggp_management_events.py`: سجل محدود بـ200 حدث، thread-safe، لا يرسل أوامر ولا يقرر تداولًا، ويخفي الحقول التي تشير إلى token/secret/API key/password/authorization بصرف النظر عن حالة الأحرف.
- أضيف `ggp_telegram.py`: Queue محدودة (100 رسالة افتراضيًا)، عامل واحد غير متزامن، Timeout، والتحقق من استجابة Telegram `ok=true`. لا تُطبع روابط API الحساسة أو بيانات الاعتماد.
- تم ربط أحداث TP1/TP2/RANGE_SCALP/Breakeven/Trailing/Strict Exit/إغلاق خارجي/SL/الأوامر غير المحسومة بسجل موحد يحمل أيقونات وحالة تنفيذ.
- أصبح `/decision` Endpoint فعليًا ويعيد Snapshot للأحداث وسلطة القرار والحالة النشطة؛ Dashboard يعرض الأحداث الفعلية باستخدام DOM `textContent` بدل HTML غير موثوق.
- تم تحديث Telegram close notification لإرسال إشعار الإغلاق حتى عندما لا يتوفر Realized PnL، مع التصريح بأنه غير متاح بدل إسقاط التنبيه.
- لم يتم إجراء اتصال Telegram حقيقي أو تحقق BingX API من بيئة البناء.

### الاختبارات بعد تحديث السجل

- `python -m pytest -q`: 81 passed.
- `python -m compileall -q .`: نجاح.
- `preflight.py` الكامل لم يكتمل بسبب غياب `ccxt` و`flask` في بيئة البناء.
