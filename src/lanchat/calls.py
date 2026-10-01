"""Voice/video calls: 1:1 WebRTC with WebSocket signaling (in-memory only).

Media is peer-to-peer (host candidates, so plain LAN works with no STUN).
The server only relays: invite -> accept/decline -> SDP/ICE Trickle ->
hangup. Unanswered invites become a "missed call" chat message.
Group conversations are 1:1-only for calls (mesh for N peers is out of
scope); the client hides call buttons outside DMs.
"""
import threading
import time

from . import chat as _chat
from . import store as _store

RING_TIMEOUT = 45  # seconds before a call is marked missed

_lock = threading.RLock()
_calls = {}  # id -> call dict
_next_call = [1]


def _new_id():
    with _lock:
        cid = f"call-{_next_call[0]}"
        _next_call[0] += 1
        return cid


def _public(call):
    return {
        "id": call["id"], "kind": call["kind"], "conv": call.get("conv"),
        "from": call["from"], "to": call["to"],
        "status": call["status"], "ts": call["ts"],
    }


def _other(call, user_key):
    return call["to"] if user_key == call["from"] else call["from"]


def _post_chat(conv_id, sender, text):
    try:
        if conv_id and _store._convs.get(conv_id):
            msg = _chat.add_message(conv_id, sender, text, msg_type="text")
            try:
                from . import realtime as _rt
                _rt.notify_message(conv_id, msg)
            except Exception:
                pass
    except Exception:
        pass


def handle_ws(kind, user_key, msg, send):
    from . import realtime as _rt
    if kind == "call.invite":
        to = str(msg.get("to") or "").strip().lower()
        conv = str(msg.get("conv") or "")
        kind_ = msg.get("kind") or "voice"
        if kind_ not in ("voice", "video"):
            send({"t": "call.error", "error": "kind must be voice or video"})
            return
        with _lock:
            peer = None
            if to:
                peer = _store._users.get(to)
            elif conv:
                c = _store._convs.get(conv)
                if c and c.get("type") == "dm":
                    others = [m for m in (c.get("members") or []) if m != user_key]
                    peer = _store._users.get(others[0]) if others else None
        if not peer:
            send({"t": "call.error", "error": "Calls work in 1:1 chats — pick a user"})
            return
        peer_key = peer["username"].lower()
        if peer_key == user_key:
            send({"t": "call.error", "error": "You can't call yourself"})
            return
        if not _rt.online(peer_key):
            send({"t": "call.error", "error": f"{peer.get('name', peer_key)} is offline"})
            return
        with _lock:
            # One active call per user at a time.
            for c in _calls.values():
                if c["status"] in ("ringing", "active") and user_key in (c["from"], c["to"]):
                    send({"t": "call.error", "error": "You're already in a call"})
                    return
            cid = _new_id()
            call = {
                "id": cid, "kind": kind_, "conv": conv or None,
                "from": user_key, "to": peer_key,
                "status": "ringing", "ts": time.time(),
            }
            _calls[cid] = call
        send({"t": "call.ringing", "call": _public(call)})
        _rt.send_user(peer_key, {"t": "call.invite", "call": {
            **_public(call),
            "from_name": (_store._users.get(user_key) or {}).get("name", user_key),
        }})
        return

    if kind == "call.accept":
        cid = str(msg.get("call") or msg.get("id") or "")
        with _lock:
            call = _calls.get(cid)
            if not call or call["status"] != "ringing":
                send({"t": "call.error", "error": "Call is gone"})
                return
            if user_key != call["to"]:
                send({"t": "call.error", "error": "Not your call"})
                return
            call["status"] = "active"
            call["started"] = time.time()
            pub = _public(call)
        _rt.send_user(call["from"], {"t": "call.accepted", "call": pub})
        send({"t": "call.accepted", "call": pub})
        return

    if kind == "call.decline":
        end_call(user_key, str(msg.get("call") or msg.get("id") or ""),
                 reason="declined")
        return

    if kind == "call.signal":
        cid = str(msg.get("call") or msg.get("id") or "")
        payload = msg.get("signal")
        if payload is None:
            return
        with _lock:
            call = _calls.get(cid)
            if not call or call["status"] != "active":
                return
            if user_key not in (call["from"], call["to"]):
                return
            peer = _other(call, user_key)
            pub = _public(call)
        _rt.send_user(peer, {"t": "call.signal", "call": pub["id"],
                             "from": user_key, "signal": payload})
        return

    if kind == "call.end":
        end_call(user_key, str(msg.get("call") or msg.get("id") or ""),
                 reason="ended")
        return


def end_call(user_key, cid, reason="ended"):
    """Hangup/decline/timeout. Posts missed-call or duration messages."""
    from . import realtime as _rt
    with _lock:
        call = _calls.get(cid)
        if not call:
            return
        if user_key and user_key not in (call["from"], call["to"]):
            return
        if call["status"] not in ("ringing", "active"):
            return
        was_ringing = call["status"] == "ringing"
        duration = int(time.time() - call.get("started", call["ts"])) \
            if not was_ringing else 0
        call["status"] = "ended"
        pub = _public(call)
        peers = [call["from"], call["to"]]
        conv = call.get("conv")
    for p in peers:
        _rt.send_user(p, {"t": "call.ended", "call": pub,
                           "reason": reason, "duration": duration})
    if not conv:
        return
    kind_label = "Video" if call["kind"] == "video" else "Voice"
    if was_ringing and reason != "declined-by-caller":
        leaver = call["from"]
        _post_chat(conv, leaver,
                   f"Missed {kind_label.lower()} call from "
                   f"{(_store._users.get(leaver) or {}).get('name', leaver)}")
    elif duration > 0:
        mins, secs = divmod(duration, 60)
        stamp = f"{mins}:{secs:02d}" if mins else f"0:{secs:02d}"
        _post_chat(conv, call["from"], f"{kind_label} call · {stamp}")


def sweep_missed():
    """Expire stale ringing calls (called opportunistically on new invites)."""
    now = time.time()
    stale = []
    with _lock:
        for cid, call in _calls.items():
            if call["status"] == "ringing" and now - call["ts"] > RING_TIMEOUT:
                stale.append(cid)
    for cid in stale:
        end_call("", cid, reason="missed")
