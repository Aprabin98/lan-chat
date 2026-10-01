"""File/formatting helpers (leaf module; only config, no state)."""
import os
import re
import socket
from urllib.parse import unquote

from . import config

def human_size(n):
    if n is None:
        return ""
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


ICONS = {
    ".pdf": "📕", ".doc": "📄", ".docx": "📄", ".txt": "📄", ".md": "📄",
    ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️", ".gif": "🖼️", ".svg": "🖼️", ".webp": "🖼️",
    ".mp4": "🎬", ".mov": "🎬", ".mkv": "🎬",
    ".mp3": "🎵", ".wav": "🎵",
    ".zip": "🗜️", ".rar": "🗜️", ".tar": "🗜️", ".gz": "🗜️",
    ".py": "🐍", ".js": "📜", ".html": "🌐", ".css": "🎨",
    ".xlsx": "📊", ".xls": "📊", ".csv": "📊",
    ".ppt": "📽️", ".pptx": "📽️",
}


def icon_for(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ICONS.get(ext, "📄")


def safe_join(base, name):
    """Prevent path traversal — only allow files directly inside base."""
    name = os.path.basename(unquote(name))
    path = os.path.normpath(os.path.join(base, name))
    if not path.startswith(os.path.abspath(base) + os.sep) and path != os.path.abspath(base):
        raise ValueError("invalid path")
    return path


def unique_dest(filename):
    """Pick a non-colliding destination path inside SHARE_DIR."""
    dest = safe_join(config.SHARE_DIR, filename)
    base, ext = os.path.splitext(dest)
    counter = 1
    while os.path.exists(dest):
        dest = f"{base} ({counter}){ext}"
        counter += 1
    return dest


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

