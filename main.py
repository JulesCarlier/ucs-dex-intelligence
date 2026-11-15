import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DEXI_DB_PATH", str(APP_DIR / "data" / "dexi.sqlite3")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
INGEST_API_KEY = os.getenv("DEXI_INGEST_API_KEY", "")
CORS_ORIGINS = [x.strip() for x in os.getenv("DEXI_CORS_ORIGINS", "*").split(",") if x.strip()]

app = FastAPI(title="DEX INTELLIGENCE API", version="1.0.0", description="API de lecture, suivi et simulation de DEX INTELLIGENCE.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY,
            chain TEXT NOT NULL,
            address TEXT NOT NULL,
            name TEXT,
            symbol TEXT,
            url TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            initial_price REAL,
            latest_price REAL,
            latest_score INTEGER,
            latest_decision TEXT,
            latest_metrics TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL,
            label TEXT NOT NULL,
            observed_at REAL NOT NULL,
            price_usd REAL,
            score INTEGER,
            decision TEXT,
            metrics TEXT NOT NULL,
            positives TEXT NOT NULL DEFAULT '[]',
            risks TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY(candidate_id) REFERENCES candidates(id)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_candidate_time ON snapshots(candidate_id, observed_at DESC);
        CREATE TABLE IF NOT EXISTS virtual_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL,
            amount_usd REAL NOT NULL,
            entry_price REAL NOT NULL,
            opened_at REAL NOT NULL,
            closed_at REAL,
            exit_price REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            FOREIGN KEY(candidate_id) REFERENCES candidates(id)
        );
        """)


init_db()


class ScannerEvent(BaseModel):
    event_type: str = Field(pattern="^(detection|checkpoint)$")
    label: Optional[str] = None
    pair: dict[str, Any]
    analysis: dict[str, Any]
    metrics: dict[str, Any]
    observed_at: Optional[float] = None


class VirtualPositionRequest(BaseModel):
    candidate_id: str
    amount_usd: float = Field(gt=0, le=1000000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def require_ingest_key(x_api_key: Optional[str]):
    if not INGEST_API_KEY:
        raise HTTPException(status_code=503, detail="Ingestion non configurée : DEXI_INGEST_API_KEY manque côté serveur.")
    if not x_api_key or not secrets.compare_digest(x_api_key, INGEST_API_KEY):
        raise HTTPException(status_code=401, detail="Clé d'ingestion invalide.")


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def confirmation_for(snapshots):
    # La confirmation dépend des critères de marché, jamais du score de qualité.
    if len(snapshots) < 2:
        return {"status": "EN ATTENTE", "compliant_checks": 0, "required_checks": 2}
    compliant = 0
    for snap in snapshots[:2]:
        m = json.loads(snap["metrics"])
        ok = (
            safe_float(m.get("buy_ratio")) > 55
            and 0 <= safe_float(m.get("change_5m")) <= 8
            and safe_float(m.get("liquidity")) > 20000
        )
        if ok:
            compliant += 1
        else:
            break
    return {
        "status": "CONFIRMÉ (CRITÈRES DE SUIVI)" if compliant == 2 else "NON CONFIRMÉ",
        "compliant_checks": compliant,
        "required_checks": 2,
    }


def evolution_for(snapshots):
    if len(snapshots) < 2:
        return "DONNÉES INSUFFISANTES"
    current = snapshots[0]["score"]
    previous = snapshots[1]["score"]
    if current is None or previous is None:
        return "DONNÉES INSUFFISANTES"
    delta = int(current) - int(previous)
    if delta >= 5:
        return "EN AMÉLIORATION"
    if delta <= -5:
        return "EN DÉGRADATION"
    return "STABLE"


def serialize_candidate(row, snapshots):
    data = dict(row)
    data["latest_metrics"] = json.loads(data.get("latest_metrics") or "{}")
    history = [dict(s) for s in snapshots]
    for snap in history:
        snap["metrics"] = json.loads(snap["metrics"] or "{}")
        snap["positives"] = json.loads(snap["positives"] or "[]")
        snap["risks"] = json.loads(snap["risks"] or "[]")
    data["snapshots"] = history
    data["confirmation"] = confirmation_for(snapshots)
    data["evolution"] = evolution_for(snapshots)
    return data


@app.get("/")
def root():
    return {"service": "DEX INTELLIGENCE API", "status": "online", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "dex-intelligence-api", "timestamp": time.time()}


@app.get("/api/status")
def status():
    with db() as con:
        candidates = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        snapshots = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        open_positions = con.execute("SELECT COUNT(*) FROM virtual_positions WHERE status='OPEN'").fetchone()[0]
        latest = con.execute("SELECT MAX(observed_at) FROM snapshots").fetchone()[0]
    return {
        "api": "ONLINE", "scanner": "DONNÉES REÇUES" if latest else "EN ATTENTE",
        "candidate_count": candidates, "snapshot_count": snapshots,
        "open_virtual_positions": open_positions, "last_update": latest,
        "mode": "LECTURE / SIMULATION UNIQUEMENT"
    }


@app.post("/api/ingest")
def ingest(event: ScannerEvent, x_api_key: Optional[str] = Header(default=None)):
    require_ingest_key(x_api_key)
    pair = event.pair
    token = pair.get("baseToken") or {}
    chain = str(pair.get("chainId") or "")
    address = str(token.get("address") or "")
    if not chain or not address:
        raise HTTPException(status_code=422, detail="Le pair doit contenir chainId et baseToken.address.")
    candidate_id = f"{chain}:{address.lower()}"
    label = event.label or ("Détection" if event.event_type == "detection" else "Point de suivi")
    observed_at = event.observed_at or time.time()
    price = safe_float(pair.get("priceUsd"), 0.0)
    score = int(event.analysis.get("score") or 0)
    decision = str(event.analysis.get("decision") or "NON ÉVALUÉ")
    metrics = event.metrics

    with db() as con:
        existing = con.execute("SELECT id, initial_price FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if existing:
            con.execute("""UPDATE candidates SET name=?, symbol=?, url=?, last_seen=?, latest_price=?, latest_score=?, latest_decision=?, latest_metrics=? WHERE id=?""",
                (token.get("name"), token.get("symbol"), pair.get("url"), observed_at, price, score, decision, json.dumps(metrics), candidate_id))
        else:
            con.execute("""INSERT INTO candidates(id,chain,address,name,symbol,url,first_seen,last_seen,initial_price,latest_price,latest_score,latest_decision,latest_metrics)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, chain, address, token.get("name"), token.get("symbol"), pair.get("url"), observed_at, observed_at, price, price, score, decision, json.dumps(metrics)))
        con.execute("""INSERT INTO snapshots(candidate_id,label,observed_at,price_usd,score,decision,metrics,positives,risks)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (candidate_id, label, observed_at, price, score, decision, json.dumps(metrics), json.dumps(event.analysis.get("positives") or []), json.dumps(event.analysis.get("risks") or [])))
    return {"ok": True, "candidate_id": candidate_id, "label": label}


@app.get("/api/candidates")
def candidates(limit: int = Query(default=100, ge=1, le=500)):
    with db() as con:
        rows = con.execute("SELECT * FROM candidates ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        output = []
        for row in rows:
            snaps = con.execute("SELECT * FROM snapshots WHERE candidate_id=? ORDER BY observed_at DESC,id DESC LIMIT 8", (row["id"],)).fetchall()
            output.append(serialize_candidate(row, snaps))
    return {"items": output, "count": len(output)}


@app.get("/api/candidates/{candidate_id:path}")
def candidate_detail(candidate_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Candidat introuvable.")
        snaps = con.execute("SELECT * FROM snapshots WHERE candidate_id=? ORDER BY observed_at DESC,id DESC LIMIT 100", (candidate_id,)).fetchall()
    return serialize_candidate(row, snaps)


@app.get("/api/simulation")
def simulation():
    with db() as con:
        rows = con.execute("""SELECT p.*,c.symbol,c.name,c.chain,c.latest_price FROM virtual_positions p
            JOIN candidates c ON c.id=p.candidate_id ORDER BY p.opened_at DESC LIMIT 200""").fetchall()
    positions = []
    for row in rows:
        item = dict(row)
        current = safe_float(item.get("latest_price"), item["entry_price"])
        ref = item["exit_price"] if item["status"] == "CLOSED" else current
        item["current_price"] = ref
        item["pnl_usd"] = (ref - item["entry_price"]) / item["entry_price"] * item["amount_usd"] if item["entry_price"] else 0
        item["pnl_percent"] = (ref - item["entry_price"]) / item["entry_price"] * 100 if item["entry_price"] else 0
        positions.append(item)
    return {"items": positions, "count": len(positions), "mode": "SIMULATION VIRTUELLE"}


@app.post("/api/simulation/open")
def open_virtual_position(request: VirtualPositionRequest):
    with db() as con:
        candidate = con.execute("SELECT id,latest_price FROM candidates WHERE id=?", (request.candidate_id,)).fetchone()
        if not candidate:
            raise HTTPException(status_code=404, detail="Candidat introuvable.")
        price = safe_float(candidate["latest_price"])
        if price <= 0:
            raise HTTPException(status_code=400, detail="Prix actuel indisponible.")
        cursor = con.execute("INSERT INTO virtual_positions(candidate_id,amount_usd,entry_price,opened_at,status) VALUES(?,?,?,?, 'OPEN')",
            (request.candidate_id, request.amount_usd, price, time.time()))
        position_id = cursor.lastrowid
    return {"ok": True, "position_id": position_id, "mode": "VIRTUEL UNIQUEMENT"}


@app.post("/api/simulation/{position_id}/close")
def close_virtual_position(position_id: int):
    with db() as con:
        row = con.execute("""SELECT p.id,p.status,c.latest_price FROM virtual_positions p JOIN candidates c ON c.id=p.candidate_id WHERE p.id=?""", (position_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Position virtuelle introuvable.")
        if row["status"] != "OPEN":
            raise HTTPException(status_code=409, detail="Cette simulation est déjà clôturée.")
        price = safe_float(row["latest_price"])
        if price <= 0:
            raise HTTPException(status_code=400, detail="Prix actuel indisponible.")
        con.execute("UPDATE virtual_positions SET status='CLOSED',closed_at=?,exit_price=? WHERE id=?", (time.time(), price, position_id))
    return {"ok": True, "position_id": position_id, "mode": "VIRTUEL UNIQUEMENT"}


@app.post("/api/chat")
def chat(request: ChatRequest):
    # Assistant de secours transparent : aucune fausse prétention à une IA connectée.
    q = request.question.lower()
    if any(word in q for word in ("confirmation", "confirmé", "confirmer")):
        answer = "La confirmation est indépendante du score : il faut deux contrôles consécutifs avec plus de 55 % d'achats, une variation 5 min comprise entre 0 et +8 %, et une liquidité supérieure à 20 000 $."
    elif "score" in q or "qualité" in q:
        answer = "Le score qualité résume les indicateurs du scanner sur 100. Il ne confirme pas à lui seul un candidat et ne constitue pas une instruction d'achat ou de vente."
    elif "risque" in q or "liquidité" in q:
        answer = "Les principaux points de vigilance suivis par le scanner sont la liquidité, le volume, l'équilibre achats/ventes et les variations rapides. Une liquidité faible peut rendre les prix plus difficiles à interpréter."
    elif "simulation" in q:
        answer = "La simulation est entièrement virtuelle. Elle sert à observer les variations historiques sans ordre réel, portefeuille connecté ni transfert d'argent."
    else:
        answer = "Je peux expliquer le score, les critères de confirmation, les risques suivis ou le fonctionnement de la simulation. Le moteur IA génératif n'est pas encore connecté ; je ne vais pas inventer une analyse des données."
    return {"answer": answer, "engine": "ASSISTANT DE BASE — IA GÉNÉRATIVE NON CONFIGURÉE", "disclaimer": "Information pédagogique et simulation uniquement, pas de conseil financier personnalisé."}
