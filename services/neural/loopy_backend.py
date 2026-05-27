"""
╔══════════════════════════════════════════════════════════════╗
║  LOOPY — Real Neural Training Backend                        ║
║  Trains on: cursor, speech, GPS, play/skip, coins, sleep     ║
║  3 TensorFlow models + FastAPI + real-time dashboard         ║
╚══════════════════════════════════════════════════════════════╝

Install:
  pip install tensorflow fastapi uvicorn pandas numpy scikit-learn websockets python-multipart

Run:
  python loopy_backend.py

Then open loopy-mega-v1.html — all data flows to this server.
Admin dashboard shows REAL model accuracy.
"""

import os, json, time, math, pickle, asyncio, hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any
from collections import defaultdict

import numpy as np
import pandas as pd

# ── FastAPI ───────────────────────────────────────────────────
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── TensorFlow ────────────────────────────────────────────────
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.model_selection import train_test_split

tf.random.set_seed(42)
np.random.seed(42)

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
MODEL_DIR   = "loopy_models"
DATA_DIR    = "loopy_data"
os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(DATA_DIR,  exist_ok=True)

MODES = ["normal","lofi","hiphop","devil","gym","romance","reverb","8d"]

app = FastAPI(title="Loopy Neural Backend", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── In-memory state ───────────────────────────────────────────
MODELS: Dict[str, Any]    = {}   # user_id → trained models
SCALERS: Dict[str, Any]   = {}   # user_id → scalers
EVENTS: Dict[str, list]   = defaultdict(list)  # user_id → events
ONLINE: Dict[str, dict]   = {}   # user_id → session info
WS_CLIENTS: List[WebSocket] = [] # admin websocket connections
TRAINING_STATUS: dict     = {"running": False, "progress": 0, "log": []}

# ══════════════════════════════════════════════════════════════
#  DATA SCHEMAS
# ══════════════════════════════════════════════════════════════
class EventBatch(BaseModel):
    user_id:    str
    username:   str = "guest"
    events:     List[Dict[str, Any]]
    session_id: str = ""

class PredictRequest(BaseModel):
    user_id:   str
    context:   Dict[str, Any]   # current state
    candidates: List[Dict[str, Any]] = []  # tracks to rank

class TrainRequest(BaseModel):
    user_id: Optional[str] = None  # None = train all users

# ══════════════════════════════════════════════════════════════
#  FEATURE ENGINEERING
#  Turns raw events into ML features
# ══════════════════════════════════════════════════════════════
def extract_features(events: list) -> pd.DataFrame:
    """
    Converts raw Loopy events into a feature matrix.
    Each row = one song play event with all signals.
    """
    rows = []
    for i, e in enumerate(events):
        if e.get('type') not in ('play','skip','like'):
            continue

        hour        = e.get('hour', datetime.now().hour)
        dow         = e.get('dow', datetime.now().weekday())
        mode        = e.get('mode', 'normal')
        mode_idx    = MODES.index(mode) if mode in MODES else 0
        bpm         = float(e.get('bpm', 120))
        energy      = float(e.get('energy', 0.5))
        listen_pct  = float(e.get('listenPct', 1.0))
        skipped     = 1 if e.get('type') == 'skip' else 0
        liked       = 1 if e.get('type') == 'like' else 0
        coins       = float(e.get('coins', 0))

        # Cursor features
        cursor      = e.get('cursor', {})
        cursor_spd  = float(cursor.get('speed', 0))
        cursor_errt = float(cursor.get('erraticScore', 0))

        # Speech features
        speech      = e.get('speech', {})
        speech_cmd  = speech.get('command', '')
        speech_mood = 1 if any(w in speech_cmd for w in ['gym','workout','energy','fast']) else 0

        # GPS/Motion features
        motion      = e.get('motion', {})
        gps_speed   = float(motion.get('speed', 0))
        moving_fast = 1 if gps_speed > 15 else 0

        # Sleep features
        is_sleep    = 1 if e.get('sleepMode', False) else 0

        # Previous skips (look back)
        prev_skips  = sum(1 for pe in events[max(0,i-5):i] if pe.get('type')=='skip')

        # Cyclic time encoding
        sin_h = math.sin(2 * math.pi * hour / 24)
        cos_h = math.cos(2 * math.pi * hour / 24)
        sin_d = math.sin(2 * math.pi * dow / 7)
        cos_d = math.cos(2 * math.pi * dow / 7)

        rows.append({
            # Audio features
            'bpm': bpm, 'energy': energy, 'listen_pct': listen_pct,
            # Context
            'mode_idx': mode_idx, 'hour': hour, 'dow': dow,
            'sin_h': sin_h, 'cos_h': cos_h, 'sin_d': sin_d, 'cos_d': cos_d,
            # Behavior signals
            'prev_skips': prev_skips, 'liked': liked, 'coins': coins,
            # Cursor intelligence
            'cursor_spd': cursor_spd, 'cursor_erratic': cursor_errt,
            # Speech signals
            'speech_mood': speech_mood,
            # Motion/GPS
            'gps_speed': gps_speed, 'moving_fast': moving_fast,
            # Sleep
            'is_sleep': is_sleep,
            # Labels
            'skipped': skipped,
            'mood_label': mode,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def generate_synthetic(n=2000) -> pd.DataFrame:
    """
    Generate realistic synthetic data for cold start.
    Used when a user has < 50 real events.
    """
    rows = []
    for _ in range(n):
        hour     = np.random.randint(0, 24)
        dow      = np.random.randint(0, 7)
        bpm      = np.random.randint(60, 200)
        energy   = np.random.uniform(0, 1)
        mode_idx = np.random.randint(0, len(MODES))
        mode     = MODES[mode_idx]
        gps_spd  = np.random.uniform(0, 80)

        # Realistic skip logic
        skip_p = 0.2
        if bpm < 80  and mode in ('gym','devil'): skip_p += 0.45
        if energy < 0.3 and mode in ('gym','devil','hiphop'): skip_p += 0.35
        if gps_spd > 15 and mode in ('lofi','romance'): skip_p += 0.3
        if hour >= 22 or hour < 5: skip_p -= 0.1  # night owl
        if hour >= 6  and hour < 9 and mode == 'gym': skip_p -= 0.3
        skipped = int(np.random.random() < min(skip_p, 0.95))

        rows.append({
            'bpm': bpm, 'energy': energy, 'listen_pct': np.random.uniform(0,1),
            'mode_idx': mode_idx, 'hour': hour, 'dow': dow,
            'sin_h': math.sin(2*math.pi*hour/24), 'cos_h': math.cos(2*math.pi*hour/24),
            'sin_d': math.sin(2*math.pi*dow/7),   'cos_d': math.cos(2*math.pi*dow/7),
            'prev_skips': np.random.randint(0,8), 'liked': np.random.randint(0,2),
            'coins': np.random.uniform(0,50),
            'cursor_spd': np.random.uniform(0,5),
            'cursor_erratic': np.random.uniform(0,1),
            'speech_mood': np.random.randint(0,2),
            'gps_speed': gps_spd, 'moving_fast': int(gps_spd > 15),
            'is_sleep': np.random.choice([0,1], p=[0.9,0.1]),
            'skipped': skipped, 'mood_label': mode,
        })
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════
#  MODEL ARCHITECTURES
# ══════════════════════════════════════════════════════════════
SKIP_FEATS = ['bpm','energy','listen_pct','mode_idx','prev_skips',
              'sin_h','cos_h','sin_d','cos_d','liked','coins',
              'cursor_spd','cursor_erratic','speech_mood',
              'gps_speed','moving_fast','is_sleep']

MOOD_FEATS = ['bpm','energy','sin_h','cos_h','sin_d','cos_d',
              'prev_skips','listen_pct','gps_speed','moving_fast',
              'cursor_erratic','speech_mood','is_sleep']

SEQ_FEATS  = ['bpm','energy','mode_idx','skipped','gps_speed','coins']
SEQ_LEN    = 5

def build_skip_model(n_feats: int) -> keras.Model:
    inp = keras.Input(shape=(n_feats,), name="features")
    x   = layers.Dense(128, activation='relu')(inp)
    x   = layers.BatchNormalization()(x)
    x   = layers.Dropout(0.3)(x)
    x   = layers.Dense(64, activation='relu')(x)
    x   = layers.BatchNormalization()(x)
    x   = layers.Dropout(0.2)(x)
    x   = layers.Dense(32, activation='relu')(x)
    out = layers.Dense(1, activation='sigmoid', name='skip_prob')(x)
    m   = keras.Model(inp, out, name="SkipPredictor")
    m.compile(optimizer=keras.optimizers.Adam(0.001),
              loss='binary_crossentropy',
              metrics=['accuracy', keras.metrics.AUC(name='auc')])
    return m

def build_mood_model(n_feats: int, n_classes: int) -> keras.Model:
    inp = keras.Input(shape=(n_feats,), name="context")
    x   = layers.Dense(128, activation='relu')(inp)
    x   = layers.BatchNormalization()(x)
    x   = layers.Dropout(0.3)(x)
    x   = layers.Dense(64, activation='relu')(x)
    x   = layers.Dropout(0.2)(x)
    x   = layers.Dense(32, activation='relu')(x)
    out = layers.Dense(n_classes, activation='softmax', name='mood')(x)
    m   = keras.Model(inp, out, name="MoodClassifier")
    m.compile(optimizer=keras.optimizers.Adam(0.001),
              loss='sparse_categorical_crossentropy',
              metrics=['accuracy'])
    return m

def build_lstm_model(seq_len: int, n_feats: int) -> keras.Model:
    inp = keras.Input(shape=(seq_len, n_feats), name="sequence")
    x   = layers.LSTM(64, return_sequences=True)(inp)
    x   = layers.Dropout(0.2)(x)
    x   = layers.LSTM(32)(x)
    x   = layers.Dense(32, activation='relu')(x)
    x   = layers.Dropout(0.2)(x)
    out = layers.Dense(1, activation='sigmoid', name='energy_target')(x)
    m   = keras.Model(inp, out, name="LSTMRanker")
    m.compile(optimizer=keras.optimizers.Adam(0.001), loss='mse', metrics=['mae'])
    return m


# ══════════════════════════════════════════════════════════════
#  TRAINING ENGINE
# ══════════════════════════════════════════════════════════════
async def train_user(user_id: str) -> dict:
    """Train all 3 models for a specific user."""
    global TRAINING_STATUS

    log = lambda msg: (TRAINING_STATUS["log"].append(f"[{user_id}] {msg}"),
                       asyncio.create_task(broadcast_admin()))

    TRAINING_STATUS["running"] = True
    TRAINING_STATUS["progress"] = 0
    TRAINING_STATUS["log"] = []

    # Load real events
    real_events = EVENTS.get(user_id, [])
    log(f"Real events: {len(real_events)}")

    # Build feature matrix
    df_real = extract_features(real_events) if len(real_events) >= 20 else pd.DataFrame()
    df_syn  = generate_synthetic(2000 if len(df_real) < 200 else 500)

    df = pd.concat([df_real, df_syn], ignore_index=True) if not df_real.empty else df_syn
    log(f"Training on {len(df)} rows ({len(df_real)} real + {len(df_syn)} synthetic)")

    results = {"user_id": user_id, "trained_at": datetime.now().isoformat(), "rows": len(df)}

    # ── MODEL 1: Skip Predictor ───────────────────────────────
    log("Training SkipPredictor...")
    TRAINING_STATUS["progress"] = 10

    X_skip = df[SKIP_FEATS].fillna(0).values
    y_skip = df['skipped'].values

    sc_skip = StandardScaler()
    X_skip  = sc_skip.fit_transform(X_skip)
    X_tr, X_v, y_tr, y_v = train_test_split(X_skip, y_skip, test_size=0.2, random_state=42)

    skip_model = build_skip_model(X_skip.shape[1])
    cb = [keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True, monitor='val_auc', mode='max')]
    hist = skip_model.fit(X_tr, y_tr, validation_data=(X_v, y_v),
                          epochs=30, batch_size=64, callbacks=cb, verbose=0)
    skip_auc = max(hist.history['val_auc'])
    skip_acc = max(hist.history['val_accuracy'])
    log(f"SkipPredictor — AUC: {skip_auc:.4f} | Acc: {skip_acc:.4f}")
    results["skip_auc"] = round(skip_auc, 4)
    results["skip_acc"] = round(skip_acc, 4)
    TRAINING_STATUS["progress"] = 40

    # ── MODEL 2: Mood Classifier ──────────────────────────────
    log("Training MoodClassifier...")
    le_mood = LabelEncoder()
    y_mood  = le_mood.fit_transform(df['mood_label'])
    X_mood  = df[MOOD_FEATS].fillna(0).values
    sc_mood = StandardScaler()
    X_mood  = sc_mood.fit_transform(X_mood)
    X_tr2, X_v2, y_tr2, y_v2 = train_test_split(X_mood, y_mood, test_size=0.2, random_state=42)

    mood_model = build_mood_model(X_mood.shape[1], len(le_mood.classes_))
    cb2 = [keras.callbacks.EarlyStopping(patience=6, restore_best_weights=True)]
    hist2 = mood_model.fit(X_tr2, y_tr2, validation_data=(X_v2, y_v2),
                            epochs=30, batch_size=64, callbacks=cb2, verbose=0)
    mood_acc = max(hist2.history['val_accuracy'])
    log(f"MoodClassifier — Acc: {mood_acc:.4f} | Classes: {list(le_mood.classes_)}")
    results["mood_acc"]     = round(mood_acc, 4)
    results["mood_classes"] = list(le_mood.classes_)
    TRAINING_STATUS["progress"] = 70

    # ── MODEL 3: LSTM Sequence Ranker ─────────────────────────
    log("Training LSTM Ranker...")
    seqs, targets = build_sequences(df)
    lstm_mae = None
    lstm_model = None
    sc_lstm = None
    if seqs is not None and len(seqs) >= 50:
        sc_lstm  = StandardScaler()
        sh       = seqs.shape
        seqs_sc  = sc_lstm.fit_transform(seqs.reshape(-1, sh[-1])).reshape(sh)
        X_tr3, X_v3, y_tr3, y_v3 = train_test_split(seqs_sc, targets, test_size=0.2, random_state=42)
        lstm_model = build_lstm_model(SEQ_LEN, seqs.shape[2])
        cb3 = [keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True)]
        hist3 = lstm_model.fit(X_tr3, y_tr3, validation_data=(X_v3, y_v3),
                                epochs=20, batch_size=32, callbacks=cb3, verbose=0)
        lstm_mae = min(hist3.history['val_mae'])
        log(f"LSTM Ranker — MAE: {lstm_mae:.4f}")
        results["lstm_mae"] = round(lstm_mae, 4)
    else:
        log("Not enough sequence data for LSTM — using skip+mood models only")

    TRAINING_STATUS["progress"] = 90

    # ── SAVE MODELS ───────────────────────────────────────────
    uid_safe = hashlib.md5(user_id.encode()).hexdigest()[:8]
    skip_model.save(f"{MODEL_DIR}/skip_{uid_safe}.keras")
    mood_model.save(f"{MODEL_DIR}/mood_{uid_safe}.keras")
    if lstm_model: lstm_model.save(f"{MODEL_DIR}/lstm_{uid_safe}.keras")

    with open(f"{MODEL_DIR}/scalers_{uid_safe}.pkl", 'wb') as f:
        pickle.dump({'skip': sc_skip, 'mood': sc_mood, 'lstm': sc_lstm,
                     'mood_classes': list(le_mood.classes_)}, f)

    with open(f"{MODEL_DIR}/report_{uid_safe}.json", 'w') as f:
        json.dump(results, f, indent=2)

    # Cache in memory
    MODELS[user_id]  = {'skip': skip_model, 'mood': mood_model, 'lstm': lstm_model}
    SCALERS[user_id] = {'skip': sc_skip, 'mood': sc_mood, 'lstm': sc_lstm,
                        'mood_classes': list(le_mood.classes_), 'mood_le': le_mood}

    TRAINING_STATUS["running"]  = False
    TRAINING_STATUS["progress"] = 100
    log(f"Training complete! Skip AUC={skip_auc:.3f} Mood Acc={mood_acc:.3f}")

    await broadcast_admin()
    return results


def build_sequences(df: pd.DataFrame):
    """Build LSTM input sequences from event history."""
    if len(df) < SEQ_LEN + 5:
        return None, None
    data    = df[SEQ_FEATS].fillna(0).values.astype(np.float32)
    targets = df['energy'].fillna(0.5).values.astype(np.float32)
    X, y    = [], []
    for i in range(SEQ_LEN, len(data)):
        X.append(data[i-SEQ_LEN:i])
        y.append(targets[i])
    return np.array(X), np.array(y)


def load_user_models(user_id: str) -> bool:
    """Load pre-trained models from disk for a user."""
    uid_safe = hashlib.md5(user_id.encode()).hexdigest()[:8]
    try:
        skip = keras.models.load_model(f"{MODEL_DIR}/skip_{uid_safe}.keras")
        mood = keras.models.load_model(f"{MODEL_DIR}/mood_{uid_safe}.keras")
        lstm = None
        if os.path.exists(f"{MODEL_DIR}/lstm_{uid_safe}.keras"):
            lstm = keras.models.load_model(f"{MODEL_DIR}/lstm_{uid_safe}.keras")
        with open(f"{MODEL_DIR}/scalers_{uid_safe}.pkl", 'rb') as f:
            sc = pickle.load(f)
        MODELS[user_id]  = {'skip': skip, 'mood': mood, 'lstm': lstm}
        SCALERS[user_id] = sc
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
#  INFERENCE
# ══════════════════════════════════════════════════════════════
def predict_skip(user_id: str, context: dict) -> dict:
    """Predict if user will skip next track."""
    if user_id not in MODELS:
        load_user_models(user_id)

    # Rule-based fallback
    def rule_based():
        bpm, energy, mode = context.get('bpm',120), context.get('energy',.5), context.get('mode','normal')
        p = 0.2
        if bpm < 80  and mode in ('gym','devil'): p += 0.45
        if energy < 0.3 and mode in ('gym','devil'): p += 0.35
        if context.get('movingFast'): p += 0.25
        if context.get('isSleep'): p -= 0.2
        return round(min(max(p, 0.01), 0.99), 3), "rule_based"

    if user_id not in MODELS:
        p, src = rule_based()
        return {"skip_prob": p, "will_skip": p > 0.5, "source": src}

    try:
        sc   = SCALERS[user_id]['skip']
        hour = context.get('hour', datetime.now().hour)
        dow  = context.get('dow', datetime.now().weekday())
        mode = context.get('mode','normal')
        row  = [[
            context.get('bpm',120), context.get('energy',.5),
            context.get('listenPct',1.0),
            MODES.index(mode) if mode in MODES else 0,
            context.get('prevSkips',0),
            math.sin(2*math.pi*hour/24), math.cos(2*math.pi*hour/24),
            math.sin(2*math.pi*dow/7),   math.cos(2*math.pi*dow/7),
            context.get('liked',0), context.get('coins',0),
            context.get('cursorSpeed',0), context.get('cursorErratic',0),
            context.get('speechMood',0),
            context.get('gpsSpeed',0), int(context.get('movingFast',False)),
            int(context.get('isSleep',False)),
        ]]
        X   = sc.transform(row)
        p   = float(MODELS[user_id]['skip'].predict(X, verbose=0)[0][0])
        return {"skip_prob": round(p,3), "will_skip": p > 0.5, "source": "neural"}
    except Exception as ex:
        p, src = rule_based()
        return {"skip_prob": p, "will_skip": p > 0.5, "source": "rule_based", "error": str(ex)}


def predict_mood(user_id: str, context: dict) -> dict:
    """Predict the ideal mood/mode for user right now."""
    if user_id not in MODELS:
        load_user_models(user_id)

    # Rule-based fallback
    def rule_based():
        hour = context.get('hour', datetime.now().hour)
        if context.get('movingFast'): return 'devil', {}
        if context.get('isSleep'):    return 'lofi', {}
        if hour in range(6,9):        return 'gym', {}
        if hour >= 22 or hour < 3:    return 'lofi', {}
        return context.get('mode','normal'), {}

    if user_id not in MODELS:
        m, probs = rule_based()
        return {"predicted_mode": m, "probabilities": probs, "source": "rule_based"}

    try:
        sc    = SCALERS[user_id]['mood']
        le    = SCALERS[user_id]['mood_le']
        hour  = context.get('hour', datetime.now().hour)
        dow   = context.get('dow', datetime.now().weekday())
        row   = [[
            context.get('bpm',120), context.get('energy',.5),
            math.sin(2*math.pi*hour/24), math.cos(2*math.pi*hour/24),
            math.sin(2*math.pi*dow/7),   math.cos(2*math.pi*dow/7),
            context.get('prevSkips',0),  context.get('listenPct',1.0),
            context.get('gpsSpeed',0),   int(context.get('movingFast',False)),
            context.get('cursorErratic',0), context.get('speechMood',0),
            int(context.get('isSleep',False)),
        ]]
        X     = sc.transform(row)
        probs = MODELS[user_id]['mood'].predict(X, verbose=0)[0]
        idx   = int(np.argmax(probs))
        mood  = le.classes_[idx]
        prob_dict = dict(zip(le.classes_, [round(float(p),3) for p in probs]))
        return {"predicted_mode": mood, "probabilities": prob_dict, "source": "neural",
                "confidence": round(float(probs[idx]), 3)}
    except Exception as ex:
        m, probs = rule_based()
        return {"predicted_mode": m, "probabilities": probs, "source": "rule_based", "error": str(ex)}


def rank_tracks(user_id: str, context: dict, candidates: list) -> list:
    """Rank candidate tracks best-first for this user."""
    if not candidates:
        return candidates

    # Get skip probs for all candidates
    ranked = []
    for c in candidates:
        ctx = {**context, 'bpm': c.get('bpm',120), 'energy': c.get('energy',.5)}
        r   = predict_skip(user_id, ctx)
        ranked.append({**c, 'skip_prob': r['skip_prob'], 'recommended': r['skip_prob'] < 0.4})

    return sorted(ranked, key=lambda x: x['skip_prob'])


# ══════════════════════════════════════════════════════════════
#  WEBSOCKET — ADMIN REAL-TIME DASHBOARD
# ══════════════════════════════════════════════════════════════
async def broadcast_admin():
    """Send real-time stats to all connected admin clients."""
    if not WS_CLIENTS:
        return

    stats = build_admin_stats()
    msg   = json.dumps({"type": "stats", "data": stats})
    dead  = []
    for ws in WS_CLIENTS:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        WS_CLIENTS.remove(ws)


def build_admin_stats() -> dict:
    """Build the real-time admin stats payload."""
    all_events = []
    for uid, evts in EVENTS.items():
        for e in evts:
            all_events.append({**e, 'user_id': uid})

    total       = len(all_events)
    skips       = sum(1 for e in all_events if e.get('type')=='skip')
    plays       = sum(1 for e in all_events if e.get('type')=='play')
    likes       = sum(1 for e in all_events if e.get('type')=='like')
    sleep_evts  = sum(1 for e in all_events if e.get('type')=='sleep')
    motion_evts = sum(1 for e in all_events if e.get('type')=='motion')
    speech_evts = sum(1 for e in all_events if e.get('type')=='karen_cmd')

    # Mode distribution
    mode_counts = defaultdict(int)
    for e in all_events:
        m = e.get('mode','normal')
        if m: mode_counts[m] += 1

    # User models
    user_models = {}
    for uid in EVENTS:
        uid_safe = hashlib.md5(uid.encode()).hexdigest()[:8]
        rpath    = f"{MODEL_DIR}/report_{uid_safe}.json"
        if os.path.exists(rpath):
            with open(rpath) as f:
                user_models[uid] = json.load(f)

    # Hourly activity (last 24h)
    now   = time.time() * 1000
    hours = [0]*24
    for e in all_events:
        ts = e.get('ts', 0)
        if ts > now - 86400000:
            h = datetime.fromtimestamp(ts/1000).hour
            hours[h] += 1

    return {
        "online_users":   len(ONLINE),
        "total_users":    len(EVENTS),
        "total_events":   total,
        "plays":          plays,
        "skips":          skips,
        "likes":          likes,
        "sleep_events":   sleep_evts,
        "motion_events":  motion_evts,
        "speech_events":  speech_evts,
        "skip_rate":      round(skips/max(plays,1), 3),
        "mode_dist":      dict(mode_counts),
        "user_models":    user_models,
        "training":       TRAINING_STATUS,
        "hourly":         hours,
        "online":         list(ONLINE.keys()),
        "timestamp":      datetime.now().isoformat(),
    }


# ══════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════════════════
@app.get("/")
def root():
    return {
        "app": "Loopy Neural Backend v2",
        "status": "running",
        "users": len(EVENTS),
        "models": len(MODELS),
        "tip": "POST /events to log data. POST /predict to get predictions."
    }


@app.get("/status")
def status():
    return build_admin_stats()


@app.post("/events")
async def log_events(batch: EventBatch):
    """
    Receive events from the browser.
    Called every 30 seconds by Loopy frontend.
    Each event contains: play/skip/like + cursor + speech + GPS + sleep data.
    """
    uid    = batch.user_id
    events = batch.events

    # Tag each event with user_id
    for e in events:
        e['user_id']  = uid
        e['username'] = batch.username
        if 'ts' not in e:
            e['ts'] = int(time.time() * 1000)

    EVENTS[uid].extend(events)

    # Update online status
    ONLINE[uid] = {
        "username":  batch.username,
        "last_seen": datetime.now().isoformat(),
        "events":    len(EVENTS[uid]),
    }

    # Save to disk
    user_file = f"{DATA_DIR}/events_{hashlib.md5(uid.encode()).hexdigest()[:8]}.jsonl"
    with open(user_file, 'a') as f:
        for e in events:
            f.write(json.dumps(e) + '\n')

    # Auto-train if enough data
    n = len(EVENTS[uid])
    if n in (50, 100, 200, 500, 1000) and not TRAINING_STATUS["running"]:
        asyncio.create_task(train_user(uid))

    # Broadcast to admin
    await broadcast_admin()

    return {
        "logged": len(events),
        "total":  n,
        "status": "training" if TRAINING_STATUS["running"] else "collecting",
        "next_train": max(0, 50 - n) if n < 50 else "model exists",
    }


@app.post("/predict/skip")
def pred_skip(req: PredictRequest):
    return predict_skip(req.user_id, req.context)


@app.post("/predict/mood")
def pred_mood(req: PredictRequest):
    return predict_mood(req.user_id, req.context)


@app.post("/predict/rank")
def pred_rank(req: PredictRequest):
    return {"ranked": rank_tracks(req.user_id, req.context, req.candidates)}


@app.post("/predict/all")
def pred_all(req: PredictRequest):
    """Single endpoint for all predictions — called by Karen."""
    return {
        "skip":  predict_skip(req.user_id, req.context),
        "mood":  predict_mood(req.user_id, req.context),
        "ranked": rank_tracks(req.user_id, req.context, req.candidates[:10]),
    }


@app.post("/train")
async def train(req: TrainRequest):
    """Manually trigger training."""
    if TRAINING_STATUS["running"]:
        return {"status": "already_training"}
    uid = req.user_id or (list(EVENTS.keys())[0] if EVENTS else None)
    if not uid:
        return {"status": "no_data"}
    asyncio.create_task(train_user(uid))
    return {"status": "training_started", "user_id": uid}


@app.get("/events/{user_id}")
def get_events(user_id: str):
    evts = EVENTS.get(user_id, [])
    return {
        "user_id": user_id,
        "count":   len(evts),
        "types":   dict(defaultdict(int, {e.get('type','?'): 1 for e in evts})),
        "model":   user_id in MODELS,
    }


@app.get("/model/{user_id}")
def get_model_info(user_id: str):
    uid_safe = hashlib.md5(user_id.encode()).hexdigest()[:8]
    rpath    = f"{MODEL_DIR}/report_{uid_safe}.json"
    if os.path.exists(rpath):
        with open(rpath) as f:
            return json.load(f)
    return {"trained": False, "user_id": user_id}


@app.websocket("/ws/admin")
async def admin_ws(ws: WebSocket):
    """WebSocket for real-time admin dashboard."""
    await ws.accept()
    WS_CLIENTS.append(ws)
    try:
        # Send initial stats
        await ws.send_text(json.dumps({"type": "stats", "data": build_admin_stats()}))
        # Keep alive + send updates every 3s
        while True:
            await asyncio.sleep(3)
            await ws.send_text(json.dumps({"type": "stats", "data": build_admin_stats()}))
    except WebSocketDisconnect:
        WS_CLIENTS.remove(ws)


@app.on_event("startup")
async def startup():
    """Load existing events and models on startup."""
    print("=" * 55)
    print("  LOOPY Neural Backend v2 Starting...")
    print("=" * 55)

    # Load existing event files
    for f in os.listdir(DATA_DIR):
        if not f.endswith('.jsonl'): continue
        uid = f.replace('events_','').replace('.jsonl','')
        events = []
        with open(f"{DATA_DIR}/{f}") as fh:
            for line in fh:
                try: events.append(json.loads(line.strip()))
                except: pass
        if events:
            # Group by user_id
            user_map = defaultdict(list)
            for e in events:
                user_map[e.get('user_id', uid)].append(e)
            for real_uid, evts in user_map.items():
                EVENTS[real_uid].extend(evts)
            print(f"  Loaded {len(events)} events from {f}")

    # Try loading saved models
    for uid in list(EVENTS.keys()):
        if load_user_models(uid):
            print(f"  Loaded model for {uid}")

    total = sum(len(v) for v in EVENTS.values())
    print(f"\n  Users: {len(EVENTS)} | Events: {total} | Models: {len(MODELS)}")
    print(f"  API:   http://localhost:8000")
    print(f"  Admin: ws://localhost:8000/ws/admin")
    print("=" * 55)


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("loopy_backend:app", host="0.0.0.0", port=8000, reload=False)
