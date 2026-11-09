# JGB / GGP — Institutional Trade Execution & Recovery

هذه حزمة مشروع GGP الأصلية بعد إضافة معالجة أكثر صرامة لـ`RECOVERY_HOLD` واستعادة حالة الصفقات من SQLite ومصالحة BingX. ملف RORO استُخدم للمقارنة المرجعية في دورة حياة مدير الصفقة، ولم يُدمج مشروعه أو استراتيجيته داخل GGP.

## تشغيل آمن

1. فك ضغط ZIP في مجلد جديد.
2. أنشئ بيئة Python وثبت المتطلبات:
   `pip install -r requirements.txt`
3. انسخ `.env.example` إلى `.env` وأدخل المفاتيح بنفسك. لا ترسل ملف `.env` لأي طرف.
4. ابدأ بوضع PAPER، ثم شغّل `RUN_TESTS.bat` أو `python -m pytest -q`.
5. بعد ذلك اختبر حساب BingX Demo/VST فقط، وراقب سجلات Position Mode وFills وNative SL وRecovery قبل التفكير في LIVE.

## ملف RECOVERY_HOLD

إذا ظهر Hold افتح السجل وابحث عن `recovery_reason` المحدد. لا تمسح Hold يدويًا. الأسباب قد تكون سجلًا غير موجود أو مكررًا، وضع حساب غير معروف، اختلاف الكمية الحالية عن Snapshot، نقص بيانات التعبئة، أو عدم إثبات Native SL. النظام يحتفظ بالتعرض الحقيقي ويمنع الدخول الجديد وإجراءات TP/SL غير المثبتة.

## ملفات رئيسية

- `ggpbot.py`: تشغيل البوت، المدير المركزي، المصالحة والاستعادة.
- `bingx_execution.py`: بوابة التنفيذ وقراءة Position Mode.
- `bingx_protection.py`: تحليل أوامر الحماية الأصلية.
- `roro_trade_journal.py`: SQLite وTrade Journal.
- `ggp_recovery.py`: تحقق مستقل من بيانات استعادة الصفقة.
- `data/ggp_trade_journal.sqlite3`: سجل المشروع؛ احتفظ بنسخة احتياطية قبل أي ترقية.

## التقارير

راجع `FIX_REPORT_AR.md` و`EXECUTION_FORENSIC_AUDIT_AR.md` و`RECOVERY_TEST_REPORT_AR.md` و`HEDGE_ONE_WAY_TEST_REPORT_AR.md` و`TRADE_LOG_REPORT_AR.md` و`TEST_RESULTS_AR.md`.

## حالة الإصدار

الاختبارات المحلية ناجحة، لكن لم يتم الاتصال بحساب BingX Demo/VST أو Live في بيئة البناء. لذلك الإصدار مرشح PAPER/DEMO وليس LIVE READY. لا توجد ضمانة ربح أو ضمان تشغيل حقيقي قبل اختبار المنصة والحساب.

## سياسة الرافعة وتخصيص الهامش وإدارة الأرباح (إصدار 2026-11-09)
- الرافعة للدخول الجديد: 20x، مع ضبط وقراءة تحقق من BingX قبل كل دخول.
- الفرص المتوسطة: سقف هامش 20% من الرصيد المتاح.
- الفرص القوية المعتمدة مؤسسيًا: سقف هامش 30% من الرصيد المتاح؛ تسمية SNIPER وحدها لا تكفي.
- النسب أعلاه سقوف للهامش، وليست نسبة خسارة. حد المخاطرة الافتراضي عند الوقف يظل 1% من الرصيد المتاح، وقد يخفض الحجم الفعلي عن السقف.
- مدير الأرباح الوحيد هو TradeManagementBoard / LiveTradeManager: TP1 50% مثبت بالتعبئة، TP2 للكمية المتبقية، مع EMA20/EMA50 وPeak ROE وAdaptive ATR Trail.
- تحقق Native SL يطابق أمر BingX المشروط من حيث النوع والحالة والرمز والاتجاه وPosition Side وسعر التفعيل والكمية ومعرف الأمر، بما في ذلك `triggerOrderId`.
- لا تتغير رافعة أو حماية المراكز المفتوحة تلقائيًا بسبب تحديث الإصدار. افحص كل مركز قائم على BingX أولًا.
- لا تفعّل LIVE قبل اختبار PAPER وBingX Demo. نجاح الاختبارات المحلية لا يثبت تشغيلًا فعليًا 24/7.
راجع `LEVERAGE_SIZING_PROFIT_UPGRADE_AR.md` للتفاصيل.
