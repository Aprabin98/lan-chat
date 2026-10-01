"""Entry flow: CLI dispatch or HTTP server."""
import os
import re
import secrets
import sys
from http.server import ThreadingHTTPServer

from . import cli, config, server, state, store, utils

def reset_owner_password():
    """--reset-owner: mint a fresh owner password (returns the credentials)."""
    with store._lock:
        key = next((k for k, u in store._users.items() if u.get("role") == "owner"), None)
        if key is None:
            return None  # no owner yet — bootstrap_owner() will create one
        username = store._users[key]["username"]
        email = store._users[key]["email"]
        password = os.environ.get("LANCHAT_OWNER_PASS") or secrets.token_urlsafe(8)
        ok, err = users.set_password(username, password)
        if not ok:
            print(f"Could not reset the owner password: {err}")
            return None
        for tok in [t for t, s in store._sessions.items() if s.get("user") == key]:
            store._sessions.pop(tok, None)
        store._save_sessions()
        return {"username": username, "email": email, "password": password}


def main():
    # CLI mode (users/send/read/...): run the subcommand, never the server.
    if config.CLI_SUBCOMMAND not in (None, "serve"):
        rest = [a for i, a in enumerate(config._cli_args) if i != config._CLI_SUBINDEX]
        code = cli.run_subcommand(rest)
        sys.exit(code)
    if config.CLI_SUBCOMMAND == "serve":
        # `serve [share_dir] [port] [--port N] [--share DIR] [--data DIR]`
        # (global flags may sit before or after the word `serve`).
        rest = [a for i, a in enumerate(config._cli_args) if i != config._CLI_SUBINDEX]
        opts, pos = cli._parse_kv(rest)
        err = cli._apply_global_flags(opts)
        if err:
            print(f"Error: {err}")
            sys.exit(2)
        for p in pos:
            if re.fullmatch(r"\d{2,5}", p or "") and config.PORT == int(os.environ.get("LANCHAT_PORT") or 8000):
                config.PORT = int(p)
            elif os.path.abspath(config.SHARE_DIR) == os.path.abspath(config._DEFAULT_SHARE):
                config.SHARE_DIR = os.path.abspath(p)
            elif config.DATA_DIR == os.path.abspath(config._DEFAULT_DATA):
                config.DATA_DIR = os.path.abspath(p)
        store._refresh_state_paths()
        os.makedirs(config.SHARE_DIR, exist_ok=True)
        os.makedirs(config.DATA_DIR, exist_ok=True)
    state.load_state()

    owner = reset_owner_password() if config.RESET_OWNER else None
    if owner is None:
        owner = state.bootstrap_owner()

    httpd = ThreadingHTTPServer(("0.0.0.0", config.PORT), server.Handler)
    lan_ip = utils.get_lan_ip()
    print(f"Sharing:   {config.SHARE_DIR}")
    print(f"Data:      {config.DATA_DIR}")
    print(f"Local:     http://localhost:{config.PORT}")
    print(f"Network:   http://{lan_ip}:{config.PORT}   <-- open this on other devices")
    print(f"Accounts:  {len(store._users)} user(s) stored in {store.USERS_FILE}")
    if owner:
        banner = "Owner password reset — save these now:" if config.RESET_OWNER else \
                 "First run — owner account created, save these now:"
        print("")
        print(f"  *** {banner} ***")
        print(f"      username: {owner['username']}")
        print(f"      email:    {owner['email']}")
        print(f"      password: {owner['password']}")
        print("")
        print("  Sign in, then open Admin to create accounts for everyone else.")
    print("Press Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()

