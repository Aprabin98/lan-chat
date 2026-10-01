"""Live notes: shared realtime documents scoped to conversation members.

One note has members (taken from its conversation, or explicit). Edits go
over the HTTP API with a base_version check (last-write-wins, conflicts
reported so the UI can offer reload). Every accepted edit is broadcast
over the realtime hub. Presence (who currently has the note open) is
ephemeral and tracked in memory via note.open / note.close events.
Persisted in data/livenotes.json.
"""
import json
import os
import secrets
import threading
import time

from . import chat as _chat
from . import config as _config
from . import store as _store

_lock = threading.RLock()
_notes = {}  # id -> note dict
_presence = {}  # note id -> {user_key: ts}


def _path():
    return os.path.join(_config.DATA_DIR, "livenotes.json")


def _load_file(path, default):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
    return default


def _save():
    tmp = _path() + ".tmp"
    try:
        os.makedirs(_config.DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_notes, f, ensure_ascii=False)
        os.replace(tmp, _path())
    except OSError:
        pass


def _ensure_loaded():
    with _lock:
        if _notes:
            return
        raw = _load_file(_path(), {})
        if isinstance(raw, dict):
            for nid, n in raw.items():
                if isinstance(n, dict):
                    _notes[nid] = n


def _members_for(conv_id):
    if not conv_id:
        return []
    with _lock:
        conv = _store._convs.get(conv_id)
    if not conv:
        return []
    return _chat.conv_member_keys(conv)


def _can_see(user_key, note):
    if user_key in note.get("members", []):
        return True
    conv = note.get("conv")
    if conv:
        with _lock:
            c = _store._convs.get(conv)
        return bool(c) and _chat.can_see(user_key, c)
    return False


def public_note(n):
    n = dict(n)
    return n


def presence_of(nid):
    now = time.time()
    with _lock:
        viewers = _presence.get(nid, {})
        # Expire stale viewers (crashed tabs never sent close).
        live = {u: ts for u, ts in viewers.items() if now - ts < 45}
        _presence[nid] = live
        return sorted(live)


def list_notes(user_key):
    _ensure_loaded()
    out = []
    with _lock:
        for n in _notes.values():
            if _can_see(user_key, n):
                d = public_note(n)
                d["viewers"] = presence_of(n["id"])
                out.append(d)
    out.sort(key=lambda n: n.get("updated", 0), reverse=True)
    return out


def get_note(user_key, nid):
    _ensure_loaded()
    with _lock:
        n = _notes.get(nid)
    if not n or not _can_see(user_key, n):
        return None
    d = public_note(n)
    d["viewers"] = presence_of(nid)
    return d


def get_or_create_conv_note(user_key, conv_id):
    """The shared note for a conversation (one per conv, auto-created)."""
    _ensure_loaded()
    with _lock:
        conv = _store._convs.get(conv_id)
    if not conv or not _chat.can_see(user_key, conv):
        return False, "Not a member of that conversation"
    with _lock:
        for n in _notes.values():
            if n.get("conv") == conv_id and n.get("shared"):
                d = public_note(n)
                d["viewers"] = presence_of(n["id"])
                return True, d
        nid = "ln-" + secrets.token_hex(4)
        now = time.time()
        n = {
            "id": nid, "title": "Shared note", "text": "",
            "conv": conv_id, "shared": True,
            "members": _members_for(conv_id),
            "version": 1, "updated": now, "updated_by": user_key,
        }
        _notes[nid] = n
        _save()
        d = public_note(n)
        d["viewers"] = presence_of(nid)
        return True, d


def save_note(user_key, nid, text, base_version, title=None):
    """Returns (ok, note_or_error, conflicted)."""
    _ensure_loaded()
    if text is None or len(text) > 20000:
        return False, "Note is empty or too long (max 20000 chars)", False
    with _lock:
        n = _notes.get(nid)
        if not n or not _can_see(user_key, n):
            return False, "No such note", False
        try:
            base_version = int(base_version)
        except (TypeError, ValueError):
            base_version = 0
        if base_version and base_version != n.get("version", 1):
            d = public_note(n)
            d["viewers"] = presence_of(nid)
            return False, d, True
        if title is not None and str(title).strip():
            n["title"] = str(title).strip()[:80]
        n["text"] = text
        n["version"] = int(n.get("version", 1)) + 1
        n["updated"] = time.time()
        n["updated_by"] = user_key
        # Refresh member snapshot from the conversation (new members included).
        if n.get("conv"):
            n["members"] = _members_for(n["conv"])
        _save()
        d = public_note(n)
        d["viewers"] = presence_of(nid)
    try:
        from . import realtime as _rt
        _rt.notify_note(d)
    except Exception:
        pass
    return True, d, False


def handle_presence(kind, user_key, msg):
    """note.open / note.close events from the realtime socket."""
    nid = str(msg.get("note") or msg.get("id") or "")
    if not nid:
        return
    with _lock:
        viewers = _presence.setdefault(nid, {})
        if kind == "note.open":
            viewers[user_key] = time.time()
        else:
            viewers.pop(user_key, None)
    # Tell the other members who's looking (cheap, no persistence).
    _ensure_loaded()
    with _lock:
        n = _notes.get(nid)
        members = list(n.get("members", [])) if n else []
    if members:
        try:
            from . import realtime as _rt
            payload = {"t": "note.presence", "note": nid,
                       "viewers": presence_of(nid)}
            for mk in members:
                if mk != user_key:
                    _rt.send_user(mk, payload)
        except Exception:
            pass
