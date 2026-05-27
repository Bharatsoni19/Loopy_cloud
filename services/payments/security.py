"""
Loopy Pay — security & observability primitives.

Maps to the syllabus 'API security including OWASP, OAuth, JWT and mutual TLS'
and 'observability and analytics':

  * JWT (HS256) bearer tokens for stateless auth.
  * Structured JSON logs that ship cleanly to CloudWatch Logs.
  * A tiny in-memory token-bucket rate limiter (the 'throttling' concept;
    in production this lives at the gateway / nginx layer — see nginx.conf).

The signing secret comes from the environment (12-factor). On EC2 it is injected
from AWS Secrets Manager / SSM Parameter Store, never committed to git.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from collections import defaultdict

JWT_SECRET = os.getenv("LOOPY_JWT_SECRET", "dev-only-change-me")
JWT_TTL = int(os.getenv("LOOPY_JWT_TTL", "3600"))


# ── JWT (no external dependency) ──────────────────────────────
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def issue_token(user_id: str, username: str = "guest") -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"sub": user_id, "name": username, "iat": now, "exp": now + JWT_TTL}
    h = _b64(json.dumps(header, separators=(",", ":")).encode())
    p = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64(sig)}"


def verify_token(token: str) -> dict:
    try:
        h, p, s = token.split(".")
    except ValueError:
        raise PermissionError("malformed_token")
    expected = _b64(hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(expected, s):
        raise PermissionError("bad_signature")
    payload = json.loads(_b64d(p))
    if payload.get("exp", 0) < time.time():
        raise PermissionError("token_expired")
    return payload


# ── token-bucket rate limiter ─────────────────────────────────
class RateLimiter:
    def __init__(self, rate: float = 5.0, burst: int = 20):
        self.rate, self.burst = rate, burst
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (burst, time.time()))

    def allow(self, key: str) -> bool:
        tokens, last = self._buckets[key]
        now = time.time()
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        return True


# ── structured logging (CloudWatch friendly) ──────────────────
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "service": "loopy-pay",
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            base.update(record.extra_fields)  # type: ignore[attr-defined]
        return json.dumps(base)


def get_logger(name: str = "loopy-pay") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger


def log_event(logger: logging.Logger, msg: str, **fields) -> None:
    rec = logger.makeRecord(logger.name, logging.INFO, "", 0, msg, (), None)
    rec.extra_fields = fields
    logger.handle(rec)
