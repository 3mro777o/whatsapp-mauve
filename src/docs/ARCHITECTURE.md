# WhatsApp Archive — البنية

> **ملاحظة المسارات:** المسارات `tools/…` و`docker/…` و`docs/…` هنا نسبةً لمجلد `src/`. `wa`، `install.sh`، `compose.yml`، و`profiles/` بالجذر. الأوامر التالية تشتغل من `src/`: `make test`، أو `python3 tools/test_import.py`.

أرشيف تراكمي لواتساب: كل نسخة احتياطية من التلفون وكل رسالة بتوصل لايف بتنضاف لقاعدة بيانات وحدة، وما بينمسح منها شي أبداً.

```
 التلفون ──(نسخة crypt15)──► Databases/ ──► wa-import (import_msgstore.py) ──┐
 التلفون ──(مجلد Media)────► Media/ ──hardlink──► archive/store/Media/          │
 واتساب ──(جهاز مرتبط)────► wa-sync (Node/Baileys) ──JSON lines──► archive_writer.py ─┤
                                                                               ▼
                                                    archive/archive.db  (SQLite WAL)
                                                                               │
                              wa-archive (سيرفر محلي، عند الطلب) ◄──────────────┘
                              يولّد /data/*.json من القاعدة + API + الواجهة tools/viewer.html
```

## المكوّنات

| المكوّن | الملف | الوظيفة |
|---|---|---|
| الإعدادات | `wa.conf` (نسخة: `wa.conf.example`) | كل المسارات والحدود. المسارات النسبية نسبةً لمجلد المشروع (`WA_HOME`) |
| Docker | `src/Dockerfile`, `src/docker/`, `compose.yml` (مولَّد)، `wa`, `install.sh` | صورة وحدة `whatsapp-mauve:local` + أمر `wa` لعدة حسابات (`profiles/<name>/`) (شوف "Docker وتعدد الحسابات") |
| القاعدة | `archive/archive.db`, `tools/schema.sql` | المصدر الوحيد للحقيقة |
| منطق الدمج | `tools/archive_db.py` | قواعد الكتابة (ما في حذف، حفظ النسخ القديمة، الرسائل المحذوفة بتضل بمحتواها) |
| مستورد النسخ | `tools/import_msgstore.py` (`wa-import`) | فكّ تشفير `msgstore*.crypt15` بالمفتاح ودمجها، وربط ملفات `Media/` بالمخزن |
| المزامنة الحيّة | `tools/wa-sync/sync.mjs` (`wa-sync`) | جهاز مرتبط "مخفي": ما بيبعت رسائل ولا إشعارات قراءة، وحالته `unavailable` |
| الكاتب | `tools/archive_writer.py` | بيستقبل أحداث wa-sync ويكتبها بالقاعدة بنفس منطق الدمج، وبيمرّر طلبات التحميل من العارض |
| الإثراء | `tools/enrich.py` | صور معاينة للفيديو وPDF، وموجات الرسائل الصوتية (`archive/web/thumbs/`) |
| العارض | `tools/wa-archive`, `tools/viewer_data.py`, `tools/viewer.html` | سيرفر على 127.0.0.1:8777، ما بيشتغل مع الإقلاع |
| التنبيهات | `tools/wa-sync-check` | إشعار سطح المكتب إذا انفك الربط أو وقفت الخدمة، عند كل تسجيل دخول |

## قاعدة البيانات (`archive.db`)

- `chats`: المعرّف الثابت = الجزء الأول من الـjid (رقم الهاتف، أو `120363…` للمجموعات والقنوات، أو `status`). الـ`@lid` بيتحوّل لرقم الهاتف لما يكون معروف (`aliases`)، فالشخص الواحد إله محادثة وحدة. إذا تغيّر معرّف محادثة (lid صار رقم) بينحفظ بـ`chat_renames`، والعارض بيحوّل المعرّف القديم تلقائياً.
- `messages`: مفتاحها `(chat_id, key_id, from_me)`. فيها الحالة الحالية، وحقول العرض الإضافية بـ`data` (JSON).
- `message_versions`: كل نص أو تعليق قديم مع وقته. `revocations`: وقت الحذف ومين حذف. `reactions`, `group_members` (الأعضاء الحاليين والقدام)، `contacts` (الأسماء من دفتر العناوين، أو الاسم اللي حطّه الشخص لحاله).
- `media`: حالة تحميل كل ملف من المزامنة الحيّة، مع مفاتيح التحميل (mediaKey/directPath) لحتى يقدر ينحمّل لاحقاً.
- `jobs`: طلبات التحميل من العارض. `changes`: سجل التغييرات اللي بيعتمد عليه التحديث الحي بالواجهة.

## قواعد الدمج

1. ما في شي بينمسح. كل نسخة احتياطية جديدة بتضيف وبتحدّث بس.
2. رسالة صارت "محذوفة" (نوع 15 أو REVOKE) بتضل بمحتواها الأخير المعروف، وبينسجّل وقت الحذف ومين حذفها. الواجهة بتاخد `"del":{"t","by"}`.
3. إذا تغيّر النص بينحفظ القديم بـ`message_versions` (`"ed":[[ts,text],…]`). نسخة احتياطية أقدم ما بتكتب فوق نص أحدث، نصها بينحفظ كتاريخ بس.
4. نسخة احتياطية أقدم فيها محتوى رسالة كانت معروفة إنها محذوفة بس → المحتوى بيرجع.
5. الوسائط: أي ملف من `Media/` بينربط (hardlink) بـ`archive/store/Media/`، فحذفه من التلفون أو من مجلد المزامنة ما بيضيّعه. التحميلات الحيّة بتروح لـ`archive/store/Media/live/YYYY-MM/`.

## المزامنة الحيّة (wa-sync)

- خدمة systemd للمستخدم (`wa-sync.service`) بتشتغل مع الإقلاع حتى بدون تسجيل دخول (linger)، و`Restart=always`، وبأولوية منخفضة (Nice/CPUWeight/IO idle) وحد للذاكرة.
- بتستخدم جلسة الجهاز المرتبط بـ`state/auth`. ما بتمسحها أبداً حتى لو انفك الربط.
- التحميل التلقائي للوسائط لحد `WA_MEDIA_MAX_MB` (الافتراضي 200MB). الأكبر منه بيروح لقائمة انتظار (`/api/media/pending`) مع إشعار، وبينحمّل لما تطلبه من الواجهة طالما لسا موجود على سيرفرات واتساب. وسائط "تاريخ المحادثات" بتنحفظ مفاتيحها بس (`remote`) وبتنحمّل عند الطلب.
- بتكتب حالتها بـ`state/status.json`: `connecting`, `connected`, `disconnected`, `logged_out`, `stopped`, `failed`. وفي نبضة كل 30 ثانية.
- `wa-sync-check.service` بيشتغل مع كل تسجيل دخول للواجهة الرسومية، وبيبعت إشعار إذا في مشكلة. الخدمة نفسها كمان بتبعت إشعار فوري إذا في session bus.

أوامر: `wa-sync status | logs | restart | stop | pair` (`pair` بيوقف الخدمة وبيعرض QR وبعدين بيرجّعها).

### التعديلات والرسائل للعرض مرة وحدة
- **التعديلات** بتوصل هلق كـ`secretEncryptedMessage` (AES-GCM بدون AAD). المفتاح هو
  `HKDF-SHA256(messageSecret الرسالة الأصلية, salt=بلا, info = id الأصلية + jid كاتبها + jid المعدِّل + "Message Edit")`.
  عشان هيك `wa-sync` بيحفظ الـ`messageSecret` لكل رسالة بتوصل بـ`state/msg-secrets.jsonl` (JSONL بس بينضاف عليه، صلاحيات 600):
  `{"k": key_id, "s": base64 secret, "a": jid الكاتب}`.
- الرسائل الأقدم من المزامنة بتاخد أسرارها من النسخ الاحتياطية: `import_msgstore.py` مع كل استيراد بيضيف الناقص من جدول
  `message_secret` (كاتب رسائلك هو `WA_ME` بـ`wa.conf`، أو `me` من `status.json`، وإذا ما انعرف بيتجاهلها)،
  وبعدين بيبعت `SIGHUP` لـ`wa-sync` لحتى يعيد قراءة الملف بدون ما يعيد تشغيل (`kill -HUP <pid>` بيعمل نفس الشي يدوياً).
- **الرسائل للعرض مرة وحدة:** Baileys بيرمي الـstanza `unavailable` من نوع `view_once` اللي بتوصل للأجهزة المرتبطة،
  فـ`wa-sync` بيلتقطها من الـstanza الخام وبيسجّل مكانها رسالة `sys2` ("📷 رسالة للعرض مرة واحدة"). المحتوى نفسه ما بيوصل لجهاز مرتبط أصلاً.

## الوصول عن بعد (HTTPS عبر Tailscale)
- `tailscale serve --bg 8777` بيعرض العارض على `https://<جهازك>.<tailnet>.ts.net`، بس للأجهزة اللي جوّا الـtailnet.
- ⚠️ الطلبات اللي بتجي عبر `tailscale serve` بتوصل للسيرفر من `127.0.0.1`، فبتتعامل كأنها محلية، و`WA_PASSWORD` ما بينطبق عليها.
  الحماية هون هي صلاحيات الـtailnet بس، فلا تعمل `tailscale funnel` (عام عالإنترنت).
- للإيقاف: `tailscale serve --https=443 off` (أو `tailscale serve reset`).

## واجهة العارض (API)

- `/data/chats.json`, `/data/c/<id>.json`, `/data/search.json`, `/data/media.json`, `/data/people.json`: بتتولّد من القاعدة مع cache بالذاكرة، و`ETag` و`Cache-Control: no-cache`.
- `GET /api/changes?since=<seq>` بيرجّع `{"seq","chats":[…]}`. لو رجعت `"*"` يعني تغيّرت الأسماء أو المعرّفات، فلازم تنعاد قائمة المحادثات كلها.
- `GET /api/sync/status`، `GET /api/media/pending`، `POST /api/media/fetch/<chatId>/<keyId>`، `POST /api/media/skip/<chatId>/<keyId>`، `GET /api/members/<chatId>`.
- بيانات المستخدم (`archive/web/user/`): العلامات، والألقاب والمجموعات، والصور. ما بتنمسح أبداً.

## Docker وتعدد الحسابات

جذر المشروع = **whatsapp-mauve**. صورة وحدة (`whatsapp-mauve:local`، من `src/Dockerfile`) + أمر `wa` (bash بالجذر، بيتركّب بـ`~/.local/bin` عبر `install.sh`، وبيحلّ مكان المشروع بـ`readlink -f` فبيشتغل عبر الرابط). كل حساب = مجلد (profile) تحت `profiles/` فيه كل شي. مجلد `profiles/` هو السجل: الحساب = مجلد فيه `wa.conf` (ما في `~/.config`). `profiles/` مستثنى من git، والصورة بتتبنى من `src/` بس (`src/.dockerignore`) فما بتدخل البيانات.

```
profiles/<name>/wa.conf      إعدادات الحساب كلها: المنفذ، WA_ACCESS، WA_PASSWORD، WA_TS_HTTPS، WA_PHONE_DIR، WA_VIEWER_AUTOSTART
                 compose.yml مولَّد من wa.conf بأمر wa (لا تعدّله)
                 archive.db  قاعدة البيانات (SQLite WAL)
                 media/      وسائط بأسماء واتساب (WhatsApp Images/, WhatsApp Voice Notes/<yyyyww>/, .Statuses/ …)
                 state/      جلسة واتساب (auth/)، status.json، api-token، رمز القفل، contacts.vcf
                 user/       بيانات المستخدم (صور المحادثات، ملصقات/GIF المحفوظة، ألقاب، علامات)
                 cache/      يُعاد توليده (web/data, thumbs): ما بيدخل بالنسخ الاحتياطي
                 .wa_key     مفتاح الـ64 رقم (600)
```

- **التشغيل:** `compose.yml` بالجذر (`name: whatsapp-mauve`) فيه `include:` لكل `profiles/<name>/compose.yml` (بالصيغة الطويلة مع `required: false` فنسخة جديدة بدون حسابات ما بتنكسر). `docker compose up -d` بالجذر بيشغّل كل الحسابات، و`docker compose up -d <name>-sync` أو `<name>-viewer` بيشغّل خدمة وحدة. كل `profiles/<name>/compose.yml` بيولّده `wa` من `wa.conf`: خدمتين `<name>-sync` (`restart: unless-stopped`، بتشتغل مع docker عند الإقلاع) و`<name>-viewer` (restart حسب `WA_VIEWER_AUTOSTART`)، الصورة `whatsapp-mauve:local` مع `build:` (السياق = جذر المشروع) فـ`docker compose up` بيبنيها إذا ناقصة. `network_mode: host` (نفس سلوك loopback/tailscale serve)، المنافذ من `wa.conf`، و`user: uid:gid` للمستخدم الحالي مثبّت وقت التوليد. المجلد نفسه بيتركّب على `/data` (`WA_HOME`) بمسار نسبي (`.:/data`)، ومجلد التلفون على `/phone` للقراءة فقط إذا `WA_PHONE_DIR` محدد. حدود: `cap_drop: ALL`، `no-new-privileges`، `init`، ذاكرة 1GB وأولوية CPU واطية.
- **`wa regen`:** بيعيد توليد كل compose بالحسابات + قائمة `include` بالجذر. لازم بعد الاستعادة على جهاز تاني (uid مختلف) أو بعد تعديل `wa.conf` يدوياً؛ `wa up` و`wa import` و`wa link` و`wa pin` بيعملوه تلقائياً.
- **المسارات:** `waconf.py` بيحلّ كل شي نسبةً لـ`WA_HOME`. مفاتيح: `WA_MEDIA_DIR` (اسم مجلد الوسائط ببادئة المسارات بالقاعدة وبالرابط؛ الافتراضي `Media` للتثبيت القديم، `media` بالـprofile)، `WA_USER`، `WA_PHONE_DIR` (بيحدد `WA_PHONE_MEDIA=<>/Media` و`WA_BACKUPS=<>/Databases`). الـprofile بيحط `WA_STORE=.`، و`wa-archive` بيعمل روابط `cache/web/user` و`cache/web/media`. التثبيت القديم (`archive/store/Media`, `archive/web/user`) ما تغيّر.
- **الاستيراد (`wa import`):** (1) على الجهاز نفسه: `import_msgstore.py --mirror-only` بيربط (hardlink، أو نسخ إذا قرص تاني) مجلد `Media/` بالتلفون إلى `media/`، ويتجاهل: `.Links .Wamo .wamocache .udDH* "WhatsApp Bug Report Attachments" .nomedia`، وبيحتفظ بـ`.Statuses` و`WhatsApp AI Media` والملصقات. السبب: الحاويات ما بتقدر تعمل hardlink عبر نقاط تركيب مختلفة. (2) بالحاوية (`docker compose run <name>-sync import`): فك تشفير أحدث `Databases/msgstore*.crypt15` بالمفتاح ودمجها بالقاعدة. بدون مفتاح أو بمفتاح غلط (مش 64 خانة hex) أو ملف تالف: رسالة واضحة وبدون تعطيل شي. مسارات الوسائط بالقاعدة بتاخد بادئة `WA_MEDIA_DIR`.
- **التحميلات اللايف:** `sync.mjs` بينزّلها بمجلد واتساب المطابق: `WhatsApp Images/` (+`Sent/` للمرسَل)، `WhatsApp Video/`، `WhatsApp Animated Gifs/`، `WhatsApp Voice Notes/<yyyyww>/`، `WhatsApp Audio/`، `WhatsApp Documents/`، `WhatsApp Stickers/`، `.Statuses/`، بأسماء `IMG-yyyymmdd-WA<معرّف>.ext`. المسارات القديمة `media/live/...` بتنقرا عادي (القاعدة بتخزّن المسار نسبياً).
- **ترحيل التثبيت القديم:** `wa migrate-legacy <legacy_dir> <name> [--dry-run]`. بيرفض إذا `wa-sync` (systemd) شغّال أو المنفذين (من `wa.conf` القديم) مشغولين، أو إذا المجلد القديم على قرص غير قرص المشروع (لأنه بيستعمل `mv`). بينقل: `archive/archive.db(+wal/shm)` → `archive.db`، `archive/store/Media/*` → `media/`، `archive/web/user/*` → `user/`، `state/*` (عدا `handoff*`، اللوجات، النسخ القديمة) → `state/`، `.wa_key`، `contacts.vcf` → `state/`، وبيبني `wa.conf` جديد مع الإعدادات المحفوظة (`WA_ACCESS WA_PORT WA_SYNC_PORT WA_PASSWORD WA_GIPHY_KEY WA_MEDIA_MAX_MB …`) ومسارات نسبية. بالقاعدة بيستبدل `Media/` بـ`media/` بكل مكان بينخزّن فيه مسار: `media.path`، و`messages.data` (`m`، وبيعاد حساب `p` = `thumbs/sha1(m).jpg` لأن اسم الصورة المصغّرة هاش المسار فبتنقل بالاسم الجديد)، و`jobs.result`. صلاحيات: `state` 700 والأسرار 600. `--dry-run` بيطبع كل خطوة وعدد الصفوف بدون تغيير.
- **الإشعار على الجهاز (اختياري):** `wa notify-on <name>` بيركّب `wa-notify@<name>.timer` (كل 10 دقايق، `wa notify` بيقرأ `state/status.json` وبيرسل إشعار مرة وحدة لكل حالة).
- **Tailscale:** `WA_TS_HTTPS=<port>` بـ`wa.conf` → `wa up` بيشغّل `tailscale serve --bg --https=<port> http://127.0.0.1:<WA_PORT>` و`wa down` بيلغيه.
- **النسخ الاحتياطي:** `wa backup <name> <dest>` بينسخ المجلد (rsync -aH أو tar) بدون `cache/` و`compose.yml`، والقاعدة عبر `sqlite3 backup` (متّسقة والخدمة شغّالة). الاستعادة: `wa restore <dest>/<name> [--move]` (بينسخ/ينقل لـ`profiles/<name>` ثم `wa regen`).
- **الاختبارات:** `python3 tools/test_import.py` (استيراد مجلد تلفون وهمي + msgstore صناعي بملفات مؤقتة، وفحص أخطاء المفتاح)، و`tools/test_purge.py`.

### ربط `media/` مع Immich
مكتبة خارجية للقراءة فقط: mount لـ`profiles/<name>/media` على `/usr/src/app/external/whatsapp-<name>:ro` بحاوية `immich-server`، وبعدها *Administration ← External Libraries* مع Import path نفسه (Exclusion: `**/.Statuses/**` إذا ما بدك الحالات). الأسماء مطابقة لواتساب فبتقدر كمان تنسخ المجلد للتلفون كما هو.

## النقل لجهاز أو سيرفر تاني (التثبيت القديم بدون docker)

```bash
rsync -aH --info=progress2 WhatsApp/ newhost:~/WhatsApp/   # -H بيحافظ على الروابط بين Media/ والمخزن
ssh newhost 'cd ~/WhatsApp && legacy/setup.sh'                  # venv + npm + الأوامر + خدمات systemd + linger
```

- ما في أي مسار ثابت بالكود. كلشي محسوب من مجلد المشروع، أو من `wa.conf`، أو من `WA_HOME` إذا كان معرّف بالبيئة.
- `legacy/setup.sh` بيكتب ملفات الخدمات من القوالب بـ`legacy/systemd/*.in` بالمسارات الصح للجهاز الجديد (node, python, المجلد).
- الجلسة (`state/auth`) بتنتقل مع المجلد، وما لازم تشغّل نسختين بنفس الوقت بنفس الجلسة. إذا بدك تربط جهاز جديد بدل النقل: `wa-sync pair`.
- على سيرفر بدون واجهة رسومية: الخدمة بتشتغل عادي، وما في إشعارات سطح مكتب. شوف `wa-sync status` أو `/api/sync/status`.
- لتغيير المنفذ أو الواجهة: `WA_PORT`/`WA_BIND` بـ`wa.conf`. السيرفر بيقبل طلبات الكتابة من نفس الأصل بس.

## ملاحظات

- `tools/build_viewer.py` صار قديم (بناء ثابت)، والسيرفر بيستخدم القاعدة مباشرة. بس دوال الإثراء فيه لسا مستخدمة.
- `wa-fetch-avatars` ما بيشتغل وwa-sync شغّال لأنهم بيستخدموا نفس الجلسة. wa-sync نفسه بيجيب الصور الناقصة مرة باليوم.
- الحالات الشخصية (status) ما بتنحفظ بالنسخ الاحتياطية، فبتتسجّل بس من المزامنة الحيّة.

## الوصول للعارض (WA_ACCESS)
| القيمة | مين بيقدر يفتح العارض |
|---|---|
| `local` | هالجهاز بس (الربط على 127.0.0.1) |
| `tailscale` | هالجهاز + عناوين Tailscale (`100.64.0.0/10`، `fd7a:115c:a1e0::/48`). باقي العناوين بترجع 403 |
| `lan` | كمان الشبكات الخاصة، وهون `WA_PASSWORD` إجباري |

- `WA_PASSWORD` بيفعّل Basic Auth باسم المستخدم `wa` لأي جهاز غير هالجهاز.
- الـ API بيقبل الكتابة بس من نفس المصدر، يعني `Origin` لازم يطابق `Host`.
- `WA_VIEWER_AUTOSTART=1` + `legacy/setup.sh` بيفعّلوا `wa-archive.service` مع الإقلاع.
- مع ufw: `sudo ufw allow in on tailscale0 to any port 8777 proto tcp`.

## قفل العارض بالرمز

- `tools/wa-archive --set-pin` بيطلب الرمز مرتين (4 أحرف على الأقل)، و`--clear-pin` بيلغيه. الرمز بيتخزّن بـ scrypt مع salt بـ`state/viewer-pin` (صلاحيات 600). بدون الملف العارض بيشتغل زي قبل.
- مع الرمز، كل المسارات (الصفحة، `/data`، `/api`، الوسائط، PWA) بتحتاج كوكي `wa_s` (HttpOnly، SameSite=Strict، مدتها `WA_SESSION_DAYS`). بدون كوكي: `/` بتروح لـ`/login`، والباقي بيرجع 401. `/login` و`POST /api/login` و`POST /api/logout` مفتوحين.
- الجلسات مخزّنة كـ hash بـ`state/viewer-sessions.json`، فإعادة تشغيل السيرفر ما بتطلّع أحد. تغيير الرمز أو إلغاؤه بيطلّع كل الجلسات.
- بعد 5 محاولات غلط متتالية: قفل 30 ثانية، يتضاعف لحد ساعة. العدّ لكل جهاز (من `X-Forwarded-For` عند `tailscale serve`) وكمان للكل مع بعض.
- الأدوات الخلفية (`tools/avatar-fetcher/fetch.mjs`) بتقرأ `state/api-token` وبتبعته بالهيدر `X-WA-Token`، فما بتحتاج كوكي. wa-sync والمستوردات بتكتب بالقاعدة مباشرة، ما بتستخدم HTTP.
- الـService worker (`wa-shell-v4`) ما بيخزّن صفحة الدخول ولا أي رد 401/302.
- كل رد فيه `X-Frame-Options: DENY` و`frame-ancestors 'none'` و`nosniff` و`Referrer-Policy: no-referrer`، فالعارض ما ينفتح داخل iframe. صفحة `/login` عندها CSP صارم (`default-src 'none'`، inline script/style، `connect-src 'self'`).
- الوسائط والمصغّرات `private, max-age=86400`، و`/data` بـ`private, no-cache` (الكاش بالمتصفح بس، مش عند أي وسيط).
- `GET /api/lock` بيرجّع `{"pin": bool}`. زر "🔒 قفل الآن" بقائمة ⋮ يظهر لو فيه رمز، ويعمل `POST /api/logout` ويرجّع لـ`/login`. أي 401 "login required" من الواجهة بيرجّع لـ`/login` تلقائياً.
- معاينة الـPDF بتتحمّل كـblob (مش iframe على رابط السيرفر)، لأن `DENY` بيمنع الـiframe على نفس الأصل.
