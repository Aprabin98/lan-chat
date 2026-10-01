"""Load/bootstrap/reset persisted state (operates on store in place)."""
import os
import re
import secrets
import time

from . import chat, config, store, users

def load_state():
    store.setup()
    raw_users = store._load_json(store.USERS_FILE, {})
    store._users.clear()
    store._users.update({k.lower(): v for k, v in raw_users.items() if isinstance(v, dict)} if isinstance(raw_users, dict) else {})

    now = time.time()
    raw_sessions = store._load_json(store.SESSIONS_FILE, {})
    store._sessions.clear()
    if isinstance(raw_sessions, dict):
        for tok, s in raw_sessions.items():
            if isinstance(s, dict) and now - s.get("created", 0) < store.SESSION_TTL and s.get("user") in store._users:
                store._sessions[tok] = s

    raw_convs = store._load_json(store.CONVS_FILE, {})
    store._convs.clear()
    store._convs.update({k: v for k, v in raw_convs.items() if isinstance(v, dict)} if isinstance(raw_convs, dict) else {})

    raw_msgs = store._load_json(store.MESSAGES_FILE, {})
    store._messages.clear()
    store._messages.update({k: v for k, v in raw_msgs.items() if isinstance(v, list)} if isinstance(raw_msgs, dict) else {})

    max_id = 0
    for bucket in store._messages.values():
        for m in bucket:
            if isinstance(m, dict):
                max_id = max(max_id, m.get("id", 0))
    store.reset_msg_id(max_id + 1)

    # Migrate history from the original single-room version of this app.
    if not store._messages:
        legacy = store._load_json(store.LEGACY_HISTORY, [])
        if isinstance(legacy, list) and legacy:
            chat.ensure_general()
            bucket = store._messages.setdefault(store.GENERAL_CONV, [])
            for lm in legacy:
                if not isinstance(lm, dict):
                    continue
                lm = dict(lm)
                lm["id"] = store.next_msg_id()
                lm["conv"] = store.GENERAL_CONV
                lm.setdefault("name", lm.get("user", "Anonymous"))
                lm.setdefault("type", "text")
                bucket.append(lm)
            bucket.sort(key=lambda m: m.get("id", 0))
            store._save_messages()

    chat.ensure_general()


def bootstrap_owner():
    """Create the owner's account on the very first run."""
    with store._lock:
        if store._users:
            return None
        username = os.environ.get("LANCHAT_OWNER", "admin")
        email = os.environ.get("LANCHAT_OWNER_EMAIL", "owner@localhost")
        password = os.environ.get("LANCHAT_OWNER_PASS") or secrets.token_urlsafe(8)
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,24}", username):
            username = "admin"
        salt, digest = users.hash_password(password)
        store._users[username.lower()] = {
            "username": username,
            "name": username,
            "email": email,
            "pw": digest,
            "salt": salt,
            "role": "owner",
            "reads": {},
            "created": time.time(),
        }
        store._save_users()
        return {"username": username, "email": email, "password": password}


# ---------------------------------------------------------------------------
# HTML / UI
# ---------------------------------------------------------------------------

