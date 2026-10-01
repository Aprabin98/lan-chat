# Shaare — LAN Share + Messenger

A single-file LAN chat and file-sharing app with zero external dependencies (pure Python standard library). Open one address on your network and get Messenger-style chats plus a shared Files tab.

## Features

- **Accounts & roles** — owner creates users (username, display name, email, password); everyone signs in with username or email. Owner-only Admin panel with create / reset-password / delete.
- **Chats** — 1:1 DMs, group chats, a `General` room for everyone, and private `My Notes` per user.
- **Messenger behavior** — bubbles, replies, copy, read-more for long messages, per-conversation unread counts, `Delete for me` (per message or whole chat) and owner `Unsend / Delete group for everyone`.
- **Chat extras** — http/https links auto-linkify, chat pinning to top, tab unread counter + favicon badge that sticks until the chat is viewed, notification sound with mute toggle, dark / light mode.
- **Attachments** — private per-conversation chat files (member-only download, image preview) separate from the public Files tab.
- **Files tab** — drag-and-drop upload, download, delete, human sizes, per-type icons.
- **Profiles** — display name, email, avatar upload (PNG/JPG/GIF/WEBP, magic-byte checked, 2 MB max).
- **Mobile responsive** — single-column layout with back-button navigation on small screens.
- **Persistence** — atomic JSON writes under `data/`; 30-day sessions; no database.

## Quickstart

Requires Python 3 (no pip install needed).

```bash
python src/share.py            # share ./share on port 8000
python src/share.py 9000       # share ./share on port 9000
```

Then open:

- Local: `http://localhost:8000`
- Other devices on the LAN: `http://<your-lan-ip>:8000` (printed on startup)

First run creates an **owner** account and prints the credentials once — save them, sign in, then create accounts for everyone else in **Admin**.

## Usage

```bash
python src/share.py [share_dir] [port]
python src/share.py serve [share_dir] [port]   # explicit form
python src/share.py --reset-owner   # mint a fresh owner password
python src/share.py help            # list all CLI commands
```

| Example                          | Meaning                  |
| -------------------------------- | ------------------------ |
| `python src/share.py`            | `./share` on port `8000`  |
| `python src/share.py 9000`       | `./share` on port `9000`  |
| `python src/share.py /path/to/dir` | custom dir, port `8000` |
| `python src/share.py /path/to/dir 9000` | custom dir + port |

Environment overrides:

| Variable            | Default              | Purpose                          |
| ------------------- | -------------------- | -------------------------------- |
| `LANCHAT_SHARE`     | `<project>/share`    | public shared-files directory    |
| `LANCHAT_DATA`      | `<project>/data`     | private state (users, chats, …)  |
| `LANCHAT_PORT`      | `8000`               | port (CLI arg wins)              |
| `LANCHAT_OWNER`     | `admin`              | first-run owner username         |
| `LANCHAT_OWNER_EMAIL` | `owner@localhost`  | first-run owner email            |
| `LANCHAT_OWNER_PASS` | random, printed once | first-run / reset owner password |

## CLI reference

Everything also works from the terminal (same defaults, no server needed except `serve`):

```bash
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
python src/share.py serve [share_dir] [port]
```

Global flags for every command: `--data DIR  --share DIR  --port N` (before or after the subcommand). `--text -` reads the message from stdin. Stop the server before `restore`.

## Project layout
```text
shaare/
  src/share.py      # thin entry shim (keeps `python src/share.py ...` working)
  src/lanchat/      # the app, one module per concern (stdlib only)
    config.py       # paths, port, env vars, CLI-mode detection
    store.py        # shared state, JSON persistence, legacy migration
    utils.py        # file/formatting helpers
    users.py        # accounts, passwords, sessions, avatars
    chat.py         # conversations, messages, pins, hidden state, reads
    state.py        # load/bootstrap/reset persisted state
    ui.py           # HTML/CSS/JS template + page rendering
    server.py       # HTTP handler (pages, JSON API, uploads)
    cli.py          # terminal commands (status/users/convs/send/...)
    main.py         # entry flow: CLI dispatch or HTTP server
  share/            # public shared files (Files tab) — runtime, git-ignored
  data/             # private state — runtime, git-ignored
    users.json sessions.json convs.json messages.json
    avatars/ chatfiles/
```

Legacy dotfiles (`.lanchat_*.json`) from older layouts auto-migrate into `data/` on first run.

## Notes

- **Deleting a 1:1 chat** removes it for you only; old messages stay hidden and only new arrivals reappear.
- **Unread badges** clear only when you actually view the chat — switching tabs, minimizing, or sitting on Files/Profile keeps the tab counter until you return.
- **Passwords** are stored as salted PBKDF2-SHA256 hashes. Changing your password signs out your other devices; an owner password reset signs that user out everywhere.
- **Chat history** is capped at 500 messages per conversation (oldest trimmed, orphaned private files cleaned up).
- Stop with `Ctrl+C`.
