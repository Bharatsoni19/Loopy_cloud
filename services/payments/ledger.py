"""
Loopy Pay — double-entry ledger.

Every movement of minutes or coins is recorded as a transaction made of two or
more balancing legs whose deltas sum to zero per dimension. This gives us:

  * auditability  — replay the journal to rebuild any balance
  * integrity     — a transfer can never leak or mint value
  * idempotency   — a client-supplied key prevents double-spend on retries

Storage is SQLite (file-backed, zero-ops) so the service runs identically on a
laptop and on EC2. Swap the DSN for RDS/Aurora in production without code
changes — the schema is plain SQL.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from models import (
    LoopCard, LedgerEntry, TxnType, new_id, card_pan,
    MINUTES_PER_RECHARGE, COINS_PER_RECHARGE, MAX_CARD_MINUTES,
)


class LedgerError(Exception):
    pass


class Ledger:
    def __init__(self, dsn: str = "loopy_pay.db"):
        self.dsn = dsn
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(dsn, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    # ── schema ────────────────────────────────────────────────
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cards(
                    card_id TEXT PRIMARY KEY, pan TEXT UNIQUE, owner_id TEXT,
                    theme TEXT, name TEXT, minutes INTEGER, active_until REAL,
                    shared_with TEXT, created_at REAL, status TEXT, version INTEGER
                );
                CREATE TABLE IF NOT EXISTS ledger(
                    entry_id TEXT PRIMARY KEY, txn_id TEXT, txn_type TEXT,
                    account TEXT, delta_minutes INTEGER, delta_coins INTEGER,
                    ts REAL, end_to_end_id TEXT, note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_txn ON ledger(txn_id);
                CREATE INDEX IF NOT EXISTS idx_ledger_acct ON ledger(account);
                CREATE TABLE IF NOT EXISTS idempotency(
                    key TEXT PRIMARY KEY, txn_id TEXT, response TEXT, ts REAL
                );
                CREATE TABLE IF NOT EXISTS coin_balances(
                    user_id TEXT PRIMARY KEY, coins INTEGER
                );
                """
            )
            self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ── idempotency ───────────────────────────────────────────
    def _replay(self, key: str | None) -> dict | None:
        if not key:
            return None
        row = self._conn.execute(
            "SELECT response FROM idempotency WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["response"]) if row else None

    def _remember(self, key: str | None, txn_id: str, response: dict) -> None:
        if not key:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO idempotency(key,txn_id,response,ts) VALUES(?,?,?,?)",
            (key, txn_id, json.dumps(response), time.time()),
        )

    # ── coin economy (mirrors the frontend coin wallet) ───────
    def coins(self, user_id: str) -> int:
        row = self._conn.execute(
            "SELECT coins FROM coin_balances WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["coins"] if row else 0

    def grant_coins(self, user_id: str, amount: int) -> int:
        with self._tx() as c:
            cur = self.coins(user_id) + amount
            c.execute(
                "INSERT OR REPLACE INTO coin_balances(user_id,coins) VALUES(?,?)",
                (user_id, cur),
            )
        return cur

    # ── cards ─────────────────────────────────────────────────
    def _row_to_card(self, row: sqlite3.Row) -> LoopCard:
        return LoopCard(
            card_id=row["card_id"], pan=row["pan"], owner_id=row["owner_id"],
            theme=row["theme"], name=row["name"], minutes=row["minutes"],
            active_until=row["active_until"],
            shared_with=json.loads(row["shared_with"] or "[]"),
            created_at=row["created_at"], status=row["status"], version=row["version"],
        )

    def _save_card(self, c: sqlite3.Connection, card: LoopCard) -> None:
        c.execute(
            """INSERT OR REPLACE INTO cards
               (card_id,pan,owner_id,theme,name,minutes,active_until,
                shared_with,created_at,status,version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (card.card_id, card.pan, card.owner_id, card.theme, card.name,
             card.minutes, card.active_until, json.dumps(card.shared_with),
             card.created_at, card.status, card.version),
        )

    def get_card(self, card_id: str) -> LoopCard:
        row = self._conn.execute(
            "SELECT * FROM cards WHERE card_id=?", (card_id,)
        ).fetchone()
        if not row:
            raise LedgerError("card_not_found")
        return self._row_to_card(row)

    def get_card_by_pan(self, pan: str) -> LoopCard:
        row = self._conn.execute(
            "SELECT * FROM cards WHERE pan=?", (pan,)
        ).fetchone()
        if not row:
            raise LedgerError("card_not_found")
        return self._row_to_card(row)

    def list_cards(self, user_id: str) -> list[LoopCard]:
        rows = self._conn.execute(
            "SELECT * FROM cards WHERE owner_id=? OR shared_with LIKE ? "
            "ORDER BY created_at DESC",
            (user_id, f'%"{user_id}"%'),
        ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def _post(self, c: sqlite3.Connection, txn_id: str, ttype: TxnType,
              legs: list[tuple[str, int, int]], e2e: str | None, note: str) -> None:
        """Write balancing legs. Raises if minutes or coins don't net to zero."""
        if sum(m for _, m, _ in legs) != 0:
            raise LedgerError("unbalanced_minutes")
        if sum(k for _, _, k in legs) != 0:
            raise LedgerError("unbalanced_coins")
        for account, dmin, dcoin in legs:
            c.execute(
                """INSERT INTO ledger
                   (entry_id,txn_id,txn_type,account,delta_minutes,delta_coins,ts,end_to_end_id,note)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("leg"), txn_id, ttype.value, account, dmin, dcoin,
                 time.time(), e2e, note),
            )

    # ── operations ────────────────────────────────────────────
    def issue_card(self, owner_id: str, name: str, theme: str,
                   idem: str | None = None) -> dict:
        if (cached := self._replay(idem)) is not None:
            return cached
        with self._tx() as c:
            card = LoopCard(card_id=new_id("card"), pan=card_pan(),
                            owner_id=owner_id, name=name, theme=theme)
            self._save_card(c, card)
            txn = new_id("txn")
            # System reserve mirrors the new (empty) card so the journal balances.
            self._post(c, txn, TxnType.ISSUE,
                       [("system:reserve", 0, 0), (f"card:{card.card_id}", 0, 0)],
                       None, "card issued")
            resp = {"ok": True, "txn_id": txn, "card": card.to_public()}
            self._remember(idem, txn, resp)
            return resp

    def recharge(self, card_id: str, actor_id: str, idem: str | None = None) -> dict:
        """Spend coins → bank 60 premium minutes and start a fresh 60-min pass."""
        if (cached := self._replay(idem)) is not None:
            return cached
        with self._tx() as c:
            card = self.get_card(card_id)
            if card.status != "active":
                raise LedgerError("card_not_active")
            if self.coins(actor_id) < COINS_PER_RECHARGE:
                raise LedgerError("insufficient_coins")
            if card.minutes + MINUTES_PER_RECHARGE > MAX_CARD_MINUTES:
                raise LedgerError("card_limit_reached")

            # debit coins
            self.grant_coins(actor_id, -COINS_PER_RECHARGE)
            # bank minutes + (re)start the rolling 60-minute pass
            card.minutes += MINUTES_PER_RECHARGE
            base = max(time.time(), card.active_until)
            card.active_until = base + MINUTES_PER_RECHARGE * 60
            card.version += 1
            self._save_card(c, card)

            txn = new_id("txn")
            self._post(
                c, txn, TxnType.RECHARGE,
                [(f"coins:{actor_id}", 0, -COINS_PER_RECHARGE),
                 ("system:reserve", -MINUTES_PER_RECHARGE, COINS_PER_RECHARGE),
                 (f"card:{card_id}", MINUTES_PER_RECHARGE, 0)],
                None, f"+{MINUTES_PER_RECHARGE}min for {COINS_PER_RECHARGE} coins",
            )
            resp = {"ok": True, "txn_id": txn, "card": card.to_public()}
            self._remember(idem, txn, resp)
            return resp

    def transfer(self, card_id: str, actor_id: str, to_user: str,
                 minutes: int, idem: str | None = None) -> dict:
        """P2P share: move minutes from a card into a friend's auto-created card."""
        if (cached := self._replay(idem)) is not None:
            return cached
        with self._tx() as c:
            src = self.get_card(card_id)
            if actor_id not in (src.owner_id, *src.shared_with):
                raise LedgerError("not_authorised")
            if minutes <= 0 or src.minutes < minutes:
                raise LedgerError("insufficient_minutes")

            # destination: friend's first card, or mint a fresh one
            dst_cards = [x for x in self.list_cards(to_user) if x.owner_id == to_user]
            if dst_cards:
                dst = dst_cards[0]
            else:
                dst = LoopCard(card_id=new_id("card"), pan=card_pan(),
                               owner_id=to_user, name="Gifted Loop Card",
                               theme="candy")
            src.minutes -= minutes
            src.version += 1
            dst.minutes += minutes
            dst.active_until = max(dst.active_until, time.time() + minutes * 60)
            dst.version += 1
            self._save_card(c, src)
            self._save_card(c, dst)

            txn = new_id("txn")
            e2e = new_id("e2e")  # ISO 20022-style end-to-end traceability id
            self._post(
                c, txn, TxnType.TRANSFER,
                [(f"card:{src.card_id}", -minutes, 0),
                 (f"card:{dst.card_id}", minutes, 0)],
                e2e, f"{actor_id} shared {minutes}min with {to_user}",
            )
            resp = {"ok": True, "txn_id": txn, "end_to_end_id": e2e,
                    "from": src.to_public(), "to": dst.to_public()}
            self._remember(idem, txn, resp)
            return resp

    def redeem(self, card_id: str, actor_id: str, minutes: int,
               idem: str | None = None) -> dict:
        """Spend minutes as the user listens (called by the player/sync loop)."""
        if (cached := self._replay(idem)) is not None:
            return cached
        with self._tx() as c:
            card = self.get_card(card_id)
            if actor_id not in (card.owner_id, *card.shared_with):
                raise LedgerError("not_authorised")
            spend = min(minutes, card.minutes)
            card.minutes -= spend
            card.version += 1
            self._save_card(c, card)
            txn = new_id("txn")
            self._post(
                c, txn, TxnType.REDEEM,
                [(f"card:{card_id}", -spend, 0), ("system:reserve", spend, 0)],
                None, f"{actor_id} listened {spend}min",
            )
            resp = {"ok": True, "txn_id": txn, "spent": spend,
                    "card": card.to_public()}
            self._remember(idem, txn, resp)
            return resp

    def share_access(self, card_id: str, actor_id: str, friend_id: str) -> dict:
        """Grant a friend the right to spend from this card (not a balance move)."""
        with self._tx() as c:
            card = self.get_card(card_id)
            if actor_id != card.owner_id:
                raise LedgerError("only_owner_can_share")
            if friend_id not in card.shared_with:
                card.shared_with.append(friend_id)
                card.version += 1
                self._save_card(c, card)
        return {"ok": True, "card": card.to_public()}

    def journal(self, card_id: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ledger WHERE account=? ORDER BY ts DESC LIMIT ?",
            (f"card:{card_id}", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def trial_balance(self) -> dict:
        """Sum of all legs must be zero on every dimension — the core invariant."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(delta_minutes),0) m, COALESCE(SUM(delta_coins),0) k "
            "FROM ledger"
        ).fetchone()
        return {"minutes_net": row["m"], "coins_net": row["k"],
                "balanced": row["m"] == 0 and row["k"] == 0}
