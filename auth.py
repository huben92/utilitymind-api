"""Session, password, and local simulator credential helpers."""

import hmac
import os
from pathlib import Path
import re
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash


ROLES = ("super_admin", "admin", "viewer")
USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,49}\Z")
_ATTEMPTS = {}
_ATTEMPTS_LOCK = threading.Lock()
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(24))


def secret_file(path):
    """Create a process-shared secret once, readable only by the local owner."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w") as file:
            file.write(secrets.token_urlsafe(48))
    return path.read_text().strip()


def normalize_username(value):
    username = str(value or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Username must be 3–50 characters: lowercase letters, numbers, dots, dashes, or underscores")
    return username


def validate_password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        raise ValueError("Password must be 12–128 characters")
    return value


def password_hash(value):
    return generate_password_hash(validate_password(value))


def password_matches(stored_hash, value):
    return check_password_hash(stored_hash or _DUMMY_HASH, str(value or ""))


def service_token_matches(path, supplied):
    return bool(supplied) and hmac.compare_digest(secret_file(path), supplied)


def login_limited(key):
    now = time.monotonic()
    with _ATTEMPTS_LOCK:
        recent = [stamp for stamp in _ATTEMPTS.get(key, ()) if now - stamp < 900]
        if recent:
            _ATTEMPTS[key] = recent
        else:
            _ATTEMPTS.pop(key, None)
        return len(recent) >= 5


def record_login_failure(key):
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.setdefault(key, []).append(time.monotonic())


def clear_login_failures(key):
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.pop(key, None)
