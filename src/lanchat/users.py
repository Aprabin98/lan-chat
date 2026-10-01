"""Accounts, passwords, sessions, avatars."""
import hashlib
import hmac
import os
import re
import secrets
import time

from .store import (
    AVATARS_DIR,
    _convs,
    _lock,
    _save_convs,
    _save_sessions,
    _save_users,
    _sessions,
    _users,
)

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

