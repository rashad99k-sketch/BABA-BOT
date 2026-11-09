# تدقيق دمج Institutional Watchlist بين RORO وGGP

## البنية المضافة

تجمع طبقة الدمج مصادر GGP وRORO في سجل موحد، منها RF Watchlist وScanner V2 Buy/Sell وRadar وTop Candidates ومصدر الذاكرة الرئيسي. تستخدم `canonical_symbol()` كمفتاح دمج مستقرًا، وتحافظ على `exchange_symbol()` بصيغة CCXT مثل `BTC/USDT:USDT` لجلب البيانات والتنفيذ.

يتم تحديث عدد محدود من الرموز في كل دورة، ويُستخدم إطار شموع مغلقة قبل التحليل. يتضمن ناتج الرمز حالة Market Regime وMarket Structure وLiquidity/Sweep وOB وFVG وVolume وSetup Type وEntry State وStructural SL وجودة البيانات والتوقيت. كما يستدعي Callback محركات GGP الموجودة فعليًا لكل اتجاه: `SmartMoneyEngine` و`MomentumFlowEngine` و`ADXDIIntelligence` ونسخة مستقلة لكل رمز من `InstitutionalTrendEngine` و`TradeStateMachine`. تُعرض نتائجها كأدلة تشخيصية/مساندة ولا تُستخدم لتجاوز الشروط البنيوية الإلزامية.

## الأيقونات

الأيقونات والحالات في Watchlist مرتبطة بقيم التحليل، وليست عناصر ثابتة. أضيفت أيقونات Smart Money وMomentum وTrend Engine، مع عرض Bias وContinuation Strength وADX/DI وصحة الاتجاه في تفاصيل الرمز. تتضمن اتجاه السوق والبنية والسيولة وSweep/Stop Hunt وFailed Auction وOrder Block وFVG والحجم ونوع Setup والتصحيح والوقف وحالة الدخول. البيانات غير المتاحة تظهر UNKNOWN أو حالة رمادية، ولا تتحول إلى تأكيد إيجابي.

## مسار القرار

`GGP/RORO SOURCES → MERGE & ROTATION → CLOSED-CANDLE ANALYSIS → REVERSAL / CONTINUATION → UNIFIED AUTHORITY → ORIGINAL GGP execute_entry()`

طبقة التحليل لا ترسل أوامر تداول مباشرة.

## حدود

تم إثبات وظائف الدمج والتحليل المعزولة باختبارات Python، لكن تعذر استيراد المضيف كاملًا في بيئة البناء بسبب عدم وجود `ccxt` و`flask`. يلزم تأكيد تشغيل Dashboard وAPI على Windows بعد تثبيت المتطلبات. لا تثبت اختبارات الوحدة صحة الربحية أو بيانات السوق الحية.

## تحديث نهائي بعد مراجعة `hj.py` و`roro.py` — 2026-11-08

أضيف `ggp_discovery_scanner.py` لتطبيق Professional Lightweight Scan بحد 80 رمزًا، مع نفس OHLCV cache المستخدم في V2 وRF. الـScanner يرشح بناءً على السيولة بالدولار وتسارع الحجم والزخم المرجح بالحجم والتقلب والقرب من Swing مؤكد والبنية واتجاه EMA20/50. هذه نتائج اكتشاف فقط.

تم تعديل ترتيب دورة Watchlist ليعطي وزنًا لحالة القرار السابقة ودرجة Golden Zone وقرب OB بالـATR وثقة السيولة وAnalysis Priority ودرجة الاكتشاف وحداثة التحليل، مع استمرار التدوير حتى لا تُحرم الرموز الأقل ترتيبًا من الفحص.

تم إصلاح استدعاء MomentumFlowEngine ليتوافق مع توقيع GGP الفعلي؛ هذه الدالة اتجاهية محايدة، أما مواءمة BUY/SELL فتتم في Trend Engine والتحليل البنيوي لكل جانب.
