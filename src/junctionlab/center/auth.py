"""Password hashing (PBKDF2, stdlib) and JWT bearer authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

ITERATIONS = 240_000
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
    return hmac.compare_digest(dk, base64.b64decode(hash_b64))


class TokenService:
    def __init__(self, secret: str, hours: int) -> None:
        self.secret, self.hours = secret, hours

    def issue(self, username: str, role: str) -> str:
        now = datetime.now(UTC)
        return jwt.encode({"sub": username, "role": role, "iat": now, "exp": now + timedelta(hours=self.hours)},
                          self.secret, algorithm="HS256")

    def decode(self, token: str) -> dict:
        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc


def current_user_dep(tokens: TokenService):
    async def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
        if creds is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        return tokens.decode(creds.credentials)
    return current_user


def require_operator(user: dict) -> None:
    if user.get("role") != "operator":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")
