"""Real-time layer: RFC 6455 WebSocket endpoint + broadcast hub (stdlib only).

The HTTP handler upgrades `/ws` connections here. Auth reuses the session
cookie, so only signed-in users can connect. Everything else in the app
(chat, games, notes, calls) pushes through Hub; the 1.3 s HTTP polling in
the client stays as an automatic fallback if the socket drops.
"""
import base64
import hashlib
import json
import select
import struct
import threading

from . import chat as _chat
from . import store as _store

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 4 * 1024 * 1024  # 4 MB — notes/calls stay far below this

_lock = threading.RLock()
_conns = {}  # conn_id -> {"user": key, "send": fn, "sock": sock}
_next_conn = [1]


def ws_accept_key(key):
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def send_frame(sock, send_lock, opcode, payload):
    """Server -> client frame (never masked). opcode 0x1=text 0x8=close 0x9=ping 0xA=pong."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    header = bytes([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack("!H", n)
    else:
        header += bytes([127]) + struct.pack("!Q", n)
    with send_lock:
        sock.sendall(header + payload)


def _read_exact(rfile, n):
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def recv_message(rfile):
    """Read one full client message (reassembles fragments, answers nothing).

    Returns (opcode, bytes) or raises ConnectionError on close/error.
    Control frames are handled inline: ping is answered by the caller via
    the returned ('ping', payload) tuple, close raises ConnectionError.
    """
    text_parts = []
    while True:
        head = _read_exact(rfile, 2)
        b1, b2 = head[0], head[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", _read_exact(rfile, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(rfile, 8))[0]
        if length > MAX_FRAME:
            raise ConnectionError("frame too large")
        mask = _read_exact(rfile, 4) if masked else None
        payload = _read_exact(rfile, length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x8:  # close
            raise ConnectionError("client close")
        if opcode == 0x9:  # ping
            return ("ping", payload)
        if opcode == 0xA:  # pong
            return ("pong", payload)
        if opcode == 0x1 or opcode == 0x0:  # text / continuation
            text_parts.append(payload)
            if fin:
                return ("text", b"".join(text_parts))
        elif opcode == 0x2:  # binary: not used, skip
            if fin:
                return ("binary", payload)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
def register(user_key, send_fn, sock):
    with _lock:
        cid = _next_conn[0]
        _next_conn[0] += 1
        _conns[cid] = {"user": user_key, "send": send_fn, "sock": sock}
        return cid


def unregister(cid):
    with _lock:
        _conns.pop(cid, None)


def online(user_key):
    with _lock:
        return any(c["user"] == user_key for c in _conns.values())


def online_users():
    with _lock:
        return sorted({c["user"] for c in _conns.values()})


def send_user(user_key, obj):
    """Push one JSON event to every socket of a user. Returns True if sent."""
    with _lock:
        targets = [c["send"] for c in _conns.values() if c["user"] == user_key]
    ok = False
    for send in targets:
        try:
            send(obj)
            ok = True
        except (OSError, ConnectionError):
            pass
    return ok


def send_conn(cid, obj):
    with _lock:
        c = _conns.get(cid)
    if c:
        try:
            c["send"](obj)
            return True
        except (OSError, ConnectionError):
            return False
    return False


def broadcast_conv(conv_id, obj, exclude_user=None):
    """Push to all online members of a conversation (lazy chat import safe)."""
    try:
        members = _chat.conv_member_keys(_chat._convs.get(conv_id) or {})
    except Exception:
        members = []
    if not members and conv_id:
        # Fall back: conversation may live in store directly.
        try:
            conv = _store._convs.get(conv_id)
            members = _chat.conv_member_keys(conv) if conv else []
        except Exception:
            members = []
    for mk in members:
        if exclude_user and mk == exclude_user:
            continue
        send_user(mk, obj)


def broadcast_all(obj, exclude_user=None):
    with _lock:
        targets = [(cid, c["send"]) for cid, c in _conns.items()
                   if not exclude_user or c["user"] != exclude_user]
    for cid, send in targets:
        try:
            send(obj)
        except (OSError, ConnectionError):
            unregister(cid)


# ---- app-level notify helpers (called by HTTP handlers after commits) ----
def notify_message(conv_id, msg):
    broadcast_conv(conv_id, {"t": "conv.message", "conv": conv_id, "msg": msg})


def notify_game(game):
    payload = {"t": "game.state", "game": game}
    for mk in game.get("players", []) + game.get("watchers", []):
        send_user(mk, payload)
    if game.get("conv"):
        broadcast_conv(game["conv"], payload)


def notify_note(note, presence=None):
    payload = {"t": "note.state", "note": note}
    if presence is not None:
        payload["presence"] = presence
    for mk in note.get("members", []):
        send_user(mk, payload)


def serve_ws(handler, user_key):
    """Upgrade handler._current connection to WebSocket and run its loop."""
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        handler.send_error(400, "WebSocket upgrade required")
        return
    body = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {ws_accept_key(key.strip())}\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        handler.connection.sendall(body)
    except OSError:
        return
    handler.close_connection = True  # don't process further HTTP on this socket
    sock = handler.connection
    rfile = sock.makefile("rb")
    send_lock = threading.Lock()

    def send(obj):
        send_frame(sock, send_lock, 0x1, json.dumps(obj, ensure_ascii=False))

    cid = register(user_key, send, sock)
    try:
        send({"t": "hello", "you": user_key, "online": online_users()})
        while True:
            rlist, _, _ = select.select([sock], [], [], 30)
            if not rlist:
                try:
                    send_frame(sock, send_lock, 0x9, b"ping")
                except (OSError, ConnectionError):
                    break
                continue
            try:
                kind, payload = recv_message(rfile)
            except ConnectionError:
                break
            if kind == "ping":
                try:
                    send_frame(sock, send_lock, 0xA, payload)
                except (OSError, ConnectionError):
                    break
                continue
            if kind != "text":
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict):
                _route(cid, user_key, send, msg)
    finally:
        unregister(cid)
        try:
            send_frame(sock, send_lock, 0x8, b"")
        except (OSError, ConnectionError):
            pass
        try:
            rfile.close()
        except OSError:
            pass


def _route(cid, user_key, send, msg):
    """Client -> server realtime messages (calls + note presence)."""
    t = msg.get("t")
    if t == "ping":
        send({"t": "pong"})
        return
    if t in ("call.invite", "call.accept", "call.decline", "call.signal", "call.end"):
        from . import calls as _calls
        _calls.handle_ws(t, user_key, msg, send)
        return
    if t in ("note.open", "note.close"):
        from . import livenotes as _notes
        _notes.handle_presence(t, user_key, msg)
        return
