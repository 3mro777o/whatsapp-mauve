"""Load wa.conf (KEY=VALUE) and expose absolute paths. Environment variables override the file.

Every path in the project is derived from WA_HOME (default: the repo root = two levels above this file, i.e. parent of src/),
so the whole folder can be moved to another machine without edits.
"""
import os

TOOLS = os.path.dirname(os.path.realpath(__file__))
_DEFAULT_HOME = os.path.dirname(os.path.dirname(TOOLS))  # src/tools -> repo root

DEFAULTS = {
    "WA_PORT": "8777", "WA_BIND": "127.0.0.1",
    "WA_DB": "archive/archive.db", "WA_STORE": "archive/store", "WA_WEB": "archive/web",
    "WA_STATE": "state", "WA_AUTH": "state/auth", "WA_PHONE_MEDIA": "Media", "WA_BACKUPS": "Databases",
    "WA_KEY_FILE": ".wa_key", "WA_MEDIA_MAX_MB": "200", "WA_DOWNLOAD_STATUS": "1", "WA_DEFAULT_CC": "90", "WA_PYTHON": "python3",
    "WA_SYSTEM_THEME": "~/.local/state/omarchy/current/theme/colors.toml", "WA_SESSION_DAYS": "30",
    "WA_SYNC_PORT": "8778", "WA_UPLOAD_MAX_MB": "100", "WA_GIPHY_KEY": "", "WA_KLIPY_KEY": "",
    # docker instance layout (see docker/): WA_MEDIA_DIR = name of the WhatsApp Media folder inside WA_STORE (also the
    # prefix of media paths in archive.db and of the URL), WA_USER = user data dir (default WA_WEB/user),
    # WA_PHONE_DIR = the phone's WhatsApp folder (then WA_PHONE_MEDIA = <it>/Media, WA_BACKUPS = <it>/Databases)
    "WA_MEDIA_DIR": "Media", "WA_USER": "", "WA_PHONE_DIR": "",
}


def _parse(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split(" #", 1)[0].strip().strip('"').strip("'")
                out[k.strip()] = v
    except FileNotFoundError:
        pass
    return out


HOME = os.path.abspath(os.environ.get("WA_HOME") or _DEFAULT_HOME)
_SET = {**_parse(os.path.join(HOME, "wa.conf")), **{k: v for k, v in os.environ.items() if k.startswith("WA_")}}
CONF = {**DEFAULTS, **_SET}
for _k, _sub in (("WA_PHONE_MEDIA", "Media"), ("WA_BACKUPS", "Databases")):
    if CONF["WA_PHONE_DIR"] and _k not in _SET:
        CONF[_k] = os.path.join(os.path.expanduser(CONF["WA_PHONE_DIR"]), _sub)


def path(key):
    p = os.path.expanduser(CONF[key])
    return p if os.path.isabs(p) else os.path.join(HOME, p)


def get(key, cast=str):
    return cast(CONF[key])


DB = path("WA_DB")
STORE = path("WA_STORE")
WEB = path("WA_WEB")
STATE = path("WA_STATE")
AUTH = path("WA_AUTH")
PHONE_MEDIA = path("WA_PHONE_MEDIA")
BACKUPS = path("WA_BACKUPS")
KEY_FILE = path("WA_KEY_FILE")
THUMBS = os.path.join(WEB, "thumbs")
MEDIA_DIR = CONF["WA_MEDIA_DIR"].strip("/") or "Media"
USER = path("WA_USER") if CONF["WA_USER"] else os.path.join(WEB, "user")
PORT = get("WA_PORT", int)
BIND = CONF["WA_BIND"]
