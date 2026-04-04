from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import threading
import time
import uuid as uuid_lib
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
import telebot
from telebot.types import InlineKeyboardButton as B
from telebot.types import InlineKeyboardMarkup as K

from tg_bot import CBT

if TYPE_CHECKING:
    from cardinal import Cardinal


NAME = "FunPay User-Agent Rotator"
VERSION = "0.1.0"
DESCRIPTION = "Кнопочный ротатор User-Agent для FunPayCardinal с генерацией, проверкой и массовым импортом."
CREDITS = "@takouq, @llzzvvww"
UUID = "f25a286a-51f6-4d2c-b0f3-7b85966d5f55"
SETTINGS_PAGE = True

BIND_TO_DELETE = []

logger = logging.getLogger("FPC.funpay_useragent_rotator")

S_DIR = Path(f"storage/plugins/{UUID}")
SETTINGS_FILE = S_DIR / "settings.json"
STATE_FILE = S_DIR / "state.json"

CATALOG_URL_PLATFORMS = "https://whatmyuseragent.com/platforms"
CATALOG_URL_BROWSERS = "https://whatmyuseragent.com/browser"
VERIFY_URL = "https://whatmyuseragent.com/api"

DEFAULT_SELECT_PLATFORMS = 100
DEFAULT_SELECT_BROWSERS = 300
CATALOG_CACHE_TTL_SEC = 6 * 60 * 60
VERIFY_MIN_GAP_SEC = 3
SELECTOR_PAGE_SIZE = 8
RECENT_MAX = 60

CBT_OPEN = f"{CBT.PLUGIN_SETTINGS}:{UUID}"


def _cb(action: str) -> str:
    return f"uarot:{action}"


CBT_BACK = _cb("back")
CBT_ROTATE = _cb("rotate")
CBT_TOGGLE_AUTO = _cb("auto")
CBT_TOGGLE_NOTIFY = _cb("notify")
CBT_TOGGLE_VERIFY = _cb("verify")
CBT_VERIFY_NOW = _cb("verify_now")
CBT_CYCLE_MODE = _cb("mode")
CBT_REFRESH = _cb("refresh")
CBT_SET_INTERVAL = _cb("set_interval")
CBT_IMPORT = _cb("import")
CBT_CLEAR_CUSTOM = _cb("clear_custom")
CBT_PLAT_MENU = _cb("plats")
CBT_BROWSER_MENU = _cb("browsers")

INPUT_INTERVAL = "interval"
INPUT_IMPORT = "import"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "notify_enabled": True,
    "verify_enabled": True,
    "rotation_interval_sec": 60,
    "verify_gap_sec": VERIFY_MIN_GAP_SEC,
    "source_mode": "generated",
    "notify_chat_id": 0,
    "defaults_seeded": False,
    "catalog_defaults_applied": False,
    "scope_funpay_api": False,
    "scope_cardinal_process": True,
    "scope_server_env": False,
    "selected_platforms": [],
    "selected_browsers": [],
    "custom_user_agents": [],
}

DEFAULT_STATE: dict[str, Any] = {
    "current_user_agent": "",
    "current_source": "",
    "current_platform_name": "",
    "current_browser_name": "",
    "last_rotate_ts": 0.0,
    "next_rotate_ts": 0.0,
    "last_error": "",
    "last_verify_ts": 0.0,
    "last_verify_browser": "",
    "last_verify_os": "",
    "last_verify_device": "",
    "last_verify_brand": "",
    "last_verify_model": "",
    "last_verify_raw": {},
    "recent_user_agents": [],
    "catalogs": {
        "platforms": [],
        "browsers": [],
        "fetched_ts": 0.0,
    },
}

FALLBACK_PLATFORMS = [
    "Windows",
    "Windows 11",
    "Windows 10",
    "Windows 8.1",
    "Windows 7",
    "Windows Phone",
    "macOS",
    "Mac OS X",
    "Android",
    "Android TV",
    "iOS",
    "iPadOS",
    "Linux",
    "Ubuntu",
    "Debian",
    "Fedora",
    "Arch Linux",
    "Kali Linux",
    "Chrome OS",
    "Raspberry Pi OS",
    "FreeBSD",
    "OpenBSD",
    "PlayStation",
    "Xbox",
    "Nintendo",
    "Tizen",
    "webOS",
    "HarmonyOS",
]

FALLBACK_BROWSERS = [
    "Chrome",
    "Chrome Mobile",
    "Firefox",
    "Firefox Mobile",
    "Safari",
    "Safari Mobile",
    "Edge",
    "Edge Mobile",
    "Opera",
    "Opera GX",
    "Opera Mini",
    "Brave",
    "Vivaldi",
    "Yandex Browser",
    "Samsung Internet",
    "DuckDuckGo",
    "UC Browser",
    "Chromium",
    "Tor Browser",
    "Waterfox",
    "Pale Moon",
    "SeaMonkey",
    "QQ Browser",
    "Coc Coc",
    "Maxthon",
    "Lynx",
]

IGNORED_LINK_TEXTS = {
    "WhatMyUserAgent.com",
    "Browser",
    "Platforms",
    "Brand",
    "Bots",
    "Application",
    "API",
}


class RT:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.pending_inputs: dict[tuple[int, int], dict[str, Any]] = {}
        self.loop_started = False
        self.tg_registered = False
        self.catalog_refresh_started = False
        self.requests_patched = False
        self.original_request = None


R = RT()


class _AnchorTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_anchor = False
        self._buf: list[str] = []
        self.items: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._in_anchor = True
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a":
            return
        self._in_anchor = False
        text = "".join(self._buf).strip()
        self._buf = []
        if text:
            self.items.append(text)


def _ensure_paths() -> None:
    S_DIR.mkdir(parents=True, exist_ok=True)


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _settings() -> dict[str, Any]:
    return R.settings


def _state() -> dict[str, Any]:
    return R.state


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y", "да"}:
        return True
    if text in {"0", "false", "no", "off", "n", "нет"}:
        return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _human_seconds(sec: int) -> str:
    sec = max(0, int(sec))
    if sec >= 3600 and sec % 3600 == 0:
        return f"{sec // 3600}ч"
    if sec >= 60 and sec % 60 == 0:
        return f"{sec // 60}м"
    return f"{sec}с"


def _short_ua(value: str, limit: int = 120) -> str:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _storage_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return int(chat_id), int(user_id)


def _set_pending(chat_id: int, user_id: int, mode: str, **extra: Any) -> None:
    with R.lock:
        R.pending_inputs[_storage_key(chat_id, user_id)] = {"mode": mode, **extra}


def _get_pending(chat_id: int, user_id: int) -> dict[str, Any] | None:
    with R.lock:
        return R.pending_inputs.get(_storage_key(chat_id, user_id))


def _pop_pending(chat_id: int, user_id: int) -> dict[str, Any] | None:
    with R.lock:
        return R.pending_inputs.pop(_storage_key(chat_id, user_id), None)


def _clear_pending(chat_id: int, user_id: int) -> None:
    with R.lock:
        R.pending_inputs.pop(_storage_key(chat_id, user_id), None)


def _load_settings(force: bool = False) -> None:
    with R.lock:
        if R.settings and not force:
            return
        _ensure_paths()
        data = _json_copy(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update(raw)
            except Exception:
                logger.exception("useragent_rotator: failed to load settings")
        if not isinstance(data.get("selected_platforms"), list):
            data["selected_platforms"] = []
        if not isinstance(data.get("selected_browsers"), list):
            data["selected_browsers"] = []
        if not isinstance(data.get("custom_user_agents"), list):
            data["custom_user_agents"] = []
        R.settings = data
        _save_settings()


def _save_settings() -> None:
    with R.lock:
        _ensure_paths()
        SETTINGS_FILE.write_text(json.dumps(_settings(), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(force: bool = False) -> None:
    with R.lock:
        if R.state and not force:
            return
        _ensure_paths()
        data = _json_copy(DEFAULT_STATE)
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update(raw)
            except Exception:
                logger.exception("useragent_rotator: failed to load state")
        catalogs = data.get("catalogs", {})
        if not isinstance(catalogs, dict):
            catalogs = {}
        catalogs.setdefault("platforms", [])
        catalogs.setdefault("browsers", [])
        catalogs.setdefault("fetched_ts", 0.0)
        if not isinstance(catalogs.get("platforms"), list):
            catalogs["platforms"] = []
        if not isinstance(catalogs.get("browsers"), list):
            catalogs["browsers"] = []
        recent = data.get("recent_user_agents", [])
        data["recent_user_agents"] = recent if isinstance(recent, list) else []
        data["catalogs"] = catalogs
        R.state = data
        _save_state()


def _save_state() -> None:
    with R.lock:
        _ensure_paths()
        STATE_FILE.write_text(json.dumps(_state(), ensure_ascii=False, indent=2), encoding="utf-8")


def _catalogs() -> dict[str, Any]:
    value = _state().setdefault("catalogs", {})
    if not isinstance(value, dict):
        value = {}
        _state()["catalogs"] = value
    value.setdefault("platforms", [])
    value.setdefault("browsers", [])
    value.setdefault("fetched_ts", 0.0)
    return value


def _normalize_scopes(*, save: bool = False) -> None:
    funpay = _as_bool(_settings().get("scope_funpay_api"), False)
    cardinal = _as_bool(_settings().get("scope_cardinal_process"), True)
    server = _as_bool(_settings().get("scope_server_env"), False)

    if server:
        funpay = False
        cardinal = False
    elif not funpay and not cardinal:
        cardinal = True

    _settings()["scope_funpay_api"] = funpay
    _settings()["scope_cardinal_process"] = cardinal
    _settings()["scope_server_env"] = server
    if save:
        _save_settings()


def _scope_funpay_enabled() -> bool:
    _normalize_scopes()
    return _as_bool(_settings().get("scope_funpay_api"), False)


def _scope_cardinal_enabled() -> bool:
    _normalize_scopes()
    return _as_bool(_settings().get("scope_cardinal_process"), True)


def _scope_server_enabled() -> bool:
    _normalize_scopes()
    return _as_bool(_settings().get("scope_server_env"), False)


def _toggle_scope(scope_name: str) -> None:
    _normalize_scopes()
    if scope_name == "server":
        enabled = not _scope_server_enabled()
        _settings()["scope_server_env"] = enabled
        if enabled:
            _settings()["scope_funpay_api"] = False
            _settings()["scope_cardinal_process"] = False
    elif scope_name == "funpay":
        _settings()["scope_funpay_api"] = not _scope_funpay_enabled()
        if _settings()["scope_funpay_api"]:
            _settings()["scope_server_env"] = False
    elif scope_name == "cardinal":
        _settings()["scope_cardinal_process"] = not _scope_cardinal_enabled()
        if _settings()["scope_cardinal_process"]:
            _settings()["scope_server_env"] = False
    _normalize_scopes(save=True)


def _scope_summary() -> str:
    parts: list[str] = []
    if _scope_server_enabled():
        parts.append("Ubuntu env/profile")
    else:
        if _scope_funpay_enabled():
            parts.append("FunPay API")
        if _scope_cardinal_enabled():
            parts.append("Cardinal process")
    return " + ".join(parts) if parts else "не выбран"


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        value = re.sub(r"\s+", " ", str(item or "").strip())
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _fetch_catalog(url: str, kind: str) -> list[str]:
    response = requests.get(url, timeout=25)
    response.raise_for_status()
    parser = _AnchorTextParser()
    parser.feed(response.text)
    items: list[str] = []
    for raw in parser.items:
        item = re.sub(r"\s+", " ", raw).strip()
        if not item or item in IGNORED_LINK_TEXTS:
            continue
        if item.startswith("©"):
            continue
        if len(item) > 80:
            continue
        if kind == "platforms" and item.lower() == "browser":
            continue
        items.append(item)
    return _unique_strings(items)


def _catalog_list(kind: str) -> list[str]:
    cached = _catalogs().get(kind, [])
    if isinstance(cached, list) and cached:
        return [str(x) for x in cached if str(x).strip()]
    return FALLBACK_PLATFORMS[:] if kind == "platforms" else FALLBACK_BROWSERS[:]


def _selected_platforms() -> list[str]:
    current = [str(x) for x in _settings().get("selected_platforms", []) if str(x).strip()]
    catalog = _catalog_list("platforms")
    return [x for x in current if x in catalog]


def _selected_browsers() -> list[str]:
    current = [str(x) for x in _settings().get("selected_browsers", []) if str(x).strip()]
    catalog = _catalog_list("browsers")
    return [x for x in current if x in catalog]


def _seed_defaults_if_needed() -> None:
    if _as_bool(_settings().get("defaults_seeded"), False):
        return
    changed = False
    if not [x for x in _settings().get("selected_platforms", []) if str(x).strip()]:
        _settings()["selected_platforms"] = _catalog_list("platforms")[:DEFAULT_SELECT_PLATFORMS]
        changed = True
    if not [x for x in _settings().get("selected_browsers", []) if str(x).strip()]:
        _settings()["selected_browsers"] = _catalog_list("browsers")[:DEFAULT_SELECT_BROWSERS]
        changed = True
    _settings()["defaults_seeded"] = True
    if changed:
        _save_settings()


def _refresh_catalogs(force: bool = False) -> tuple[bool, str]:
    catalogs = _catalogs()
    fetched_ts = float(catalogs.get("fetched_ts") or 0.0)
    if not force and fetched_ts > 0 and time.time() - fetched_ts < CATALOG_CACHE_TTL_SEC:
        return True, "catalog cache is fresh"

    try:
        platforms = _fetch_catalog(CATALOG_URL_PLATFORMS, "platforms")
        browsers = _fetch_catalog(CATALOG_URL_BROWSERS, "browsers")
    except Exception as exc:
        logger.warning("useragent_rotator: catalog refresh failed: %s", exc)
        if not catalogs.get("platforms"):
            catalogs["platforms"] = FALLBACK_PLATFORMS[:]
        if not catalogs.get("browsers"):
            catalogs["browsers"] = FALLBACK_BROWSERS[:]
        _save_state()
        return False, str(exc)

    if platforms:
        catalogs["platforms"] = platforms
    if browsers:
        catalogs["browsers"] = browsers
    catalogs["fetched_ts"] = time.time()

    current_platforms = [x for x in _selected_platforms() if x in catalogs["platforms"]]
    current_browsers = [x for x in _selected_browsers() if x in catalogs["browsers"]]

    if not _as_bool(_settings().get("catalog_defaults_applied"), False):
        for item in catalogs["platforms"]:
            if item not in current_platforms:
                current_platforms.append(item)
            if len(current_platforms) >= min(DEFAULT_SELECT_PLATFORMS, len(catalogs["platforms"])):
                break
        for item in catalogs["browsers"]:
            if item not in current_browsers:
                current_browsers.append(item)
            if len(current_browsers) >= min(DEFAULT_SELECT_BROWSERS, len(catalogs["browsers"])):
                break
        _settings()["catalog_defaults_applied"] = True

    _settings()["selected_platforms"] = current_platforms
    _settings()["selected_browsers"] = current_browsers

    _seed_defaults_if_needed()
    _save_settings()
    _save_state()
    return True, f"platforms={len(catalogs['platforms'])}, browsers={len(catalogs['browsers'])}"


def _start_catalog_refresh(cardinal: Any | None = None, *, force: bool = False, chat_id: int | None = None) -> None:
    with R.lock:
        if R.catalog_refresh_started and not force:
            return
        R.catalog_refresh_started = True

    def worker() -> None:
        try:
            ok, msg = _refresh_catalogs(force=force)
            if chat_id:
                text = (
                    f"Каталоги обновлены.\nПлатформ: <code>{len(_catalog_list('platforms'))}</code>\n"
                    f"Браузеров: <code>{len(_catalog_list('browsers'))}</code>"
                ) if ok else f"Не удалось обновить каталоги: <code>{html.escape(msg[:220])}</code>"
                _tg_send(cardinal, text, chat_id=chat_id)
        finally:
            with R.lock:
                R.catalog_refresh_started = False

    threading.Thread(target=worker, daemon=True).start()


def _current_user_agent() -> str:
    return str(_state().get("current_user_agent") or "").strip()


def _remember_recent_user_agent(user_agent: str) -> None:
    recent = _state().get("recent_user_agents", [])
    if not isinstance(recent, list):
        recent = []
    values = [str(x) for x in recent if str(x).strip()]
    values.append(user_agent)
    _state()["recent_user_agents"] = values[-RECENT_MAX:]


def _platform_profile(platform_name: str) -> tuple[str, str]:
    name = str(platform_name or "").strip()
    lower = name.lower()

    if "windows phone" in lower:
        return "Windows Phone 10.0; Android 6.0.1; Microsoft; Lumia 950", "mobile"
    if "windows" in lower:
        if "11" in lower or "10" in lower:
            return "Windows NT 10.0; Win64; x64", "desktop"
        if "8.1" in lower:
            return "Windows NT 6.3; Win64; x64", "desktop"
        if "8" in lower:
            return "Windows NT 6.2; Win64; x64", "desktop"
        if "7" in lower:
            return "Windows NT 6.1; Win64; x64", "desktop"
        return "Windows NT 10.0; Win64; x64", "desktop"
    if "iphone" in lower or "ios" in lower:
        return "iPhone; CPU iPhone OS 17_4 like Mac OS X", "iphone"
    if "ipad" in lower or "ipados" in lower:
        return "iPad; CPU OS 17_4 like Mac OS X", "ipad"
    if "mac" in lower:
        return "Macintosh; Intel Mac OS X 14_4", "mac"
    if "chrome os" in lower or "chromebook" in lower or "fydeos" in lower:
        return "CrOS x86_64 16181.47.0", "desktop"
    if any(x in lower for x in ("android tv", "google tv", "smart tv", "smarttv", "webos", "tizen", "vidaa", "coolita", "whale os", "roku")):
        return "Linux; Android 12; Smart TV", "tv"
    if "tablet" in lower:
        return "Linux; Android 14; Tablet", "tablet"
    if any(x in lower for x in ("wear os", "watch", "watchos")):
        return "Linux; Android 13; Wear", "wearable"
    if any(x in lower for x in ("android", "lineage", "cyanogen", "fire os", "harmony", "yunos", "kaios", "plasma mobile", "sailfish", "maemo", "meego", "mocordroid", "smartisan", "blackberry", "symbian")):
        return "Linux; Android 14; Mobile", "mobile"
    if "playstation" in lower or lower == "ps5" or lower == "ps4":
        return "PlayStation 5 1.0", "console"
    if "xbox" in lower:
        return "Xbox; Xbox Series X", "console"
    if "nintendo" in lower or "switch" in lower:
        return "Nintendo Switch; WifiWebAuthApplet", "console"
    if "bsd" in lower:
        return f"X11; {name}; amd64", "desktop"
    if any(x in lower for x in ("solaris", "os/2", "haiku", "amiga", "aix", "irix", "hp-ux", "serenity", "morphos", "risc os")):
        return name, "desktop"
    if any(x in lower for x in ("ubuntu", "debian", "fedora", "arch", "kali", "suse", "gentoo", "mint", "raspbian", "raspberry", "linux")):
        return f"X11; {name}; Linux x86_64", "desktop"
    return f"X11; {name}; Linux x86_64", "desktop"


def _rand_chrome_version() -> tuple[int, int, int, int]:
    return random.randint(122, 135), 0, random.randint(6100, 6999), random.randint(40, 180)


def _rand_firefox_version() -> int:
    return random.randint(120, 137)


def _rand_webkit_version() -> tuple[int, int]:
    return random.randint(605, 617), random.randint(1, 50)


def _browser_family(browser_name: str) -> str:
    name = str(browser_name or "").lower()
    if "internet explorer" in name or name == "ie":
        return "ie"
    if any(x in name for x in ("lynx", "w3m", "links")):
        return "text"
    if any(x in name for x in ("firefox", "waterfox", "palemoon", "pale moon", "basilisk", "seamonkey", "librewolf", "tor browser")):
        return "firefox"
    if "safari" in name and "chrome" not in name and "edge" not in name and "opera" not in name:
        return "safari"
    if any(x in name for x in ("edge", "edg")):
        return "edge"
    if any(x in name for x in ("opera", "opr", "gx")):
        return "opera"
    if "samsung" in name:
        return "samsung"
    if "uc browser" in name or name.startswith("uc "):
        return "uc"
    if "duckduckgo" in name:
        return "duck"
    if "brave" in name:
        return "brave"
    if "vivaldi" in name:
        return "vivaldi"
    if "yandex" in name:
        return "yandex"
    return "chromium"


def _build_generated_user_agent(browser_name: str, platform_name: str) -> str:
    profile, device = _platform_profile(platform_name)
    family = _browser_family(browser_name)
    chrome_major, chrome_minor, chrome_build, chrome_patch = _rand_chrome_version()
    chrome_version = f"{chrome_major}.{chrome_minor}.{chrome_build}.{chrome_patch}"
    firefox_major = _rand_firefox_version()
    wk_major, wk_minor = _rand_webkit_version()
    webkit = f"{wk_major}.{wk_minor}.{random.randint(1, 15)}"
    mobile_safari = "Mobile/15E148" if device in {"mobile", "iphone", "ipad", "tablet", "wearable"} else ""
    safari_tail = "Mobile Safari/537.36" if device in {"mobile", "iphone", "ipad", "tablet", "wearable"} else "Safari/537.36"

    if family == "ie":
        return f"Mozilla/5.0 ({profile}; Trident/7.0; rv:11.0) like Gecko"
    if family == "text":
        return f"Lynx/2.{random.randint(8, 9)}.{random.randint(0, 9)} libwww-FM/2.14 SSL-MM/1.4.1"
    if family == "firefox":
        extra = " Mobile" if device in {"mobile", "iphone", "ipad", "tablet"} else ""
        return f"Mozilla/5.0 ({profile}; rv:{firefox_major}.0) Gecko/20100101 Firefox/{firefox_major}.0{extra}"
    if family == "safari":
        version_major = random.randint(16, 18)
        if device in {"iphone", "ipad", "tablet"}:
            return (
                f"Mozilla/5.0 ({profile}) AppleWebKit/{webkit} (KHTML, like Gecko) "
                f"Version/{version_major}.0 {mobile_safari} Safari/{webkit}"
            )
        return (
            f"Mozilla/5.0 ({profile}) AppleWebKit/{webkit} (KHTML, like Gecko) "
            f"Version/{version_major}.0 Safari/{webkit}"
        )

    base = f"Mozilla/5.0 ({profile}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} {safari_tail}"
    if family == "edge":
        token = "EdgA" if device in {"mobile", "iphone", "ipad", "tablet"} else "Edg"
        return f"{base} {token}/{random.randint(122, 135)}.0.{random.randint(2100, 2600)}.{random.randint(40, 120)}"
    if family == "opera":
        return f"{base} OPR/{random.randint(100, 118)}.0.{random.randint(4700, 5200)}.{random.randint(40, 120)}"
    if family == "samsung":
        mobile = profile if device in {"mobile", "tablet", "iphone", "ipad"} else "Linux; Android 14; SAMSUNG SM-S928B"
        return (
            f"Mozilla/5.0 ({mobile}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"SamsungBrowser/{random.randint(24, 28)}.0 Chrome/{chrome_version} Mobile Safari/537.36"
        )
    if family == "uc":
        mobile = profile if device in {"mobile", "tablet", "iphone", "ipad"} else "Linux; Android 13; Mobile"
        return (
            f"Mozilla/5.0 ({mobile}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Mobile Safari/537.36 UCBrowser/{random.randint(13, 15)}.0.{random.randint(1000, 2500)}.{random.randint(10, 99)}"
        )
    if family == "duck":
        return f"{base} DuckDuckGo/{random.randint(5, 8)}"
    if family == "brave":
        return f"{base} Brave/{chrome_major}.1.{random.randint(40, 99)}.{random.randint(10, 180)}"
    if family == "vivaldi":
        return f"{base} Vivaldi/{random.randint(6, 7)}.{random.randint(0, 9)}.{random.randint(2000, 3200)}.{random.randint(10, 80)}"
    if family == "yandex":
        return f"{base} YaBrowser/{random.randint(24, 26)}.{random.randint(1, 9)}.{random.randint(1000, 3000)}.{random.randint(10, 99)}"
    return base


def _generate_from_pool() -> dict[str, str]:
    platforms = _selected_platforms()
    browsers = _selected_browsers()
    if not platforms:
        raise RuntimeError("platform pool is empty")
    if not browsers:
        raise RuntimeError("browser pool is empty")

    recent = set(str(x) for x in _state().get("recent_user_agents", []) if str(x).strip())
    for _ in range(25):
        platform_name = random.choice(platforms)
        browser_name = random.choice(browsers)
        user_agent = _build_generated_user_agent(browser_name, platform_name)
        if user_agent not in recent:
            return {
                "user_agent": user_agent,
                "source": "generated",
                "browser_name": browser_name,
                "platform_name": platform_name,
            }
    platform_name = random.choice(platforms)
    browser_name = random.choice(browsers)
    return {
        "user_agent": _build_generated_user_agent(browser_name, platform_name),
        "source": "generated",
        "browser_name": browser_name,
        "platform_name": platform_name,
    }


def _pick_user_agent_entry() -> dict[str, str]:
    mode = str(_settings().get("source_mode") or "generated").strip().lower()
    custom = [str(x).strip() for x in _settings().get("custom_user_agents", []) if str(x).strip()]
    if mode == "custom":
        choices = ["custom"] if custom else ["generated"]
    elif mode == "mixed":
        choices = ["generated"] + (["custom"] if custom else [])
    else:
        choices = ["generated"]
    picked = random.choice(choices)
    if picked == "custom":
        return {
            "user_agent": random.choice(custom),
            "source": "custom",
            "browser_name": "custom",
            "platform_name": "custom",
        }
    return _generate_from_pool()


def _verify_user_agent(user_agent: str, *, force: bool = False) -> tuple[bool, str]:
    if not user_agent:
        return False, "empty user-agent"
    gap = max(VERIFY_MIN_GAP_SEC, _as_int(_settings().get("verify_gap_sec"), VERIFY_MIN_GAP_SEC))
    now_ts = time.time()
    last_verify_ts = float(_state().get("last_verify_ts") or 0.0)
    if not force and last_verify_ts > 0 and now_ts - last_verify_ts < gap:
        return False, f"verify rate limit: wait {gap - int(now_ts - last_verify_ts)}s"

    try:
        response = requests.get(VERIFY_URL, params={"ua": user_agent, "key": "NOTREQUIED"}, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        _state()["last_error"] = str(exc)
        _save_state()
        return False, str(exc)

    browser = data.get("browser", {}) if isinstance(data, dict) else {}
    os_data = data.get("os", {}) if isinstance(data, dict) else {}
    device = data.get("device", {}) if isinstance(data, dict) else {}

    _state()["last_verify_ts"] = time.time()
    _state()["last_verify_browser"] = str(browser.get("name") or browser.get("family") or "").strip()
    _state()["last_verify_os"] = str(os_data.get("name") or os_data.get("family") or "").strip()
    _state()["last_verify_device"] = str(device.get("deviceType") or "").strip()
    _state()["last_verify_brand"] = str(device.get("brand") or "").strip()
    _state()["last_verify_model"] = str(device.get("model") or "").strip()
    _state()["last_verify_raw"] = data if isinstance(data, dict) else {}
    _save_state()
    return True, "ok"


def _server_profile_path() -> Path | None:
    if os.name != "posix":
        return None
    return Path.home() / ".config" / "fpc-user-agent.env"


def _write_server_profile(user_agent: str) -> None:
    os.environ["USER_AGENT"] = user_agent
    os.environ["HTTP_USER_AGENT"] = user_agent
    profile_path = _server_profile_path()
    if profile_path is None:
        return
    try:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        escaped = user_agent.replace("\\", "\\\\").replace('"', '\\"')
        profile_path.write_text(
            f'export USER_AGENT="{escaped}"\nexport HTTP_USER_AGENT="{escaped}"\n',
            encoding="utf-8",
        )
    except Exception:
        logger.debug("useragent_rotator: failed to write server profile", exc_info=True)


def _install_requests_patch() -> None:
    with R.lock:
        if R.requests_patched:
            return
        R.original_request = requests.sessions.Session.request

        def wrapped(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
            headers = dict(kwargs.get("headers") or {})
            current_ua = _current_user_agent()
            if current_ua and (_scope_cardinal_enabled() or _scope_server_enabled()):
                headers["User-Agent"] = current_ua
                kwargs["headers"] = headers
                try:
                    session.headers["User-Agent"] = current_ua
                except Exception:
                    pass
            return R.original_request(session, method, url, **kwargs)

        requests.sessions.Session.request = wrapped
        R.requests_patched = True


def _apply_user_agent(cardinal: Any, user_agent: str) -> None:
    if _scope_server_enabled():
        _write_server_profile(user_agent)

    if _scope_cardinal_enabled() or _scope_server_enabled():
        try:
            setattr(cardinal, "user_agent", user_agent)
        except Exception:
            pass
        for owner in (cardinal, getattr(cardinal, "account", None)):
            if owner is None:
                continue
            for attr in ("session", "requests_session", "http_session"):
                obj = getattr(owner, attr, None)
                if isinstance(obj, requests.Session):
                    try:
                        obj.headers["User-Agent"] = user_agent
                    except Exception:
                        pass
            for attr in ("headers", "default_headers"):
                obj = getattr(owner, attr, None)
                if isinstance(obj, dict):
                    obj["User-Agent"] = user_agent

    if _scope_funpay_enabled():
        acc = getattr(cardinal, "account", None)
        if acc is not None:
            for attr in ("session", "requests_session", "http_session"):
                obj = getattr(acc, attr, None)
                if isinstance(obj, requests.Session):
                    try:
                        obj.headers["User-Agent"] = user_agent
                    except Exception:
                        pass
            for attr in ("headers", "default_headers"):
                obj = getattr(acc, attr, None)
                if isinstance(obj, dict):
                    obj["User-Agent"] = user_agent


def _notify_chat_id(cardinal: Any) -> int:
    chat_id = _as_int(_settings().get("notify_chat_id"), 0)
    if chat_id > 0:
        return chat_id
    tg = getattr(cardinal, "telegram", None)
    auth = getattr(tg, "authorized_users", []) if tg is not None else []
    if isinstance(auth, (list, tuple, set)):
        for item in auth:
            val = _as_int(item, 0)
            if val > 0:
                return val
    return 0


def _tg_send(cardinal: Any, text: str, *, chat_id: int | None = None, markup: Any | None = None) -> None:
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    cid = int(chat_id or _notify_chat_id(cardinal) or 0)
    if cid <= 0:
        return
    tg.bot.send_message(cid, text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


def _tg_edit(cardinal: Any, call: Any, text: str, markup: Any | None = None) -> None:
    try:
        cardinal.telegram.bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.id,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        _tg_send(cardinal, text, chat_id=call.message.chat.id, markup=markup)


def _mode_label() -> str:
    mode = str(_settings().get("source_mode") or "generated").strip().lower()
    return {
        "generated": "генерация",
        "custom": "свои UA",
        "mixed": "микс",
    }.get(mode, mode)


def _render_panel() -> str:
    _load_settings(False)
    _load_state(False)
    current_ua = _current_user_agent()
    next_ts = float(_state().get("next_rotate_ts") or 0.0)
    next_in = max(0, int(next_ts - time.time())) if next_ts > 0 else 0
    platforms_total = len(_catalog_list("platforms"))
    browsers_total = len(_catalog_list("browsers"))
    lines = [
        "<b>User-Agent Rotator</b>",
        "",
        f"Разработчики: <code>{html.escape(CREDITS)}</code>",
        f"Авто-ротация: <b>{'включена' if _as_bool(_settings().get('enabled'), False) else 'выключена'}</b>",
        f"Интервал: <code>{_human_seconds(_as_int(_settings().get('rotation_interval_sec'), 60))}</code>",
        f"Охват: <code>{html.escape(_scope_summary())}</code>",
        f"Уведомления: <b>{'ON' if _as_bool(_settings().get('notify_enabled'), True) else 'OFF'}</b>",
        f"Проверка API: <b>{'ON' if _as_bool(_settings().get('verify_enabled'), True) else 'OFF'}</b>",
        f"Источник: <code>{_mode_label()}</code>",
        f"Платформ выбрано: <code>{len(_selected_platforms())}</code> / <code>{platforms_total}</code>",
        f"Браузеров выбрано: <code>{len(_selected_browsers())}</code> / <code>{browsers_total}</code>",
        f"Своих UA: <code>{len([x for x in _settings().get('custom_user_agents', []) if str(x).strip()])}</code>",
        f"Следующая смена: <code>{_human_seconds(next_in) if next_in else '-'}</code>",
    ]
    if _state().get("current_source"):
        lines.append(f"Текущий источник: <code>{html.escape(str(_state().get('current_source')))}</code>")
    if _state().get("current_platform_name"):
        lines.append(f"Текущая платформа: <code>{html.escape(str(_state().get('current_platform_name')))}</code>")
    if _state().get("current_browser_name"):
        lines.append(f"Текущий браузер: <code>{html.escape(str(_state().get('current_browser_name')))}</code>")
    if current_ua:
        lines.extend(["", "<b>Текущий User-Agent</b>", f"<code>{html.escape(_short_ua(current_ua, 240))}</code>"])

    verify_browser = str(_state().get("last_verify_browser") or "").strip()
    verify_os = str(_state().get("last_verify_os") or "").strip()
    verify_device = str(_state().get("last_verify_device") or "").strip()
    verify_brand = str(_state().get("last_verify_brand") or "").strip()
    if verify_browser or verify_os or verify_device or verify_brand:
        bits = [x for x in [verify_browser, verify_os, verify_device, verify_brand] if x]
        lines.extend(["", "<b>Последняя проверка API</b>", f"<code>{html.escape(' / '.join(bits))}</code>"])

    last_error = str(_state().get("last_error") or "").strip()
    if last_error:
        lines.extend(["", f"Последняя ошибка: <code>{html.escape(last_error[:220])}</code>"])

    lines.extend(
        [
            "",
            "Каталоги тянутся прямо с WhatMyUserAgent.",
            "Сейчас страница платформ показывает 143 позиций, а страница браузеров — 419.",
            "По умолчанию в пул автоматически выбираются первые 100 платформ и первые 300 браузеров.",
        ]
    )
    if _scope_server_enabled():
        profile_path = _server_profile_path()
        lines.append(
            "Режим Ubuntu env/profile записывает текущий UA в профиль текущего пользователя/сервиса"
            + (f": <code>{html.escape(str(profile_path))}</code>" if profile_path else ".")
        )
    return "\n".join(lines)


def _panel_kb() -> K:
    notify_label = "🔔 Уведы: ON" if _as_bool(_settings().get("notify_enabled"), True) else "🔕 Уведы: OFF"
    verify_label = "🧪 API: ON" if _as_bool(_settings().get("verify_enabled"), True) else "🧪 API: OFF"
    auto_label = "🟢 Авто: ON" if _as_bool(_settings().get("enabled"), False) else "⏸ Авто: OFF"
    funpay_label = "🎯 FP API: ON" if _scope_funpay_enabled() else "🎯 FP API: OFF"
    cardinal_label = "⚙ Cardinal: ON" if _scope_cardinal_enabled() else "⚙ Cardinal: OFF"
    server_label = "🖥 Server: ON" if _scope_server_enabled() else "🖥 Server: OFF"
    kb = K(row_width=2)
    kb.row(B("🎲 Сменить сейчас", callback_data=CBT_ROTATE), B(auto_label, callback_data=CBT_TOGGLE_AUTO))
    kb.row(B("⏱ Интервал", callback_data=CBT_SET_INTERVAL), B(notify_label, callback_data=CBT_TOGGLE_NOTIFY))
    kb.row(B(verify_label, callback_data=CBT_TOGGLE_VERIFY), B("🔎 Проверить UA", callback_data=CBT_VERIFY_NOW))
    kb.row(B(funpay_label, callback_data="uarot:scope:funpay"), B(cardinal_label, callback_data="uarot:scope:cardinal"))
    kb.row(B(server_label, callback_data="uarot:scope:server"))
    kb.row(B("🧬 Источник", callback_data=CBT_CYCLE_MODE), B("♻ Обновить каталоги", callback_data=CBT_REFRESH))
    kb.row(B("🖥 Платформы", callback_data=f"{CBT_PLAT_MENU}:0"), B("🌐 Браузеры", callback_data=f"{CBT_BROWSER_MENU}:0"))
    kb.row(B("📥 Массовый импорт UA", callback_data=CBT_IMPORT), B("🧹 Очистить свои UA", callback_data=CBT_CLEAR_CUSTOM))
    kb.row(B("📚 Platforms", url=CATALOG_URL_PLATFORMS), B("🧭 Browsers", url=CATALOG_URL_BROWSERS))
    return kb


def _selector_page(items: list[str], selected: list[str], kind: str, page: int) -> tuple[str, K]:
    total_pages = max(1, (len(items) + SELECTOR_PAGE_SIZE - 1) // SELECTOR_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * SELECTOR_PAGE_SIZE
    chunk = items[start : start + SELECTOR_PAGE_SIZE]
    selected_set = set(selected)
    title = "Платформы" if kind == "platforms" else "Браузеры"
    text_lines = [
        f"<b>{title}</b>",
        "",
        f"Страница: <code>{page + 1}</code> / <code>{total_pages}</code>",
        f"Выбрано: <code>{len(selected)}</code> / <code>{len(items)}</code>",
        "",
        "Нажимай по кнопке, чтобы добавлять или убирать элемент из пула генерации.",
    ]
    kb = K(row_width=1)
    for offset, value in enumerate(chunk, start=start):
        mark = "✅" if value in selected_set else "⬜"
        if kind == "platforms":
            kb.row(B(f"{mark} {value}", callback_data=f"uarot:pt:{offset}:{page}"))
        else:
            kb.row(B(f"{mark} {value}", callback_data=f"uarot:br:{offset}:{page}"))

    if kind == "platforms":
        kb.row(B("🎯 Топ 100", callback_data="uarot:ps:def"), B("🌍 Все", callback_data="uarot:ps:all"))
        kb.row(B("🧹 Очистить", callback_data="uarot:ps:clear"))
        kb.row(B("⬅️ Назад", callback_data=f"{CBT_PLAT_MENU}:{max(0, page - 1)}"), B("➡️ Далее", callback_data=f"{CBT_PLAT_MENU}:{min(total_pages - 1, page + 1)}"))
    else:
        kb.row(B("🎯 Топ 300", callback_data="uarot:bs:def"), B("🌍 Все", callback_data="uarot:bs:all"))
        kb.row(B("🧹 Очистить", callback_data="uarot:bs:clear"))
        kb.row(B("⬅️ Назад", callback_data=f"{CBT_BROWSER_MENU}:{max(0, page - 1)}"), B("➡️ Далее", callback_data=f"{CBT_BROWSER_MENU}:{min(total_pages - 1, page + 1)}"))
    kb.row(B("🏠 В панель", callback_data=CBT_BACK))
    return "\n".join(text_lines), kb


def _toggle_selection(kind: str, index: int) -> None:
    items = _catalog_list(kind)
    if index < 0 or index >= len(items):
        return
    value = items[index]
    key = "selected_platforms" if kind == "platforms" else "selected_browsers"
    selected = [str(x) for x in _settings().get(key, []) if str(x).strip()]
    if value in selected:
        selected = [x for x in selected if x != value]
    else:
        selected.append(value)
    _settings()[key] = selected
    _save_settings()


def _set_default_selection(kind: str) -> None:
    items = _catalog_list(kind)
    key = "selected_platforms" if kind == "platforms" else "selected_browsers"
    limit = DEFAULT_SELECT_PLATFORMS if kind == "platforms" else DEFAULT_SELECT_BROWSERS
    _settings()[key] = items[:limit]
    _save_settings()


def _set_all_selection(kind: str) -> None:
    items = _catalog_list(kind)
    key = "selected_platforms" if kind == "platforms" else "selected_browsers"
    _settings()[key] = items[:]
    _save_settings()


def _clear_selection(kind: str) -> None:
    key = "selected_platforms" if kind == "platforms" else "selected_browsers"
    _settings()[key] = []
    _save_settings()


def _cycle_mode() -> str:
    order = ["generated", "mixed", "custom"]
    current = str(_settings().get("source_mode") or "generated").strip().lower()
    try:
        idx = order.index(current)
    except ValueError:
        idx = 0
    new_mode = order[(idx + 1) % len(order)]
    if new_mode == "custom" and not [x for x in _settings().get("custom_user_agents", []) if str(x).strip()]:
        new_mode = "generated"
    _settings()["source_mode"] = new_mode
    _save_settings()
    return new_mode


def _set_interval(sec: int) -> None:
    sec = max(1, int(sec))
    _settings()["rotation_interval_sec"] = sec
    _state()["next_rotate_ts"] = time.time() + sec
    _save_settings()
    _save_state()


def _perform_rotation(cardinal: Any, *, manual: bool = False, chat_id: int | None = None) -> tuple[bool, str]:
    try:
        entry = _pick_user_agent_entry()
        user_agent = str(entry.get("user_agent") or "").strip()
        if not user_agent:
            raise RuntimeError("generated empty user-agent")

        _state()["current_user_agent"] = user_agent
        _state()["current_source"] = str(entry.get("source") or "")
        _state()["current_platform_name"] = str(entry.get("platform_name") or "")
        _state()["current_browser_name"] = str(entry.get("browser_name") or "")
        _state()["last_rotate_ts"] = time.time()
        _state()["next_rotate_ts"] = _state()["last_rotate_ts"] + max(1, _as_int(_settings().get("rotation_interval_sec"), 60))
        _state()["last_error"] = ""
        _remember_recent_user_agent(user_agent)
        _save_state()

        _apply_user_agent(cardinal, user_agent)

        verify_summary = ""
        if _as_bool(_settings().get("verify_enabled"), True):
            ok_verify, msg_verify = _verify_user_agent(user_agent, force=False)
            if ok_verify:
                pieces = [
                    str(_state().get("last_verify_browser") or "").strip(),
                    str(_state().get("last_verify_os") or "").strip(),
                    str(_state().get("last_verify_device") or "").strip(),
                ]
                pieces = [x for x in pieces if x]
                verify_summary = " / ".join(pieces)
            else:
                verify_summary = f"проверка пропущена: {msg_verify}"

        message_lines = [
            "<b>User-Agent обновлён</b>",
            f"Источник: <code>{html.escape(str(_state().get('current_source') or '-'))}</code>",
            f"Платформа: <code>{html.escape(str(_state().get('current_platform_name') or '-'))}</code>",
            f"Браузер: <code>{html.escape(str(_state().get('current_browser_name') or '-'))}</code>",
            f"UA: <code>{html.escape(_short_ua(user_agent, 220))}</code>",
        ]
        if verify_summary:
            message_lines.append(f"API: <code>{html.escape(verify_summary)}</code>")
        message = "\n".join(message_lines)

        notify = manual or _as_bool(_settings().get("notify_enabled"), True)
        if notify:
            _tg_send(cardinal, message, chat_id=chat_id)
        return True, message
    except Exception as exc:
        logger.exception("useragent_rotator: rotation failed")
        _state()["last_error"] = str(exc)
        _save_state()
        message = f"Не удалось сменить User-Agent: <code>{html.escape(str(exc)[:220])}</code>"
        if manual and chat_id:
            _tg_send(cardinal, message, chat_id=chat_id)
        return False, message


def _verify_current(cardinal: Any, chat_id: int) -> None:
    ua = _current_user_agent()
    if not ua:
        _tg_send(cardinal, "Сейчас ещё нет активного User-Agent. Сначала нажми <b>Сменить сейчас</b>.", chat_id=chat_id)
        return
    ok, msg = _verify_user_agent(ua, force=True)
    if not ok:
        _tg_send(cardinal, f"Проверка не удалась: <code>{html.escape(msg[:220])}</code>", chat_id=chat_id)
        return
    info = [
        str(_state().get("last_verify_browser") or "").strip(),
        str(_state().get("last_verify_os") or "").strip(),
        str(_state().get("last_verify_device") or "").strip(),
        str(_state().get("last_verify_brand") or "").strip(),
        str(_state().get("last_verify_model") or "").strip(),
    ]
    info = [x for x in info if x]
    _tg_send(cardinal, f"Проверка WhatMyUserAgent:\n<code>{html.escape(' / '.join(info) or 'ok')}</code>", chat_id=chat_id)


def _loop(cardinal: Any) -> None:
    while True:
        try:
            _load_settings(False)
            _load_state(False)
            if not _as_bool(_settings().get("enabled"), False):
                time.sleep(1)
                continue
            interval = max(1, _as_int(_settings().get("rotation_interval_sec"), 60))
            now_ts = time.time()
            next_ts = float(_state().get("next_rotate_ts") or 0.0)
            if next_ts <= 0:
                _state()["next_rotate_ts"] = now_ts + interval
                _save_state()
            elif now_ts >= next_ts:
                _perform_rotation(cardinal, manual=False, chat_id=_notify_chat_id(cardinal))
            time.sleep(1)
        except Exception:
            logger.exception("useragent_rotator: loop failed")
            time.sleep(3)


def _start_loop(cardinal: Any) -> None:
    with R.lock:
        if R.loop_started:
            return
        R.loop_started = True
    threading.Thread(target=lambda: _loop(cardinal), daemon=True).start()


def _authorized(cardinal: Any, user_id: int) -> bool:
    tg = getattr(cardinal, "telegram", None)
    auth = getattr(tg, "authorized_users", []) if tg is not None else []
    return True if not auth else int(user_id) in set(auth)


def _open_panel(cardinal: Any, chat_id: int, user_id: int, message_id: int | None = None) -> None:
    _clear_pending(chat_id, user_id)
    text = _render_panel()
    markup = _panel_kb()
    if message_id:
        try:
            cardinal.telegram.bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass
    _tg_send(cardinal, text, chat_id=chat_id, markup=markup)


def _handle_callback(cardinal: Any, call: Any) -> None:
    if not _authorized(cardinal, call.from_user.id):
        return
    try:
        cardinal.telegram.bot.answer_callback_query(call.id)
    except Exception:
        pass

    chat_id = int(call.message.chat.id)
    user_id = int(call.from_user.id)
    data = str(call.data or "")
    _settings()["notify_chat_id"] = chat_id
    _save_settings()

    if data.startswith(CBT_OPEN) or data == CBT_BACK:
        _open_panel(cardinal, chat_id, user_id, call.message.id)
        return
    if data == CBT_ROTATE:
        _perform_rotation(cardinal, manual=True, chat_id=chat_id)
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == CBT_TOGGLE_AUTO:
        _settings()["enabled"] = not _as_bool(_settings().get("enabled"), False)
        if _settings()["enabled"]:
            _state()["next_rotate_ts"] = time.time() + max(1, _as_int(_settings().get("rotation_interval_sec"), 60))
            _save_state()
        _save_settings()
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == CBT_TOGGLE_NOTIFY:
        _settings()["notify_enabled"] = not _as_bool(_settings().get("notify_enabled"), True)
        _save_settings()
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == CBT_TOGGLE_VERIFY:
        _settings()["verify_enabled"] = not _as_bool(_settings().get("verify_enabled"), True)
        _save_settings()
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == CBT_VERIFY_NOW:
        threading.Thread(target=lambda: _verify_current(cardinal, chat_id), daemon=True).start()
        return
    if data == CBT_CYCLE_MODE:
        _cycle_mode()
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == "uarot:scope:funpay":
        _toggle_scope("funpay")
        current = _current_user_agent()
        if current:
            _apply_user_agent(cardinal, current)
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == "uarot:scope:cardinal":
        _toggle_scope("cardinal")
        current = _current_user_agent()
        if current:
            _apply_user_agent(cardinal, current)
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == "uarot:scope:server":
        _toggle_scope("server")
        current = _current_user_agent()
        if current:
            _apply_user_agent(cardinal, current)
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == CBT_REFRESH:
        _tg_send(cardinal, "Обновляю каталоги платформ и браузеров...", chat_id=chat_id)
        _start_catalog_refresh(cardinal, force=True, chat_id=chat_id)
        return
    if data == CBT_SET_INTERVAL:
        _set_pending(chat_id, user_id, INPUT_INTERVAL)
        kb = K(row_width=2)
        kb.row(B("5с", callback_data="uarot:int:5"), B("10с", callback_data="uarot:int:10"))
        kb.row(B("30с", callback_data="uarot:int:30"), B("60с", callback_data="uarot:int:60"))
        kb.row(B("120с", callback_data="uarot:int:120"), B("300с", callback_data="uarot:int:300"))
        kb.row(B("🏠 В панель", callback_data=CBT_BACK))
        _tg_send(cardinal, "Выбери готовый интервал кнопкой ниже или пришли своё число в секундах одним сообщением.", chat_id=chat_id, markup=kb)
        return
    if data.startswith("uarot:int:"):
        sec = _as_int(data.split(":")[-1], 60)
        _set_interval(sec)
        _clear_pending(chat_id, user_id)
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        return
    if data == CBT_IMPORT:
        _set_pending(chat_id, user_id, INPUT_IMPORT)
        _tg_send(cardinal, "Отправь свои User-Agent одним сообщением.\nФормат: <code>1 строка = 1 UA</code>.", chat_id=chat_id)
        return
    if data == CBT_CLEAR_CUSTOM:
        _settings()["custom_user_agents"] = []
        if str(_settings().get("source_mode") or "") == "custom":
            _settings()["source_mode"] = "generated"
        _save_settings()
        _tg_edit(cardinal, call, _render_panel(), _panel_kb())
        _tg_send(cardinal, "Список своих User-Agent очищен.", chat_id=chat_id)
        return
    if data.startswith(f"{CBT_PLAT_MENU}:"):
        page = _as_int(data.split(":")[-1], 0)
        text, markup = _selector_page(_catalog_list("platforms"), _selected_platforms(), "platforms", page)
        _tg_edit(cardinal, call, text, markup)
        return
    if data.startswith(f"{CBT_BROWSER_MENU}:"):
        page = _as_int(data.split(":")[-1], 0)
        text, markup = _selector_page(_catalog_list("browsers"), _selected_browsers(), "browsers", page)
        _tg_edit(cardinal, call, text, markup)
        return
    if data.startswith("uarot:pt:"):
        _, _, index_str, page_str = data.split(":")
        _toggle_selection("platforms", _as_int(index_str, -1))
        text, markup = _selector_page(_catalog_list("platforms"), _selected_platforms(), "platforms", _as_int(page_str, 0))
        _tg_edit(cardinal, call, text, markup)
        return
    if data.startswith("uarot:br:"):
        _, _, index_str, page_str = data.split(":")
        _toggle_selection("browsers", _as_int(index_str, -1))
        text, markup = _selector_page(_catalog_list("browsers"), _selected_browsers(), "browsers", _as_int(page_str, 0))
        _tg_edit(cardinal, call, text, markup)
        return
    if data == "uarot:ps:def":
        _set_default_selection("platforms")
        text, markup = _selector_page(_catalog_list("platforms"), _selected_platforms(), "platforms", 0)
        _tg_edit(cardinal, call, text, markup)
        return
    if data == "uarot:ps:all":
        _set_all_selection("platforms")
        text, markup = _selector_page(_catalog_list("platforms"), _selected_platforms(), "platforms", 0)
        _tg_edit(cardinal, call, text, markup)
        return
    if data == "uarot:ps:clear":
        _clear_selection("platforms")
        text, markup = _selector_page(_catalog_list("platforms"), _selected_platforms(), "platforms", 0)
        _tg_edit(cardinal, call, text, markup)
        return
    if data == "uarot:bs:def":
        _set_default_selection("browsers")
        text, markup = _selector_page(_catalog_list("browsers"), _selected_browsers(), "browsers", 0)
        _tg_edit(cardinal, call, text, markup)
        return
    if data == "uarot:bs:all":
        _set_all_selection("browsers")
        text, markup = _selector_page(_catalog_list("browsers"), _selected_browsers(), "browsers", 0)
        _tg_edit(cardinal, call, text, markup)
        return
    if data == "uarot:bs:clear":
        _clear_selection("browsers")
        text, markup = _selector_page(_catalog_list("browsers"), _selected_browsers(), "browsers", 0)
        _tg_edit(cardinal, call, text, markup)


def _handle_message(cardinal: Any, m: Any) -> None:
    if not _authorized(cardinal, m.from_user.id):
        return
    pending = _get_pending(m.chat.id, m.from_user.id)
    if pending is None:
        return
    text = str(m.text or "").strip()
    if not text:
        return

    mode = str(pending.get("mode") or "")
    if mode == INPUT_INTERVAL:
        sec = _as_int(text, -1)
        if sec <= 0:
            _tg_send(cardinal, "Нужно прислать положительное число секунд.", chat_id=m.chat.id)
            return
        _set_interval(sec)
        _pop_pending(m.chat.id, m.from_user.id)
        _tg_send(cardinal, f"Интервал ротации обновлён: <code>{sec}</code> сек.", chat_id=m.chat.id)
        return
    if mode == INPUT_IMPORT:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            _tg_send(cardinal, "Список пуст. Отправь хотя бы один User-Agent.", chat_id=m.chat.id)
            return
        existing = [str(x).strip() for x in _settings().get("custom_user_agents", []) if str(x).strip()]
        existing_set = {x.casefold() for x in existing}
        added = 0
        skipped = 0
        for line in lines:
            if len(line) < 10 or "/" not in line:
                skipped += 1
                continue
            key = line.casefold()
            if key in existing_set:
                skipped += 1
                continue
            existing.append(line)
            existing_set.add(key)
            added += 1
        _settings()["custom_user_agents"] = existing
        _save_settings()
        _pop_pending(m.chat.id, m.from_user.id)
        _tg_send(cardinal, "<b>Импорт User-Agent завершён</b>\n"
                 f"Строк получено: <code>{len(lines)}</code>\n"
                 f"Добавлено: <code>{added}</code>\n"
                 f"Пропущено: <code>{skipped}</code>", chat_id=m.chat.id)


def _cmd_open(cardinal: Any, m: Any) -> None:
    if not _authorized(cardinal, m.from_user.id):
        return
    _settings()["notify_chat_id"] = int(m.chat.id)
    _save_settings()
    _open_panel(cardinal, m.chat.id, m.from_user.id)


def _register_tg_handlers(cardinal: Any) -> None:
    tg = getattr(cardinal, "telegram", None)
    if tg is None or R.tg_registered:
        return
    tg.cbq_handler(lambda c: _handle_callback(cardinal, c), lambda c: str(c.data or "").startswith(CBT_OPEN))
    tg.cbq_handler(lambda c: _handle_callback(cardinal, c), lambda c: str(c.data or "").startswith("uarot:"))
    tg.msg_handler(lambda m: _cmd_open(cardinal, m), commands=["uarot"])
    tg.msg_handler(lambda m: _handle_message(cardinal, m), func=lambda m: _get_pending(m.chat.id, m.from_user.id) is not None)
    try:
        cardinal.add_telegram_commands(UUID, [("uarot", "User-Agent rotator", True)])
    except Exception:
        logger.debug("useragent_rotator: add_telegram_commands failed", exc_info=True)
    R.tg_registered = True


def init_plugin(cardinal: Any, *_args: Any, **_kwargs: Any) -> None:
    _load_settings()
    _load_state()
    _normalize_scopes(save=True)
    _seed_defaults_if_needed()
    _install_requests_patch()
    _register_tg_handlers(cardinal)


def post_init(cardinal: Any, *_args: Any, **_kwargs: Any) -> None:
    _load_settings()
    _load_state()
    _normalize_scopes(save=True)
    _seed_defaults_if_needed()
    _install_requests_patch()
    _register_tg_handlers(cardinal)
    _start_loop(cardinal)
    _start_catalog_refresh(cardinal, force=False, chat_id=None)
    current = _current_user_agent()
    if current:
        _apply_user_agent(cardinal, current)


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_MESSAGE = []
BIND_TO_NEW_ORDER = []
BIND_TO_ORDER_STATUS_CHANGED = []
