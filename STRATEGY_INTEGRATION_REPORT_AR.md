# تقرير دمج الاستراتيجية المؤسسية

## Reversal Engine

يعالج مسار Liquidity Sweep من مستوى سيولة معروف إلى Sweep/Reclaim ثم MSS/CHoCH مؤكد على Swing Structure، ثم Retest لاحق ومحفز اتجاهي لاحق. يوجد مسار Failed Auction منفصل يتطلب قبولًا سعريًا خارج مستوى مرجعي ثم فشل قبول وReclaim، وبعده تأكيدًا بنيويًا واختبارًا لاحقًا. Exhaustion Reversal لا يمنح دخول LIVE في الإصدار الحالي.

## Trend Continuation Engine

لا يشترط وجود Sweep أو EMA9/VWAP Cross. يشترط اتجاهًا وبنية ملائمين، وBOS/Displacement، ثم Pullback أو Retest أو Micro Pullback صالحًا ومحفزًا لاحقًا. يتم تطبيق NO_CHASE عند ابتعاد السعر عن المنطقة السببية. Acceptance دون Retest ليس مسار دخول LIVE في هذه المرحلة.

## دور المؤشرات

EMA9 وSession VWAP وRF وADX/DI وVolume وMomentum أدوات أدلة مساعدة. لا يُسمح للكروس أو ADX وحدهما بامتلاك قرار الدخول لكل نوع Setup. شروط الأمان والمخاطر الحالية تظل مستقلة عن درجة Score.

## Stop Loss

Structural Stop Loss يُشتق من نقطة إبطال الفرضية مع هامش ATR وتحقق من جهة السعر، ثم يمر إلى مسار تنفيذ GGP الحالي. يجب اختبار تثبيت Native Stop فعليًا على BingX Demo؛ لم يتم ذلك في بيئة البناء.

## حدود

تم اختبار محركات القرار والسيناريوهات المحددة ببيانات اختبارية، لكن لا يوجد Backtest خارج العينة أو قياس Win Rate معاير. لا ينبغي اعتبار Score توقعًا إحصائيًا لاحتمال الربح.
