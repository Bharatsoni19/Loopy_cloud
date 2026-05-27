"""
Loopy Pay — domain models for the Loop Cards payment service.

A "Loop Card" is a prepaid, shareable, time-based wallet. Users top it up with
Loopy Coins; each recharge grants 60 minutes of premium listening. Cards can be
shared peer-to-peer (P2P) with friends, who can then spend the minutes.

The economy is tracked with a minimal double-entry ledger (see ledger.py) so that
no minutes are ever created or destroyed without a balancing entry — the same
invariant real payment systems rely on.
"""
from __future__ import annotations

import enum
import secrets
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# 1 recharge == 60 minutes of premium listening time.
MINUTES_PER_RECHARGE = 60
# Coin price the user pays per recharge (debited from the coin economy).
COINS_PER_RECHARGE = 120
# Hard ceiling so a single card cannot hoard unlimited time (fraud guard).
MAX_CARD_MINUTES = 60 * 24  # 24 hours banked


class CardTheme(str, enum.Enum):
    """Visual skins the frontend renders as 'graphically fabulous' holo cards."""
    AURORA = "aurora"
    MIDNIGHT = "midnight"
    SUNSET = "sunset"
    MATRIX = "matrix"
    CANDY = "candy"
    GOLD = "gold"


class TxnType(str, enum.Enum):
    ISSUE = "issue"            # card created
    RECHARGE = "recharge"      # +60 min, coins spent
    TRANSFER = "transfer"      # P2P share of minutes
    REDEEM = "redeem"          # minutes spent while listening
    REFUND = "refund"          # reversal
    EXPIRE = "expire"          # minutes lapsed


def new_id(prefix: str) -> str:
    """ULID-ish sortable, collision-resistant id."""
    return f"{prefix}_{int(time.time()*1000):013d}{secrets.token_hex(4)}"


def card_pan() -> str:
    """A human-shareable 'card number' (not a real PAN — purely an app handle)."""
    raw = secrets.token_hex(8).upper()
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


@dataclass
class LoopCard:
    card_id: str
    pan: str                      # shareable handle, e.g. 4F2A-9C1B-...
    owner_id: str
    theme: str = CardTheme.AURORA.value
    name: str = "My Loop Card"
    minutes: int = 0              # banked premium minutes
    active_until: float = 0.0     # epoch seconds; when the current 60-min pass ends
    shared_with: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: str = "active"        # active | frozen | closed
    version: int = 1              # optimistic-lock counter

    @property
    def live(self) -> bool:
        return self.status == "active" and time.time() < self.active_until

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.active_until - time.time()))

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["live"] = self.live
        d["remaining_seconds"] = self.remaining_seconds
        return d


@dataclass
class LedgerEntry:
    """One leg of a double-entry transaction."""
    entry_id: str
    txn_id: str
    txn_type: str
    account: str          # e.g. card:<id>, coins:<user>, system:reserve
    delta_minutes: int    # signed
    delta_coins: int      # signed
    ts: float = field(default_factory=time.time)
    # ISO 20022-flavoured correlation reference for traceable P2P transfers.
    end_to_end_id: Optional[str] = None
    note: str = ""
