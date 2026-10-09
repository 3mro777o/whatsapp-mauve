[English](README.md) | **العربية**

# whatsapp-mauve: دليل المطوّر

كل الكود هنا. المشروع يشتغل داخل صورة Docker وحدة (`whatsapp-mauve:local`) والبيانات بـ`profiles/` بالجذر (ما بتدخل الصورة).

## خريطة الكود

| الملف | الوظيفة |
|---|---|
| `tools/wa-archive` | سيرفر العارض والـAPI على `127.0.0.1:8777`، وقفل PIN. يخدم `viewer.html` مباشرة |
| `tools/viewer.html` | واجهة واحدة (شكل واتساب ويب). تعديلها ما بيحتاج بناء، بس الحاوية لازم تتحدّث |
| `tools/viewer_data.py` | قراءة `archive.db` للعارض (المحادثات، الرسائل، الوسائط) |
| `tools/viewer_lock.py` | قفل PIN وتوكن محلي للأدوات الخلفية (`state/api-token`) |
| `tools/wa-sync/sync.mjs` | المزامنة الحيّة: جهاز مرتبط عبر Baileys، والإرسال، وإشارات القراءة |
| `tools/wa-sync/patch-baileys.mjs` | تعديل صغير على Baileys بعد `npm ci` |
| `tools/archive_writer.py` + `archive_db.py` + `schema.sql` | الكاتب الوحيد للقاعدة وقواعد الدمج (ما في حذف، النسخ القديمة محفوظة) |
| `tools/import_msgstore.py` | `wa import`: فكّ تشفير `msgstore*.crypt15` ودمجه، وربط `Media/` |
| `tools/sender.py` | الإرسال والملصقات والـGIF (GIPHY وKLIPY) والرفع، من خلف `wa-archive` |
| `tools/avatars_lib.py`، `import_avatars.py`، `avatar-fetcher/fetch.mjs` | صور المحادثات (الاستيراد بالجملة، والجلب من واتساب) |
| `tools/enrich.py`، `build_viewer.py`، `build_index.py`، `dedupe_library.py`، `build_emoji.py` | إثراء (صور معاينة، موجات الصوت) وبناء ثابت قديم وأدوات صيانة |
| `tools/waconf.py` | يقرأ `wa.conf` ويحسب كل المسارات من `WA_HOME` |
| `tools/web-static/`، `tools/fonts/`، `tools/login.html` | ملفات ثابتة للواجهة |
| `tools/make_fake_phone.py`، `test_import.py`، `test_purge.py` | الاختبارات (انظر أدناه) |
| `docker/entrypoint.sh` | نقطة دخول الحاوية: `sync \| viewer \| link \| import \| pin \| clearpin \| shell` |
| `docker/wa-notify@.service`، `.timer` | تنبيه سطح المكتب عند انفكاك الربط (يركّبها `wa notify-on`) |
| `Dockerfile`، `.dockerignore` | الصورة. الـcontext هو `src/`، والـallow-list بتسمح بس بالكود |
| `Makefile` | أهداف للتطوير: `make test` وغيرها |
| `docs/ARCHITECTURE.ar.md` | البنية والقرارات (بالعربي) |

ملاحظة: داخل الصورة الكود بـ`/app/tools` (`WORKDIR /app`)، والمسار الثابت هو `WA_HOME=/data`.

## إعادة البناء

من جذر المشروع:

    docker compose build && docker compose up -d

أو `../wa build` ثم `docker compose up -d`. الـ`compose.yml` لكل حساب بيبني من `src/` (`context: ../../src`)، فأي تعديل داخل `src/` يحتاج بناء. تعديل `profiles/` ما يحتاج بناء.

## الاختبارات

من `src/`:

    make test                      # test_purge.py + test_import.py (بملفات مؤقتة، ما بتلمس البيانات)
    python3 tools/sender.py        # self-check للإرسال والـGIF
    python3 tools/viewer_lock.py   # self-check للقفل (ما بيلمس ملفات)

الاختبارات بتشتغل على الجهاز (مش داخل الحاوية) وبتستعمل مجلدات مؤقتة.

## الإعدادات

كل المفاتيح موثّقة في `../wa.conf.example` (النسخة الحقيقية لكل حساب: `profiles/<اسم>/wa.conf`). الأسماء والقيم الافتراضية بالكود في `tools/waconf.py` (`DEFAULTS`). القيم من البيئة (`WA_*`) بتغلب على الملف.

## ملاحظات

- المسارات داخل الكود نسبية لمكانه: إذا نقلت ملف، عدّل عدد مستويات `dirname` في أوله (مثلاً `waconf.py`، `sync.mjs`، `wa-search`).
- `wa` بالجذر (مش هنا) وبيبني من `src/`. `install.sh` و`compose.yml` بالجذر.
- التثبيت القديم بدون docker في `../legacy/`، وهو غير مصان.
