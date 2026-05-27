#!/usr/bin/env python3
"""
seed_demo.py — drive the full Loop Cards lifecycle against a running stack.

Works against either entrypoint:
    • the API gateway     :  python seed_demo.py http://<host>/api/pay
    • payments directly   :  python seed_demo.py http://localhost:8100   (default)

It tells a small story so a demo / viva has live data to show:
    1. Bharat and a friend (Aarav) each get a JWT session.
    2. Bharat is granted Loopy Coins.
    3. Bharat issues two graphically-themed Loop Cards.
    4. He recharges one card  (120 coins -> 60 listening minutes).
    5. He transfers 25 minutes to Aarav  (P2P, ISO-20022-style end_to_end id).
    6. He shares the card so Aarav can also redeem from it.
    7. Aarav redeems a few minutes (as if listening together).
    8. We print wallets, the card journal and the system trial balance.

Pure standard library — no extra dependencies.
"""
import json
import sys
import urllib.request
import urllib.error
import uuid

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8100"


def call(method, path, token=None, body=None, idem=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if idem:
        req.add_header("Idempotency-Key", idem)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise SystemExit(f"  !! {method} {path} -> {e.code}: {detail}")


def session(user_id, username):
    return call("POST", "/auth/token",
                body={"user_id": user_id, "username": username})["access_token"]


def step(n, msg):
    print(f"\n[{n}] {msg}")


def main():
    print(f"Seeding demo data against: {BASE}")

    step(1, "Open sessions for Bharat and Aarav")
    bharat = session("bharat", "Bharat")
    aarav = session("aarav", "Aarav")
    print("    both sessions minted")

    step(2, "Grant Bharat 300 Loopy Coins")
    w = call("POST", "/coins/grant?amount=300", token=bharat)
    print(f"    Bharat wallet: {w}")

    step(3, "Issue two themed Loop Cards for Bharat")
    c1 = call("POST", "/cards", token=bharat,
              body={"name": "Friday Night Loop", "theme": "aurora"},
              idem=str(uuid.uuid4()))
    c2 = call("POST", "/cards", token=bharat,
              body={"name": "Study Beats", "theme": "matrix"},
              idem=str(uuid.uuid4()))
    print(f"    card 1: {c1['card']['name']}  [{c1['card']['theme']}]  id={c1['card']['card_id'][:8]}")
    print(f"    card 2: {c2['card']['name']}  [{c2['card']['theme']}]  id={c2['card']['card_id'][:8]}")
    card_id = c1["card"]["card_id"]

    step(4, "Recharge card 1  (120 coins -> 60 minutes)")
    r = call("POST", f"/cards/{card_id}/recharge", token=bharat, idem=str(uuid.uuid4()))
    coins = call("GET", "/wallet", token=bharat).get("coins", "?")
    print(f"    card now holds {r['card']['minutes']} min "
          f"({r['card']['remaining_seconds']}s on the live pass); coins left = {coins}")

    step(5, "Transfer 25 minutes from Bharat's card to Aarav (P2P)")
    t = call("POST", f"/cards/{card_id}/transfer", token=bharat,
             body={"to_user": "aarav", "minutes": 25}, idem=str(uuid.uuid4()))
    print(f"    end_to_end_id = {t.get('end_to_end_id', '?')}")

    step(6, "Share card 1 with Aarav so he can redeem from it too")
    call("POST", f"/cards/{card_id}/share", token=bharat, body={"friend_id": "aarav"})
    print("    shared OK")

    step(7, "Aarav redeems 5 minutes (listening together)")
    rd = call("POST", f"/cards/{card_id}/redeem", token=aarav,
              body={"minutes": 5}, idem=str(uuid.uuid4()))
    print(f"    card minutes remaining: {rd['card']['minutes']}")

    step(8, "Final state")
    print("    Bharat wallet:", call("GET", "/wallet", token=bharat))
    print("    Aarav  wallet:", call("GET", "/wallet", token=aarav))
    print("    Bharat cards :", len(call("GET", "/cards", token=bharat)["cards"]), "card(s)")
    print("    Aarav  cards :", len(call("GET", "/cards", token=aarav)["cards"]), "card(s) (incl. shared)")
    health = call("GET", "/health")
    print("    trial balance:", health["trial_balance"])
    assert health["trial_balance"]["balanced"], "LEDGER OUT OF BALANCE!"
    print("\nDemo seed complete — ledger is balanced. Open the app and visit the Loop Cards tab.")


if __name__ == "__main__":
    main()
