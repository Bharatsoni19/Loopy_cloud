"""
╔══════════════════════════════════════════════════════════════╗
║  LOOPY PAY — Loop Cards payment microservice                 ║
║  Shareable, rechargeable (60-min) prepaid music-time cards    ║
║                                                               ║
║  Syllabus coverage:                                           ║
║   · FinTech payment integration · microservices · REST API    ║
║   · JWT/OAuth security · idempotency · rate limiting          ║
║   · event-driven webhooks · observability (/metrics, logs)    ║
║   · cloud sync to S3 (raw zone feeds the Glue ETL)            ║
╚══════════════════════════════════════════════════════════════╝

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8100
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from ledger import Ledger, LedgerError
from models import CardTheme, COINS_PER_RECHARGE, MINUTES_PER_RECHARGE
from security import (
    issue_token, verify_token, RateLimiter, get_logger, log_event,
)

# ── config (12-factor: everything from env) ───────────────────
DB_DSN = os.getenv("LOOPY_PAY_DSN", "loopy_pay.db")
S3_BUCKET = os.getenv("LOOPY_RAW_BUCKET", "")          # e.g. loopy-raw-events
S3_PREFIX = os.getenv("LOOPY_RAW_PREFIX", "payments")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

log = get_logger()
ledger = Ledger(DB_DSN)
limiter = RateLimiter(rate=8, burst=40)

app = FastAPI(title="Loopy Pay — Loop Cards", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# lightweight metrics (Prometheus exposition format on /metrics)
METRICS: dict[str, float] = {"http_requests_total": 0, "txn_total": 0,
                             "errors_total": 0}


# ── schemas ───────────────────────────────────────────────────
class TokenReq(BaseModel):
    user_id: str
    username: str = "guest"


class IssueReq(BaseModel):
    name: str = "My Loop Card"
    theme: str = CardTheme.AURORA.value


class TransferReq(BaseModel):
    to_user: str
    minutes: int = 30


class RedeemReq(BaseModel):
    minutes: int = 1


class ShareReq(BaseModel):
    friend_id: str


# ── auth helper ───────────────────────────────────────────────
def auth(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        return verify_token(authorization.split(" ", 1)[1])
    except PermissionError as e:
        raise HTTPException(401, str(e))


# ── middleware: metrics + throttling ──────────────────────────
@app.middleware("http")
async def instrument(request: Request, call_next):
    METRICS["http_requests_total"] += 1
    client = request.client.host if request.client else "anon"
    if request.url.path.startswith("/cards") and not limiter.allow(client):
        METRICS["errors_total"] += 1
        return JSONResponse({"detail": "rate_limited"}, status_code=429)
    t0 = time.time()
    resp = await call_next(request)
    log_event(log, "http", path=request.url.path, method=request.method,
              status=resp.status_code, ms=round((time.time() - t0) * 1000, 1))
    return resp


# ── S3 sync (cloud) ───────────────────────────────────────────
def archive_to_s3(record: dict) -> None:
    """Append a payment event to the S3 raw zone that AWS Glue later crawls.
    No-op (logs only) when no bucket is configured, so local dev still works."""
    if not S3_BUCKET:
        log_event(log, "s3_skip", reason="no bucket configured")
        return
    try:
        import boto3  # imported lazily so local dev needs no AWS creds
        body = (json.dumps(record) + "\n").encode()
        day = time.strftime("%Y/%m/%d", time.gmtime())
        key = f"{S3_PREFIX}/dt={day}/{record['txn_id']}.json"
        boto3.client("s3", region_name=AWS_REGION).put_object(
            Bucket=S3_BUCKET, Key=key, Body=body)
        log_event(log, "s3_put", bucket=S3_BUCKET, key=key)
    except Exception as e:  # never fail the user txn because of analytics
        METRICS["errors_total"] += 1
        log_event(log, "s3_error", error=str(e))


# ── routes ────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"app": "Loopy Pay", "minutes_per_recharge": MINUTES_PER_RECHARGE,
            "coins_per_recharge": COINS_PER_RECHARGE, "status": "running"}


@app.get("/health")
def health():
    tb = ledger.trial_balance()
    code = 200 if tb["balanced"] else 500
    return JSONResponse({"status": "ok" if tb["balanced"] else "ledger_imbalance",
                         "trial_balance": tb}, status_code=code)


@app.get("/metrics")
def metrics():
    lines = [f"loopy_{k} {v}" for k, v in METRICS.items()]
    return PlainTextResponse("\n".join(lines) + "\n")


@app.post("/auth/token")
def token(req: TokenReq):
    """Demo token mint. In production this is your IdP / OAuth provider."""
    return {"access_token": issue_token(req.user_id, req.username),
            "token_type": "bearer", "expires_in": 3600}


@app.get("/wallet")
def wallet(authorization: str = Header(None)):
    u = auth(authorization)
    return {"user_id": u["sub"], "coins": ledger.coins(u["sub"]),
            "cards": [c.to_public() for c in ledger.list_cards(u["sub"])]}


@app.post("/coins/grant")
def grant(amount: int = 0, authorization: str = Header(None)):
    """Mirror of the in-app coin economy (listening earns coins)."""
    u = auth(authorization)
    bal = ledger.grant_coins(u["sub"], max(0, min(amount, 1000)))
    return {"coins": bal}


@app.post("/cards")
def create_card(req: IssueReq, authorization: str = Header(None),
                idempotency_key: str = Header(None)):
    u = auth(authorization)
    try:
        r = ledger.issue_card(u["sub"], req.name, req.theme, idem=idempotency_key)
    except LedgerError as e:
        raise HTTPException(400, str(e))
    METRICS["txn_total"] += 1
    archive_to_s3({"txn_id": r["txn_id"], "type": "issue", "user": u["sub"],
                   "ts": time.time(), "card_id": r["card"]["card_id"]})
    return r


@app.get("/cards")
def my_cards(authorization: str = Header(None)):
    u = auth(authorization)
    return {"cards": [c.to_public() for c in ledger.list_cards(u["sub"])]}


@app.post("/cards/{card_id}/recharge")
def recharge(card_id: str, authorization: str = Header(None),
             idempotency_key: str = Header(None)):
    u = auth(authorization)
    try:
        r = ledger.recharge(card_id, u["sub"], idem=idempotency_key)
    except LedgerError as e:
        METRICS["errors_total"] += 1
        raise HTTPException(400, str(e))
    METRICS["txn_total"] += 1
    archive_to_s3({"txn_id": r["txn_id"], "type": "recharge", "user": u["sub"],
                   "ts": time.time(), "card_id": card_id,
                   "minutes": MINUTES_PER_RECHARGE, "coins": COINS_PER_RECHARGE})
    return r


@app.post("/cards/{card_id}/transfer")
def transfer(card_id: str, req: TransferReq, authorization: str = Header(None),
             idempotency_key: str = Header(None)):
    u = auth(authorization)
    try:
        r = ledger.transfer(card_id, u["sub"], req.to_user, req.minutes,
                            idem=idempotency_key)
    except LedgerError as e:
        METRICS["errors_total"] += 1
        raise HTTPException(400, str(e))
    METRICS["txn_total"] += 1
    archive_to_s3({"txn_id": r["txn_id"], "type": "transfer", "user": u["sub"],
                   "ts": time.time(), "card_id": card_id, "to": req.to_user,
                   "minutes": req.minutes, "end_to_end_id": r["end_to_end_id"]})
    return r


@app.post("/cards/{card_id}/share")
def share(card_id: str, req: ShareReq, authorization: str = Header(None)):
    u = auth(authorization)
    try:
        return ledger.share_access(card_id, u["sub"], req.friend_id)
    except LedgerError as e:
        raise HTTPException(400, str(e))


@app.post("/cards/{card_id}/redeem")
def redeem(card_id: str, req: RedeemReq, authorization: str = Header(None),
           idempotency_key: str = Header(None)):
    """Called by the player sync loop — debits minutes as the user listens."""
    u = auth(authorization)
    try:
        r = ledger.redeem(card_id, u["sub"], req.minutes, idem=idempotency_key)
    except LedgerError as e:
        raise HTTPException(400, str(e))
    METRICS["txn_total"] += 1
    return r


@app.get("/cards/{card_id}/journal")
def journal(card_id: str, authorization: str = Header(None)):
    auth(authorization)
    return {"card_id": card_id, "entries": ledger.journal(card_id)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8100, reload=False)
