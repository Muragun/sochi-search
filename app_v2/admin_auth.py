"""
Вход в служебный раздел: форма на сайте плюс подписанная кука.

Basic-авторизация тоже принимается — она удобна для curl и скриптов, — но
человек видит обычную страницу входа, а не всплывающее окно браузера.

Куки подписываются HMAC на стандартной библиотеке. Отдельного хранилища
сессий нет: в подписи лежит имя пользователя и срок годности, поэтому
перезапуск API не выкидывает вошедших, пока не меняется ключ.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

COOKIE_NAME = "sochi_admin"
DEFAULT_TTL_SECONDS = 12 * 60 * 60


class AuthNotConfigured(RuntimeError):
    """Пароль не задан — раздел не должен открываться вовсе."""


def admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"


def expected_password_hash() -> str | None:
    explicit = os.getenv("ADMIN_PASSWORD_SHA256", "").strip().lower()

    if explicit:
        return explicit

    plain = os.getenv("ADMIN_PASSWORD", "")

    if not plain:
        return None

    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def session_ttl() -> int:
    try:
        return max(300, int(os.getenv("ADMIN_SESSION_HOURS", "12")) * 3600)
    except ValueError:
        return DEFAULT_TTL_SECONDS


def signing_key() -> bytes:
    """
    Ключ подписи куки.

    Если он не задан явно, он выводится из пароля: тогда смена пароля
    автоматически обесценивает все выданные куки. Отдельный ключ имеет
    смысл, только если нужно разлогинить всех, не меняя пароль.
    """

    explicit = os.getenv("ADMIN_SECRET_KEY", "").strip()

    if explicit:
        return explicit.encode("utf-8")

    password_hash = expected_password_hash()

    if not password_hash:
        raise AuthNotConfigured(
            "Не задан ни ADMIN_PASSWORD_SHA256, ни ADMIN_PASSWORD"
        )

    return hashlib.sha256(
        ("sochi-admin-cookie:" + password_hash).encode("utf-8")
    ).digest()


def check_password(username: str, password: str) -> bool:
    expected_hash = expected_password_hash()

    if not expected_hash:
        raise AuthNotConfigured(
            "Не задан ни ADMIN_PASSWORD_SHA256, ни ADMIN_PASSWORD"
        )

    given_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

    # Сравнение постоянного времени по обоим полям: оно не должно
    # зависеть от того, где именно расходятся значения.
    #
    # Имена сравниваются байтами, а не строками. compare_digest со
    # строками требует ASCII и на кириллическом логине бросает
    # TypeError — форма входа отдавала бы пятисотку вместо «неверный
    # логин или пароль», причём любому, кто введёт русское слово.
    username_ok = secrets.compare_digest(
        username.encode("utf-8"),
        admin_username().encode("utf-8"),
    )
    password_ok = secrets.compare_digest(given_hash, expected_hash)

    return username_ok and password_ok


def _sign(payload: str) -> str:
    digest = hmac.new(
        signing_key(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_token(username: str, *, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + session_ttl())
    payload = f"{username}:{expires}"

    return f"{payload}:{_sign(payload)}"


def verify_token(token: str, *, now: float | None = None) -> str | None:
    """Возвращает имя пользователя или None, если кука негодна."""

    if not token or token.count(":") != 2:
        return None

    username, expires_text, signature = token.split(":")

    try:
        expires = int(expires_text)
    except ValueError:
        return None

    payload = f"{username}:{expires_text}"

    try:
        expected = _sign(payload)
    except AuthNotConfigured:
        return None

    if not secrets.compare_digest(signature, expected):
        return None

    current = now if now is not None else time.time()

    if expires < current:
        return None

    if username != admin_username():
        return None

    return username
