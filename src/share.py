#!/usr/bin/env python3
"""
LAN Share + Messenger
---------------------
A single-file LAN chat + file sharing app with:

  * Accounts & login  — you (the owner) create users with a username,
    display name, email and password from the Admin panel. Everyone
    else just signs in with their username or email + password.
  * Messenger-style UI — left sidebar with conversations, right pane
    with the conversation, chat bubbles, typing box, attachments.
  * Private (1:1) chats and group chats.
  * Shared Files tab (drag & drop, download, delete) — still here.

Usage:
    python src/share.py                 # shares ./share on port 8000
    python src/share.py 9000            # shares ./share on port 9000
    python src/share.py /path/to/dir   # shares a specific directory
    python src/share.py /path/to/dir 9000  # custom dir + port
    python src/share.py --reset-owner   # prints a brand-new owner password
                                        # (do this if you lost the first-run one)

    Project layout:
        shaare/
            src/share.py      # this file (all code)
            share/            # public shared files (Files tab)
            data/             # private app state (never shared)
                users.json
                sessions.json
                convs.json
                messages.json
                avatars/      # profile pictures
                chatfiles/    # private chat attachments

Optional environment variables for the first-run owner account:
    LANCHAT_OWNER       owner username        (default: admin)
    LANCHAT_OWNER_EMAIL owner email           (default: owner@localhost)
    LANCHAT_OWNER_PASS  owner password        (default: random, printed once)

Optional overrides:
    LANCHAT_SHARE       public share dir      (default: <project>/share)
    LANCHAT_DATA        private data dir      (default: <project>/data)
    LANCHAT_PORT        port                  (default: 8000)

No external dependencies — pure Python standard library.
"""

import os
import sys
import socket
import html
import re
import json
import time
import hmac
import hashlib
import secrets
import threading
import itertools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, quote, parse_qs, urlparse

RESET_OWNER = "--reset-owner" in sys.argv[1:]
if RESET_OWNER:
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--reset-owner"]

# Project root = parent of src/ (falls back to script dir for legacy layout).
APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.basename(os.path.dirname(__file__)).lower() != "src":
    APP_ROOT = os.path.abspath(os.path.dirname(__file__))

_DEFAULT_SHARE = os.environ.get("LANCHAT_SHARE") or os.path.join(APP_ROOT, "share")
_DEFAULT_DATA = os.environ.get("LANCHAT_DATA") or os.path.join(APP_ROOT, "data")

# Parse CLI: [share_dir] [port], with bare-number first arg treated as port
# (old code treated `share.py 9000` as a directory named "9000").
_cli_args = sys.argv[1:]
SHARE_DIR = os.path.abspath(_DEFAULT_SHARE)
PORT = int(os.environ.get("LANCHAT_PORT") or 8000)
if len(_cli_args) >= 1:
    if re.fullmatch(r"\d{2,5}", _cli_args[0] or ""):
        PORT = int(_cli_args[0])
    else:
        SHARE_DIR = os.path.abspath(_cli_args[0])
if len(_cli_args) >= 2 and re.fullmatch(r"\d{2,5}", _cli_args[1] or ""):
    PORT = int(_cli_args[1])

DATA_DIR = os.path.abspath(os.environ.get("LANCHAT_DATA") or _DEFAULT_DATA)
# A custom --data=... / third positional arg may override the data dir.
for _a in _cli_args:
    if _a.startswith("--data="):
        DATA_DIR = os.path.abspath(_a.split("=", 1)[1])
if len(_cli_args) >= 3 and not _cli_args[2].startswith("--"):
    DATA_DIR = os.path.abspath(_cli_args[2])

os.makedirs(SHARE_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# State files — private app state lives in data/, never in the public share/
# ---------------------------------------------------------------------------
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
CONVS_FILE = os.path.join(DATA_DIR, "convs.json")
MESSAGES_FILE = os.path.join(DATA_DIR, "messages.json")
LEGACY_HISTORY = os.path.join(DATA_DIR, "history.json")

# Map of new file -> legacy dotfile names to auto-migrate on first run
# (supports upgrade from the old single-folder layout).
_LEGACY_MAP = {
    USERS_FILE: [".lanchat_users.json", "users.json"],
    SESSIONS_FILE: [".lanchat_sessions.json", "sessions.json"],
    CONVS_FILE: [".lanchat_convs.json", "convs.json"],
    MESSAGES_FILE: [".lanchat_messages.json", "messages.json", ".lanchat_history.json"],
}


def _migrate_file(new_path, legacy_names):
    """Move a legacy state file into its new data/ location (first run only)."""
    if os.path.isfile(new_path):
        return
    candidates = []
    for base in (SHARE_DIR, APP_ROOT, os.getcwd(), DATA_DIR):
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
    for base in (SHARE_DIR, APP_ROOT, os.getcwd(), DATA_DIR):
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


for _new, _olds in _LEGACY_MAP.items():
    _migrate_file(_new, _olds)

MAX_MESSAGES_PER_CONV = 500
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
GENERAL_CONV = "group:general"

# Profile pictures and private chat attachments live under data/
# (never inside the public share/ folder).
AVATARS_DIR = os.path.join(DATA_DIR, "avatars")
CHATFILES_DIR = os.path.join(DATA_DIR, "chatfiles")
ALLOWED_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB
READ_MORE_CHARS = 400
IMG_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
os.makedirs(AVATARS_DIR, exist_ok=True)
os.makedirs(CHATFILES_DIR, exist_ok=True)
_migrate_dir(AVATARS_DIR, [".lanchat_avatars", "avatars"])
_migrate_dir(CHATFILES_DIR, [".lanchat_chatfiles", "chatfiles"])

_lock = threading.RLock()
_users = {}      # username(lower) -> {username,name,email,pw,salt,role,reads,created,avatar?}
_sessions = {}   # token -> {user: username(lower), created}
_convs = {}      # conv id -> {id,type,name?,members?,all?,created}
_messages = {}   # conv id -> [message, ...]
_next_id = itertools.count(1)


def looks_like_image(data, ext):
    """Check real magic bytes so nobody can upload an HTML/JS file as an avatar."""
    if ext in (".jpg", ".jpeg"):
        return data[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def save_avatar(key, data, ext):
    """Store a new picture for a user, replacing (and deleting) the old one."""
    fname = f"{key}_{secrets.token_hex(4)}{ext}"
    path = os.path.join(AVATARS_DIR, fname)
    old = _users[key].get("avatar")
    with open(path, "wb") as f:
        f.write(data)
    with _lock:
        _users[key]["avatar"] = fname
        _save_users()
    if old and old != fname:
        try:
            os.remove(os.path.join(AVATARS_DIR, os.path.basename(old)))
        except OSError:
            pass
    return fname


def remove_avatar(key):
    with _lock:
        old = _users.get(key, {}).get("avatar")
        if not old:
            return False
        _users[key].pop("avatar", None)
        _save_users()
    try:
        os.remove(os.path.join(AVATARS_DIR, os.path.basename(old)))
    except OSError:
        pass
    return True


def avatar_of(key):
    u = _users.get(key)
    return u.get("avatar") if u else None


def update_profile(key, name, email):
    name = (name or "").strip()[:40]
    email = (email or "").strip()[:120]
    if not name:
        return False, "Display name can't be empty"
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        return False, "Enter a valid email address"
    with _lock:
        user = _users.get(key)
        if not user:
            return False, "No such user"
        if any(k != key and u.get("email", "").lower() == email.lower()
               for k, u in _users.items()):
            return False, "That email address is already in use"
        user["name"] = name
        user["email"] = email
        _save_users()
        return True, user


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def human_size(n):
    if n is None:
        return ""
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


ICONS = {
    ".pdf": "📕", ".doc": "📄", ".docx": "📄", ".txt": "📄", ".md": "📄",
    ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️", ".gif": "🖼️", ".svg": "🖼️", ".webp": "🖼️",
    ".mp4": "🎬", ".mov": "🎬", ".mkv": "🎬",
    ".mp3": "🎵", ".wav": "🎵",
    ".zip": "🗜️", ".rar": "🗜️", ".tar": "🗜️", ".gz": "🗜️",
    ".py": "🐍", ".js": "📜", ".html": "🌐", ".css": "🎨",
    ".xlsx": "📊", ".xls": "📊", ".csv": "📊",
    ".ppt": "📽️", ".pptx": "📽️",
}


def icon_for(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ICONS.get(ext, "📄")


def safe_join(base, name):
    """Prevent path traversal — only allow files directly inside base."""
    name = os.path.basename(unquote(name))
    path = os.path.normpath(os.path.join(base, name))
    if not path.startswith(os.path.abspath(base) + os.sep) and path != os.path.abspath(base):
        raise ValueError("invalid path")
    return path


def unique_dest(filename):
    """Pick a non-colliding destination path inside SHARE_DIR."""
    dest = safe_join(SHARE_DIR, filename)
    base, ext = os.path.splitext(dest)
    counter = 1
    while os.path.exists(dest):
        dest = f"{base} ({counter}){ext}"
        counter += 1
    return dest


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


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
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return salt, digest.hex()


def verify_password(password, salt, good):
    try:
        _, digest = hash_password(password, salt)
        return hmac.compare_digest(digest, good)
    except Exception:
        return False


def create_user(username, display, email, password, role="user"):
    """Returns (ok, user_or_error_message). Only the owner calls this."""
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,24}", username):
        return False, "Username must be 3-24 characters (letters, digits, _ . -)"
    key = username.lower()
    display = (display or "").strip() or username
    if len(display) > 40:
        display = display[:40]
    email = (email or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        return False, "Enter a valid email address"
    if len(password or "") < 6:
        return False, "Password must be at least 6 characters"
    with _lock:
        if any(u.get("email", "").lower() == email.lower() for u in _users.values()):
            return False, "That email address is already registered"
        if key in _users:
            return False, "That username is already taken"
        salt, digest = hash_password(password)
        _users[key] = {
            "username": username,
            "name": display,
            "email": email,
            "pw": digest,
            "salt": salt,
            "role": role,
            "reads": {},
            "created": time.time(),
        }
        _save_users()
        return True, _users[key]


def delete_user(username):
    key = (username or "").lower()
    with _lock:
        u = _users.get(key)
        if not u:
            return False, "No such user"
        if u.get("role") == "owner":
            return False, "The owner account can't be deleted"
        del _users[key]
        avatar = u.get("avatar")
        for conv in _convs.values():
            if conv.get("members") and key in conv["members"]:
                conv["members"] = [m for m in conv["members"] if m != key]
        _save_convs()
        _save_users()
        for tok in [t for t, s in _sessions.items() if s.get("user") == key]:
            _sessions.pop(tok, None)
        _save_sessions()
    if avatar:
        try:
            os.remove(os.path.join(AVATARS_DIR, os.path.basename(avatar)))
        except OSError:
            pass
    return True, "deleted"


def drop_user_sessions(key, keep_token=None):
    """Sign out every session of one user (optionally keeping one token)."""
    with _lock:
        for tok in list(_sessions):
            if tok == keep_token:
                continue
            if _sessions.get(tok, {}).get("user") == key:
                _sessions.pop(tok, None)
        _save_sessions()


def change_own_password(key, current_password, new_password, keep_token):
    """Self-service password change: must know the current one. Keeps the
    caller signed in and signs their other devices out."""
    with _lock:
        user = _users.get(key)
        if not user:
            return False, "No such user"
        if not verify_password(current_password or "", user["salt"], user["pw"]):
            return False, "Your current password is incorrect"
        ok, err = set_password(user["username"], new_password)
        if not ok:
            return False, err
    drop_user_sessions(key, keep_token)
    return True, "ok"


def set_password(username, new_password):
    key = (username or "").lower()
    if len(new_password or "") < 6:
        return False, "Password must be at least 6 characters"
    with _lock:
        if key not in _users:
            return False, "No such user"
        salt, digest = hash_password(new_password)
        _users[key]["salt"] = salt
        _users[key]["pw"] = digest
        _save_users()
        return True, "ok"


def authenticate(identifier, password):
    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    with _lock:
        user = _users.get(ident)
        if user is None:
            user = next((u for u in _users.values() if u.get("email", "").lower() == ident), None)
        if not user:
            return None
        if not verify_password(password or "", user["salt"], user["pw"]):
            return None
        return user


def public_user(u):
    if not u:
        return None
    return {
        "username": u["username"],
        "name": u["name"],
        "email": u["email"],
        "role": u.get("role", "user"),
        "avatar": u.get("avatar"),
        "created": u.get("created", 0),
    }


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
def dm_id(a, b):
    return "dm:" + ":".join(sorted([a.lower(), b.lower()]))


def notes_id(key):
    return "notes:" + key.lower()


def ensure_notes(key):
    """Personal notes — a private conversation only `key` can see."""
    key = key.lower()
    cid = notes_id(key)
    with _lock:
        if cid not in _convs:
            _convs[cid] = {"id": cid, "type": "notes", "name": "My Notes",
                           "members": [key], "created": time.time()}
            _save_convs()
        else:
            conv = _convs[cid]
            if key not in (conv.get("members") or []):
                conv["members"] = [key]
                _save_convs()
    return cid


def hidden_ids(key, conv_id):
    u = _users.get(key) or {}
    h = u.get("hidden") or {}
    try:
        return set(h.get(conv_id) or [])
    except Exception:
        return set()


def is_pinned_conv(key, conv_id):
    u = _users.get(key) or {}
    try:
        return conv_id in (u.get("pinned_convs") or [])
    except Exception:
        return False


def set_pinned_conv(key, conv_id, pinned):
    with _lock:
        u = _users.get(key)
        if not u:
            return False, "No such user"
        pins = u.setdefault("pinned_convs", [])
        if pinned and conv_id not in pins:
            pins.append(conv_id)
        elif not pinned and conv_id in pins:
            u["pinned_convs"] = [c for c in pins if c != conv_id]
        _save_users()
        return True, "ok"


def is_hidden_conv(key, conv_id):
    u = _users.get(key) or {}
    try:
        return conv_id in (u.get("hidden_convs") or [])
    except Exception:
        return False


def unhide_conv_for_members(conv_id, member_keys):
    with _lock:
        changed = False
        for mk in member_keys:
            u = _users.get(mk)
            if not u:
                continue
            hc = u.get("hidden_convs")
            if hc and conv_id in hc:
                try:
                    u["hidden_convs"] = [c for c in hc if c != conv_id]
                    changed = True
                except Exception:
                    pass
        if changed:
            _save_users()


def conv_member_keys(conv):
    if conv.get("all"):
        return sorted(_users.keys())
    return list(conv.get("members") or [])


def can_see(key, conv):
    if conv.get("all"):
        return True
    return key in (conv.get("members") or [])


def get_or_create_dm(a, b):
    a_key, b_key = a.lower(), b.lower()
    cid = dm_id(a_key, b_key)
    with _lock:
        if cid not in _convs:
            _convs[cid] = {"id": cid, "type": "dm", "members": sorted([a_key, b_key]), "created": time.time()}
            _save_convs()
        return cid


def create_group(name, member_keys, creator_key):
    name = (name or "").strip()[:40] or "New group"
    members = sorted({m.lower() for m in member_keys} | {creator_key})
    members = [m for m in members if m in _users]
    if creator_key not in members:
        members.append(creator_key)
    cid = "g" + secrets.token_hex(4)
    with _lock:
        _convs[cid] = {"id": cid, "type": "group", "name": name, "members": members, "created": time.time()}
        _save_convs()
    return cid


def ensure_general():
    with _lock:
        if GENERAL_CONV not in _convs:
            _convs[GENERAL_CONV] = {
                "id": GENERAL_CONV, "type": "group", "name": "General",
                "all": True, "created": time.time(),
            }
            _save_convs()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def add_message(conv_id, username_key, text, msg_type="text", file=None, size=None,
                reply=None, private=False, stored=None):
    with _lock:
        msg = {
            "id": next(_next_id),
            "conv": conv_id,
            "user": username_key,
            "name": display_name(username_key),
            "text": text,
            "ts": time.time(),
            "type": msg_type,
        }
        if reply:
            msg["reply"] = reply
        if msg_type == "file":
            msg["file"] = file
            msg["size"] = size
            msg["size_human"] = human_size(size)
            msg["icon"] = icon_for(file)
            if private:
                msg["private"] = True
            if stored:
                msg["stored"] = stored
        bucket = _messages.setdefault(conv_id, [])
        bucket.append(msg)
        if len(bucket) > MAX_MESSAGES_PER_CONV:
            # If we trim old file messages, also remove their private stored files.
            trimmed = bucket[: len(bucket) - MAX_MESSAGES_PER_CONV]
            del bucket[: len(bucket) - MAX_MESSAGES_PER_CONV]
            for tm in trimmed:
                if tm.get("private") and tm.get("stored"):
                    try:
                        os.remove(os.path.join(CHATFILES_DIR, os.path.basename(tm["stored"])))
                    except OSError:
                        pass
        # A new message un-hides the conversation for every member
        # (Messenger behaviour: a deleted chat reappears on new activity).
        try:
            conv = _convs.get(conv_id)
            members = conv_member_keys(conv) if conv else []
        except Exception:
            members = []
        for mk in members:
            u = _users.get(mk)
            if u and conv_id in (u.get("hidden_convs") or []):
                try:
                    u["hidden_convs"] = [c for c in u["hidden_convs"] if c != conv_id]
                except Exception:
                    pass
        _save_messages()
        _save_users()
        return msg


def find_message(conv_id, msg_id):
    for m in _messages.get(conv_id, []):
        if m.get("id") == msg_id:
            return m
    return None


def delete_message_everyone(conv_id, msg_id, requester_key, is_owner):
    with _lock:
        bucket = _messages.get(conv_id, [])
        idx = next((i for i, m in enumerate(bucket) if m.get("id") == msg_id), None)
        if idx is None:
            return False, "Message not found"
        msg = bucket[idx]
        if msg.get("user") != requester_key and not is_owner:
            return False, "You can only unsend your own messages"
        if msg.get("private") and msg.get("stored"):
            try:
                os.remove(os.path.join(CHATFILES_DIR, os.path.basename(msg["stored"])))
            except OSError:
                pass
        del bucket[idx]
        _save_messages()
        return True, "ok"


def delete_message_for_me(key, conv_id, msg_id):
    with _lock:
        u = _users.get(key)
        if not u:
            return False, "No such user"
        if not find_message(conv_id, msg_id):
            return False, "Message not found"
        h = u.setdefault("hidden", {})
        lst = h.setdefault(conv_id, [])
        if msg_id not in lst:
            lst.append(int(msg_id))
        _save_users()
        return True, "ok"


def hide_conv_for_me(key, conv_id):
    with _lock:
        u = _users.get(key)
        if not u:
            return False, "No such user"
        hc = u.setdefault("hidden_convs", [])
        if conv_id not in hc:
            hc.append(conv_id)
        # FIX (bug #6): deleting a chat for me must also hide all current
        # messages for that user, otherwise when the chat reappears on new
        # activity all the old history pops back up.
        # We record every existing message id in the per-user "hidden" map
        # so only messages arriving AFTER the delete are visible.
        try:
            h = u.setdefault("hidden", {})
            lst = h.setdefault(conv_id, [])
            existing = {int(x) for x in lst} if isinstance(lst, list) else set()
            for m in _messages.get(conv_id, []):
                mid = m.get("id")
                if isinstance(mid, int) and mid not in existing:
                    lst.append(mid)
                    existing.add(mid)
            # Unpin when the chat is deleted for me.
            if conv_id in (u.get("pinned_convs") or []):
                u["pinned_convs"] = [c for c in u["pinned_convs"] if c != conv_id]
        except Exception:
            pass
        # Clearing personal notes also wipes its messages for you.
        if conv_id == notes_id(key):
            _messages[conv_id] = []
            # Notes were wiped, so drop their hidden markers too.
            try:
                if u.get("hidden") and conv_id in u["hidden"]:
                    u["hidden"].pop(conv_id, None)
            except Exception:
                pass
            _save_messages()
        _save_users()
        return True, "ok"


def delete_conv_everywhere(conv_id, requester_key, is_owner):
    with _lock:
        conv = _convs.get(conv_id)
        if not conv:
            return False, "No such conversation"
        if conv.get("type") == "notes" or conv.get("id") == GENERAL_CONV:
            return False, "That conversation can't be deleted"
        if conv.get("type") != "group":
            return False, "Only group chats can be deleted for everyone (DMs: Delete for me)"
        if not is_owner and requester_key not in (conv.get("members") or []):
            return False, "Not a member"
        if not is_owner:
            return False, "Only the owner can delete a group for everyone"
        # Remove private stored files belonging to this conversation.
        for m in _messages.get(conv_id, []):
            if m.get("private") and m.get("stored"):
                try:
                    os.remove(os.path.join(CHATFILES_DIR, os.path.basename(m["stored"])))
                except OSError:
                    pass
        _convs.pop(conv_id, None)
        _messages.pop(conv_id, None)
        for u in _users.values():
            try:
                if conv_id in (u.get("hidden_convs") or []):
                    u["hidden_convs"] = [c for c in u["hidden_convs"] if c != conv_id]
                if conv_id in (u.get("pinned_convs") or []):
                    u["pinned_convs"] = [c for c in u["pinned_convs"] if c != conv_id]
                if u.get("hidden") and conv_id in u["hidden"]:
                    u["hidden"].pop(conv_id, None)
                if u.get("reads") and conv_id in u["reads"]:
                    u["reads"].pop(conv_id, None)
            except Exception:
                pass
        _save_convs()
        _save_messages()
        _save_users()
        return True, "ok"


def get_messages(conv_id, since_id, requester_key=None):
    with _lock:
        hidden = hidden_ids(requester_key, conv_id) if requester_key else set()
        out = []
        for m in _messages.get(conv_id, []):
            if m.get("id", 0) <= since_id:
                continue
            if m.get("id") in hidden:
                continue
            mm = dict(m)
            u = _users.get(m.get("user"))
            if u:
                # Resolve the current display name/picture, so a profile
                # update shows up everywhere (old messages included).
                mm["name"] = u["name"]
                mm["avatar"] = u.get("avatar")
            out.append(mm)
        return out


def mark_read(key, conv_id, last_id):
    with _lock:
        u = _users.get(key)
        if not u:
            return
        reads = u.setdefault("reads", {})
        if last_id > reads.get(conv_id, 0):
            reads[conv_id] = int(last_id)
            _save_users()


def conv_payload(key, conv):
    cid = conv["id"]
    msgs = _messages.get(cid, [])
    hid = hidden_ids(key, cid)
    visible = [m for m in msgs if m.get("id") not in hid]
    last = dict(visible[-1]) if visible else None
    if last:
        last_user = _users.get(last.get("user"))
        if last_user:
            last["name"] = last_user["name"]
            last["avatar"] = last_user.get("avatar")
        if len(last.get("text", "")) > 220:
            last["text"] = last["text"][:220] + "…"
    read_id = _users.get(key, {}).get("reads", {}).get(cid, 0)
    unread = sum(1 for m in visible if m.get("id", 0) > read_id)
    members = conv_member_keys(conv)
    names = [display_name(m) for m in members if m in _users]

    subtitle = ""
    avatar = None
    ctype = conv.get("type", "group")
    if ctype == "notes":
        title = "My Notes"
        subtitle = "Personal · Only you"
    elif ctype == "dm":
        other = [m for m in members if m != key]
        if other and other[0] in _users:
            title = _users[other[0]]["name"]
            subtitle = _users[other[0]]["email"]
            avatar = _users[other[0]].get("avatar")
        else:
            title = "Conversation"
    else:
        title = conv.get("name") or "Group"

    return {
        "id": cid,
        "type": ctype,
        "title": title,
        "subtitle": subtitle,
        "avatar": avatar,
        "members": names,
        "unread": unread,
        "last": last,
        "created": conv.get("created", 0),
        "pinned": is_pinned_conv(key, cid),
    }


def conversations_for(key):
    ensure_notes(key)
    out = []
    with _lock:
        for conv in _convs.values():
            if can_see(key, conv) and not is_hidden_conv(key, conv["id"]):
                out.append(conv_payload(key, conv))
    # Pinned chats first, then most-recent activity.
    out.sort(key=lambda c: (bool(c.get("pinned")),
                            c["last"]["ts"] if c["last"] else c["created"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Bootstrapping
# ---------------------------------------------------------------------------
def load_state():
    global _users, _sessions, _convs, _messages, _next_id

    raw_users = _load_json(USERS_FILE, {})
    _users = {k.lower(): v for k, v in raw_users.items() if isinstance(v, dict)} if isinstance(raw_users, dict) else {}

    now = time.time()
    raw_sessions = _load_json(SESSIONS_FILE, {})
    _sessions = {}
    if isinstance(raw_sessions, dict):
        for tok, s in raw_sessions.items():
            if isinstance(s, dict) and now - s.get("created", 0) < SESSION_TTL and s.get("user") in _users:
                _sessions[tok] = s

    raw_convs = _load_json(CONVS_FILE, {})
    _convs = {k: v for k, v in raw_convs.items() if isinstance(v, dict)} if isinstance(raw_convs, dict) else {}

    raw_msgs = _load_json(MESSAGES_FILE, {})
    _messages = {k: v for k, v in raw_msgs.items() if isinstance(v, list)} if isinstance(raw_msgs, dict) else {}

    max_id = 0
    for bucket in _messages.values():
        for m in bucket:
            if isinstance(m, dict):
                max_id = max(max_id, m.get("id", 0))
    _next_id = itertools.count(max_id + 1)

    # Migrate history from the original single-room version of this app.
    if not _messages:
        legacy = _load_json(LEGACY_HISTORY, [])
        if isinstance(legacy, list) and legacy:
            ensure_general()
            bucket = _messages.setdefault(GENERAL_CONV, [])
            for lm in legacy:
                if not isinstance(lm, dict):
                    continue
                lm = dict(lm)
                lm["id"] = next(_next_id)
                lm["conv"] = GENERAL_CONV
                lm.setdefault("name", lm.get("user", "Anonymous"))
                lm.setdefault("type", "text")
                bucket.append(lm)
            bucket.sort(key=lambda m: m.get("id", 0))
            _save_messages()

    ensure_general()


def bootstrap_owner():
    """Create the owner's account on the very first run."""
    with _lock:
        if _users:
            return None
        username = os.environ.get("LANCHAT_OWNER", "admin")
        email = os.environ.get("LANCHAT_OWNER_EMAIL", "owner@localhost")
        password = os.environ.get("LANCHAT_OWNER_PASS") or secrets.token_urlsafe(8)
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,24}", username):
            username = "admin"
        salt, digest = hash_password(password)
        _users[username.lower()] = {
            "username": username,
            "name": username,
            "email": email,
            "pw": digest,
            "salt": salt,
            "role": "owner",
            "reads": {},
            "created": time.time(),
        }
        _save_users()
        return {"username": username, "email": email, "password": password}


# ---------------------------------------------------------------------------
# HTML / UI
# ---------------------------------------------------------------------------
FILE_ROW_TEMPLATE = """<div class="fileRow">
  <span class="fIconBig">{icon}</span>
  <span class="fMeta">
    <a href="/download/{href}" download>{name}</a>
    <span class="fSizeLbl">{size}</span>
  </span>
  <a class="delLink" href="/delete/{href}" data-del="{name_attr}">Delete</a>
</div>"""

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link id="favicon" rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%92%AC%3C/text%3E%3C/svg%3E">
<title>LAN Share &amp; Messenger</title>
<style>
  :root {
    --bg-1: #0b0e14; --bg-2: #12172a;
    --card: rgba(255,255,255,0.045); --card-solid: #151926;
    --border: rgba(255,255,255,0.09);
    --text: #eef1f6; --muted: #8d94a6;
    --accent: #6c8bff; --accent-2: #9b6cff;
    --bubble-me-1: #6c8bff; --bubble-me-2: #8b6cff; --bubble-them: #1d2233;
    --danger: #ff6b6b; --success: #4ce0a6;
    --radius-lg: 18px; --radius-md: 14px; --radius-sm: 10px;
    --shadow: 0 8px 30px rgba(0,0,0,0.35);
    --head-bg: rgba(15,18,28,0.6); --form-bg: rgba(15,18,28,0.55);
    --auth-bg: rgba(21,25,38,0.94); --toast-bg: #1d2233;
    --input-bg: rgba(255,255,255,0.05);
  }
  :root[data-theme="light"] {
    --bg-1: #eef1f8; --bg-2: #e2e7f5;
    --card: rgba(255,255,255,0.85); --card-solid: #ffffff;
    --border: rgba(20,30,60,0.12);
    --text: #1a2138; --muted: #68718c;
    --accent: #4a6cf7; --accent-2: #8b5cf6;
    --bubble-me-1: #4a6cf7; --bubble-me-2: #8b5cf6; --bubble-them: #ffffff;
    --danger: #e5484d; --success: #0ca678;
    --shadow: 0 8px 30px rgba(30,40,80,0.12);
    --head-bg: rgba(255,255,255,0.85); --form-bg: rgba(255,255,255,0.85);
    --auth-bg: rgba(255,255,255,0.97); --toast-bg: #ffffff;
    --input-bg: rgba(20,30,60,0.05);
  }
  :root[data-theme="light"] body {
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(74,108,247,0.12), transparent),
      radial-gradient(900px 500px at 100% 0%, rgba(139,92,246,0.1), transparent),
      linear-gradient(180deg, var(--bg-1), var(--bg-2));
  }
  :root[data-theme="light"] .authCard { background: var(--auth-bg); }
  :root[data-theme="light"] .sidebar { background: rgba(255,255,255,0.75); }
  :root[data-theme="light"] .chatHead { background: var(--head-bg); }
  :root[data-theme="light"] #chatForm { background: var(--form-bg); }
  :root[data-theme="light"] #toast { background: var(--toast-bg); box-shadow: var(--shadow); }
  :root[data-theme="light"] .authCard input,
  :root[data-theme="light"] .card input,
  :root[data-theme="light"] .modalField input,
  :root[data-theme="light"] .sideSearch,
  :root[data-theme="light"] #chatInput { background: var(--input-bg); }
  :root[data-theme="light"] .them .bubble { box-shadow: 0 1px 4px rgba(30,40,80,0.1); }
  :root[data-theme="light"] ::-webkit-scrollbar-thumb { background: rgba(20,30,60,0.18); }
  :root[data-theme="light"] ::-webkit-scrollbar-thumb:hover { background: rgba(20,30,60,0.3); }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.14); border-radius: 8px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.24); }

  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text); overflow: hidden;
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(108,139,255,0.16), transparent),
      radial-gradient(900px 500px at 100% 0%, rgba(155,108,255,0.14), transparent),
      linear-gradient(180deg, var(--bg-1), var(--bg-2));
    background-attachment: fixed;
  }
  button, input, textarea { font-family: inherit; color: var(--text); }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  /* ---------------- Sign in ---------------- */
  #authScreen { height: 100vh; display: none; align-items: center; justify-content: center; padding: 20px; }
  .authCard {
    width: 100%; max-width: 390px; text-align: center;
    background: var(--auth-bg); border: 1px solid var(--border);
    border-radius: 24px; padding: 32px 28px; box-shadow: var(--shadow); backdrop-filter: blur(16px);
    animation: fadeIn .3s ease;
  }
  .authIcon {
    width: 56px; height: 56px; margin: 0 auto 14px; border-radius: 17px;
    display: flex; align-items: center; justify-content: center; font-size: 1.7rem;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    box-shadow: 0 6px 20px rgba(108,139,255,0.4);
  }
  .authCard h1 { margin: 0 0 6px; font-size: 1.3rem; font-weight: 650; }
  .authSub { margin: 0 0 18px; color: var(--muted); font-size: 0.85rem; }
  .authCard label { display: block; text-align: left; font-size: 0.74rem; color: var(--muted); margin: 12px 0 6px; }
  .authCard input {
    width: 100%; padding: 11px 14px; border-radius: 12px; border: 1px solid var(--border);
    background: var(--input-bg); font-size: 0.92rem; outline: none; transition: border-color .15s;
  }
  .authCard input:focus { border-color: var(--accent); }
  .authErr { min-height: 18px; color: var(--danger); font-size: 0.78rem; margin-top: 10px; }
  .authNote { margin: 16px 0 0; color: var(--muted); font-size: 0.73rem; line-height: 1.55; }
  .authNote code { background: rgba(255,255,255,0.07); padding: 1px 5px; border-radius: 5px; }

  .btn {
    display: inline-block; padding: 10px 20px; border-radius: 999px; border: none; cursor: pointer;
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff;
    font-size: 0.86rem; font-weight: 600; box-shadow: 0 4px 14px rgba(108,139,255,0.3);
    transition: transform .15s ease;
  }
  .btn:hover { transform: translateY(-1px); }
  .btn.block { width: 100%; padding: 12px; }

  /* ---------------- App shell ---------------- */
  #app { display: none; height: 100vh; }
  #app.on { display: grid; grid-template-columns: 322px minmax(0, 1fr); }

  .sidebar {
    display: flex; flex-direction: column; min-height: 0;
    background: rgba(13,16,26,0.72); border-right: 1px solid var(--border); backdrop-filter: blur(16px);
  }
  .sideTop { display: flex; align-items: center; gap: 10px; padding: 14px 14px 10px; }
  .appIcon {
    width: 38px; height: 38px; border-radius: 12px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-size: 1.2rem;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    box-shadow: 0 4px 16px rgba(108,139,255,0.35);
  }
  .sideTitle { flex: 1; min-width: 0; font-weight: 650; font-size: 0.98rem; line-height: 1.2; }
  .sideSub { font-size: 0.7rem; color: var(--muted); font-weight: 400; margin-top: 2px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .iconBtn {
    width: 32px; height: 32px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--card); color: var(--text); cursor: pointer; font-size: 0.9rem;
    flex-shrink: 0; transition: background .15s;
  }
  .iconBtn:hover { background: rgba(255,255,255,0.1); }

  .meChip {
    display: flex; align-items: center; gap: 10px; margin: 0 12px 10px; padding: 9px 11px;
    border-radius: 14px; background: var(--card); border: 1px solid var(--border);
  }
  .meMeta { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .meMeta b { font-size: 0.86rem; }
  .meMeta span { font-size: 0.7rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge { font-size: 0.6rem; font-weight: 700; padding: 3px 7px; border-radius: 999px; letter-spacing: .05em; }
  .badge.owner { background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff; }
  .badge.role { background: rgba(255,255,255,0.09); color: var(--muted); }

  .nav { display: flex; gap: 6px; padding: 0 12px 10px; }
  .navBtn {
    flex: 1 1 0; min-width: 0; padding: 8px 2px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--card); color: var(--muted); font-size: 0.72rem; font-weight: 600;
    cursor: pointer; transition: all .15s; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .navBtn:hover { color: var(--text); }
  .navBtn.active {
    color: #fff; background: linear-gradient(135deg, var(--accent), var(--accent-2));
    border-color: transparent; box-shadow: 0 4px 14px rgba(108,139,255,0.3);
  }

  .chatsSide { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  #notesPin { padding: 0 8px 6px; }
  #notesPin:empty { display: none; }
  .sideSearch {
    margin: 0 12px 8px; padding: 9px 13px; border-radius: 11px; border: 1px solid var(--border);
    background: var(--input-bg); font-size: 0.84rem; outline: none; transition: border-color .15s;
  }
  .sideSearch:focus { border-color: var(--accent); }
  .convActions { display: flex; gap: 6px; padding: 0 12px 8px; }
  .miniBtn {
    flex: 1; padding: 8px 6px; border-radius: 10px; cursor: pointer; font-size: 0.75rem; font-weight: 600;
    border: 1px solid rgba(108,139,255,0.4); background: rgba(108,139,255,0.14); color: var(--text);
    transition: background .15s;
  }
  .miniBtn:hover { background: rgba(108,139,255,0.28); }
  .miniBtn.alt { border-color: rgba(155,108,255,0.4); background: rgba(155,108,255,0.14); }
  .miniBtn.alt:hover { background: rgba(155,108,255,0.28); }

  .convList { flex: 1; min-height: 0; overflow-y: auto; padding: 2px 8px 10px; display: flex; flex-direction: column; gap: 3px; }
  .convItem {
    display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 13px;
    cursor: pointer; border: 1px solid transparent; transition: background .15s;
  }
  .convItem:hover { background: rgba(255,255,255,0.055); }
  .convItem.active { background: rgba(108,139,255,0.16); border-color: rgba(108,139,255,0.35); }
  .convBody { flex: 1; min-width: 0; }
  .convTop { display: flex; align-items: center; gap: 6px; }
  .convTop b { flex: 1; min-width: 0; font-size: 0.87rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .convTop .t { font-size: 0.66rem; color: var(--muted); flex-shrink: 0; }
  .convPrev { font-size: 0.75rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
  .unread {
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff;
    font-size: 0.65rem; font-weight: 700; min-width: 19px; height: 19px; padding: 0 5px;
    border-radius: 999px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .avatar {
    width: 34px; height: 34px; border-radius: 50%; flex-shrink: 0; position: relative; overflow: hidden;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.78rem; font-weight: 700; color: #fff;
  }
  .avatar.groupAv { background: linear-gradient(135deg, #4b5573, #6b7391); font-size: 1rem; }
  .avatarImg { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
  .sideFoot {
    padding: 9px 14px 12px; font-size: 0.66rem; color: var(--muted);
    border-top: 1px solid var(--border); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  .main { display: flex; flex-direction: column; min-width: 0; min-height: 0; position: relative; }
  .view { display: none; flex-direction: column; flex: 1; min-height: 0; }
  .view.active { display: flex; animation: fadeIn .2s ease; }

  /* ---------------- Chats ---------------- */
  .emptyState { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
                 gap: 10px; color: var(--muted); text-align: center; padding: 30px; }
  .emptyState .big { font-size: 2.4rem; }
  .emptyState b { color: var(--text); font-size: 1rem; }
  .emptyState p { margin: 0; font-size: 0.85rem; max-width: 320px; line-height: 1.5; }

  .chatPane { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .chatHead {
    display: flex; align-items: center; gap: 11px; padding: 11px 16px;
    border-bottom: 1px solid var(--border); background: var(--head-bg); backdrop-filter: blur(14px);
  }
  .backBtn { display: none; width: 32px; height: 32px; border-radius: 10px; border: 1px solid var(--border);
             background: var(--card); color: var(--text); cursor: pointer; font-size: 1rem; }
  .headMeta { min-width: 0; }
  .headTitle { font-size: 0.97rem; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .headSub { font-size: 0.72rem; color: var(--muted); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  #chatBox { flex: 1; min-height: 0; overflow-y: auto; padding: 18px; display: flex; flex-direction: column; gap: 13px; }
  .bubble-row { display: flex; gap: 9px; max-width: 82%; animation: slideIn .2s ease; }
  @keyframes slideIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .bubble-row.me { align-self: flex-end; flex-direction: row-reverse; }
  .bubble-row.them { align-self: flex-start; }
  .bcol { display: flex; flex-direction: column; min-width: 0; }
  .bubble-row.me .bcol { align-items: flex-end; }
  .bubble-row.them .bcol { align-items: flex-start; }
  .msgAvatar { width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0; position: relative; overflow: hidden;
               display: flex; align-items: center; justify-content: center; font-size: 0.72rem; font-weight: 700;
               color: #fff; margin-top: 16px; }
  .meta { font-size: 0.67rem; color: var(--muted); margin: 0 6px 3px; }
  .bubble {
    padding: 9px 13px; border-radius: 16px; font-size: 0.9rem; line-height: 1.45;
    word-wrap: break-word; white-space: pre-wrap; max-width: 100%;
  }
  .me .bubble { background: linear-gradient(135deg, var(--bubble-me-1), var(--bubble-me-2)); color: #fff; border-bottom-right-radius: 5px; }
  .them .bubble { background: var(--bubble-them); border: 1px solid var(--border); border-bottom-left-radius: 5px; }
  .quote {
    font-size: 0.72rem; margin: 0 6px 4px; padding: 5px 9px; max-width: 260px; overflow: hidden;
    border-left: 3px solid var(--accent); background: rgba(108,139,255,0.12); border-radius: 6px;
  }
  .quote b { display: block; font-size: 0.68rem; color: var(--accent); }
  .quote span { display: block; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .msgActions { display: flex; gap: 12px; margin: 4px 6px 0; opacity: 0; transition: opacity .15s ease; }
  .bubble-row:hover .msgActions { opacity: 1; }
  .linkBtn { background: none; border: none; color: var(--muted); font-size: 0.7rem; font-weight: 600; cursor: pointer; padding: 2px 0; }
  .linkBtn:hover { color: var(--accent); }
  .linkBtn.copied { color: var(--success); }
  .linkBtn.danger:hover { color: var(--danger); }
  .bubbleTxt { white-space: pre-wrap; word-wrap: break-word; }
  .bubbleTxt.clamped { display: -webkit-box; -webkit-line-clamp: 6; -webkit-box-orient: vertical; overflow: hidden; }
  .readMoreBtn { background: none; border: none; cursor: pointer; font-size: 0.75rem; font-weight: 700;
                 padding: 4px 0 0; opacity: 0.9; color: inherit; text-decoration: underline; }
  .me .readMoreBtn { color: #fff; }
  .them .readMoreBtn { color: var(--accent); }
  .headActions { margin-left: auto; display: flex; gap: 6px; flex-shrink: 0; }
  .avatar.notesAv { background: linear-gradient(135deg, #e0a84c, #ff6c9b) !important; font-size: 1rem; }

  .fileCard {
    display: flex; align-items: center; gap: 10px; padding: 9px 11px; min-width: 210px;
    background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.06); border-radius: var(--radius-sm);
  }
  .me .fileCard { background: rgba(255,255,255,0.18); border-color: rgba(255,255,255,0.12); }
  .fileCard .fIcon { font-size: 1.6rem; }
  .fileCard .fInfo { display: flex; flex-direction: column; overflow: hidden; }
  .fileCard .fName { font-size: 0.87rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
  .fileCard .fSize { font-size: 0.71rem; opacity: 0.75; }
  .fileCard a { color: inherit; text-decoration: none; display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
  .filePreview { max-width: 220px; max-height: 220px; border-radius: var(--radius-sm); margin-top: 6px; display: block;
                 box-shadow: 0 4px 16px rgba(0,0,0,0.3); }

  #replyBanner {
    display: none; align-items: center; justify-content: space-between; gap: 8px; margin: 0 16px;
    background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--accent);
    border-radius: var(--radius-sm); padding: 8px 12px; font-size: 0.78rem; color: var(--muted);
  }
  #replyBanner b { color: var(--text); }
  #replyBanner .cancelReply { cursor: pointer; color: var(--muted); font-size: 1rem; line-height: 1; padding: 0 4px; }
  #replyBanner .cancelReply:hover { color: var(--danger); }

  #chatForm { display: flex; gap: 8px; align-items: flex-end; padding: 10px 16px 16px;
              border-top: 1px solid var(--border); background: var(--form-bg); }
  #chatInput {
    flex: 1; padding: 12px 16px; border-radius: 20px; border: 1px solid var(--border);
    background: var(--input-bg); font-size: 0.92rem; outline: none; resize: none;
    max-height: 120px; min-height: 44px; line-height: 1.35; transition: border-color .15s;
  }
  #chatInput:focus { border-color: var(--accent); }
  #attachBtn, #sendBtn {
    padding: 11px 18px; border-radius: 20px; border: none; cursor: pointer; font-size: 0.9rem;
    flex-shrink: 0; font-weight: 600; color: #fff; transition: transform .15s ease;
  }
  #sendBtn { background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 4px 14px rgba(108,139,255,0.3); }
  #attachBtn { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 11px 14px; }
  #attachBtn:hover, #sendBtn:hover { transform: translateY(-1px); }
  #chatFileInput { display: none; }

  /* ---------------- Files / Admin ---------------- */
  .scrollArea { flex: 1; min-height: 0; overflow-y: auto; padding: 22px 22px 30px; }
  .vTitle { margin: 0 0 14px; font-size: 1.05rem; font-weight: 650; }
  .pathLbl { font-size: 0.72rem; color: var(--muted); margin: -8px 0 16px; word-break: break-all; }
  .dropzone {
    border: 1.5px dashed var(--border); border-radius: var(--radius-lg); padding: 34px 16px;
    text-align: center; color: var(--muted); cursor: pointer; transition: all .2s;
    background: var(--card); backdrop-filter: blur(10px); display: block;
  }
  .dropzone.drag { border-color: var(--accent); color: var(--text); background: rgba(108,139,255,0.08); }
  .dropzone input { display: none; }
  .dzBtn { display: inline-block; margin-top: 12px; padding: 9px 20px; border-radius: 999px;
           background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff;
           font-size: 0.86rem; font-weight: 600; }
  #progress-wrap { margin-top: 14px; display: none; }
  #progress-bar { height: 6px; background: rgba(255,255,255,0.08); border-radius: 3px; overflow: hidden; }
  #progress-fill { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); transition: width .1s; }

  .fileList { margin-top: 22px; display: flex; flex-direction: column; gap: 8px; }
  .fileRow {
    display: flex; align-items: center; gap: 12px; padding: 11px 14px; border-radius: var(--radius-md);
    background: var(--card); border: 1px solid var(--border); backdrop-filter: blur(10px);
  }
  .fileRow:hover { background: rgba(255,255,255,0.075); }
  .fileRow .fIconBig { font-size: 1.4rem; flex-shrink: 0; }
  .fileRow .fMeta { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .fileRow .fMeta a { color: var(--text); text-decoration: none; font-size: 0.92rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fileRow .fMeta a:hover { color: var(--accent); }
  .fileRow .fSizeLbl { color: var(--muted); font-size: 0.74rem; margin-top: 1px; }
  .fileRow .delLink { color: var(--muted); text-decoration: none; font-size: 0.78rem; padding: 5px 10px;
                      border-radius: 999px; transition: all .15s; flex-shrink: 0; }
  .fileRow .delLink:hover { color: var(--danger); background: rgba(255,107,107,0.1); }
  .empty { color: var(--muted); text-align: center; padding: 26px; font-size: 0.88rem; }

  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 16px; backdrop-filter: blur(10px); margin-bottom: 16px;
  }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
  .card label { display: block; font-size: 0.72rem; color: var(--muted); margin-bottom: 5px; }
  .card input {
    width: 100%; padding: 10px 12px; border-radius: 11px; border: 1px solid var(--border);
    background: var(--input-bg); font-size: 0.87rem; outline: none; transition: border-color .15s;
  }
  .card input:focus { border-color: var(--accent); }
  .hint { font-size: 0.73rem; color: var(--muted); margin: 10px 0 0; line-height: 1.5; }
  .formMsg { font-size: 0.78rem; color: var(--danger); min-height: 16px; margin-top: 8px; }
  .formMsg.ok { color: var(--success); }

  /* --- Profile --- */
  .profRow { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
  .profAvatar {
    width: 88px; height: 88px; border-radius: 50%; position: relative; overflow: hidden; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-size: 1.9rem; font-weight: 700; color: #fff;
    border: 2px solid var(--border); box-shadow: var(--shadow);
  }
  .profAvatarBtns { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
  .fileBtn {
    display: inline-block; padding: 8px 16px; border-radius: 999px; cursor: pointer;
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff;
    font-size: 0.8rem; font-weight: 600; margin-bottom: 0;
  }
  .fileBtn input { display: none; }
  .card input[readonly] { opacity: 0.55; cursor: default; }
  .card input[readonly]:focus { border-color: var(--border); }
  .profMeta { font-size: 0.78rem; color: var(--muted); }
  .profMeta b { color: var(--text); }
  .userTable { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  .userTable th { text-align: left; font-size: 0.7rem; color: var(--muted); font-weight: 600;
                  padding: 8px 10px; border-bottom: 1px solid var(--border); }
  .userTable td { padding: 9px 10px; border-bottom: 1px solid rgba(255,255,255,0.05); vertical-align: middle; }
  .userTable tr:last-child td { border-bottom: none; }
  .userTable .uName b { display: block; }
  .userTable .uName span { font-size: 0.72rem; color: var(--muted); }
  .rowActions { display: flex; gap: 6px; justify-content: flex-end; }
  .ghostBtn, .dangerBtn {
    padding: 5px 11px; border-radius: 999px; font-size: 0.72rem; font-weight: 600; cursor: pointer;
    border: 1px solid var(--border); background: transparent; color: var(--muted); transition: all .15s;
  }
  .ghostBtn:hover { color: var(--text); border-color: var(--accent); }
  .dangerBtn:hover { color: var(--danger); border-color: rgba(255,107,107,0.5); }

  /* ---------------- Modal / toast ---------------- */
  #modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none;
    align-items: center; justify-content: center; padding: 20px; z-index: 50; backdrop-filter: blur(4px);
  }
  #modal.on { display: flex; animation: fadeIn .18s ease; }
  .modalCard {
    width: 100%; max-width: 420px; max-height: 80vh; overflow-y: auto;
    background: var(--card-solid); border: 1px solid var(--border); border-radius: 20px;
    box-shadow: var(--shadow); padding: 18px;
  }
  .modalHead { display: flex; align-items: center; justify-content: space-between; font-size: 0.98rem; font-weight: 650; margin-bottom: 12px; }
  .pickRow { display: flex; align-items: center; gap: 11px; padding: 9px 10px; border-radius: 12px; cursor: pointer; transition: background .15s; }
  .pickRow:hover { background: rgba(255,255,255,0.07); }
  .pickMeta b { display: block; font-size: 0.87rem; }
  .pickMeta span { font-size: 0.72rem; color: var(--muted); }
  .pickRow input { accent-color: var(--accent); width: 16px; height: 16px; flex-shrink: 0; }
  .modalField { margin-bottom: 12px; }
  .modalField label { display: block; font-size: 0.74rem; color: var(--muted); margin-bottom: 5px; }
  .modalField input {
    width: 100%; padding: 10px 12px; border-radius: 11px; border: 1px solid var(--border);
    background: var(--input-bg); font-size: 0.88rem; outline: none;
  }
  .modalField input:focus { border-color: var(--accent); }

  #toast {
    position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%) translateY(20px);
    background: var(--toast-bg); border: 1px solid var(--border); color: var(--text);
    padding: 10px 18px; border-radius: 999px; font-size: 0.83rem;
    opacity: 0; pointer-events: none; transition: all .25s; z-index: 60;
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  #toast.ok { border-color: rgba(76,224,166,0.5); }
  #toast.err { border-color: rgba(255,107,107,0.5); }

  /* Chat links (http/https auto-linkified) */
  .bubble a.chatLink { color: inherit; text-decoration: underline; word-break: break-all; }
  .me .bubble a.chatLink { color: #fff; }
  .them .bubble a.chatLink { color: var(--accent); }
  .pinTag { font-size: 0.72rem; margin-left: 4px; flex-shrink: 0; }
  .convPinBtn {
    background: none; border: none; cursor: pointer; font-size: 0.8rem; padding: 2px 4px;
    opacity: 0; color: var(--muted); flex-shrink: 0; border-radius: 6px;
  }
  .convItem:hover .convPinBtn { opacity: 1; }
  .convPinBtn.pinned { opacity: 1; }
  .convPinBtn:hover { background: rgba(255,255,255,0.1); }

  /* ---------------- Small screens ---------------- */
  @media (max-width: 780px) {
    #app.on { display: flex; flex-direction: column; grid-template-columns: none; }
    .sidebar { border-right: none; border-bottom: 1px solid var(--border); max-height: 100vh; }
    .main { flex: 1; min-height: 0; }
    #app.on.chatOpen[data-view="chats"] .sidebar { display: none; }
    #app.on:not(.chatOpen)[data-view="chats"] .main { display: none; }
    #app.on[data-view="chats"] .sidebar { flex: 1; min-height: 0; }
    #app.on[data-view="chats"] .main { min-height: 0; }
    #app.on:not([data-view="chats"]) { overflow-y: auto; }
    #app.on:not([data-view="chats"]) .sidebar { flex-shrink: 0; max-height: none; }
    #app.on:not([data-view="chats"]) .main { flex: 1; }
    .backBtn { display: flex; align-items: center; justify-content: center; }
    .bubble-row { max-width: 92%; }
    .scrollArea { padding: 16px 14px 26px; }
    .chatHead { padding: 10px 12px; gap: 8px; }
    #chatBox { padding: 12px; gap: 10px; }
    #chatForm { padding: 8px 10px 12px; gap: 6px; }
    #sendBtn { padding: 11px 14px; }
    .sideTop { padding: 12px 12px 8px; }
    .convItem { padding: 10px 8px; }
    .msgActions { opacity: 1; }
    .userTable { display: block; overflow-x: auto; white-space: nowrap; }
    .modalCard { margin: 0 4px; padding: 16px; }
    .fileCard .fName { max-width: 150px; }
    .filePreview { max-width: 170px; max-height: 170px; }
  }
</style>
</head>
<body>

<!-- ============================ SIGN IN ============================ -->
<div id="authScreen">
  <form class="authCard" id="loginForm">
    <div class="authIcon">💬</div>
    <h1>LAN Share &amp; Messenger</h1>
    <p class="authSub">Sign in to chat and share files on this network.</p>

    <label for="loginId">Username or email</label>
    <input id="loginId" type="text" autocomplete="username" autofocus required>
    <label for="loginPw">Password</label>
    <input id="loginPw" type="password" autocomplete="current-password" required>

    <div class="authErr" id="loginErr"></div>
    <button class="btn block" type="submit">Sign in</button>
    <p class="authNote">Accounts are created by the <b>owner</b>. Ask them to add you with a username, email and password.</p>
  </form>
</div>

<!-- ============================== APP ============================== -->
<div id="app" data-view="chats">
  <aside class="sidebar">
    <div class="sideTop">
      <div class="appIcon">💬</div>
      <div class="sideTitle">LAN Chat<div class="sideSub" id="sideSub"></div></div>
      <button class="iconBtn" id="themeBtn" title="Toggle dark / light mode">🌙</button>
      <button class="iconBtn" id="soundBtn" title="Toggle notification sound">🔔</button>
      <button class="iconBtn" id="logoutBtn" title="Sign out">⏻</button>
    </div>

    <div class="meChip" id="meChip"></div>

    <div class="nav">
      <button class="navBtn active" data-view="chats">💬 Chats</button>
      <button class="navBtn" data-view="files">📁 Files</button>
      <button class="navBtn" data-view="profile">👤 Profile</button>
      <button class="navBtn" data-view="admin" id="adminNav" style="display:none">⚙️ Admin</button>
    </div>

    <div class="chatsSide" id="chatsSide">
      <div id="notesPin"></div>
      <input class="sideSearch" id="convSearch" placeholder="🔍 Search chats">
      <div class="convActions">
        <button class="miniBtn" id="newChatBtn" type="button">＋ New chat</button>
        <button class="miniBtn alt" id="newGroupBtn" type="button">＋ New group</button>
      </div>
      <div class="convList" id="convList"></div>
    </div>

    <div class="sideFoot" id="sideFoot"></div>
  </aside>

  <main class="main">
    <!-- ---------- Chats ---------- -->
    <section id="chatsView" class="view active">
      <div class="emptyState" id="chatEmpty">
        <div class="big">💬</div>
        <b>Your messages</b>
        <p>Pick a conversation on the left, or start a new private chat or group.</p>
      </div>

      <div class="chatPane" id="chatPane" style="display:none">
        <header class="chatHead">
          <button class="backBtn" id="backBtn" type="button" title="Back">←</button>
          <div id="headAvatar"></div>
          <div class="headMeta">
            <div class="headTitle" id="headTitle"></div>
            <div class="headSub" id="headSub"></div>
          </div>
          <div class="headActions">
            <button class="iconBtn" id="chatMenuBtn" type="button" title="Conversation options">⋯</button>
          </div>
        </header>

        <div id="chatBox"></div>

        <div id="replyBanner">
          <span>Replying to <b id="replyToName"></b>: <span id="replyToText"></span></span>
          <span class="cancelReply" id="cancelReply">✕</span>
        </div>

        <form id="chatForm">
          <button id="attachBtn" type="button" title="Attach a file">📎</button>
          <input type="file" id="chatFileInput" multiple>
          <textarea id="chatInput" rows="1" placeholder="Type a message… (Shift+Enter for a new line)"></textarea>
          <button id="sendBtn" type="submit">Send</button>
        </form>
      </div>
    </section>

    <!-- ---------- Files ---------- -->
    <section id="filesView" class="view">
      <div class="scrollArea">
        <h2 class="vTitle">📁 Shared files</h2>
        <div class="pathLbl" id="filesPath"></div>
        <form id="uploadForm">
          <label class="dropzone" id="dropzone">
            <input type="file" id="fileInput" multiple>
            <div id="dzText">Drag &amp; drop files here, or tap to choose</div>
            <span class="dzBtn">Choose files</span>
          </label>
          <div id="progress-wrap">
            <div id="progress-bar"><div id="progress-fill"></div></div>
          </div>
        </form>
        <div class="fileList">
          @@ROWS@@
        </div>
        @@EMPTY_MSG@@
      </div>
    </section>

    <!-- ---------- Profile ---------- -->
    <section id="profileView" class="view">
      <div class="scrollArea">
        <h2 class="vTitle">👤 Your profile</h2>

        <form class="card" id="avatarForm">
          <div class="profRow">
            <div class="profAvatar" id="profAvatar"></div>
            <div class="profAvatarBtns">
              <label class="fileBtn">Change picture
                <input type="file" id="avatarInput" accept="image/png,image/jpeg,image/gif,image/webp">
              </label>
              <button class="ghostBtn" type="button" id="removeAvatarBtn">Remove picture</button>
              <div class="profMeta">PNG, JPG, GIF or WEBP · up to 2 MB</div>
              <div class="formMsg" id="avatarMsg"></div>
            </div>
          </div>
        </form>

        <form class="card" id="profileForm">
          <div class="grid2">
            <div>
              <label for="pfUser">Username</label>
              <input id="pfUser" type="text" readonly>
            </div>
            <div>
              <label for="pfName">Display name</label>
              <input id="pfName" type="text" maxlength="40" required>
            </div>
            <div>
              <label for="pfEmail">Email</label>
              <input id="pfEmail" type="email" maxlength="120" required>
            </div>
          </div>
          <button class="btn" type="submit" style="margin-top:12px">Save changes</button>
          <div class="formMsg" id="pfMsg"></div>
          <p class="hint">Your name and picture show up beside every message you send.</p>
        </form>

        <form class="card" id="passwordForm">
          <div class="grid2">
            <div>
              <label for="pwCurrent">Current password</label>
              <input id="pwCurrent" type="password" autocomplete="current-password" required>
            </div>
            <div>
              <label for="pwNew">New password</label>
              <input id="pwNew" type="password" autocomplete="new-password" required>
            </div>
            <div>
              <label for="pwConfirm">Confirm new password</label>
              <input id="pwConfirm" type="password" autocomplete="new-password" required>
            </div>
          </div>
          <button class="btn" type="submit" style="margin-top:12px">Change password</button>
          <div class="formMsg" id="pwMsg"></div>
          <p class="hint">Passwords are never saved as plain text — only as a salted
             PBKDF2-SHA256 hash. Changing it signs out your other devices.</p>
        </form>
      </div>
    </section>

    <!-- ---------- Admin ---------- -->
    <section id="adminView" class="view">
      <div class="scrollArea">
        <h2 class="vTitle">⚙️ User accounts</h2>

        <form class="card" id="createUserForm">
          <div class="grid2">
            <div>
              <label for="nuUser">Username</label>
              <input id="nuUser" type="text" placeholder="e.g. rahul" required>
            </div>
            <div>
              <label for="nuName">Display name</label>
              <input id="nuName" type="text" placeholder="e.g. Rahul Sharma" required>
            </div>
            <div>
              <label for="nuEmail">Email</label>
              <input id="nuEmail" type="email" placeholder="rahul@example.com" required>
            </div>
            <div>
              <label for="nuPass">Password</label>
              <input id="nuPass" type="text" placeholder="min 6 characters" required>
            </div>
          </div>
          <button class="btn" type="submit" style="margin-top:12px">Create user</button>
          <div class="formMsg" id="createMsg"></div>
          <p class="hint">You are the owner — these are the only accounts. Users sign in with their
             <b>username or email</b> plus the password you set here.</p>
        </form>

        <div class="card">
          <table class="userTable">
            <thead>
              <tr><th>User</th><th>Email</th><th>Role</th><th>Created</th><th style="text-align:right">Actions</th></tr>
            </thead>
            <tbody id="userRows"></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>
</div>

<div id="modal"><div class="modalCard" id="modalCard"></div></div>
<div id="toast"></div>

<script>
const USER = @@USER_JSON@@;
const LAN_IP = "@@LAN_IP@@";
const LAN_PORT = @@PORT@@;
const SHARE_DIR = @@SHARE_DIR_JS@@;
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = (url, body) => fetch(url, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(body || {})
});

let USERS = [];
let CONVS = [];
let activeConv = null;
let lastId = 0;
let lastMarked = 0;
let knownIds = new Set();
let replyCtx = null;

/* ---------------- helpers ---------------- */
const PALETTE = ['#6c8bff','#9b6cff','#ff6c9b','#ff9b6c','#6cd3ff','#6cff9b','#e06cff','#ffd66c'];
function colorFor(name) {
  let hash = 0;
  name = String(name || '?');
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return PALETTE[Math.abs(hash) % PALETTE.length];
}
function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/);
  const a = (parts[0] || '?')[0] || '?';
  const b = parts[1] ? parts[1][0] : '';
  return (a + b).toUpperCase();
}
function avatarHTML(name, avatarFile, extraClass) {
  const base = '<div class="avatar ' + (extraClass || '') + '" style="background:' + colorFor(name) + '">' +
               esc(initials(name));
  if (avatarFile) {
    // picture layered on top of the coloured initials — if the file ever goes
    // missing, onerror removes the <img> and the initials show through again
    return base + '<img class="avatarImg" src="/avatar/' + encodeURIComponent(avatarFile) +
           '" alt="" onerror="this.remove()"></div>';
  }
  return base + '</div>';
}
function avatarNode(name, avatarFile) {
  const el = document.createElement('div');
  el.className = 'msgAvatar';
  el.style.background = colorFor(name);
  el.textContent = initials(name);
  if (avatarFile) {
    const img = document.createElement('img');
    img.className = 'avatarImg';
    img.src = '/avatar/' + encodeURIComponent(avatarFile);
    img.alt = '';
    img.onerror = () => img.remove();
    el.appendChild(img);
  }
  return el;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
}
function fmtWhen(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return fmtTime(ts);
  return (d.getMonth()+1) + '/' + d.getDate();
}
function toast(msg, ok) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show ' + (ok ? 'ok' : 'err');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = ''; }, 2800);
}
function copyText(text, btn) {
  function done() {
    const original = btn.textContent;
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1500);
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) {}
  document.body.removeChild(ta);
}

/* ---------------- links / notifications ---------------- */
const URL_RE = /(https?:\/\/[^\s<>"')\]]+)/g;
function linkifyHTML(text) {
  // Escape first, then turn http(s) URLs into clickable links.
  const safe = esc(text);
  return safe.replace(URL_RE, (url) => {
    // Strip trailing punctuation that is rarely part of the URL.
    let trail = '';
    while (url.length && /[.,;:!?]$/.test(url)) { trail = url.slice(-1) + trail; url = url.slice(0, -1); }
    // Balance a trailing ")" or "]" typed around the link.
    let suffix = '';
    if (/[)\]]$/.test(url)) { suffix = url.slice(-1); url = url.slice(0, -1); }
    const href = esc(url);
    const label = esc(url.length > 80 ? url.slice(0, 80) + '…' : url);
    return '<a class="chatLink" href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>' + suffix + trail;
  });
}
function fillLinkified(el, text) {
  el.innerHTML = linkifyHTML(text || '');
}

let prevTotalUnread = 0;
function soundEnabled() {
  try { return localStorage.getItem('lanchat_sound') !== 'off'; } catch (e) { return true; }
}
function updateSoundBtn() {
  const b = $('soundBtn');
  if (b) b.textContent = soundEnabled() ? '🔔' : '🔕';
}
function playNotify() {
  if (!soundEnabled()) return;
  const now = Date.now();
  if (playNotify._last && now - playNotify._last < 1500) return;
  playNotify._last = now;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    playNotify._ctx = playNotify._ctx || new Ctx();
    const ctx = playNotify._ctx;
    if (ctx.state === 'suspended') ctx.resume();
    const t = ctx.currentTime;
    [660, 880].forEach((freq, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = 'sine';
      o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, t + i * 0.12);
      g.gain.exponentialRampToValueAtTime(0.25, t + i * 0.12 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.12 + 0.11);
      o.connect(g); g.connect(ctx.destination);
      o.start(t + i * 0.12); o.stop(t + i * 0.12 + 0.13);
    });
  } catch (e) {}
}
function updateTabBadge() {
  try {
    const total = CONVS.reduce((n, c) => n + (c.unread || 0), 0);
    document.title = total > 0 ? '(' + total + ') LAN Share & Messenger' : 'LAN Share & Messenger';
    updateFavicon(total);
    if ('setAppBadge' in navigator) {
      if (total > 0) navigator.setAppBadge(total).catch(() => {});
      else navigator.clearAppBadge().catch(() => {});
    }
    if (total > prevTotalUnread && prevTotalUnread !== 0) playNotify();
    else if (total > 0 && prevTotalUnread === 0 && document.hidden) playNotify();
    prevTotalUnread = total;
  } catch (e) {}
}
function updateFavicon(total) {
  try {
    const link = $('favicon');
    if (!link) return;
    const n = Math.max(0, total || 0);
    if (!n) {
      link.href = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%92%AC%3C/text%3E%3C/svg%3E";
      return;
    }
    const label = n > 99 ? '99+' : String(n);
    const size = 64;
    const cv = document.createElement('canvas');
    cv.width = size; cv.height = size;
    const ctx = cv.getContext('2d');
    ctx.font = '46px serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('💬', size / 2, size / 2 + 2);
    const w = label.length > 2 ? 30 : label.length > 1 ? 24 : 18;
    const x = size - w / 2 - 1, y = 13;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x - w / 2, y - 11, w, 22, 11);
    else ctx.rect(x - w / 2, y - 11, w, 22);
    ctx.fillStyle = '#e5484d';
    ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = '#fff';
    ctx.stroke();
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 13px system-ui, sans-serif';
    ctx.fillText(label, x, y + 1);
    link.href = cv.toDataURL('image/png');
  } catch (e) {}
}

/* ---------------- theme (dark / light) ---------------- */
function getTheme() {
  try { return localStorage.getItem('lanchat_theme') === 'light' ? 'light' : 'dark'; }
  catch (e) { return 'dark'; }
}
function applyTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark';
  try {
    if (t === 'light') document.documentElement.dataset.theme = 'light';
    else delete document.documentElement.dataset.theme;
    localStorage.setItem('lanchat_theme', t);
  } catch (e) {}
  const b = $('themeBtn');
  if (b) b.textContent = t === 'light' ? '☀️' : '🌙';
}
function isFreshMsg(m) {
  try {
    const ts = Number(m.ts || 0);
    if (!ts) return false;
    return (Date.now() / 1000 - ts) < 20;
  } catch (e) { return false; }
}

/* ---------------- sign in ---------------- */
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('loginErr').textContent = '';
  try {
    const r = await api('/api/login', {login: $('loginId').value, password: $('loginPw').value});
    const d = await r.json();
    if (!r.ok) { $('loginErr').textContent = d.error || 'Sign-in failed.'; return; }
    location.reload();
  } catch (err) {
    $('loginErr').textContent = 'Cannot reach the server.';
  }
});

/* ---------------- navigation ---------------- */
function showView(name) {
  document.querySelectorAll('.navBtn').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === name + 'View'));
  $('chatsSide').style.display = (name === 'chats') ? '' : 'none';
  $('app').dataset.view = name;
  try { localStorage.setItem('lanchat_view', name); } catch (e) {}
  if (name === 'files') loadFiles();
  if (name === 'profile') renderProfile();
}

/* ---------------- data loading ---------------- */
async function loadUsers() {
  try {
    const r = await fetch('/api/users');
    if (r.ok) USERS = await r.json();
  } catch (e) {}
}
async function refreshConvs() {
  try {
    const r = await fetch('/api/convs');
    if (!r.ok) return;
    CONVS = await r.json();
    renderConvs();
    if (activeConv && !CONVS.some(c => c.id === activeConv)) closeChat();
  } catch (e) {}
}

function previewText(c) {
  if (!c.last) return 'No messages yet';
  const who = c.last.user === USER.username ? 'You: ' : (c.type === 'group' ? (c.last.name || '') + ': ' : '');
  const body = c.last.type === 'file' ? '📎 ' + (c.last.file || 'file') : (c.last.text || '');
  return who + body.replace(/\s+/g, ' ');
}

function convItemHTML(c) {
  let av;
  if (c.type === 'notes') {
    av = '<div class="avatar notesAv">📝</div>';
  } else if (c.type === 'group') {
    av = '<div class="avatar groupAv">👥</div>';
  } else {
    av = avatarHTML(c.title, c.avatar);
  }
  const time = c.last ? fmtWhen(c.last.ts) : '';
  const badge = (c.unread > 0 && c.id !== activeConv)
    ? '<span class="unread">' + (c.unread > 99 ? '99+' : c.unread) + '</span>' : '';
  const pinTag = c.pinned ? '<span class="pinTag" title="Pinned">📌</span>' : '';
  return av +
    '<div class="convBody">' +
      '<div class="convTop"><b>' + esc(c.title) + '</b>' + pinTag + '<span class="t">' + esc(time) + '</span></div>' +
      '<div class="convPrev">' + esc(previewText(c)) + '</div>' +
    '</div>' + badge;
}

function renderConvs() {
  updateTabBadge();
  const q = ($('convSearch').value || '').toLowerCase();
  // Personal notes live pinned above the search box, outside the list.
  const notes = CONVS.find(c => c.type === 'notes');
  const pin = $('notesPin');
  pin.innerHTML = '';
  if (notes) {
    const item = document.createElement('div');
    item.className = 'convItem' + (notes.id === activeConv ? ' active' : '');
    item.dataset.id = notes.id;
    item.innerHTML = convItemHTML(notes);
    item.addEventListener('click', () => openConv(notes.id));
    pin.appendChild(item);
  }
  const list = CONVS.filter(c => c.type !== 'notes' && (!q || (c.title || '').toLowerCase().includes(q)));
  const el = $('convList');
  el.innerHTML = '';
  if (!list.length) {
    el.innerHTML = '<div class="empty">No conversations yet.<br>Start a new chat below.</div>';
    return;
  }
  for (const c of list) {
    const item = document.createElement('div');
    item.className = 'convItem' + (c.id === activeConv ? ' active' : '');
    item.dataset.id = c.id;

    item.innerHTML = convItemHTML(c);

    const pinBtn = document.createElement('button');
    pinBtn.type = 'button';
    pinBtn.className = 'convPinBtn' + (c.pinned ? ' pinned' : '');
    pinBtn.textContent = c.pinned ? '📌' : '📍';
    pinBtn.title = c.pinned ? 'Unpin chat' : 'Pin chat';
    pinBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        const r = await api('/api/convs/pin', {conv: c.id, pinned: !c.pinned});
        if (!r.ok) { toast('Could not pin', false); return; }
        await refreshConvs();
        toast(c.pinned ? 'Unpinned' : 'Pinned to top', true);
      } catch (err) { toast('Could not pin', false); }
    });
    item.appendChild(pinBtn);

    item.addEventListener('click', () => openConv(c.id));
    el.appendChild(item);
  }
}

/* ---------------- conversation view ---------------- */
function showEmptyChat() {
  activeConv = null;
  $('chatPane').style.display = 'none';
  $('chatEmpty').style.display = 'flex';
  $('app').classList.remove('chatOpen');
  localStorage.removeItem('lanchat_conv');
  renderConvs();
}
function closeChat() { showEmptyChat(); }

function renderHeader(c) {
  if (c.type === 'notes') {
    $('headAvatar').innerHTML = '<div class="avatar notesAv">📝</div>';
  } else {
    $('headAvatar').innerHTML = c.type === 'group'
      ? '<div class="avatar groupAv">👥</div>' : avatarHTML(c.title, c.avatar);
  }
  $('headTitle').textContent = c.title;
  let sub = c.subtitle || '';
  if (c.type === 'notes') {
    sub = 'Personal · Only you can see this';
  } else if (c.type === 'group') {
    sub = (c.members || []).filter(n => n !== c.title).join(', ');
    if (!sub) sub = (c.members || []).join(', ');
    sub = (c.members || []).length + ' member' + ((c.members || []).length === 1 ? '' : 's') +
          (sub ? ' · ' + sub : '');
  }
  $('headSub').textContent = sub;
}

async function openConv(id) {
  const c = CONVS.find(x => x.id === id);
  if (!c) return;
  activeConv = id;
  lastId = 0;
  lastMarked = 0;
  knownIds = new Set();
  replyCtx = null;
  $('replyBanner').style.display = 'none';
  $('chatBox').innerHTML = '';
  $('chatEmpty').style.display = 'none';
  $('chatPane').style.display = 'flex';
  $('app').classList.add('chatOpen');
  showView('chats');
  localStorage.setItem('lanchat_conv', id);
  renderHeader(c);
  renderConvs();
  $('chatInput').placeholder = (c.type === 'notes')
    ? 'Write a personal note… (only you can see this)'
    : 'Type a message… (Shift+Enter for a new line)';
  await fetchMsgs(false, true);
}

async function fetchMsgs(reset, silent) {
  if (!activeConv) return;
  try {
    const since = reset ? 0 : lastId;
    const r = await fetch('/api/messages?conv=' + encodeURIComponent(activeConv) + '&since=' + since);
    if (!r.ok) return;
    const msgs = await r.json();
    if (reset) { lastId = 0; knownIds = new Set(); }
    if (msgs.length || reset) {
      renderMessages(msgs, reset, silent);
      markRead();
      refreshConvs();
    }
  } catch (e) {}
}

function markRead() {
  if (!activeConv || lastId <= lastMarked) return;
  lastMarked = lastId;
  api('/api/read', {conv: activeConv, last_id: lastId}).catch(() => {});
}

function chatFileURL(m) {
  // Private chat files (new) download via member-only endpoint.
  // Legacy chat files fall back to the old public download path.
  if (m.private && m.id) return '/chatfile/' + m.id + '/' + encodeURIComponent(m.file || 'file');
  return '/download/' + encodeURIComponent(m.file);
}

function buildActions(m) {
  const actions = document.createElement('div');
  actions.className = 'msgActions';

  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'linkBtn';
  copyBtn.textContent = 'Copy';
  copyBtn.addEventListener('click', () => copyText(m.type === 'file' ? m.file : m.text, copyBtn));

  const replyBtn = document.createElement('button');
  replyBtn.type = 'button';
  replyBtn.className = 'linkBtn';
  replyBtn.textContent = 'Reply';
  replyBtn.addEventListener('click', () => startReply(m.name || m.user,
    m.type === 'file' ? ('📎 ' + m.file) : m.text));

  const delBtn = document.createElement('button');
  delBtn.type = 'button';
  delBtn.className = 'linkBtn danger';
  delBtn.textContent = 'Delete';
  delBtn.addEventListener('click', () => askDeleteMessage(m));

  actions.appendChild(copyBtn);
  actions.appendChild(replyBtn);
  actions.appendChild(delBtn);
  return actions;
}

function askDeleteMessage(m) {
  const canUnsend = (m.user === USER.username) || (USER.role === 'owner');
  let html = modalHead('Delete message');
  html += '<div class="modalField"><label>Messenger-style options</label></div>';
  html += '<button class="btn block" type="button" id="delForMe" style="margin-bottom:8px">Delete for me</button>';
  if (canUnsend) {
    html += '<button class="btn block" type="button" id="delForAll" style="background:linear-gradient(135deg,#ff6b6b,#ff9b6c)">Unsend for everyone</button>';
  } else {
    html += '<div class="empty">Only the sender (or owner) can unsend for everyone.</div>';
  }
  showModal(html);
  $('delForMe').addEventListener('click', async () => {
    await doDeleteMessage(m, 'me');
  });
  const allBtn = $('delForAll');
  if (allBtn) allBtn.addEventListener('click', async () => {
    if (!confirm('Unsend this message for everyone?')) return;
    await doDeleteMessage(m, 'everyone');
  });
}

async function doDeleteMessage(m, scope) {
  try {
    const r = await api('/api/message/delete', {conv: activeConv, id: m.id, scope: scope});
    const d = await r.json();
    if (!r.ok) { toast(d.error || 'Could not delete', false); return; }
    hideModal();
    // Remove locally for instant Messenger-like feedback.
    const box = $('chatBox');
    if (scope === 'me' || scope === 'everyone') {
      knownIds.delete(m.id);
    }
    if (scope === 'everyone') {
      // Simplest reliable refresh: reload messages from scratch.
      lastId = 0; knownIds = new Set(); box.innerHTML = '';
      await fetchMsgs(true, true);
    } else {
      // Delete-for-me: drop just this row from the DOM.
      // We tag rows with data-mid below; fall back to full reload.
      const row = box.querySelector('[data-mid="' + m.id + '"]');
      if (row) row.remove();
      else { lastId = 0; knownIds = new Set(); box.innerHTML = ''; await fetchMsgs(true, true); }
    }
    toast(scope === 'everyone' ? 'Message unsent' : 'Message deleted for you', true);
    refreshConvs();
  } catch (e) { toast('Could not delete', false); }
}

function buildTextBubble(m) {
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const LONG = 400;
  const txt = m.text || '';
  if (txt.length <= LONG) {
    fillLinkified(bubble, txt);
    return bubble;
  }
  const span = document.createElement('span');
  span.className = 'bubbleTxt clamped';
  fillLinkified(span, txt);
  const fullHTML = linkifyHTML(txt);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'readMoreBtn';
  btn.textContent = 'Read more';
  btn.addEventListener('click', () => {
    const clamped = span.classList.toggle('clamped');
    btn.textContent = clamped ? 'Read more' : 'Show less';
  });
  bubble.appendChild(span);
  bubble.appendChild(document.createElement('br'));
  bubble.appendChild(btn);
  return bubble;
}

function buildFileCard(m) {
  const card = document.createElement('div');
  card.className = 'fileCard';

  const url = chatFileURL(m);
  const link = document.createElement('a');
  link.href = url;
  link.setAttribute('download', '');

  const icon = document.createElement('span');
  icon.className = 'fIcon';
  icon.textContent = m.icon || '📄';

  const info = document.createElement('span');
  info.className = 'fInfo';
  const nameEl = document.createElement('span');
  nameEl.className = 'fName';
  nameEl.textContent = m.file;
  const sizeEl = document.createElement('span');
  sizeEl.className = 'fSize';
  sizeEl.textContent = m.size_human || '';
  info.appendChild(nameEl);
  info.appendChild(sizeEl);

  link.appendChild(icon);
  link.appendChild(info);
  card.appendChild(link);

  const wrap = document.createElement('div');
  wrap.appendChild(card);

  const ext = '.' + (m.file.split('.').pop() || '').toLowerCase();
  if (['.jpg','.jpeg','.png','.gif','.webp'].includes(ext)) {
    const img = document.createElement('img');
    img.className = 'filePreview';
    img.src = url;
    img.alt = m.file;
    wrap.appendChild(img);
  }
  return wrap;
}

function renderMessages(msgs, reset, silent) {
  const chatBox = $('chatBox');
  if (reset) { chatBox.innerHTML = ''; }
  const wasAtBottom = chatBox.scrollTop + chatBox.clientHeight >= chatBox.scrollHeight - 40;
  let playedForBatch = false;
  for (const m of msgs) {
    if (knownIds.has(m.id)) continue;
    knownIds.add(m.id);
    lastId = Math.max(lastId, m.id);

    // Only ping for genuinely new incoming messages — never for history
    // loaded when switching chats (openConv uses silent=true) and never
    // for old messages (recency guard covers reset/reload edge cases).
    if (!silent && m.user !== USER.username && !playedForBatch && isFreshMsg(m)) {
      playNotify();
      playedForBatch = true;
    }

    const isMe = m.user === USER.username;
    const row = document.createElement('div');
    row.className = 'bubble-row ' + (isMe ? 'me' : 'them');
    row.dataset.mid = m.id;

    const avatar = avatarNode(m.name || m.user, m.avatar);

    const bcol = document.createElement('div');
    bcol.className = 'bcol';

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = (m.name || m.user) + ' · ' + fmtTime(m.ts);
    bcol.appendChild(meta);

    if (m.reply) {
      const q = document.createElement('div');
      q.className = 'quote';
      const qb = document.createElement('b');
      qb.textContent = m.reply.name || '';
      const qs = document.createElement('span');
      qs.textContent = m.reply.text || '';
      q.appendChild(qb);
      q.appendChild(qs);
      bcol.appendChild(q);
    }

    if (m.type === 'file') {
      bcol.appendChild(buildFileCard(m));
    } else {
      bcol.appendChild(buildTextBubble(m));
    }

    bcol.appendChild(buildActions(m));
    row.appendChild(avatar);
    row.appendChild(bcol);
    chatBox.appendChild(row);
  }
  if (wasAtBottom) chatBox.scrollTop = chatBox.scrollHeight;
}

/* ---------------- replies ---------------- */
function startReply(name, text) {
  replyCtx = {name: name, text: text};
  $('replyToName').textContent = name;
  $('replyToText').textContent = text.length > 80 ? text.slice(0, 80) + '…' : text;
  $('replyBanner').style.display = 'flex';
  $('chatInput').focus();
}
$('cancelReply').addEventListener('click', () => {
  replyCtx = null;
  $('replyBanner').style.display = 'none';
});

/* ---------------- composer ---------------- */
const chatInput = $('chatInput');
chatInput.addEventListener('input', () => {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
});
chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    $('chatForm').requestSubmit();
  }
});

$('chatForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!activeConv) return;
  const text = chatInput.value.trim();
  if (!text) return;

  const payload = {conv: activeConv, text: text};
  if (replyCtx) { payload.reply = replyCtx; replyCtx = null; $('replyBanner').style.display = 'none'; }

  chatInput.value = '';
  chatInput.style.height = 'auto';
  try {
    const r = await api('/api/send', payload);
    if (!r.ok) { toast('Failed to send', false); return; }
    await fetchMsgs();
    refreshConvs();
  } catch (err) {
    toast('Failed to send — check the connection.', false);
  }
});

/* ---------------- chat attachments ---------------- */
$('attachBtn').addEventListener('click', () => $('chatFileInput').click());
$('chatFileInput').addEventListener('change', () => {
  if ($('chatFileInput').files.length) uploadChatFiles($('chatFileInput').files);
  $('chatFileInput').value = '';
});
function uploadChatFiles(files) {
  if (!activeConv) return;
  const formData = new FormData();
  formData.append('conv', activeConv);
  for (const f of files) formData.append('file', f);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload', true);
  xhr.onload = () => { if (xhr.status === 200) { fetchMsgs(); refreshConvs(); } else toast('Upload failed', false); };
  xhr.onerror = () => toast('Upload failed', false);
  xhr.send(formData);
}

/* ---------------- new chat / new group ---------------- */
function showModal(html) {
  $('modalCard').innerHTML = html;
  $('modal').classList.add('on');
}
function hideModal() { $('modal').classList.remove('on'); }
$('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') hideModal(); });

function modalHead(title) {
  return '<div class="modalHead">' + esc(title) +
         '<button class="iconBtn" type="button" data-close>✕</button></div>';
}
document.addEventListener('click', (e) => {
  if (e.target.closest && e.target.closest('[data-close]')) hideModal();
});

$('newChatBtn').addEventListener('click', () => {
  const others = USERS.filter(u => u.username.toLowerCase() !== USER.username.toLowerCase());
  let html = modalHead('Start a new chat');
  if (!others.length) {
    html += '<div class="empty">No other users yet — create some in the Admin panel.</div>';
  }
  for (const u of others) {
    html += '<div class="pickRow" data-user="' + esc(u.username) + '">' +
            avatarHTML(u.name, u.avatar) +
            '<div class="pickMeta"><b>' + esc(u.name) + '</b><span>' + esc(u.email) + '</span></div></div>';
  }
  showModal(html);
  $('modalCard').querySelectorAll('.pickRow').forEach(row => {
    row.addEventListener('click', async () => {
      try {
        const r = await api('/api/convs/dm', {user: row.dataset.user});
        if (!r.ok) { toast('Could not start that chat', false); return; }
        const d = await r.json();
        hideModal();
        await refreshConvs();
        openConv(d.id);
      } catch (e) { toast('Could not start that chat', false); }
    });
  });
});

$('newGroupBtn').addEventListener('click', () => {
  const others = USERS.filter(u => u.username.toLowerCase() !== USER.username.toLowerCase());
  let html = modalHead('Create a group');
  html += '<div class="modalField"><label>Group name</label><input id="groupName" maxlength="40" placeholder="e.g. Project Team"></div>';
  html += '<div class="modalField"><label>Members</label></div>';
  if (!others.length) html += '<div class="empty">No other users yet.</div>';
  for (const u of others) {
    html += '<label class="pickRow"><input type="checkbox" value="' + esc(u.username) + '" class="grpMember">' +
            avatarHTML(u.name, u.avatar) +
            '<div class="pickMeta"><b>' + esc(u.name) + '</b><span>' + esc(u.email) + '</span></div></label>';
  }
  html += '<button class="btn block" type="button" id="createGroupBtn" style="margin-top:12px">Create group</button>';
  showModal(html);

  $('createGroupBtn').addEventListener('click', async () => {
    const name = $('groupName').value.trim();
    if (!name) { toast('Give the group a name', false); return; }
    const members = Array.from(document.querySelectorAll('.grpMember:checked')).map(c => c.value);
    if (!members.length) { toast('Pick at least one member', false); return; }
    try {
      const r = await api('/api/convs/group', {name: name, members: members});
      if (!r.ok) { toast('Could not create the group', false); return; }
      const d = await r.json();
      hideModal();
      await refreshConvs();
      openConv(d.id);
    } catch (e) { toast('Could not create the group', false); }
  });
});

/* ---------------- files tab ---------------- */
const dropzone = $('dropzone');
const fileInput = $('fileInput');
const dzText = $('dzText');
const progressWrap = $('progress-wrap');
const progressFill = $('progress-fill');

async function loadFiles() {
  try {
    const r = await fetch('/api/files');
    if (!r.ok) return;
    const files = await r.json();
    const listEl = document.querySelector('.fileList');
    if (listEl) {
      listEl.innerHTML = files.map(f =>
        '<div class="fileRow">' +
          '<span class="fIconBig">' + (f.icon || '📄') + '</span>' +
          '<span class="fMeta">' +
            '<a href="/download/' + encodeURIComponent(f.name) + '" download>' + esc(f.name) + '</a>' +
            '<span class="fSizeLbl">' + esc(f.size_human) + '</span>' +
          '</span>' +
          '<a class="delLink" href="/delete/' + encodeURIComponent(f.name) + '" data-del="' + esc(f.name) + '">Delete</a>' +
        '</div>'
      ).join('');
    }
    const empt = $('emptyFiles');
    if (empt) empt.style.display = files.length ? 'none' : '';
  } catch (e) {}
}

/* Delete without leaving the messenger view (the plain link still works too). */
document.addEventListener('click', async (e) => {
  const link = e.target.closest && e.target.closest('a.delLink');
  if (!link) return;
  e.preventDefault();
  if (!confirm('Delete ' + (link.dataset.del || 'this file') + '?')) return;
  try { await fetch(link.getAttribute('href')); } catch (err) {}
  loadFiles();
  toast('File deleted', true);
});

['dragenter','dragover'].forEach(evt =>
  dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.add('drag'); })
);
['dragleave','drop'].forEach(evt =>
  dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.remove('drag'); })
);
dropzone.addEventListener('drop', e => {
  if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) uploadFiles(fileInput.files);
});

function uploadFiles(files) {
  const formData = new FormData();
  for (const f of files) formData.append('file', f);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload', true);
  progressWrap.style.display = 'block';
  progressFill.style.width = '0%';
  dzText.textContent = 'Uploading...';

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      progressFill.style.width = Math.round((e.loaded / e.total) * 100) + '%';
    }
  };
  xhr.onload = () => {
    if (xhr.status === 200) {
      dzText.textContent = 'Drag & drop files here, or tap to choose';
      progressWrap.style.display = 'none';
      progressFill.style.width = '0%';
      toast('Uploaded', true);
      loadFiles();
    } else {
      dzText.textContent = 'Upload failed — try again';
      progressWrap.style.display = 'none';
    }
  };
  xhr.onerror = () => {
    dzText.textContent = 'Upload failed — try again';
    progressWrap.style.display = 'none';
  };
  xhr.send(formData);
}

/* ---------------- admin ---------------- */
function renderUsers() {
  const tbody = $('userRows');
  tbody.innerHTML = '';
  const sorted = USERS.slice().sort((a, b) => (b.role === 'owner') - (a.role === 'owner'));
  for (const u of sorted) {
    const tr = document.createElement('tr');
    const created = u.created ? new Date(u.created * 1000).toLocaleDateString() : '—';
    tr.innerHTML =
      '<td class="uName"><b>' + esc(u.name) + '</b><span>@' + esc(u.username) + '</span></td>' +
      '<td>' + esc(u.email) + '</td>' +
      '<td><span class="badge ' + (u.role === 'owner' ? 'owner' : 'role') + '">' +
        (u.role === 'owner' ? 'OWNER' : 'USER') + '</span></td>' +
      '<td>' + esc(created) + '</td>' +
      '<td><div class="rowActions"></div></td>';

    const actions = tr.querySelector('.rowActions');

    const resetBtn = document.createElement('button');
    resetBtn.type = 'button';
    resetBtn.className = 'ghostBtn';
    resetBtn.textContent = 'Reset password';
    resetBtn.addEventListener('click', async () => {
      const pw = prompt('New password for ' + u.name + ' (min 6 characters):');
      if (!pw) return;
      const r = await api('/api/users/password', {username: u.username, password: pw});
      const d = await r.json();
      toast(d.error || 'Password updated', !d.error);
    });
    actions.appendChild(resetBtn);

    if (u.role !== 'owner') {
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'dangerBtn';
      delBtn.textContent = 'Delete';
      delBtn.addEventListener('click', async () => {
        if (!confirm('Delete ' + u.name + '? They will be signed out everywhere.')) return;
        const r = await api('/api/users/delete', {username: u.username});
        const d = await r.json();
        if (d.error) { toast(d.error, false); return; }
        toast('User deleted', true);
        await loadUsers();
        renderUsers();
        await refreshConvs();
      });
      actions.appendChild(delBtn);
    }
    tbody.appendChild(tr);
  }
}

$('createUserForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('createMsg').textContent = '';
  try {
    const r = await api('/api/users/create', {
      username: $('nuUser').value,
      name: $('nuName').value,
      email: $('nuEmail').value,
      password: $('nuPass').value
    });
    const d = await r.json();
    if (!r.ok || d.error) { $('createMsg').textContent = d.error || 'Could not create the user.'; return; }
    toast('Created ' + d.user.name, true);
    $('createUserForm').reset();
    await loadUsers();
    renderUsers();
  } catch (err) {
    $('createMsg').textContent = 'Could not reach the server.';
  }
});

/* ---------------- profile (picture / details / password) ---------------- */
function showMsg(id, text, ok) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'formMsg' + (ok ? ' ok' : '');
}

function renderProfile() {
  if (!USER) return;
  $('pfUser').value = USER.username;
  $('pfName').value = USER.name;
  $('pfEmail').value = USER.email;
  showMsg('pfMsg', '', true);
  showMsg('pwMsg', '', true);
  showMsg('avatarMsg', '', true);

  const box = $('profAvatar');
  box.textContent = initials(USER.name);
  box.style.background = colorFor(USER.name);
  if (USER.avatar) {
    const img = document.createElement('img');
    img.className = 'avatarImg';
    img.src = '/avatar/' + encodeURIComponent(USER.avatar);
    img.alt = '';
    img.onerror = () => img.remove();
    box.appendChild(img);
  }
  $('removeAvatarBtn').style.display = USER.avatar ? '' : 'none';
}

$('avatarInput').addEventListener('change', () => {
  const file = $('avatarInput').files[0];
  $('avatarInput').value = '';
  if (!file) return;
  const fd = new FormData();
  fd.append('avatar', file, file.name);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/avatar', true);
  xhr.onload = () => {
    let d = {};
    try { d = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status === 200 && d.ok) {
      Object.assign(USER, d.user || {});
      renderMe();
      renderProfile();
      showMsg('avatarMsg', 'Profile picture updated', true);
      refreshConvs();
    } else {
      showMsg('avatarMsg', d.error || 'Could not upload that picture', false);
    }
  };
  xhr.onerror = () => showMsg('avatarMsg', 'Could not reach the server', false);
  xhr.send(fd);
});

$('removeAvatarBtn').addEventListener('click', async () => {
  try {
    const r = await api('/api/avatar/remove', {});
    const d = await r.json();
    if (!r.ok) { showMsg('avatarMsg', d.error || 'Could not remove the picture', false); return; }
    Object.assign(USER, d.user || {});
    renderMe();
    renderProfile();
    showMsg('avatarMsg', 'Picture removed', true);
    refreshConvs();
  } catch (e) {
    showMsg('avatarMsg', 'Could not reach the server', false);
  }
});

$('profileForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  showMsg('pfMsg', '', true);
  try {
    const r = await api('/api/profile', {name: $('pfName').value, email: $('pfEmail').value});
    const d = await r.json();
    if (!r.ok) { showMsg('pfMsg', d.error || 'Could not save your profile', false); return; }
    Object.assign(USER, d.user || {});
    renderMe();
    showMsg('pfMsg', 'Profile saved', true);
    loadUsers();
    refreshConvs();
  } catch (err) {
    showMsg('pfMsg', 'Could not reach the server', false);
  }
});

$('passwordForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  showMsg('pwMsg', '', true);
  const current = $('pwCurrent').value;
  const next = $('pwNew').value;
  const confirmPw = $('pwConfirm').value;
  if (next.length < 6) { showMsg('pwMsg', 'New password must be at least 6 characters', false); return; }
  if (next !== confirmPw) { showMsg('pwMsg', 'The two new passwords don\'t match', false); return; }
  try {
    const r = await api('/api/profile/password', {current: current, password: next});
    const d = await r.json();
    if (!r.ok) { showMsg('pwMsg', d.error || 'Could not change the password', false); return; }
    $('passwordForm').reset();
    showMsg('pwMsg', 'Password changed — your other devices were signed out', true);
  } catch (err) {
    showMsg('pwMsg', 'Could not reach the server', false);
  }
});

/* ---------------- boot ---------------- */
function activeConvObj() { return CONVS.find(x => x.id === activeConv) || null; }

function openChatMenu() {
  const c = activeConvObj();
  if (!c) return;
  const isNotes = c.type === 'notes';
  const isGroup = c.type === 'group';
  let html = modalHead(isNotes ? 'My Notes options' : 'Conversation options');
  if (isNotes) {
    html += '<div class="empty">Notes are personal — only you can see them. Files you attach here stay private.</div>';
    html += '<button class="btn block" type="button" id="clearNotesBtn" style="background:linear-gradient(135deg,#ff6b6b,#ff9b6c)">Clear all notes (for me)</button>';
  } else {
    html += '<button class="btn block" type="button" id="pinConvBtn" style="margin-bottom:8px">' + (c.pinned ? '📌 Unpin chat' : '📌 Pin chat to top') + '</button>';
    html += '<button class="btn block" type="button" id="hideConvBtn" style="margin-bottom:8px">Delete chat (for me)</button>';
    if (isGroup && USER.role === 'owner') {
      html += '<button class="btn block" type="button" id="delGroupBtn" style="background:linear-gradient(135deg,#ff6b6b,#ff9b6c)">Delete group for everyone</button>';
    } else if (isGroup) {
      html += '<div class="empty">Only the owner can delete a group for everyone.</div>';
    } else {
      html += '<div class="empty">Deleting a 1:1 chat removes it for you only — old messages stay deleted, only new messages reappear.</div>';
    }
  }
  showModal(html);
  const pinBtn = $('pinConvBtn');
  if (pinBtn) pinBtn.addEventListener('click', async () => {
    try {
      const r = await api('/api/convs/pin', {conv: activeConv, pinned: !c.pinned});
      const d = await r.json();
      if (!r.ok) { toast(d.error || 'Could not pin', false); return; }
      c.pinned = !c.pinned;
      hideModal(); refreshConvs();
      toast(c.pinned ? 'Pinned to top' : 'Unpinned', true);
    } catch (e) { toast('Could not pin', false); }
  });
  const hideBtn = $('hideConvBtn');
  if (hideBtn) hideBtn.addEventListener('click', async () => {
    if (!confirm('Delete this chat for you? It will reappear on new messages.')) return;
    try {
      const r = await api('/api/convs/hide', {conv: activeConv});
      const d = await r.json();
      if (!r.ok) { toast(d.error || 'Could not delete', false); return; }
      hideModal(); showEmptyChat(); refreshConvs();
      toast('Chat deleted for you', true);
    } catch (e) { toast('Could not delete', false); }
  });
  const clearBtn = $('clearNotesBtn');
  if (clearBtn) clearBtn.addEventListener('click', async () => {
    if (!confirm('Clear all your personal notes?')) return;
    try {
      const r = await api('/api/convs/hide', {conv: activeConv});
      const d = await r.json();
      if (!r.ok) { toast(d.error || 'Could not clear', false); return; }
      hideModal(); lastId = 0; knownIds = new Set(); $('chatBox').innerHTML = '';
      await fetchMsgs(true, true); refreshConvs();
      toast('Notes cleared', true);
    } catch (e) { toast('Could not clear', false); }
  });
  const delGroupBtn = $('delGroupBtn');
  if (delGroupBtn) delGroupBtn.addEventListener('click', async () => {
    if (!confirm('Delete this group for EVERYONE? This cannot be undone.')) return;
    try {
      const r = await api('/api/convs/delete', {conv: activeConv});
      const d = await r.json();
      if (!r.ok) { toast(d.error || 'Could not delete', false); return; }
      hideModal(); showEmptyChat(); refreshConvs();
      toast('Group deleted', true);
    } catch (e) { toast('Could not delete', false); }
  });
}

function wire() {
  document.querySelectorAll('.navBtn').forEach(b =>
    b.addEventListener('click', () => showView(b.dataset.view)));

  $('convSearch').addEventListener('input', renderConvs);
  $('backBtn').addEventListener('click', closeChat);
  $('chatMenuBtn').addEventListener('click', openChatMenu);
  updateSoundBtn();
  applyTheme(getTheme());
  const th = $('themeBtn');
  if (th) th.addEventListener('click', () => {
    const next = getTheme() === 'light' ? 'dark' : 'light';
    applyTheme(next);
    toast(next === 'light' ? 'Light mode' : 'Dark mode', true);
  });
  const snd = $('soundBtn');
  if (snd) snd.addEventListener('click', () => {
    try {
      const on = !soundEnabled();
      localStorage.setItem('lanchat_sound', on ? 'on' : 'off');
    } catch (e) {}
    updateSoundBtn();
    toast(soundEnabled() ? 'Notification sound on' : 'Notification sound off', true);
  });
  document.addEventListener('pointerdown', () => {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) {
        playNotify._ctx = playNotify._ctx || new Ctx();
        if (playNotify._ctx.state === 'suspended') playNotify._ctx.resume();
      }
    } catch (e) {}
  }, {once: true});

  $('logoutBtn').addEventListener('click', async () => {
    try { await api('/api/logout', {}); } catch (e) {}
    location.reload();
  });
}

async function tick() {
  await refreshConvs();
  if (activeConv) await fetchMsgs();
}

function renderMe() {
  if (!USER) return;
  $('meChip').innerHTML =
    avatarHTML(USER.name, USER.avatar) +
    '<div class="meMeta"><b>' + esc(USER.name) + '</b><span>' + esc(USER.email) + '</span></div>' +
    '<span class="badge ' + (USER.role === 'owner' ? 'owner' : 'role') + '">' +
    (USER.role === 'owner' ? 'OWNER' : 'USER') + '</span>';
  $('meChip').title = '@' + USER.username;
  $('sideSub').textContent = USER.name + ' · @' + USER.username;
}

function rememberedView() {
  const v = localStorage.getItem('lanchat_view');
  if (v === 'files' || v === 'profile') return v;
  if (v === 'admin' && USER && USER.role === 'owner') return v;
  return 'chats';
}

function boot() {
  applyTheme(getTheme());
  if (!USER) {
    $('authScreen').style.display = 'flex';
    return;
  }
  $('app').classList.add('on');
  renderMe();
  $('sideFoot').textContent = 'http://' + LAN_IP + ':' + LAN_PORT;
  $('sideFoot').title = SHARE_DIR;
  if ($('filesPath')) $('filesPath').textContent = SHARE_DIR;

  if (USER.role === 'owner') {
    $('adminNav').style.display = '';
  }

  wire();
  loadFiles();

  const view = rememberedView();
  showView(view);

  Promise.all([loadUsers(), refreshConvs()]).then(() => {
    if (USER.role === 'owner') renderUsers();
    const saved = localStorage.getItem('lanchat_conv');
    if (view === 'chats' && saved && CONVS.some(c => c.id === saved)) {
      openConv(saved);
    } else {
      showEmptyChat();
    }
  });

  setInterval(tick, 1300);
}

boot();
</script>
</body>
</html>
"""


def list_shared_files():
    entries = []
    try:
        for fname in sorted(os.listdir(SHARE_DIR)):
            if fname.startswith("."):
                continue  # hide internal files (state, chat history)
            fpath = os.path.join(SHARE_DIR, fname)
            if os.path.isfile(fpath):
                entries.append({
                    "name": fname,
                    "size": os.path.getsize(fpath),
                    "size_human": human_size(os.path.getsize(fpath)),
                    "icon": icon_for(fname),
                })
    except FileNotFoundError:
        pass
    return entries


def render_index(user=None):
    entries = list_shared_files()

    if entries:
        rows = "\n".join(
            FILE_ROW_TEMPLATE.format(
                href=quote(f["name"]),
                name=html.escape(f["name"]),
                name_attr=html.escape(f["name"]),
                size=f["size_human"],
                icon=f["icon"],
            )
            for f in entries
        )
    else:
        rows = ""
    empty_msg = ('<div class="empty" id="emptyFiles">No files yet — drop something above.</div>'
                 if not entries else
                 '<div class="empty" id="emptyFiles" style="display:none">No files yet — drop something above.</div>')

    user_json = json.dumps(public_user(user), ensure_ascii=False) if user else "null"
    # Keep JSON safe inside a <script> block (no "</" or "&" breakout).
    user_json = user_json.replace("<", "\\u003c").replace("&", "\\u0026")

    page = PAGE_TEMPLATE
    page = page.replace("@@USER_JSON@@", user_json)
    page = page.replace("@@LAN_IP@@", get_lan_ip())
    page = page.replace("@@PORT@@", str(PORT))
    share_dir_js = json.dumps(SHARE_DIR).replace("<", "\\u003c").replace("&", "\\u0026")
    page = page.replace("@@SHARE_DIR_JS@@", share_dir_js)
    page = page.replace("@@ROWS@@", rows)
    page = page.replace("@@EMPTY_MSG@@", empty_msg)
    return page


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "LANShareChat/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _session_token(self):
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"(?:^|;\s*)lanchat_session=([^;\s]+)", cookie)
        return unquote(m.group(1)) if m else None

    def _current_user(self):
        token = self._session_token()
        if not token:
            return None
        with _lock:
            sess = _sessions.get(token)
            if not sess:
                return None
            return _users.get(sess.get("user"))

    def _require_user(self):
        user = self._current_user()
        if not user:
            self._send_json({"error": "Not signed in"}, 401)
            return None
        return user

    def _require_owner(self):
        user = self._require_user()
        if user is None:
            return None
        if user.get("role") != "owner":
            self._send_json({"error": "Owner only"}, 403)
            return None
        return user

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", ""):
            self._send_html(render_index(self._current_user()))
            return

        if path == "/api/me":
            user = self._current_user()
            self._send_json({"user": public_user(user)}, 200 if user else 401)
            return

        if path == "/api/users":
            if not self._require_user():
                return
            with _lock:
                people = [public_user(u) for u in _users.values()]
            people.sort(key=lambda p: p["name"].lower())
            self._send_json(people)
            return

        if path == "/api/convs":
            user = self._require_user()
            if not user:
                return
            self._send_json(conversations_for(user["username"].lower()))
            return

        if path == "/api/messages":
            user = self._require_user()
            if not user:
                return
            qs = parse_qs(parsed.query)
            conv_id = qs.get("conv", [""])[0]
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with _lock:
                conv = _convs.get(conv_id)
                visible = bool(conv) and can_see(user["username"].lower(), conv)
            if not visible:
                self._send_json({"error": "Not a member of that conversation"}, 403)
                return
            self._send_json(get_messages(conv_id, since, user["username"].lower()))
            return

        if path == "/api/files":
            if not self._require_user():
                return
            self._send_json(list_shared_files())
            return

        if path.startswith("/avatar/"):
            if not self._require_user():
                return
            name = os.path.basename(unquote(path[len("/avatar/"):]))
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED_IMG_EXT or not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
                self.send_error(404, "Not found")
                return
            fpath = os.path.join(AVATARS_DIR, name)
            if not os.path.isfile(fpath):
                self.send_error(404, "Not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", IMG_CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(os.path.getsize(fpath)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            with open(fpath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        if path.startswith("/chatfile/"):
            user = self._require_user()
            if not user:
                return
            me = user["username"].lower()
            # Format: /chatfile/<msg_id>[/anything] — member-only private chat file.
            rest = unquote(path[len("/chatfile/"):]).split("/", 1)[0]
            try:
                mid = int(rest)
            except ValueError:
                self.send_error(404, "Not found")
                return
            with _lock:
                found = None
                found_conv = None
                for cid, bucket in _messages.items():
                    for m in bucket:
                        if m.get("id") == mid and m.get("type") == "file":
                            found = m
                            found_conv = _convs.get(cid)
                            break
                    if found:
                        break
                visible = bool(found and found_conv) and can_see(me, found_conv)
                if found and found.get("private") and mid in hidden_ids(me, found.get("conv")):
                    visible = False
            if not visible:
                self.send_error(404, "Not found")
                return
            if found.get("private") and found.get("stored"):
                fpath = os.path.join(CHATFILES_DIR, os.path.basename(found["stored"]))
                disp = found.get("file") or "file"
            else:
                # Legacy chat files (previously stored publicly).
                try:
                    fpath = safe_join(SHARE_DIR, found.get("file") or "")
                except ValueError:
                    self.send_error(404, "Not found")
                    return
                disp = os.path.basename(fpath)
            if not os.path.isfile(fpath):
                self.send_error(404, "File not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{disp}"')
            self.send_header("Content-Length", str(os.path.getsize(fpath)))
            self.end_headers()
            with open(fpath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        if path.startswith("/download/"):
            if not self._require_user():
                return
            name = path[len("/download/"):]
            if os.path.basename(unquote(name)).startswith("."):
                self.send_error(404, "File not found")
                return
            try:
                fpath = safe_join(SHARE_DIR, name)
            except ValueError:
                self.send_error(400, "Invalid path")
                return
            if not os.path.isfile(fpath):
                self.send_error(404, "File not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(fpath)}"')
            self.send_header("Content-Length", str(os.path.getsize(fpath)))
            self.end_headers()
            with open(fpath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        if path.startswith("/delete/"):
            if not self._require_user():
                return
            name = path[len("/delete/"):]
            if os.path.basename(unquote(name)).startswith("."):
                self.send_error(404, "File not found")
                return
            try:
                fpath = safe_join(SHARE_DIR, name)
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except ValueError:
                pass
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        self.send_error(404, "Not found")

    # ---- POST ----
    def do_POST(self):
        if self.path == "/api/login":
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            user = authenticate(data.get("login"), data.get("password"))
            if not user:
                time.sleep(0.3)  # slow down guessing a little
                self._send_json({"error": "Wrong username/email or password"}, 401)
                return
            token = create_session(user["username"].lower())
            body = json.dumps({"user": public_user(user)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"lanchat_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/logout":
            token = self._session_token()
            if token:
                drop_session(token)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "lanchat_session=; Path=/; HttpOnly; Max-Age=0")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/users/create":
            owner = self._require_owner()
            if not owner:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = create_user(data.get("username"), data.get("name"),
                                     data.get("email"), data.get("password"))
            if not ok:
                self._send_json({"error": result}, 400)
                return
            self._send_json({"ok": True, "user": public_user(result)})
            return

        if self.path == "/api/users/delete":
            if not self._require_owner():
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = delete_user(data.get("username"))
            self._send_json({"ok": ok} if ok else {"error": result}, 200 if ok else 400)
            return

        if self.path == "/api/users/password":
            if not self._require_owner():
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = set_password(data.get("username"), data.get("password"))
            if ok:
                # a password reset signs that user out everywhere else
                drop_user_sessions((data.get("username") or "").strip().lower(),
                                   self._session_token())
            self._send_json({"ok": ok} if ok else {"error": result}, 200 if ok else 400)
            return

        if self.path == "/api/profile":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = update_profile(user["username"].lower(),
                                        data.get("name"), data.get("email"))
            if not ok:
                self._send_json({"error": result}, 400)
                return
            self._send_json({"ok": True, "user": public_user(result)})
            return

        if self.path == "/api/profile/password":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = change_own_password(user["username"].lower(),
                                             data.get("current"),
                                             data.get("password"),
                                             self._session_token())
            self._send_json({"ok": ok} if ok else {"error": result}, 200 if ok else 400)
            return

        if self.path == "/api/avatar/remove":
            user = self._require_user()
            if not user:
                return
            remove_avatar(user["username"].lower())
            self._send_json({"ok": True, "user": public_user(self._current_user())})
            return

        if self.path == "/api/avatar":
            user = self._require_user()
            if not user:
                return
            self._handle_avatar(user)
            return

        if self.path == "/api/convs/dm":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            target = (data.get("user") or "").strip().lower()
            me = user["username"].lower()
            with _lock:
                exists = target in _users and target != me
            if not exists:
                self._send_json({"error": "No such user"}, 400)
                return
            self._send_json({"id": get_or_create_dm(me, target)})
            return

        if self.path == "/api/convs/group":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            members = data.get("members")
            if not isinstance(members, list) or not members:
                self._send_json({"error": "Pick at least one member"}, 400)
                return
            cid = create_group(data.get("name"), [str(m) for m in members], user["username"].lower())
            self._send_json({"id": cid})
            return

        if self.path == "/api/send":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            conv_id = str(data.get("conv", ""))
            text = str(data.get("text", "")).strip()[:2000]
            if not text:
                self._send_json({"error": "Empty message"}, 400)
                return
            with _lock:
                conv = _convs.get(conv_id)
                visible = bool(conv) and can_see(user["username"].lower(), conv)
            if not visible:
                self._send_json({"error": "Not a member of that conversation"}, 403)
                return
            reply = data.get("reply")
            if isinstance(reply, dict):
                reply = {
                    "name": str(reply.get("name", ""))[:40],
                    "text": str(reply.get("text", ""))[:160],
                }
            else:
                reply = None
            msg = add_message(conv_id, user["username"].lower(), text, reply=reply)
            self._send_json(msg)
            return

        if self.path == "/api/read":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            conv_id = str(data.get("conv", ""))
            try:
                last_seen = int(data.get("last_id", 0))
            except (TypeError, ValueError):
                last_seen = 0
            with _lock:
                conv = _convs.get(conv_id)
                visible = bool(conv) and can_see(user["username"].lower(), conv)
            if not visible:
                self._send_json({"error": "Nope"}, 403)
                return
            mark_read(user["username"].lower(), conv_id, last_seen)
            self._send_json({"ok": True})
            return

        if self.path == "/api/message/delete":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            conv_id = str(data.get("conv", ""))
            try:
                mid = int(data.get("id", 0))
            except (TypeError, ValueError):
                mid = 0
            scope = str(data.get("scope", "me"))
            me = user["username"].lower()
            is_owner = user.get("role") == "owner"
            with _lock:
                conv = _convs.get(conv_id)
                visible = bool(conv) and can_see(me, conv)
            if not visible:
                self._send_json({"error": "Not a member"}, 403)
                return
            if scope == "everyone":
                ok, err = delete_message_everyone(conv_id, mid, me, is_owner)
            else:
                ok, err = delete_message_for_me(me, conv_id, mid)
            self._send_json({"ok": ok} if ok else {"error": err}, 200 if ok else 400)
            return

        if self.path == "/api/convs/hide":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            conv_id = str(data.get("conv", ""))
            me = user["username"].lower()
            with _lock:
                conv = _convs.get(conv_id)
                visible = bool(conv) and can_see(me, conv)
            if not visible:
                self._send_json({"error": "Not a member"}, 403)
                return
            ok, err = hide_conv_for_me(me, conv_id)
            self._send_json({"ok": ok} if ok else {"error": err}, 200 if ok else 400)
            return

        if self.path == "/api/convs/pin":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            conv_id = str(data.get("conv", ""))
            me = user["username"].lower()
            with _lock:
                conv = _convs.get(conv_id)
                visible = bool(conv) and can_see(me, conv)
            if not visible:
                self._send_json({"error": "Not a member"}, 403)
                return
            pinned = bool(data.get("pinned", True))
            ok, err = set_pinned_conv(me, conv_id, pinned)
            self._send_json({"ok": ok} if ok else {"error": err}, 200 if ok else 400)
            return

        if self.path == "/api/convs/delete":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            conv_id = str(data.get("conv", ""))
            me = user["username"].lower()
            ok, err = delete_conv_everywhere(conv_id, me, user.get("role") == "owner")
            self._send_json({"ok": ok} if ok else {"error": err}, 200 if ok else 400)
            return

        if self.path == "/upload":
            if not self._require_user():
                return
            self._handle_upload()
            return

        self.send_error(404, "Not found")

    def _parse_multipart(self):
        """Parse a multipart/form-data body into [{'name','filename','data'}].
        Returns None if the request isn't valid multipart."""
        content_type = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=(.+)', content_type)
        if not m:
            return None
        boundary = m.group(1).strip('"').encode()

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        parsed_parts = []
        for part in body.split(b"--" + boundary):
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            headers_raw = part[:header_end].decode(errors="replace")
            data = part[header_end + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]

            name_match = re.search(r'name="([^"]*)"', headers_raw)
            fname_match = re.search(r'filename="([^"]*)"', headers_raw)
            parsed_parts.append({
                "name": name_match.group(1) if name_match else None,
                "filename": fname_match.group(1) if fname_match else None,
                "data": data,
            })
        return parsed_parts

    def _handle_avatar(self, user):
        parts = self._parse_multipart()
        if parts is None:
            self._send_json({"error": "Bad request: no boundary"}, 400)
            return

        data = None
        orig_name = ""
        for p in parts:
            if p["name"] == "avatar" and p["filename"]:
                data = p["data"]
                orig_name = p["filename"]
                break
        if not data:
            self._send_json({"error": "No picture selected"}, 400)
            return
        if len(data) > MAX_AVATAR_BYTES:
            self._send_json({"error": "Picture is too large (max 2 MB)"}, 413)
            return

        ext = os.path.splitext(os.path.basename(orig_name))[1].lower()
        if ext not in ALLOWED_IMG_EXT or not looks_like_image(data, ext):
            self._send_json({"error": "Use a PNG, JPG, GIF or WEBP image"}, 400)
            return

        try:
            fname = save_avatar(user["username"].lower(), data, ext)
        except OSError:
            self._send_json({"error": "Could not save the picture"}, 500)
            return
        self._send_json({"ok": True, "avatar": fname,
                         "user": public_user(self._current_user())})

    def _handle_upload(self):
        parsed_parts = self._parse_multipart()
        if parsed_parts is None:
            self.send_error(400, "Bad request: no boundary")
            return

        # A 'conv' field means this came from the chat's attach button.
        # Chat files stay PRIVATE to that conversation (hidden folder +
        # member-only download) and never appear in the public Files tab.
        conv_field = None
        for p in parsed_parts:
            if p["name"] == "conv" and p["filename"] is None:
                conv_field = p["data"].decode("utf-8", errors="replace").strip()[:80]

        user = self._current_user()
        conv_ok = False
        if conv_field and user:
            with _lock:
                conv = _convs.get(conv_field)
                conv_ok = bool(conv) and can_see(user["username"].lower(), conv)

        if conv_ok and user:
            for p in parsed_parts:
                if not p["filename"]:
                    continue
                orig = os.path.basename(p["filename"])
                if not orig or orig.startswith("."):
                    continue
                safe_base = re.sub(r"[^A-Za-z0-9_.\- ]", "_", orig)[:120] or "file"
                stored = f"{secrets.token_hex(8)}_{safe_base}"
                dest = os.path.join(CHATFILES_DIR, stored)
                with open(dest, "wb") as f:
                    f.write(p["data"])
                add_message(conv_field, user["username"].lower(), "", msg_type="file",
                            file=orig, size=len(p["data"]),
                            private=True, stored=stored)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Uploaded chat file(s)")
            return

        # No conv → public Shared Files upload (Files tab).
        saved = []
        for p in parsed_parts:
            if not p["filename"]:
                continue
            filename = os.path.basename(p["filename"])
            if not filename or filename.startswith("."):
                continue
            dest = unique_dest(filename)
            with open(dest, "wb") as f:
                f.write(p["data"])
            saved.append((os.path.basename(dest), os.path.getsize(dest)))

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"Uploaded {len(saved)} file(s)".encode())


def reset_owner_password():
    """--reset-owner: mint a fresh owner password (returns the credentials)."""
    with _lock:
        key = next((k for k, u in _users.items() if u.get("role") == "owner"), None)
        if key is None:
            return None  # no owner yet — bootstrap_owner() will create one
        username = _users[key]["username"]
        email = _users[key]["email"]
        password = os.environ.get("LANCHAT_OWNER_PASS") or secrets.token_urlsafe(8)
        ok, err = set_password(username, password)
        if not ok:
            print(f"Could not reset the owner password: {err}")
            return None
        for tok in [t for t, s in _sessions.items() if s.get("user") == key]:
            _sessions.pop(tok, None)
        _save_sessions()
        return {"username": username, "email": email, "password": password}


def main():
    load_state()

    owner = reset_owner_password() if RESET_OWNER else None
    if owner is None:
        owner = bootstrap_owner()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    lan_ip = get_lan_ip()
    print(f"Sharing:   {SHARE_DIR}")
    print(f"Data:      {DATA_DIR}")
    print(f"Local:     http://localhost:{PORT}")
    print(f"Network:   http://{lan_ip}:{PORT}   <-- open this on other devices")
    print(f"Accounts:  {len(_users)} user(s) stored in {USERS_FILE}")
    if owner:
        banner = "Owner password reset — save these now:" if RESET_OWNER else \
                 "First run — owner account created, save these now:"
        print("")
        print(f"  *** {banner} ***")
        print(f"      username: {owner['username']}")
        print(f"      email:    {owner['email']}")
        print(f"      password: {owner['password']}")
        print("")
        print("  Sign in, then open Admin to create accounts for everyone else.")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
