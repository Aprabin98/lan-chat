"""Conversations, messages, pins, per-user hidden state, reads."""
import os
import secrets
import time

from . import store
from .store import (
    CHATFILES_DIR,
    GENERAL_CONV,
    MAX_MESSAGES_PER_CONV,
    _convs,
    _lock,
    _messages,
    _save_convs,
    _save_messages,
    _save_users,
    _users,
    display_name,
)
from .utils import human_size, icon_for

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
            "id": store.next_msg_id(),
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

