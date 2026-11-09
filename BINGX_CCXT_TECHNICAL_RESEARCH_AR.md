# تقرير بحث تقني موحّد: BingX / CCXT لمشروع GGP

**تاريخ البحث:** 2026-11-09

## 1. نطاق التقرير والتنبيه التنفيذي

يجمع هذا التقرير نتائج مراجعة توثيق BingX الرسمي، وتوثيق CCXT، وكود محول BingX في CCXT، والمراجع الرسمية المساندة وبعض قضايا المجتمع. الغرض هو تحديد عقد التكامل المناسب لمشروع GGP في تداول العقود الدائمة، مع فصل ما هو موثق عن الاستنتاجات التشغيلية وما يزال غير محسوم.

لم يُجرَ أي اختبار حي على حساب BingX أو VST، ولم يُعدّل كود المشروع. كما أن **إصدار CCXT المستخدم في مشروع GGP غير مثبت حتى الآن**؛ لذلك لا يجوز افتراض أن سلوك المشروع يطابق إصدار CCXT الذي تمت مراجعته. أحدث إصدار ظاهر في صفحة GitHub الرسمية أثناء البحث هو `v4.5.92`، منشورًا في 2026-11-04، بينما يثبت فحص المصدر وجود تحديث لمحول BingX بتاريخ 2026-11-08. يجب تثبيت الإصدار الفعلي من ملفات المشروع وبيئة التشغيل قبل اعتماد أي فرق سلوكي أو واجهة برمجية.

## 2. الخلاصة التنفيذية

- تدعم BingX وضعين للمراكز: **Hedge / Dual-Side** و**One-Way**. في Hedge يمكن وجود Long وShort للعقد نفسه، ويجب التفريق بين فتح وإغلاق كل جانب. في One-Way يوجد اتجاه صافٍ واحد، ويكون `positionSide=BOTH`.
- تبديل الوضع يتم عبر `POST /openApi/swap/v1/positionSide/dual` باستخدام `dualSidePosition=true` لـ Hedge و`false` لـ One-Way. لا ينبغي تنفيذ التبديل مع مراكز أو أوامر معلقة؛ مرجع الأخطاء يسجل ذلك كخطأ `104103`، وقد يظهر عدم توافق الوضع مع `positionSide` كـ `109400`.
- واجهة إنشاء الأمر هي `POST /openApi/swap/v2/trade/order`. تدعم BingX أنواعًا عادية ومشروطة، كما تدعم إرفاق `stopLoss` و`takeProfit` بصيغة JSON متسلسلة في طلب الأمر وفق الأمثلة الرسمية. لكن تفاصيل سلوك الحماية بعد التنفيذ الجزئي أو الإغلاق الجزئي ليست محسومة بما يكفي لتُعامل كضمان دون اختبار مصرح به.
- نجاح إنشاء الأمر لا يثبت أن الأمر امتلأ. يجب التحقق من حالة الأمر والتنفيذات والمركز، مع الاستعداد لمصالحة REST بعد انقطاع WebSocket أو انتهاء مهلة/استجابة `504`.
- محول CCXT يضيف طبقة توحيد، لكنه لا يلغي المعاملات الخاصة بـ BingX. أسماء المعاملات والقيم والتصرفات تختلف باختلاف نوع السوق ووضع الحساب؛ لا يجوز نقل `params` من منصة أخرى تلقائيًا.
- في حالة انتهاء مهلة إنشاء الأمر أو وجود خطأ شبكة، يجب الاستعلام باستخدام `clientOrderId` أو معرف الأمر قبل إعادة الإرسال لتجنب التكرار.
- حدود المعدل متعارضة بين بعض صفحات المسارات وإعلان BingX الرسمي؛ يلزم اعتماد limiter محافظ مؤقتًا وتهيئته بعد التحقق من البيئة والحساب الفعليين.

## 3. واجهات BingX المؤكدة

### 3.1 وضع المركز

الواجهة:

```text
POST /openApi/swap/v1/positionSide/dual
```

المعاملات الأساسية:

- `dualSidePosition`: قيمة منطقية؛ `true` لتفعيل Hedge و`false` لتفعيل One-Way.
- `timestamp`: مطلوب.
- `recvWindow`: اختياري.

القيود التشغيلية:

- لا يغيّر الطلب وضع الحساب عند وجود مراكز أو أوامر معلقة.
- في Hedge تكون قيمة `positionSide` عادة `LONG` أو `SHORT`.
- في One-Way تكون `positionSide=BOTH`.
- توثيق CCXT يعرّض ذلك من خلال `fetchPositionMode` و`setPositionMode` للسوق الخطي، ولا يثبت دعم هذه الطريقة لعقود Coin-M العكسية.

### 3.2 قراءة المراكز

الواجهة:

```text
GET /openApi/swap/v2/user/positions
```

المعاملات:

- `symbol` اختياري.
- `currency` اختياري.
- `timestamp` مطلوب.

تتضمن الاستجابة، بحسب التوثيق، حقولًا مثل `positionId` و`positionSide` و`positionAmt` و`availableAmt` و`avgPrice` و`leverage` و`markPrice` و`liquidationPrice` و`riskRate` وغيرها. يجب عدم تفسير `code=109500` تلقائيًا على أنه “لا توجد مراكز”، لأن النتيجة قد تعكس فشل الطلب أو حالة غير صالحة.

يقابل ذلك في CCXT `fetchPositions(symbols, params?)`، مع نتيجة موحدة لهياكل المراكز، ووجود خيار خاص `params.standard` في توثيق BingX. سلوك حذف `symbols` أو تمرير قائمة فارغة لم يُتحقق منه حيًا.

### 3.3 إنشاء الأوامر

الواجهة:

```text
POST /openApi/swap/v2/trade/order
```

الأنواع الموثقة تشمل:

`LIMIT`, `MARKET`, `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`, `TRIGGER_LIMIT`, `TRIGGER_MARKET`, `TRAILING_STOP_MARKET`, `TRAILING_TP_SL`.

المعاملات التي ينبغي تصميم serializer لها صراحةً:

- هوية الأمر: `symbol`, `clientOrderId`, `timestamp`, `recvWindow`.
- الاتجاه والحجم: `side`, `positionSide`, `quantity`, `quoteOrderQty`, `reduceOnly`.
- نوع الأمر والسعر: `type`, `price`, `timeInForce`.
- التفعيل والحماية: `stopPrice`, `priceRate`, `workingType`, `closePosition`, `activationPrice`, `stopGuaranteed`.
- الربط بالمركز أو أوامر الحماية: `positionId`, `stopLoss`, `takeProfit`.

في تنفيذ CCXT، تُستخدم تسمية `clientOrderId` للعقود، مقابل `newClientOrderId` في Spot. كما يرسل المحول `positionSide=BOTH` في One-Way، و`LONG` أو `SHORT` في Hedge، ويعكس الاتجاه تلقائيًا في بعض أوامر `reduceOnly`. لذلك يجب عدم بناء منطق GGP على افتراض أن المعلمات الموحدة وحدها كافية.

### 3.4 الاستعلام والإلغاء والتنفيذات

يوثق CCXT `fetchOrder(id, symbol, params?)`، وتقبل BingX خيارًا خاصًا `params.twap` لأوامر TWAP. في المسار العادي يتطلب المحول `symbol`، مع دعم `clientOrderId` أو `clientOrderID` كبديل عن `orderId` وفق السياق.

كما يوثق CCXT `fetchMyTrades(symbol?, since?, limit?, params?)`، وتدعم BingX معاملات سياقية مثل `until` و`trandingUnit` و`orderId`. يجب استخدام سجل التنفيذات، لا رد إنشاء الأمر وحده، لإثبات الكمية المنفذة ومتوسط سعر التنفيذ.

حالات الأمر التي يعالجها محول CCXT تشمل تحويل:

- `NEW`, `PENDING`, `PARTIALLY_FILLED`, `RUNNING` إلى `open`.
- `FILLED` إلى `closed`.
- `CANCELED`, `CANCELLED`, `FAILED` إلى `canceled`.

وتذكر مراجع BingX حالات أخرى مثل `EXPIRED`. لذلك ينبغي أن يبقى parser متسامحًا مع اختلافات التسمية، وألا يفترض أن كل حالة غير معروفة تعني اكتمالًا أو إلغاءً.

### 3.5 الإغلاق

الإغلاق الشامل:

```text
POST /openApi/swap/v2/trade/closeAllPositions
```

- `symbol` اختياري بحسب التوثيق.
- `timestamp` مطلوب.
- الاستجابة تتضمن قوائم `success` و`failed`.

كما توجد واجهة الإغلاق حسب معرف المركز:

```text
POST /openApi/swap/v1/trade/closePosition
```

في محول CCXT، إذا توفر `positionId` للعقد الخطي، يُستخدم مسار الإغلاق حسب المركز؛ وإذا لم يتوفر، يُستخدم `closeAllPositions` للعقود الخطية أو Coin-M. الوسيط `side` لا يُستخدم في هذا المسار بحسب فحص المصدر.

في طلبات الأوامر، `closePosition=true` يعبّر عن إغلاق الكمية كاملة عند التفعيل. إلا أن التوثيق يذكر أن أوامر `STOP_MARKET` و`TAKE_PROFIT_MARKET` قد تظل تتطلب `quantity` و`stopPrice`، ولا يجوز حذفهما اعتمادًا على معنى `closePosition` وحده. كما لا يجتمع `closePosition=true` مع `reduceOnly` في المواصفة التي تمت مراجعتها.

### 3.6 الرافعة

الواجهة:

```text
POST /openApi/swap/v2/trade/leverage
```

المعاملات المطلوبة:

- `symbol`.
- `side`.
- `leverage`.
- `timestamp`.

القيمة تعتمد على الوضع:

- Hedge: `LONG` أو `SHORT`، ويمكن ضبط الرافعة لكل جانب.
- One-Way: `BOTH`.

في CCXT تُستخدم `setLeverage(leverage, symbol, params?)`، مع `params.side` بقيم `long`/`short` في الوضع المزدوج و`both` في الوضع الأحادي بحسب التوثيق الموحد. فحص المصدر يوضح أن المحول يستخدم `swap/v2` للعقود الخطية و`contract/v1` لعقود Coin-M. يلزم مطابقة نوع السوق قبل استدعاء الطريقة.

## 4. Hedge وOne-Way: عقد التكامل المقترح

### Hedge / Dual-Side

يمكن أن يوجد مركز Long ومركز Short على الرمز نفسه. يجب أن يحمل كل أمر قيمة `positionSide` الصحيحة، وأن يفرق منطق GGP بين فتح مركز وإغلاقه. يجب عدم تعميم `reduceOnly` من One-Way إلى Hedge دون مطابقة ما تقبله BingX لكل نوع أمر؛ التوثيق الحالي يذكر أن `reduceOnly` يستخدم في One-Way للتخفيض فقط، بينما في Hedge لا ينبغي افتراض إرساله بنفس الطريقة.

### One-Way

يوجد مركز صافٍ واحد، ويكون `positionSide=BOTH`. تستخدم أوامر التخفيض `reduceOnly=true` وفق توثيق BingX، بينما قد يُستخدم `closePosition=true` في الأوامر المشروطة للإغلاق الكامل. لا ينبغي الجمع بين المعاملين دون تحقق، لأن التوثيق يذكر عدم اجتماعهما.

### تبديل الوضع

التبديل عملية حسابية/تشغيلية حساسة وليست إعدادًا لكل أمر. يجب قبلها:

1. إلغاء أوامر GGP المفتوحة أو إثبات عدم وجودها.
2. التحقق من عدم وجود مراكز غير صفرية عبر REST.
3. تنفيذ التغيير.
4. إعادة قراءة الوضع وتثبيته محليًا قبل إنشاء أوامر جديدة.

## 5. التحقق من الأمر والـ Fill والمصالحة

القاعدة الأساسية: **قبول REST لطلب إنشاء الأمر لا يساوي تنفيذ الصفقة**. يجب تصميم دورة الأمر كالتالي:

1. توليد `clientOrderId` فريد وقابل للتتبع.
2. إرسال الأمر بعد ضبط الوضع والرافعة والرمز والكمية والدقة.
3. حفظ الاستجابة الخام، ومعرف BingX، ووقت الإرسال، و`clientOrderId`.
4. انتظار `TRADE_UPDATE` أو الاستعلام عبر `fetchOrder`.
5. قراءة `fetchMyTrades` عند الحاجة لإثبات التنفيذات الجزئية، والكمية التراكمية، ومتوسط السعر.
6. إعادة قراءة المركز عبر `fetchPositions` أو endpoint المراكز عند تغير التعرض.
7. معالجة `PARTIALLY_FILLED` كحالة مفتوحة جزئيًا، لا كنجاح نهائي.

عند timeout أو `504` أو خطأ شبكة، لا يُعاد إرسال الأمر مباشرة. يجب أولًا الاستعلام باستخدام معرف الأمر أو `clientOrderId`، لأن الطلب قد يكون قُبل على المنصة رغم عدم وصول الرد. ويجب الاحتفاظ بمسار REST للمصالحة بعد إعادة الاتصال أو فقد حدث WebSocket.

WebSocket الإنتاج:

```text
wss://open-api-swap.bingx.com/swap-market?listenKey=...
```

وVST:

```text
wss://vst-open-api-ws.bingx.com/swap-market
```

لا يلزم اشتراك channel بعد الاتصال وفق النتائج المراجعة. ويجب إنشاء Listen Key وتجديده؛ صلاحيته ساعة ويُمدد دوريًا، وتذكر مراجع BingX التمديد كل 30 دقيقة. الأحداث المهمة تشمل `ACCOUNT_UPDATE` و`TRADE_UPDATE` و`ACCOUNT_CONFIG_UPDATE` و`listenKeyExpired`. قد تصل الرسائل بصيغة GZIP مع Ping/Pong.

لا ينبغي افتراض أن صفحة “Order update push” لا ترسل إلا نوعًا واحدًا من الأحداث. في `TRADE_UPDATE` توجد حقول مثل نوع التنفيذ `o.x`، حالة الأمر `o.X`، جانب المركز `o.ps`، وقد يظهر `o.li` لربط أمر حماية بالأمر الأب. أما `ACCOUNT_UPDATE` فيحمل جانب المركز ضمن `a.P[].ps`.

## 6. Native SL/TP والحماية

تدعم BingX إرفاق `stopLoss` و`takeProfit` أثناء وضع أمر `MARKET` أو `LIMIT`، على هيئة JSON متسلسلة تتضمن عادةً `stopPrice` و`workingType` و`type` و`quantity`. كما يمكن أن يحول استخدام `stopLossPrice` أو `takeProfitPrice` في محول CCXT النوع إلى `STOP` أو `TAKE_PROFIT` ويجعل الأمر `reduceOnly` ضمنيًا وفق فحص المصدر.

أنواع الحماية الموثقة تشمل:

- `STOP_MARKET` و`STOP`.
- `TAKE_PROFIT_MARKET` و`TAKE_PROFIT`.
- `TRAILING_STOP_MARKET`.
- `TRAILING_TP_SL`.

`stopGuaranteed` ليس مفعّلًا افتراضيًا وقد يفرض رسمًا، وقد لا يتاح لبعض الأزواج أو الحسابات.

يجب التمييز بين native API SL/TP وبين سلوك واجهة المنتج. لا يثبت البحث أن حماية مرفقة بأمر أولي ستبقى دائمًا بنفس الكمية بعد partial close، ولا أن كمية الطفل ستُعاد ضبطها تلقائيًا. كما لم يُحسم تطابق `closePosition=true` في API مع كل سلوك Position TP/SL في واجهة BingX، خصوصًا في Hedge وSeparate Isolated. لذلك ينبغي اعتبار ما بعد التنفيذ الجزئي سيناريو اختبار قبول مستقلًا.

## 7. الفروق بين وثائق BingX وتنفيذ CCXT

1. **الإصدار والمسارات:** وثائق BingX الحالية مختلطة بين v1 وv2: وضع المركز على v1، بينما المراكز والأوامر والرافعة والإغلاق الشامل على v2. محول CCXT يخفي بعض هذا الاختلاف، لكنه يغير المسار لعقود Coin-M.
2. **المعاملات الموحدة والخاصة:** CCXT يميز بين Unified API وexchange-specific/implicit API. وجود `reduceOnly` أو `clientOrderId` في الواجهة الموحدة لا يضمن قبول نفس الصيغة والقيمة في كل سوق BingX.
3. **معرف العميل:** عقود BingX تستخدم `clientOrderId`، بينما Spot يستخدم `newClientOrderId`. parser في المحول يقبل صيغًا متعددة، لكن التكامل يجب أن يثبت الاسم حسب السوق.
4. **`closePosition`:** المحول يستهلك هذا المعنى محليًا في بعض المسارات لمنع إرسال `quantity`، ولا يمرر بالضرورة المفتاح الخام نفسه. لذلك لا يجوز افتراض أن `params.closePosition=true` سينتج طلب BingX مطابقًا دون فحص إصدار المحول.
5. **SL/TP:** المحول يبني JSON للحماية ويتعامل مع `stopLossPrice` و`takeProfitPrice`، لكن توثيق BingX يصف أيضًا `stopLoss` و`takeProfit` الخام. ينبغي اختيار طبقة واحدة واضحة ومنع إرسال الصيغتين معًا دون سبب.
6. **الحالات:** قد تعرض BingX حالات مثل `PENDING` أو `FAILED` أو `EXPIRED`، بينما يختزل CCXT بعضها في حالات موحدة. يجب الاحتفاظ بالحالة الخام للرقابة والتدقيق.
7. **المراكز:** `fetchPositions` و`fetchPositionMode` في CCXT موجهان للسوق الخطي في بعض الطرق، ولا ينبغي تعميمهما على Coin-M أو الأسواق العكسية.

## 8. حدود المعدل والتشغيل

ورد إعلان BingX الرسمي أن حد وضع الأمر ارتفع إلى 10 طلبات/ثانية ابتداءً من 2025-10-16، وأن حد IP العام أصبح 2,000 طلب لكل 10 ثوانٍ ابتداءً من 2025-09-10، مع معدلات مستقلة للمراكز والرافعة ووضع المركز والتداول التاريخي. في المقابل، تعرض بعض صفحات المسارات أرقامًا مختلفة، منها 4/ثانية لوضع المركز، و10/ثانية للمراكز، و5/ثانية للرافعة مقابل أرقام أقل في الإعلان.

إلى حين التحقق من بيئة الحساب الفعلية، يجب استخدام limiter محافظ ومفصول حسب endpoint، مع backoff، وقياس معدلات الرفض، وعدم اعتبار إعلان عام أو صفحة مسار منفردة مرجعًا وحيدًا. كما تذكر إعلانات BingX قيودًا على التداول بعد ارتفاع عدد الأوامر ونسبة الإلغاء؛ وهذا قد يؤثر على استراتيجيات cancel-replace وإعادة التسعير.

## 9. النقاط غير المحسومة ومخاطر الاعتماد

- لم يُثبت إصدار CCXT في مشروع GGP، ولذلك لا يمكن الجزم بأن سلوك المحول الفعلي يطابق المصدر الذي تمت مراجعته.
- لم يُنفذ اختبار حي أو اختبار VST؛ لا يوجد إثبات عملي لنجاح كل endpoint أو ترتيب أحداث REST وWebSocket.
- لا يوجد حسم موثوق لبقاء native SL/TP أو كمية أمر الحماية بعد partial close.
- لا يوجد حسم لذرّية cancel-replace أو ما يحدث للوقف الأصلي عند نجاح الإلغاء وفشل الأمر البديل أو انتهاء المهلة.
- لا يحسم المصدر أي endpoint هو المرجع عند تعارض `reduceOnly` في بعض conditional orders ضمن بلاغ VST المفتوح.
- لا يثبت ظهور `o.li` في كل تحديثات WebSocket أو أنه يكفي وحده لمعرفة كمية الحماية المتبقية.
- لا يوضح `closeAllPositions` صراحةً في النص المتاح ما إذا كان غياب `symbol` يغلق كل رموز USDT وUSDC في كل أنواع الحسابات.
- لا تثبت `code=109500` عدم وجود مركز.
- لا تزال قيود طول وصيغة `clientOrderId` غير مثبتة لكل سوق.
- لم يُحسم سلوك `fetchPositions` عند حذف `symbols` أو تمرير قائمة فارغة، ولا قبول `fetchOrder` دون `symbol` في كل الحالات.
- توجد تقارير مجتمع عن فقد تحديثات `ACCOUNT_UPDATE` في إصدارات CCXT قديمة؛ لذلك يلزم REST reconciliation حتى مع وجود WebSocket.
- قضايا المجتمع المتعلقة بـ trailing/native SL/TP، ومواضع `positionSide`، وتأخير تحميل الأسواق هي أدلة متوسطة وليست مواصفات BingX العامة.

## 10. توصيات تنفيذية لمشروع GGP

1. تثبيت وإظهار إصدار CCXT صراحةً في بيئة المشروع، وتسجيل نسخة محول BingX المستخدمة.
2. بناء طبقة BingX adapter تملك serializer صريحًا لـ One-Way وHedge، بدل تمرير قاموس `params` عام غير متحقق.
3. فرض state machine محلية للأمر تحفظ الطلب الخام، والاستجابة، والتحديثات، والتنفيذات، والمصالحة.
4. جعل `clientOrderId` مفتاح idempotency، والاستعلام قبل إعادة الإرسال بعد timeout أو 504.
5. فصل مسارات linear swap وCoin-M وعدم مشاركة افتراضات position mode أو leverage بينها.
6. تشغيل REST reconciliation دوريًا وبعد reconnect أو `listenKeyExpired`، مع مقارنة المراكز والتنفيذات والأوامر المفتوحة.
7. إضافة اختبارات قبول مصرح بها لـ Hedge وOne-Way، و`reduceOnly`، والإغلاق الكامل، وpartial fill، وnative SL/TP، وcancel-replace، وتجديد Listen Key.
8. اعتماد limiter محافظ قابل للتهيئة، مع telemetry لرموز الخطأ ومعدلات الرفض، إلى أن تُحسم الفروق بين صفحات BingX والإعلان الرسمي.
9. عدم اعتبار رد `createOrder` أو وصول `TRADE_UPDATE` منفردًا دليلًا كافيًا على اتساق المركز؛ يجب مطابقة order/trades/position.
10. إبقاء النقاط غير المحسومة خلف feature flags أو سياسات تشغيل قابلة للتعطيل، خصوصًا native protection و`closePosition` في السيناريوهات المختلطة.

## 11. المراجع

### BingX API وBingX Support

- [Set Position Mode](https://bingx-api.github.io/docs-v3/#/en/Swap/Trades%20Endpoints/Set%20Position%20Mode)
- [Place Order](https://bingx-api.github.io/docs-v3/#/en/Swap/Trades%20Endpoints/Place%20order)
- [Query Position Data](https://bingx-api.github.io/docs-v3/#/en/Swap/Account%20Endpoints/Query%20position%20data)
- [Close All Positions](https://bingx-api.github.io/docs-v3/#/en/Swap/Trades%20Endpoints/Close%20All%20Positions)
- [Set Leverage](https://bingx-api.github.io/docs-v3/#/en/Swap/Trades%20Endpoints/Set%20Leverage)
- [Order Update Push](https://bingx-api.github.io/docs-v3/#/en/Swap/Websocket%20Account%20Data/Order%20update%20push)
- [Perpetual Futures: One-Way Mode](https://bingx.com/en/support/articles/26343882391833-perpetual-futures-introducing-one-way-mode)
- [Perpetual Futures: Position TP/SL](https://bingx.com/en/support/articles/26392412034713-perpetual-futures-introducing-position-tp-sl)
- [BingX API trading rules and rate updates](https://bingx.com/en/support/articles/31103871611289)
- [One-Way Mode Explained](https://bingx.com/en/support/articles/23233089254169-perpetualfuturesone-waymodeexplained)
- [Spot and Perpetual Futures API Trading Rules](https://bingx.com/en/support/articles/39958644800537-updates-on-bingx-spot-and-perpetual-futures-api-trading-rules-2024-11-15)

### BingX-API GitHub

- [BingX-API/api-ai-skills](https://github.com/BingX-API/api-ai-skills)
- [Swap Trade Skill](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/SKILL.md)
- [Swap Trade API Reference](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md)
- [Swap WebSocket Account API Reference](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-ws-account/api-reference.md)
- [Swap WebSocket Account Skill](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-ws-account/SKILL.md)
- [Error Codes](https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/error-codes.md)
- [Change Log](https://github.com/BingX-API/api-ai-skills/blob/main/CHANGELOG.md)
- [Open Issue #2](https://github.com/BingX-API/api-ai-skills/issues/2)
- [Open Issue #4](https://github.com/BingX-API/api-ai-skills/issues/4)
- [BingX Standard Contract REST API](https://github.com/BingX-API/BingX-Standard-Contract-doc/blob/main/REST%20API.md)
- [BingX Swap API Issue #28](https://github.com/BingX-API/BingX-swap-api-doc/issues/28)

### CCXT

- [CCXT Manual](https://docs.ccxt.com/docs/manual)
- [CCXT BingX Exchange Documentation](https://docs.ccxt.com/docs/exchanges/bingx)
- [CCXT Create Order Base Specification](https://docs.ccxt.com/docs/base-spec#createorder)
- [CCXT Fetch Order Base Specification](https://docs.ccxt.com/docs/base-spec#fetchorder)
- [CCXT Fetch My Trades Base Specification](https://docs.ccxt.com/docs/base-spec#fetchmytrades)
- [CCXT Official BingX Adapter](https://raw.githubusercontent.com/ccxt/ccxt/master/python/ccxt/bingx.py)
- [CCXT Latest Releases](https://github.com/ccxt/ccxt/releases/latest)
- [CCXT Issue #20548](https://github.com/ccxt/ccxt/issues/20548)
- [CCXT Issue #20840](https://github.com/ccxt/ccxt/issues/20840)
- [CCXT Issue #21685](https://github.com/ccxt/ccxt/issues/21685)
- [CCXT Issue #22836](https://github.com/ccxt/ccxt/issues/22836)

**حالة هذا التقرير:** بحث مكتبي موحّد؛ لا يمثل اختبارًا حيًا ولا تغييرًا في كود مشروع GGP.

## 12. ملحق مراجعة 2026-11-09 — Recovery ونسخة الاعتماد

- أصبح `requirements.txt` يثبت `ccxt==4.5.92`؛ لذلك لم يعد الاعتماد على إصدار مفتوح. يجب التحقق من `ccxt.__version__` على خادم التشغيل نفسه بعد تثبيت المتطلبات.
- وثائق CCXT الحالية لمحول BingX توثق `fetchPositionMode(symbol)` كطريقة قراءة وضع المركز، وتوضح أنها تستهدف أسواق Swap الخطية ولا تدعم Coin-M العكسية. كما توثق شكل الحقل الموحد `hedged`. تم الإبقاء على القراءة فقط؛ لا يستخدم GGP `setPositionMode()` تلقائيًا. مرجع: <https://docs.ccxt.com/docs/exchanges/bingx>.
- مرجع CCXT المفتوح يوضح تحويل Dual Side من `dualSidePosition` إلى `hedged`، ويوثق معاملات وقف الخسارة الشرطي على مستوى الواجهة الموحدة. هذه مراجع لفهم المحول وليست بديلًا عن اختبار الإصدار المثبت على حساب Demo. مرجع الكود: <https://github.com/ccxt/ccxt/blob/master/php/bingx.php>.
- استعادة GGP الجديدة لا تفترض أن نجاح `fetchPositionMode` وحده يكفي؛ يلزم تطابق سجل الصفقة، الاتجاه، الكمية، وقراءة Native STOP_MARKET. في حال اختلاف كمية المركز عن Snapshot المحفوظ تبقى المصالحة مطلوبة قبل تفعيل TP1/TP2.
- لم يتم تأكيد استجابة حساب BingX للمشروع المعدل على Demo/VST ضمن بيئة البناء؛ الاختبار المحلي لا يثبت صلاحية LIVE.
