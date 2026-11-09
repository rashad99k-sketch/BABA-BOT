# تقرير الإصلاح النهائي — GGP Native SL / Recovery / Position Sizing 10x

**الحالة:** تم تعديل الكود وتشغيل الاختبارات المحلية. **ليس اعتماد LIVE**؛ لم يتم الاتصال بحساب BingX للمستخدم أو إرسال أوامر فعلية.

## التشخيص الذي عالجته النسخة

1. كان `verify_native_stop_loss()` يمرر `position_side="BOTH"` ثابتًا. في Hedge Mode يجب مطابقة LONG أو SHORT الفعلي؛ وهذا وحده كافٍ لرفض أمر حماية صحيح.
2. الاعتماد على `fetch_open_orders()` الموحد وحده قد لا يكشف كل تفاصيل أوامر التفعيل. وثائق BingX Swap تعرض endpoint مخصصًا `GET /openApi/swap/v2/trade/openOrders` مع فلتر `type=STOP_MARKET`، بينما CCXT يوفر المسار الموحد وimplicit endpoint `swapV2PrivateGetTradeOpenOrders`.
3. كانت هوية `trade_id` التي تصل إلى التنفيذ معرضة للاختلاف عن هوية الحالة المحلية؛ الآن يمرر `execute_entry()` نفس المعرف إلى `open_position()` ثم إلى `TradeExecutionService`.
4. إذا تعذر إثبات Native SL بعد تعبئة مؤكدة، كان مسار الدخول يرجع دون أن يسلم نتيجة التعبئة إلى استكمال بناء حالة الإدارة. الآن يحتفظ بنتيجة التعبئة ويكمل حفظ بيانات المركز مع إبقاء `RECOVERY_HOLD` ومنع المداخل الجديدة.
5. كان سجل `ENTRY_PENDING` لا يحفظ SL ولقطة أولية لمراحل الإدارة. الآن يحفظ `structural_stop` ولقطة `management_state` قبل إرسال MARKET، ثم تحفظ لقطة الحالة المكتملة بعد تنفيذ الدخول.

## ما تم تغييره

- `ggpbot.py`: رافعة ثابتة 10x؛ التحقق من read-back يقارن بـ10؛ تمرير position side الحقيقي للتحقق الثاني وللاستعادة؛ استخدام fill quantity وaverage price الفعليين؛ الحفاظ على exposure عند فشل التحقق؛ حفظ state snapshot؛ تحميل `.env` عند بدء التشغيل قبل قراءة إعدادات المنصة.
- `bingx_execution.py`: دعم التحقق من أوامر الوقف عبر CCXT unified open orders ثم raw Swap openOrders implicit method مع فلتر STOP_MARKET؛ المطابقة تتطلب رقم الأمر والرمز والاتجاه وPosition Side وسعر التفعيل والكمية.
- `bingx_protection.py`: تطبيع قراءة استجابات BingX المتداخلة.
- `ggp_position_sizing.py`: فرض 10x بدل 20x؛ تبقى نسب الهامش 30% للفرصة القياسية و40% للفرصة القوية.
- `roro_trade_journal.py`: `ENTRY_PENDING` يحفظ SL ولقطة TP1/TP2 وهوية الصفقة قبل أمر السوق.
- `.env.example` و`ENV_SETUP_AR.md` وREADME وتقارير الاختبار: تحديث سياسة 10x وطريقة إعداد ENV.

## قاعدة رأس المال الجديدة

الأساس هو `free USDT` الذي تعيده BingX، أي الرصيد المتاح بعد الهامش المحجوز. لا يوجد خصم 2% إضافي من هذا الأساس.

- Standard: target margin = available balance × 30%.
- Strong: target margin = available balance × 40% بعد تحقق شروط القوة المؤسسية؛ لا يكفي اسم SNIPER وحده.
- Target notional = target margin × 10x.
- حجم العقد الفعلي هو الأقل بين سقف التخصيص وسقف المخاطرة عند Structural SL. `MAX_TRADE_RISK_PCT=1.0` ما زال قائمًا؛ لذلك قد يكون الهامش النهائي أقل من 30% أو 40% عندما يكون وقف الخسارة بعيدًا. هذا مقصود للحفاظ على سقف المخاطرة، وليس خطأ في الضرب.
- بعد التقريب إلى دقة العقد يعاد فحص الهامش والحد الأدنى والحد الأقصى للمخاطرة.

## ملف ENV

- `.env` يقرأ تلقائيًا في بداية `ggpbot.py`.
- `.env` الحقيقي غير مرفق ولا يرفع إلى GitHub؛ `.env.example` قالب ظاهر فارغ للمفاتيح.
- DigitalOcean: توضع الأسرار في Environment Variables المشفرة.
- لا يتم طباعة قيمة أي مفتاح في سجل التشغيل.

## الاختبارات

- `python -m compileall -q .` — PASS
- `PYTHONPATH=. python -m pytest -q` — **117 passed**
- تمت إضافة اختبارات لقراءة أوامر BingX الشرطية الخام، Hedge Position Side، تخزين إدارة الصفقة قبل MARKET، وحساب تخصيص 30% عند 10x.

## حدود التحقق

- لم يتم تسجيل الدخول إلى حساب BingX الخاص بالمستخدم، ولا يمكنني الوصول إلى حسابه أو مفاتيحه.
- لم يتم إرسال أمر Demo أو Live.
- لم أجد في البحث العام فيديو YouTube أو نقاش Discord محددًا موثوقًا يمكن اعتماده كمرجع تقني؛ لذلك لم أستخدمهما لإثبات سلوك API. التوثيق الرسمي لـBingX ومصدر CCXT هما المرجعان الأساسيان.
- يجب اختبار النسخة على BingX Demo والتأكد من ظهور STOP_MARKET في raw openOrders وتطابق Position Side والكمية قبل أي Live.

## مراجع تقنية

- BingX Swap API reference: `GET /openApi/swap/v2/trade/openOrders`, فلتر `STOP_MARKET`, وحقول `positionSide`, `origQty`, `stopPrice`, `status`.
- CCXT BingX implementation: `fetchOpenOrders()` يستخدم Swap v2 `trade/openOrders` ويحوّل `data.orders` إلى order structures؛ كما يوفر `swapV2PrivateGetTradeOpenOrders` كـimplicit endpoint.
- CCXT docs: `fetchOpenOrders(symbol, since, limit, params)` و`setLeverage` و`fetchLeverage` و`fetchPositionMode`.
