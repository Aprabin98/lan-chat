"""Runtime paths, ports and CLI-mode detection (no app imports)."""
import os
import re
import sys

RESET_OWNER = "--reset-owner" in sys.argv[1:]
if RESET_OWNER:
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--reset-owner"]

# Project root = parent of src/ (falls back to script dir for legacy layout).
# __file__ is src/lanchat/config.py — project root is the parent of `src/`.
_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_HERE).lower() != "src":
    # Inside the lanchat package (or another folder): step up one level,
    # then fall back to the legacy single-file rule below.
    _HERE = os.path.dirname(_HERE)
APP_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if os.path.basename(_HERE).lower() != "src":
    APP_ROOT = os.path.abspath(_HERE)

_DEFAULT_SHARE = os.environ.get("LANCHAT_SHARE") or os.path.join(APP_ROOT, "share")
_DEFAULT_DATA = os.environ.get("LANCHAT_DATA") or os.path.join(APP_ROOT, "data")

# Parse CLI: [share_dir] [port], with bare-number first arg treated as port
# (old code treated `share.py 9000` as a directory named "9000").
# A first arg matching a subcommand (serve/users/send/...) switches to CLI
# mode instead of being mistaken for a share directory.
_CLI_SUBCOMMANDS = ("serve", "users", "convs", "send", "read",
                    "backup", "restore", "status", "help")
_CLI_FLAGS_WITH_VALUE = ("--data", "--share", "--port")


def _find_subcommand(args):
    """First non-flag token wins; flag values are skipped, so global flags
    may come before or after the subcommand word."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in _CLI_FLAGS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return (a, i) if a in _CLI_SUBCOMMANDS else (None, -1)
    return None, -1


_cli_args = sys.argv[1:]
CLI_SUBCOMMAND, _CLI_SUBINDEX = _find_subcommand(_cli_args)
SHARE_DIR = os.path.abspath(_DEFAULT_SHARE)
PORT = int(os.environ.get("LANCHAT_PORT") or 8000)
DATA_DIR = os.path.abspath(os.environ.get("LANCHAT_DATA") or _DEFAULT_DATA)


def _scan_global_flags(args):
    """Apply --data/--share/--port in `--k v` and `--k=v` forms (import time)."""
    global SHARE_DIR, PORT, DATA_DIR
    i = 0
    while i < len(args):
        a = args[i]
        if a in _CLI_FLAGS_WITH_VALUE and i + 1 < len(args):
            v = args[i + 1]
            if a == "--data":
                DATA_DIR = os.path.abspath(v)
            elif a == "--share":
                SHARE_DIR = os.path.abspath(v)
            else:
                try:
                    PORT = int(v)
                except ValueError:
                    pass
            i += 2
            continue
        if a.startswith("--data="):
            DATA_DIR = os.path.abspath(a.split("=", 1)[1])
        elif a.startswith("--share="):
            SHARE_DIR = os.path.abspath(a.split("=", 1)[1])
        elif a.startswith("--port="):
            try:
                PORT = int(a.split("=", 1)[1])
            except ValueError:
                pass
        i += 1


if CLI_SUBCOMMAND is None:
    if len(_cli_args) >= 1:
        if re.fullmatch(r"\d{2,5}", _cli_args[0] or ""):
            PORT = int(_cli_args[0])
        else:
            SHARE_DIR = os.path.abspath(_cli_args[0])
    if len(_cli_args) >= 2 and re.fullmatch(r"\d{2,5}", _cli_args[1] or ""):
        PORT = int(_cli_args[1])
    # A custom --data=... / third positional arg may override the data dir.
    for _a in _cli_args:
        if _a.startswith("--data="):
            DATA_DIR = os.path.abspath(_a.split("=", 1)[1])
    if len(_cli_args) >= 3 and not _cli_args[2].startswith("--"):
        DATA_DIR = os.path.abspath(_cli_args[2])
    os.makedirs(SHARE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    # CLI mode: honour global flags anywhere; never treat the
    # subcommand word itself as a directory.
    _scan_global_flags(_cli_args)

# ---------------------------------------------------------------------------
# State files — private app state lives in data/, never in the public share/
# ---------------------------------------------------------------------------

