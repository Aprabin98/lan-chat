"""Terminal commands: status/users/convs/send/read/backup/restore."""
import os
import sys
import time
import zipfile

from . import chat, config, state, store, users, utils

def _apply_global_flags(opts):
    """Apply --data/--share/--port shared by every subcommand."""
    err = None
    if opts.get("data"):
        config.DATA_DIR = os.path.abspath(opts["data"])
    if opts.get("share"):
        config.SHARE_DIR = os.path.abspath(opts["share"])
    if opts.get("port"):
        try:
            config.PORT = int(opts["port"])
        except (TypeError, ValueError):
            err = "port must be a number"
    store._refresh_state_paths()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    return err


def _parse_kv(args):
    """Split `--key value` / `--key=value` / `--flag` from positionals."""
    opts, pos = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a[2:].split("=", 1)
                opts[k] = v
            elif i + 1 < len(args) and not args[i + 1].startswith("--"):
                opts[a[2:]] = args[i + 1]
                i += 1
            else:
                opts[a[2:]] = True
        else:
            pos.append(a)
        i += 1
    return opts, pos


def _require_opt(opts, *names):
    for n in names:
        v = opts.get(n)
        if v and v is not True:
            return str(v)
    return None


def _fmt_ts(ts):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "--"


CLI_HELP = """Usage:
  python src/share.py [share_dir] [port]          serve (legacy form)
  python src/share.py serve [share_dir] [port]    serve explicitly

  python src/share.py status
  python src/share.py users list
  python src/share.py users create --username U --name N --email E --password P [--role user]
  python src/share.py users delete --username U [--yes]
  python src/share.py users password --username U --password P
  python src/share.py convs list [--user U]
  python src/share.py send --as USER (--conv ID | --to USER | --group NAME --members a,b) --text TEXT
  python src/share.py read --conv ID [--as USER] [--since N] [--limit N]
  python src/share.py backup [--out FILE]
  python src/share.py restore --in FILE [--yes]

Global flags (every command): --data DIR  --share DIR  --port N
--reset-owner works with the serve form. --text - reads the message from stdin."""


def cmd_status(opts, pos):
    lan = utils.get_lan_ip()
    with store._lock:
        n_users, n_convs = len(store._users), len(store._convs)
        n_msgs = sum(len(b) for b in store._messages.values())
        n_sess = len(store._sessions)
        owner = next((u["username"] for u in store._users.values() if u.get("role") == "owner"), "-")
    print(f"Share dir:  {config.SHARE_DIR}")
    print(f"Data dir:   {config.DATA_DIR}")
    print(f"Local:      http://localhost:{config.PORT}")
    print(f"Network:    http://{lan}:{config.PORT}")
    print(f"Users:      {n_users} (owner: {owner})")
    print(f"Convs:      {n_convs}")
    print(f"Messages:   {n_msgs}")
    print(f"Sessions:   {n_sess}")
    return 0


def cmd_users(opts, pos):
    if not pos:
        print("users needs an action: list | create | delete | password")
        return 2
    action = pos[0]
    if action == "list":
        rows = sorted(store._users.values(), key=lambda u: u.get("username", ""))
        if not rows:
            print("No users yet.")
            return 0
        print(f"{'USERNAME':<16}{'NAME':<20}{'EMAIL':<28}ROLE")
        for u in rows:
            print(f"{u.get('username',''):<16}{u.get('name',''):<20}"
                  f"{u.get('email',''):<28}{u.get('role','user')}")
        return 0
    if action == "create":
        username = _require_opt(opts, "username", "user")
        email = _require_opt(opts, "email")
        password = _require_opt(opts, "password", "pass")
        name = _require_opt(opts, "name") or (username or "")
        role = _require_opt(opts, "role") or "user"
        if not username or not email or not password:
            print("Usage: users create --username U --name N --email E --password P [--role user]")
            return 2
        if role not in ("user", "owner"):
            print("role must be user or owner")
            return 2
        ok, res = users.create_user(username, name, email, password, role)
        if not ok:
            print(f"Error: {res}")
            return 1
        print(f"Created {res['username']} ({res['email']})")
        return 0
    if action == "delete":
        username = _require_opt(opts, "username", "user")
        if not username:
            print("Usage: users delete --username U [--yes]")
            return 2
        if not opts.get("yes") and not opts.get("y"):
            try:
                ans = input(f"Delete user {username}? They are signed out everywhere. [y/N] ")
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return 1
            if ans.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 1
        ok, res = users.delete_user(username)
        if not ok:
            print(f"Error: {res}")
            return 1
        print(f"Deleted {username}")
        return 0
    if action == "password":
        username = _require_opt(opts, "username", "user")
        password = _require_opt(opts, "password", "pass")
        if not username or not password:
            print("Usage: users password --username U --password P")
            return 2
        ok, res = users.set_password(username, password)
        if not ok:
            print(f"Error: {res}")
            return 1
        users.drop_user_sessions(username.strip().lower())
        print(f"Password updated for {username} (signed out everywhere)")
        return 0
    print(f"Unknown users action: {action}")
    return 2


def cmd_convs(opts, pos):
    as_user = (_require_opt(opts, "user", "as") or "").lower()
    with store._lock:
        items = list(store._convs.values())
    if as_user:
        items = [c for c in items if chat.can_see(as_user, c)]
    if not items:
        print("No conversations.")
        return 0
    print(f"{'ID':<24}{'TYPE':<8}{'MESSAGES':<9}TITLE / MEMBERS")
    for c in sorted(items, key=lambda c: c.get("created", 0)):
        n = len(store._messages.get(c["id"], []))
        if c.get("type") == "dm":
            title = "DM: " + ",".join(c.get("members") or [])
        elif c.get("type") == "notes":
            title = "Notes: " + ",".join(c.get("members") or [])
        else:
            title = c.get("name") or c["id"]
        print(f"{c['id']:<24}{c.get('type',''):<8}{n:<9}{title}")
    return 0


def cmd_send(opts, pos):
    sender = (_require_opt(opts, "as", "from", "user") or "").lower()
    text = _require_opt(opts, "text", "message", "msg")
    if text == "-":
        text = sys.stdin.read().strip()
    if not sender:
        print("Usage: send --as USER (--conv ID | --to USER | --group NAME --members a,b) --text TEXT")
        return 2
    if not text:
        print("Nothing to send (empty --text).")
        return 2
    if len(text) > 2000:
        text = text[:2000]
    with store._lock:
        if sender not in store._users:
            print(f"Error: no such user '{sender}'")
            return 1
    conv_id = _require_opt(opts, "conv")
    target = _require_opt(opts, "to", "dm")
    if conv_id is None and target:
        target = target.strip().lower()
        with store._lock:
            if target not in store._users:
                print(f"Error: no such user '{target}'")
                return 1
        conv_id = chat.get_or_create_dm(sender, target)
    group = _require_opt(opts, "group")
    if conv_id is None and group:
        members = [m.strip() for m in (_require_opt(opts, "members") or "").split(",") if m.strip()]
        if not members:
            print("send --group needs --members user1,user2")
            return 2
        conv_id = chat.create_group(group, members, sender)
        print(f"Created group {conv_id}")
    if conv_id is None:
        print("Usage: send --as USER (--conv ID | --to USER | --group NAME --members a,b) --text TEXT")
        return 2
    with store._lock:
        conv = store._convs.get(conv_id)
        if not conv or not chat.can_see(sender, conv):
            print(f"Error: '{sender}' cannot post to '{conv_id}'")
            return 1
    msg = chat.add_message(conv_id, sender, text)
    print(f"Sent #{msg['id']} to {conv_id}")
    return 0


def cmd_read(opts, pos):
    conv_id = _require_opt(opts, "conv", "id") or (pos[0] if pos else None)
    if not conv_id:
        print("Usage: read --conv ID [--as USER] [--since N] [--limit N]")
        return 2
    as_user = (_require_opt(opts, "as", "user") or "").lower() or None
    try:
        since = int(_require_opt(opts, "since") or 0)
    except ValueError:
        print("--since must be a number")
        return 2
    try:
        limit = int(_require_opt(opts, "limit") or 50)
    except ValueError:
        print("--limit must be a number")
        return 2
    with store._lock:
        conv = store._convs.get(conv_id)
        if not conv:
            print(f"Error: no such conversation '{conv_id}'")
            return 1
        if as_user and not chat.can_see(as_user, conv):
            print(f"Error: '{as_user}' is not a member of '{conv_id}'")
            return 1
    msgs = chat.get_messages(conv_id, since, as_user)
    msgs = msgs[-limit:] if limit > 0 else msgs
    if not msgs:
        print("(no messages)")
        return 0
    for m in msgs:
        who = m.get("name") or m.get("user")
        if m.get("type") == "file":
            body = f"📎 {m.get('file')} ({m.get('size_human','')})"
        else:
            body = m.get("text", "")
        print(f"[{m.get('id')}] {_fmt_ts(m.get('ts'))} {who}: {body}")
    return 0


def cmd_backup(opts, pos):
    out = _require_opt(opts, "out", "o", "file")
    if not out:
        out = f"lanchat-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    out = os.path.abspath(out)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(config.DATA_DIR):
                for f in sorted(files):
                    if f.endswith(".tmp"):
                        continue
                    full = os.path.join(root, f)
                    zf.write(full, os.path.relpath(full, config.DATA_DIR))
    except OSError as e:
        print(f"Error: cannot write {out}: {e}")
        return 1
    print(f"Backed up {config.DATA_DIR} -> {out}")
    return 0


def cmd_restore(opts, pos):
    src = _require_opt(opts, "in", "file") or (pos[0] if pos else None)
    if not src or not os.path.isfile(src):
        print("Usage: restore --in FILE [--yes]")
        return 2
    if not opts.get("yes") and not opts.get("y"):
        try:
            ans = input(f"Restore {src} into {config.DATA_DIR}? Stop the server first. [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    try:
        with zipfile.ZipFile(src, "r") as zf:
            for member in zf.namelist():
                # Zip-slip guard: only relative paths inside DATA_DIR.
                dest = os.path.normpath(os.path.join(config.DATA_DIR, member))
                if dest != config.DATA_DIR and not dest.startswith(os.path.abspath(config.DATA_DIR) + os.sep):
                    print(f"Skipping unsafe entry: {member}")
                    continue
                zf.extract(member, config.DATA_DIR)
    except (zipfile.BadZipFile, OSError) as e:
        print(f"Error: cannot restore: {e}")
        return 1
    print(f"Restored {src} -> {config.DATA_DIR} (restart the server)")
    return 0


def run_subcommand(argv):
    """Dispatch CLI subcommands. argv excludes the subcommand word itself."""
    if config.CLI_SUBCOMMAND in (None, "help"):
        print(CLI_HELP)
        return 0
    if config.CLI_SUBCOMMAND == "serve":
        return None  # handled by main(): fall through to the server flow
    opts, pos = _parse_kv(argv)
    err = _apply_global_flags(opts)
    if err:
        print(f"Error: {err}")
        return 2
    os.makedirs(config.DATA_DIR, exist_ok=True)
    state.load_state()
    if config.CLI_SUBCOMMAND == "status":
        return cmd_status(opts, pos)
    if config.CLI_SUBCOMMAND == "users":
        return cmd_users(opts, pos)
    if config.CLI_SUBCOMMAND == "convs":
        return cmd_convs(opts, pos)
    if config.CLI_SUBCOMMAND == "send":
        return cmd_send(opts, pos)
    if config.CLI_SUBCOMMAND == "read":
        return cmd_read(opts, pos)
    if config.CLI_SUBCOMMAND == "backup":
        return cmd_backup(opts, pos)
    if config.CLI_SUBCOMMAND == "restore":
        return cmd_restore(opts, pos)
    print(CLI_HELP)
    return 2

