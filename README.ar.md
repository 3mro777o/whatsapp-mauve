[English](README.md) | **العربية**

# whatsapp-mauve

أرشيف واتساب شخصي: نسخة احتياطية من التلفون (Android، crypt15) بعد فكّ تشفيرها، ومزامنة حيّة للرسائل الجديدة عبر جهاز مرتبط، وعارض بشكل واتساب ويب على `127.0.0.1`. كل حساب واتساب له مجلد مستقل، وكل شي بيشتغل داخل Docker.

> أول مرة؟ ابدأ بـ [HOW-IT-WORKS.ar.md](HOW-IT-WORKS.ar.md): شرح بسيط لكيف بيشتغل المشروع.

## التخطيط

```
whatsapp-mauve/
  README.ar.md       هذا الملف (للاستعمال)
  HOW-IT-WORKS.ar.md شرح بسيط للمبتدئين
  compose.yml        مولَّد بـ`wa regen`: يعمل include لكل حساب (لا تعدّله)
  wa                 أمر التحكم بكل شي (نقطة الدخول)
  install.sh         المثبّت (wa + بناء الصورة + حساب جديد + تشغيل)
  wa.conf.example    قالب الإعدادات (كل المفاتيح موثّقة فيه)
  profiles/          البيانات: مجلد لكل حساب (قاعدة + وسائط + جلسة). ما تنلمس
  state/handoff/     ملاحظات التطوير
  src/               كل كود المشروع (انظر src/README.ar.md)
    Dockerfile, .dockerignore, Makefile
    docker/          نقطة دخول الحاوية + وحدات تنبيه systemd
    tools/           الكود نفسه (السيرفر، المزامنة، المستوردات، الواجهة، الاختبارات)
    docs/            ARCHITECTURE.ar.md (البنية والتفاصيل)
  legacy/            التثبيت القديم بدون docker (غير مصان)
```

## التثبيت

المتطلبات: `docker` (مع `docker compose`) و`python3`.

    git clone <repo> whatsapp-mauve && cd whatsapp-mauve
    ./install.sh          # معالج عربي/إنجليزي: يثبّت `wa` وينزّل الصورة (ويبنيها محليًا إذا ما انسحبت) ويضيف حساب ويشغّله

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
    wa update [اسم]                         ثبّت أحدث إصدار (شوف الإصدارات والتحديث)
    wa autoupdate-on|autoupdate-off [اسم]   تحديث بنقرة من العارض (+ تحديث ليلي اختياري)
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

    wa backup myname /mnt/backup         # بيعمل /mnt/backup/myname (القاعدة عبر sqlite backup، والـhardlinks محفوظة)
    wa restore /mnt/backup/myname        # على أي جهاز عليه docker وinstall.sh (ينسخ لـprofiles/ ثم wa regen)

النسخة فيها الجلسة (`state/auth`) ومفتاح التشفير (`.wa_key`) وكل الرسائل: خزّنها بمكان خاص. ما تشغّل نسخة مستعادة والأصل شغّال على نفس الجلسة بنفس الوقت.

## ربط `media/` مع Immich

مكتبة خارجية للقراءة فقط. حط هذا تحت `immich-server` في `docker-compose.yml` حق Immich:

    volumes:
      - /path/to/whatsapp-mauve/profiles/<اسم>/media:/usr/src/app/external/whatsapp-<اسم>:ro

وبـImmich: *Administration ← External Libraries ← Create*، وImport path = `/usr/src/app/external/whatsapp-<اسم>`، وExclusion patterns = `**/.Statuses/**` إذا ما بدك الحالات.

## الإصدارات والتحديث

كل إصدار صورة متعددة المعمارية (amd64 وarm64) على GitHub Container Registry: `ghcr.io/3mro777o/whatsapp-mauve` (الوسوم `1.0.1` و`1.0` و`latest`). افتراضيًا ملفات compose للحسابات بتستعمل `ghcr.io/3mro777o/whatsapp-mauve:latest`.

- **وضع الصورة (الافتراضي):** `wa update` = `docker compose pull` ثم `up -d`.
- **وضع المطوّر:** ضع `WA_IMAGE=whatsapp-mauve:local` بـ`profiles/<اسم>/wa.conf`. ملف compose بيبقى فيه `build: ../../src`، و`wa update` = `git pull --ff-only` ثم `docker compose build` ثم `up -d`.
- **إشعار التحديث:** العارض بيسأل GitHub عن آخر إصدار مرة كل 12 ساعة بالأكثر (`WA_UPDATE_CHECK=0` بيطفّيه) وبيعرض شريط «نسخة جديدة vX.Y.Z» مع «ما الجديد» و«تحديث الآن». «تحديث الآن» بيحتاج PIN؛ الحاوية بس بتكتب `state/update-request` (ما إلها وصول لـdocker). شغّل `wa autoupdate-on` مرة على الجهاز: وحدة systemd (path) للمستخدم بتشوف الملف وبتشغّل `wa update` (السجل: `journalctl --user -u 'wa-update@*'`). `WA_AUTO_UPDATE=1` بـ`wa.conf` (ثم `wa autoupdate-on` من جديد) بيضيف `wa update` ليلي؛ `wa autoupdate-off` بيشيل الاثنين.

إصدار نسخة جديدة (للمطوّر): عدّل `src/VERSION` (مثلاً `1.0.1`)، commit، ثم

    git tag v1.0.1 && git push --tags

GitHub Actions (`.github/workflows/docker.yml`) بيبني الصورة وبيرفعها وبينشئ GitHub Release. لازم يطابق الوسم `src/VERSION`. بعد أول رفع اجعل الحزمة عامة مرة وحدة: GitHub ← حسابك ← Packages ← whatsapp-mauve ← Package settings ← Change visibility ← Public (وإلا `docker pull` بيحتاج تسجيل دخول).

## تعديل الكود وإعادة البناء

الكود بالصورة (`src/`)، فبعد أي تعديل:

    docker compose build && docker compose up -d

التفاصيل (خريطة الكود، الاختبارات، أين تتوثّق مفاتيح الإعدادات) بـ[src/README.ar.md](src/README.ar.md). البنية بـ[src/docs/ARCHITECTURE.ar.md](src/docs/ARCHITECTURE.ar.md).

## legacy/

التثبيت القديم المباشر على الجهاز بدون docker (`legacy/setup.sh`، وقوالب systemd). غير مصان، ومُبقى للمرجع فقط. الاستعمال الموصى به هو Docker.

## الترخيص

[AGPL-3.0](LICENSE). فيك تستعمل المشروع وتعدّل عليه وتشغّله على سيرفرك بحرية. بس إذا وزّعته، أو شغّلت نسخة معدّلة كخدمة لناس تانيين، لازم تنشر تعديلاتك بنفس الترخيص. إذا بدك **ترخيص تجاري** بشروط تانية، تواصل مع صاحب المشروع عبر GitHub ([@3mro777o](https://github.com/3mro777o)). وأي مساهمة بتحتاج موافقة على [CLA](CLA.md)، وتفاصيلها بـ [CONTRIBUTING.md](CONTRIBUTING.md).

## تنويه

هاد المشروع **مش تابع لواتساب ولا لـ Meta، ولا معتمد منهن**. بيستعمل مكتبة غير رسمية ([Baileys](https://github.com/WhiskeySockets/Baileys)) لحتى يرتبط كجهاز، وهاد ممكن يوقف يشتغل بأي وقت، وممكن يخالف شروط واتساب. استعمله بس مع حسابك وبياناتك، وعلى مسؤوليتك. وخلّي الأرشيف بمكان آمن، لأن فيه رسايلك الخاصة وجلسة الربط ومفتاح النسخة الاحتياطية.
