"""Shared mutable state: in-memory DB, JSON persistence, paths."""
import itertools
import json
import os
import secrets
import threading
import time

from . import config

USERS_FILE = os.path.join(config.DATA_DIR, "users.json")
SESSIONS_FILE = os.path.join(config.DATA_DIR, "sessions.json")
CONVS_FILE = os.path.join(config.DATA_DIR, "convs.json")
MESSAGES_FILE = os.path.join(config.DATA_DIR, "messages.json")
LEGACY_HISTORY = os.path.join(config.DATA_DIR, "history.json")

# Map of new file -> legacy dotfile names to auto-migrate on first run
# (supports upgrade from the old single-folder layout).
# Built inside setup() so --data/--share flags are honoured first.


def _migrate_file(new_path, legacy_names):
    """Move a legacy state file into its new data/ location (first run only)."""
    if os.path.isfile(new_path):
        return
    candidates = []
    for base in (config.SHARE_DIR, config.APP_ROOT, os.getcwd(), config.DATA_DIR):
        for name in legacy_names:
            candidates.append(os.path.join(base, name))
    # Old hidden folders that became data/avatars and data/chatfiles.
    for cand in candidates:
        if cand != new_path and os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    json.load(f)  # only migrate valid JSON
                os.replace(cand, new_path)
            except (OSError, ValueError):
                pass
            return


def _migrate_dir(new_dir, legacy_names):
    """Move a legacy folder (avatars/chatfiles) into data/ (first run only)."""
    if os.path.isdir(new_dir) and os.listdir(new_dir):
        return
    for base in (config.SHARE_DIR, config.APP_ROOT, os.getcwd(), config.DATA_DIR):
        for name in legacy_names:
            cand = os.path.join(base, name)
            if os.path.isdir(cand) and cand != new_dir:
                try:
                    for fname in os.listdir(cand):
                        src = os.path.join(cand, fname)
                        dst = os.path.join(new_dir, fname)
                        if os.path.isfile(src) and not os.path.exists(dst):
                            os.replace(src, dst)
                    try:
                        os.rmdir(cand)
                    except OSError:
                        pass
                except OSError:
                    pass
                return


def setup():
    """Create dirs + run first-run legacy migration. Called by load_state(),
    never at import, so `--help` and friends stay side-effect free and
    --data/--share flags take effect before any file is touched."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(AVATARS_DIR, exist_ok=True)
    os.makedirs(CHATFILES_DIR, exist_ok=True)
    legacy_map = {
        USERS_FILE: [".lanchat_users.json", "users.json"],
        SESSIONS_FILE: [".lanchat_sessions.json", "sessions.json"],
        CONVS_FILE: [".lanchat_convs.json", "convs.json"],
        MESSAGES_FILE: [".lanchat_messages.json", "messages.json", ".lanchat_history.json"],
    }
    for _new, _olds in legacy_map.items():
        _migrate_file(_new, _olds)
    _migrate_dir(AVATARS_DIR, [".lanchat_avatars", "avatars"])
    _migrate_dir(CHATFILES_DIR, [".lanchat_chatfiles", "chatfiles"])

MAX_MESSAGES_PER_CONV = 500
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
GENERAL_CONV = "group:general"

# Profile pictures and private chat attachments live under data/
# (never inside the public share/ folder).
AVATARS_DIR = os.path.join(config.DATA_DIR, "avatars")
CHATFILES_DIR = os.path.join(config.DATA_DIR, "chatfiles")
ALLOWED_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB
READ_MORE_CHARS = 400
IMG_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

_lock = threading.RLock()
_users = {}      # username(lower) -> {username,name,email,pw,salt,role,reads,created,avatar?}
_sessions = {}   # token -> {user: username(lower), created}
_convs = {}      # conv id -> {id,type,name?,members?,all?,created}
_messages = {}   # conv id -> [message, ...]
_next_id = itertools.count(1)


def _load_json(path, default):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
    return default


def _save_json(path, obj):
    """Atomic write so a crash mid-write can't corrupt the file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass  # best-effort persistence


def _save_users():
    _save_json(USERS_FILE, _users)


def _save_sessions():
    _save_json(SESSIONS_FILE, _sessions)


def _save_convs():
    _save_json(CONVS_FILE, _convs)


def _save_messages():
    _save_json(MESSAGES_FILE, _messages)


# ---------------------------------------------------------------------------
# Passwords / users
# ---------------------------------------------------------------------------


def _refresh_state_paths():
    """Re-derive file/dir globals after DATA_DIR/SHARE_DIR change (CLI flags)."""
    global USERS_FILE, SESSIONS_FILE, CONVS_FILE, MESSAGES_FILE
    global LEGACY_HISTORY, AVATARS_DIR, CHATFILES_DIR
    USERS_FILE = os.path.join(config.DATA_DIR, "users.json")
    SESSIONS_FILE = os.path.join(config.DATA_DIR, "sessions.json")
    CONVS_FILE = os.path.join(config.DATA_DIR, "convs.json")
    MESSAGES_FILE = os.path.join(config.DATA_DIR, "messages.json")
    LEGACY_HISTORY = os.path.join(config.DATA_DIR, "history.json")
    AVATARS_DIR = os.path.join(config.DATA_DIR, "avatars")
    CHATFILES_DIR = os.path.join(config.DATA_DIR, "chatfiles")


def display_name(key):
    u = _users.get(key)
    return u["name"] if u else key


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(username_key):
    token = secrets.token_urlsafe(24)
    with _lock:
        _sessions[token] = {"user": username_key, "created": time.time()}
        _save_sessions()
    return token


def drop_session(token):
    with _lock:
        if token in _sessions:
            del _sessions[token]
            _save_sessions()


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


_counter = itertools.count(1)


def next_msg_id():
    """Next global message id (single choke point; safe across modules)."""
    return next(_counter)


def reset_msg_id(start):
    """Reset the id counter (used when loading persisted state)."""
    global _counter
    _counter = itertools.count(start)
