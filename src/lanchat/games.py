"""Game hub: tic-tac-toe vs friend, vs bot, persisted in data/games.json.

A game belongs to an optional conversation (its members may watch) or is
just between its players. Moves come in over the HTTP API; the server
broadcasts the new state over the realtime hub. Finished games post a
one-line result into the linked conversation.
"""
import json
import os
import random
import threading
import time

from . import chat as _chat
from . import config as _config
from . import store as _store

LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)

_lock = threading.RLock()
_games = {}  # id -> game dict


def _path():
    return os.path.join(_config.DATA_DIR, "games.json")


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
            json.dump(_games, f, ensure_ascii=False)
        os.replace(tmp, _path())
    except OSError:
        pass


def _ensure_loaded():
    with _lock:
        if _games:
            return
        raw = _load_file(_path(), {})
        if isinstance(raw, dict):
            for gid, g in raw.items():
                if isinstance(g, dict):
                    _games[gid] = g


# ---------------------------------------------------------------------------
# Engine (pure)
# ---------------------------------------------------------------------------
def winner_of(board):
    for a, b, c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(cell for cell in board):
        return "draw"
    return None


def _minimax(board, me, turn, alpha, beta):
    result = winner_of(board)
    if result == me:
        return 10, None
    if result == "draw":
        return 0, None
    if result is not None:
        return -10, None
    best_pos = None
    if turn == me:
        best = -100
        for i in range(9):
            if not board[i]:
                board[i] = turn
                score, _ = _minimax(board, me, "O" if turn == "X" else "X", alpha, beta)
                board[i] = ""
                if score > best:
                    best, best_pos = score, i
                alpha = max(alpha, best)
                if beta <= alpha:
                    break
        return best, best_pos
    best = 100
    for i in range(9):
        if not board[i]:
            board[i] = turn
            score, _ = _minimax(board, me, "O" if turn == "X" else "X", alpha, beta)
            board[i] = ""
            if score < best:
                best, best_pos = score, i
            beta = min(beta, best)
            if beta <= alpha:
                break
    return best, best_pos


def bot_move(board, mark, level="hard"):
    empty = [i for i, c in enumerate(board) if not c]
    if not empty:
        return None
    if level == "easy":
        return random.choice(empty)
    _, pos = _minimax(list(board), mark, mark, -100, 100)
    return pos if pos is not None else random.choice(empty)


def new_board():
    return [""] * 9


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def public_game(g):
    g = dict(g)
    return g


def _assert_member(user_key, game):
    conv = game.get("conv")
    if conv:
        with _lock:
            c = _store._convs.get(conv)
        if c and not _chat.can_see(user_key, c):
            return False, "Not a member of that conversation"
    elif user_key not in game.get("players", []) and user_key not in game.get("watchers", []):
        return False, "Not a player of that game"
    return True, "ok"


def create_game(creator, mode="bot", level="hard", conv=None, opponent=None):
    """mode: bot | friend. Returns (ok, game_or_error)."""
    _ensure_loaded()
    creator = (creator or "").lower()
    if mode not in ("bot", "friend"):
        return False, "mode must be bot or friend"
    if conv:
        with _lock:
            c = _store._convs.get(conv)
        if not c or not _chat.can_see(creator, c):
            return False, "Not a member of that conversation"
    gid = "ttt-" + os.urandom(4).hex()
    now = time.time()
    if mode == "bot":
        game = {
            "id": gid, "kind": "tictactoe", "mode": "bot",
            "level": level if level in ("easy", "hard") else "hard",
            "players": [creator], "marks": {creator: "X"},
            "board": new_board(), "turn": creator,
            "status": "playing", "winner": None,
            "conv": conv, "watchers": [],
            "created": now, "updated": now, "moves": 0,
        }
    else:
        opp = (opponent or "").strip().lower() or None
        if opp and opp == creator:
            return False, "Pick someone else as opponent"
        game = {
            "id": gid, "kind": "tictactoe", "mode": "friend",
            "level": None, "players": [creator], "marks": {creator: "X"},
            "board": new_board(), "turn": None,
            "status": "waiting", "winner": None,
            "invited": opp, "conv": conv, "watchers": [],
            "created": now, "updated": now, "moves": 0,
        }
    with _lock:
        _games[gid] = game
        _save()
    return True, public_game(game)


def list_games(user_key):
    _ensure_loaded()
    out = []
    with _lock:
        for g in _games.values():
            if user_key in g.get("players", []):
                out.append(public_game(g))
                continue
            conv = g.get("conv")
            if conv:
                c = _store._convs.get(conv)
                if c and _chat.can_see(user_key, c):
                    out.append(public_game(g))
    out.sort(key=lambda g: g.get("updated", 0), reverse=True)
    return out


def get_game(user_key, gid):
    _ensure_loaded()
    with _lock:
        g = _games.get(gid)
    if not g:
        return None
    ok, _ = _assert_member(user_key, g)
    return public_game(g) if ok else None


def accept_game(user_key, gid):
    _ensure_loaded()
    with _lock:
        g = _games.get(gid)
        if not g:
            return False, "No such game"
        if g["status"] != "waiting":
            return False, "Game already started"
        if user_key == g["players"][0]:
            return False, "Wait for your opponent"
        invited = g.get("invited")
        if invited and user_key != invited:
            return False, "That invite is for someone else"
        conv = g.get("conv")
        if conv:
            c = _store._convs.get(conv)
            if c and not _chat.can_see(user_key, c):
                return False, "Not a member of that conversation"
        g["players"].append(user_key)
        g["marks"][user_key] = "O"
        g["turn"] = g["players"][0]
        g["status"] = "playing"
        g["updated"] = time.time()
        _save()
        return True, public_game(g)


def _finish(g, result):
    g["status"] = "win" if result in ("X", "O") else "draw"
    g["winner"] = result if result in ("X", "O") else None
    g["turn"] = None
    g["updated"] = time.time()
    conv = g.get("conv")
    if conv and _store._convs.get(conv):
        try:
            if result == "draw":
                text = "Tic-tac-toe ended in a draw."
                poster = g["players"][0]
            else:
                winner = next((p for p, m in g["marks"].items() if m == result), "?")
                wname = _store._users.get(winner, {}).get("name", winner)
                text = f"Tic-tac-toe: {wname} wins!"
                poster = winner
            msg = _chat.add_message(conv, poster, text, msg_type="text")
            try:
                from . import realtime as _rt
                _rt.notify_message(conv, msg)
            except Exception:
                pass
        except Exception:
            pass


def make_move(user_key, gid, pos):
    _ensure_loaded()
    try:
        pos = int(pos)
    except (TypeError, ValueError):
        return False, "Pick a square 0-8"
    if pos < 0 or pos > 8:
        return False, "Pick a square 0-8"
    with _lock:
        g = _games.get(gid)
        if not g:
            return False, "No such game"
        ok, err = _assert_member(user_key, g)
        if not ok:
            return False, err
        if g["status"] != "playing":
            return False, "Game is over"
        if g["turn"] != user_key:
            return False, "Not your turn"
        if g["board"][pos]:
            return False, "Square taken"
        mark = g["marks"][user_key]
        g["board"][pos] = mark
        g["moves"] += 1
        result = winner_of(g["board"])
        if result:
            _finish(g, result)
            _save()
            return True, public_game(g)
        # Next turn: other player, or the bot.
        if g["mode"] == "bot":
            bmark = "O" if mark == "X" else "X"
            bpos = bot_move(g["board"], bmark, g.get("level") or "hard")
            if bpos is not None:
                g["board"][bpos] = bmark
                g["moves"] += 1
                result = winner_of(g["board"])
                if result:
                    _finish(g, result)
                    _save()
                    return True, public_game(g)
            g["turn"] = user_key
        else:
            others = [p for p in g["players"] if p != user_key]
            g["turn"] = others[0] if others else None
        g["updated"] = time.time()
        _save()
        return True, public_game(g)


def forfeit(user_key, gid):
    _ensure_loaded()
    with _lock:
        g = _games.get(gid)
        if not g:
            return False, "No such game"
        if user_key not in g.get("players", []):
            return False, "Not a player"
        if g["status"] not in ("playing", "waiting"):
            return False, "Game is over"
        if g["mode"] == "bot" or len(g.get("players", [])) < 2:
            g["status"] = "draw"
            g["winner"] = None
        else:
            other = next(p for p in g["players"] if p != user_key)
            g["winner"] = g["marks"][other]
            g["status"] = "win"
        g["turn"] = None
        g["updated"] = time.time()
        _save()
        return True, public_game(g)
