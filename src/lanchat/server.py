"""HTTP server: request handler (HTML + JSON API + uploads)."""
import json
import os
import re
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from . import chat, config, store, ui, users, utils

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
        with store._lock:
            sess = store._sessions.get(token)
            if not sess:
                return None
            return store._users.get(sess.get("user"))

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
            self._send_html(ui.render_index(self._current_user()))
            return

        if path == "/api/me":
            user = self._current_user()
            self._send_json({"user": users.public_user(user)}, 200 if user else 401)
            return

        if path == "/api/users":
            if not self._require_user():
                return
            with store._lock:
                people = [users.public_user(u) for u in store._users.values()]
            people.sort(key=lambda p: p["name"].lower())
            self._send_json(people)
            return

        if path == "/api/convs":
            user = self._require_user()
            if not user:
                return
            self._send_json(chat.conversations_for(user["username"].lower()))
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
            with store._lock:
                conv = store._convs.get(conv_id)
                visible = bool(conv) and chat.can_see(user["username"].lower(), conv)
            if not visible:
                self._send_json({"error": "Not a member of that conversation"}, 403)
                return
            self._send_json(chat.get_messages(conv_id, since, user["username"].lower()))
            return

        if path == "/api/files":
            if not self._require_user():
                return
            self._send_json(ui.list_shared_files())
            return

        if path.startswith("/avatar/"):
            if not self._require_user():
                return
            name = os.path.basename(unquote(path[len("/avatar/"):]))
            ext = os.path.splitext(name)[1].lower()
            if ext not in store.ALLOWED_IMG_EXT or not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
                self.send_error(404, "Not found")
                return
            fpath = os.path.join(store.AVATARS_DIR, name)
            if not os.path.isfile(fpath):
                self.send_error(404, "Not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", store.IMG_CONTENT_TYPES.get(ext, "application/octet-stream"))
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
            with store._lock:
                found = None
                found_conv = None
                for cid, bucket in store._messages.items():
                    for m in bucket:
                        if m.get("id") == mid and m.get("type") == "file":
                            found = m
                            found_conv = store._convs.get(cid)
                            break
                    if found:
                        break
                visible = bool(found and found_conv) and chat.can_see(me, found_conv)
                if found and found.get("private") and mid in chat.hidden_ids(me, found.get("conv")):
                    visible = False
            if not visible:
                self.send_error(404, "Not found")
                return
            if found.get("private") and found.get("stored"):
                fpath = os.path.join(store.CHATFILES_DIR, os.path.basename(found["stored"]))
                disp = found.get("file") or "file"
            else:
                # Legacy chat files (previously stored publicly).
                try:
                    fpath = utils.safe_join(config.SHARE_DIR, found.get("file") or "")
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
                fpath = utils.safe_join(config.SHARE_DIR, name)
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
                fpath = utils.safe_join(config.SHARE_DIR, name)
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
            user = users.authenticate(data.get("login"), data.get("password"))
            if not user:
                time.sleep(0.3)  # slow down guessing a little
                self._send_json({"error": "Wrong username/email or password"}, 401)
                return
            token = store.create_session(user["username"].lower())
            body = json.dumps({"user": users.public_user(user)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"lanchat_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={store.SESSION_TTL}")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/logout":
            token = self._session_token()
            if token:
                store.drop_session(token)
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
            ok, result = users.create_user(data.get("username"), data.get("name"),
                                     data.get("email"), data.get("password"))
            if not ok:
                self._send_json({"error": result}, 400)
                return
            self._send_json({"ok": True, "user": users.public_user(result)})
            return

        if self.path == "/api/users/delete":
            if not self._require_owner():
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = users.delete_user(data.get("username"))
            self._send_json({"ok": ok} if ok else {"error": result}, 200 if ok else 400)
            return

        if self.path == "/api/users/password":
            if not self._require_owner():
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = users.set_password(data.get("username"), data.get("password"))
            if ok:
                # a password reset signs that user out everywhere else
                users.drop_user_sessions((data.get("username") or "").strip().lower(),
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
            ok, result = users.update_profile(user["username"].lower(),
                                        data.get("name"), data.get("email"))
            if not ok:
                self._send_json({"error": result}, 400)
                return
            self._send_json({"ok": True, "user": users.public_user(result)})
            return

        if self.path == "/api/profile/password":
            user = self._require_user()
            if not user:
                return
            data = self._json_body()
            if data is None:
                self._send_json({"error": "Bad JSON"}, 400)
                return
            ok, result = users.change_own_password(user["username"].lower(),
                                             data.get("current"),
                                             data.get("password"),
                                             self._session_token())
            self._send_json({"ok": ok} if ok else {"error": result}, 200 if ok else 400)
            return

        if self.path == "/api/avatar/remove":
            user = self._require_user()
            if not user:
                return
            users.remove_avatar(user["username"].lower())
            self._send_json({"ok": True, "user": users.public_user(self._current_user())})
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
            with store._lock:
                exists = target in store._users and target != me
            if not exists:
                self._send_json({"error": "No such user"}, 400)
                return
            self._send_json({"id": chat.get_or_create_dm(me, target)})
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
            cid = chat.create_group(data.get("name"), [str(m) for m in members], user["username"].lower())
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
            with store._lock:
                conv = store._convs.get(conv_id)
                visible = bool(conv) and chat.can_see(user["username"].lower(), conv)
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
            msg = chat.add_message(conv_id, user["username"].lower(), text, reply=reply)
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
            with store._lock:
                conv = store._convs.get(conv_id)
                visible = bool(conv) and chat.can_see(user["username"].lower(), conv)
            if not visible:
                self._send_json({"error": "Nope"}, 403)
                return
            chat.mark_read(user["username"].lower(), conv_id, last_seen)
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
            with store._lock:
                conv = store._convs.get(conv_id)
                visible = bool(conv) and chat.can_see(me, conv)
            if not visible:
                self._send_json({"error": "Not a member"}, 403)
                return
            if scope == "everyone":
                ok, err = chat.delete_message_everyone(conv_id, mid, me, is_owner)
            else:
                ok, err = chat.delete_message_for_me(me, conv_id, mid)
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
            with store._lock:
                conv = store._convs.get(conv_id)
                visible = bool(conv) and chat.can_see(me, conv)
            if not visible:
                self._send_json({"error": "Not a member"}, 403)
                return
            ok, err = chat.hide_conv_for_me(me, conv_id)
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
            with store._lock:
                conv = store._convs.get(conv_id)
                visible = bool(conv) and chat.can_see(me, conv)
            if not visible:
                self._send_json({"error": "Not a member"}, 403)
                return
            pinned = bool(data.get("pinned", True))
            ok, err = chat.set_pinned_conv(me, conv_id, pinned)
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
            ok, err = chat.delete_conv_everywhere(conv_id, me, user.get("role") == "owner")
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
        if len(data) > store.MAX_AVATAR_BYTES:
            self._send_json({"error": "Picture is too large (max 2 MB)"}, 413)
            return

        ext = os.path.splitext(os.path.basename(orig_name))[1].lower()
        if ext not in store.ALLOWED_IMG_EXT or not utils.looks_like_image(data, ext):
            self._send_json({"error": "Use a PNG, JPG, GIF or WEBP image"}, 400)
            return

        try:
            fname = users.save_avatar(user["username"].lower(), data, ext)
        except OSError:
            self._send_json({"error": "Could not save the picture"}, 500)
            return
        self._send_json({"ok": True, "avatar": fname,
                         "user": users.public_user(self._current_user())})

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
            with store._lock:
                conv = store._convs.get(conv_field)
                conv_ok = bool(conv) and chat.can_see(user["username"].lower(), conv)

        if conv_ok and user:
            for p in parsed_parts:
                if not p["filename"]:
                    continue
                orig = os.path.basename(p["filename"])
                if not orig or orig.startswith("."):
                    continue
                safe_base = re.sub(r"[^A-Za-z0-9_.\- ]", "_", orig)[:120] or "file"
                stored = f"{secrets.token_hex(8)}_{safe_base}"
                dest = os.path.join(store.CHATFILES_DIR, stored)
                with open(dest, "wb") as f:
                    f.write(p["data"])
                chat.add_message(conv_field, user["username"].lower(), "", msg_type="file",
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
            dest = utils.unique_dest(filename)
            with open(dest, "wb") as f:
                f.write(p["data"])
            saved.append((os.path.basename(dest), os.path.getsize(dest)))

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"Uploaded {len(saved)} file(s)".encode())

