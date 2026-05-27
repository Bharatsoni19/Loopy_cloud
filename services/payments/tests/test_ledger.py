"""Tests for the Loop Cards ledger — these run in CI (DevOps quality gate)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from ledger import Ledger, LedgerError          # noqa: E402
from models import COINS_PER_RECHARGE, MINUTES_PER_RECHARGE  # noqa: E402
import pytest                                    # noqa: E402


@pytest.fixture
def lg():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield Ledger(path)
    os.remove(path)


def test_issue_and_recharge_banks_60_minutes(lg):
    card = lg.issue_card("alice", "Test", "aurora")["card"]
    lg.grant_coins("alice", COINS_PER_RECHARGE)
    r = lg.recharge(card["card_id"], "alice")
    assert r["card"]["minutes"] == MINUTES_PER_RECHARGE
    assert r["card"]["remaining_seconds"] > 3500   # ~60 min pass started
    assert lg.coins("alice") == 0                  # coins were spent


def test_recharge_requires_coins(lg):
    card = lg.issue_card("bob", "C", "gold")["card"]
    with pytest.raises(LedgerError, match="insufficient_coins"):
        lg.recharge(card["card_id"], "bob")


def test_idempotent_recharge_charged_once(lg):
    card = lg.issue_card("carol", "C", "midnight")["card"]
    lg.grant_coins("carol", COINS_PER_RECHARGE)
    a = lg.recharge(card["card_id"], "carol", idem="k1")
    b = lg.recharge(card["card_id"], "carol", idem="k1")   # retry
    assert a["txn_id"] == b["txn_id"]                      # same txn
    assert lg.coins("carol") == 0                          # charged once only


def test_p2p_transfer_conserves_minutes(lg):
    card = lg.issue_card("dan", "C", "matrix")["card"]
    lg.grant_coins("dan", COINS_PER_RECHARGE)
    lg.recharge(card["card_id"], "dan")
    r = lg.transfer(card["card_id"], "dan", "erin", 20)
    assert r["from"]["minutes"] == 40
    assert r["to"]["minutes"] == 20
    assert r["end_to_end_id"].startswith("e2e_")           # ISO 20022-style ref


def test_shared_friend_can_redeem(lg):
    card = lg.issue_card("fred", "C", "candy")["card"]
    lg.grant_coins("fred", COINS_PER_RECHARGE)
    lg.recharge(card["card_id"], "fred")
    lg.share_access(card["card_id"], "fred", "gwen")
    r = lg.redeem(card["card_id"], "gwen", 5)              # friend listens
    assert r["spent"] == 5
    assert r["card"]["minutes"] == 55


def test_outsider_cannot_redeem(lg):
    card = lg.issue_card("hank", "C", "sunset")["card"]
    lg.grant_coins("hank", COINS_PER_RECHARGE)
    lg.recharge(card["card_id"], "hank")
    with pytest.raises(LedgerError, match="not_authorised"):
        lg.redeem(card["card_id"], "stranger", 5)


def test_trial_balance_always_zero(lg):
    card = lg.issue_card("ivy", "C", "aurora")["card"]
    lg.grant_coins("ivy", COINS_PER_RECHARGE * 2)
    lg.recharge(card["card_id"], "ivy")
    lg.transfer(card["card_id"], "ivy", "jack", 10)
    lg.redeem(card["card_id"], "ivy", 3)
    tb = lg.trial_balance()
    assert tb["balanced"] is True       # no minutes or coins leaked / minted
