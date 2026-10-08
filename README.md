# whatsapp-mauve

أرشيف واتساب شخصي: نسخة احتياطية من التلفون (Android، crypt15) بعد فكّ تشفيرها، ومزامنة حيّة للرسائل الجديدة عبر جهاز مرتبط، وعارض بشكل واتساب ويب على `127.0.0.1`. كل حساب واتساب له مجلد مستقل، وكل شي بيشتغل داخل Docker.

## التخطيط

```
whatsapp-mauve/
  README.md          هذا الملف (للاستعمال)
  compose.yml        مولَّد بـ`wa regen`: يعمل include لكل حساب (لا تعدّله)
  wa                 أمر التحكم بكل شي (نقطة الدخول)
  install.sh         المثبّت (wa + بناء الصورة + حساب جديد + تشغيل)
  wa.conf.example    قالب الإعدادات (كل المفاتيح موثّقة فيه)
  profiles/          البيانات: مجلد لكل حساب (قاعدة + وسائط + جلسة). ما تنلمس
  state/handoff/     ملاحظات التطوير
  src/               كل كود المشروع (انظر src/README.md)
    Dockerfile, .dockerignore, Makefile
    docker/          نقطة دخول الحاوية + وحدات تنبيه systemd
    tools/           الكود نفسه (السيرفر، المزامنة، المستوردات، الواجهة، الاختبارات)
    docs/            ARCHITECTURE.md (البنية والتفاصيل)
  legacy/            التثبيت القديم بدون docker (غير مصان)
```

## التثبيت

المتطلبات: `docker` (مع `docker compose`) و`python3`.

    git clone <repo> whatsapp-mauve && cd whatsapp-mauve
    ./install.sh          # معالج عربي/إنجليزي: يثبّت `wa` ويبني الصورة ويضيف حساب ويشغّله

المعالج بيشرح مفتاح الـ64 رقم. المفتاح من واتساب: **الإعدادات ← الدردشات ← النسخ الاحتياطي للدردشات ← نسخ احتياطي مشفّر تمامًا ← استخدام مفتاح تشفير من 64 رقمًا**. إذا ضاع المفتاح: أوقف التشفير وفعّله من جديد لتاخد مفتاح جديد، واعمل نسخة احتياطية جديدة (النسخ القديمة ما بتنفتح بالمفتاح الجديد).

## التشغيل

    docker compose up -d                    # كل الحسابات (الصورة بتنبني إذا ناقصة)
    docker compose up -d <اسم>-sync         # مزامنة حساب واحد
    docker compose up -d <اسم>-viewer       # العارض لحساب واحد
    docker compose logs -f <اسم>-sync       # متابعة السجل
    docker compose down                     # إيقاف الكل

الخدمات: `<اسم>-sync` (المزامنة الحيّة، بتشتغل مع الإقلاع) و`<اسم>-viewer` (العارض على المنفذ المكتوب في `wa.conf`، بيشتغل عند الطلب إلا إذا `WA_VIEWER_AUTOSTART=1`).

## أوامر `wa`

`wa` بيشتغل من أي مكان (حتى عبر الرابط `~/.local/bin/wa`).

    wa new <اسم> [--port N]                 حساب جديد profiles/<اسم>/ (منفذ حر تلقائياً)
    wa list                                 الحسابات والمنافذ وحالة التشغيل
    wa up|down|restart|status|logs [اسم|all]  بدون اسم = كل الحسابات
    wa key <اسم>                            حفظ مفتاح الـ64 رقم (.wa_key بصلاحية 600، ما بينعرض)
    wa import <اسم> [مجلد WhatsApp من التلفون]   فكّ تشفير msgstore + الوسائط
    wa link <اسم>                           ربط كجهاز مرتبط (QR)، اختياري
    wa pin <اسم> [--clear]                  رمز قفل العارض
    wa backup <اسم> <وجهة>                  نسخة كاملة من الحساب بدون cache/
    wa restore <مجلد الحساب> [--move]       استعادة الحساب إلى profiles/ ثم تشغيله
    wa regen                                إعادة توليد compose.yml لكل الحسابات
    wa notify-on|notify-off <اسم>           إشعار سطح المكتب إذا انفك الربط
    wa migrate-legacy <مجلد قديم> <اسم> [--dry-run]   ترحيل التثبيت القديم (بدون docker)
    wa build | wa shell <اسم>

بعد تعديل `wa.conf` (منفذ، `WA_VIEWER_AUTOSTART`، `WA_PHONE_DIR`) أو بعد استعادة على جهاز تاني: `wa regen` (و`wa up` بيعملها تلقائياً).

## مجلد الحساب

    profiles/<اسم>/
      wa.conf        إعدادات الحساب (المنفذ، الوصول، كلمة السر، ...)
      compose.yml    مولَّد من wa.conf (لا تعدّله)
      archive.db     القاعدة (المصدر الوحيد للحقيقة)
      media/         الوسائط بأسماء مجلدات واتساب نفسها (WhatsApp Images/، WhatsApp Voice Notes/…)
      state/         جلسة واتساب (auth/) والمفتاح والإعدادات الداخلية
      user/          الصور الشخصية والمفضلة وبيانات المستخدم
      cache/         يتولّد من جديد، ما بيدخل بالنسخ الاحتياطي
      .wa_key        مفتاح التشفير (600)

`media/` ممكن تنسخه للتلفون أو تقرأه من أي برنامج. الحساب اللي فيه `wa.conf` هو الحساب: `profiles/` هو السجل، ما في `~/.config`.

## النسخ الاحتياطي والاستعادة

    wa backup 3mro777o /mnt/backup         # بيعمل /mnt/backup/3mro777o (القاعدة عبر sqlite backup، والـhardlinks محفوظة)
    wa restore /mnt/backup/3mro777o        # على أي جهاز عليه docker وinstall.sh (ينسخ لـprofiles/ ثم wa regen)

النسخة فيها الجلسة (`state/auth`) ومفتاح التشفير (`.wa_key`) وكل الرسائل: خزّنها بمكان خاص. ما تشغّل نسخة مستعادة والأصل شغّال على نفس الجلسة بنفس الوقت.

## ربط `media/` مع Immich

مكتبة خارجية للقراءة فقط. حط هذا تحت `immich-server` في `docker-compose.yml` حق Immich:

    volumes:
      - /path/to/whatsapp-mauve/profiles/<اسم>/media:/usr/src/app/external/whatsapp-<اسم>:ro

وبـImmich: *Administration ← External Libraries ← Create*، وImport path = `/usr/src/app/external/whatsapp-<اسم>`، وExclusion patterns = `**/.Statuses/**` إذا ما بدك الحالات.

## تعديل الكود وإعادة البناء

الكود بالصورة (`src/`)، فبعد أي تعديل:

    docker compose build && docker compose up -d

التفاصيل (خريطة الكود، الاختبارات، أين تتوثّق مفاتيح الإعدادات) بـ[src/README.md](src/README.md). البنية بـ[src/docs/ARCHITECTURE.md](src/docs/ARCHITECTURE.md).

## legacy/

التثبيت القديم المباشر على الجهاز بدون docker (`legacy/setup.sh`، وقوالب systemd). غير مصان، ومُبقى للمرجع فقط. الاستعمال الموصى به هو Docker.
