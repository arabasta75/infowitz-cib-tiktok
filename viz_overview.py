"""
viz_overview.py — Agrégations corpus global pour le dashboard « CIB Overview »
de Tekkai (Infowitz · CIB TikTok).

Lit data/tekkai.db (tk_accounts) et renvoie des séries prêtes à tracer (Plotly),
en vectorisé avec Polars. Caché 60 s. Indépendant du reste de l'app.

bot_score = score d'AUTHENTICITÉ : 0 = bot · 100 = humain.
Seuils (cf. scoring.py / db.tk_stats) : <=40 bot · 40-70 zone grise · >=70 humain.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter

try:
    import polars as pl
    _HAS_POLARS = True
except Exception:  # pragma: no cover
    _HAS_POLARS = False

_DATA_DIR = (
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.environ.get("DATA_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)
_DB_PATH = os.path.join(_DATA_DIR, "tekkai.db")

_CACHE: dict = {"data": None, "ts": 0.0}
_TTL = 60

T_BOT = 40
T_HUMAN = 70
C_BOT, C_GREY, C_HUMAN, C_PINK = "#EF4444", "#F59E0B", "#10B981", "#fe2c55"


def _canon(verdict: str | None, score: float) -> str:
    v = (verdict or "").strip().lower()
    if v == "human":
        return "Authentique"
    if v == "bot":
        return "Bot"
    if v == "unclear":
        return "Zone grise"
    if score <= T_BOT:
        return "Bot"
    if score >= T_HUMAN:
        return "Authentique"
    return "Zone grise"


def _rows():
    if not os.path.exists(_DB_PATH):
        return []
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(
            "SELECT unique_id, bot_score, verdict, patterns, flagged, reported, "
            "verified, followers, following, hearts, video_count, region "
            "FROM tk_accounts")]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def _compute() -> dict:
    rows = _rows()
    total = len(rows)
    if total == 0:
        return {"total": 0}

    # Patterns
    pat = Counter()
    for r in rows:
        try:
            v = json.loads(r.get("patterns") or "[]")
            pat.update(v if isinstance(v, list) else v.keys())
        except Exception:
            pass
    patterns_top = [{"pattern": k, "count": n} for k, n in pat.most_common(16)]

    scores = [float(r["bot_score"] or 0) for r in rows]
    verdicts = [_canon(r.get("verdict"), float(r["bot_score"] or 0)) for r in rows]

    if _HAS_POLARS:
        df = pl.DataFrame({
            "score": scores, "verdict": verdicts,
            "flagged": [int(r.get("flagged") or 0) for r in rows],
            "verified": [int(r.get("verified") or 0) for r in rows],
            "followers": [int(r.get("followers") or 0) for r in rows],
            "videos": [int(r.get("video_count") or 0) for r in rows],
            "region": [(r.get("region") or "∅") for r in rows],
        })
        avg_score = float(df["score"].mean())
        n_flagged = int(df["flagged"].sum())
        n_verified = int(df["verified"].sum())
        n_highrisk = int(df.filter(pl.col("score") <= T_BOT).height)
        n_bot = sum(1 for v in verdicts if v == "Bot")
        region_vc = (df.filter(pl.col("region") != "∅")
                     .group_by("region").len().sort("len", descending=True)
                     .head(12).to_dicts())
    else:
        avg_score = sum(scores) / total
        n_flagged = sum(int(r.get("flagged") or 0) for r in rows)
        n_verified = sum(int(r.get("verified") or 0) for r in rows)
        n_highrisk = sum(1 for s in scores if s <= T_BOT)
        n_bot = sum(1 for v in verdicts if v == "Bot")
        rc = Counter(r.get("region") or "∅" for r in rows if (r.get("region") or "∅") != "∅")
        region_vc = [{"region": k, "len": v} for k, v in rc.most_common(12)]

    # Histogramme score (20 bins)
    edges = list(range(0, 105, 5))
    hist_y = [0] * (len(edges) - 1)
    for s in scores:
        hist_y[min(int(s // 5), len(hist_y) - 1)] += 1
    hist_x = [e + 2.5 for e in edges[:-1]]

    vmap = Counter(verdicts)

    # Scatter followers × authenticité
    cmap = {"Bot": C_BOT, "Zone grise": C_GREY, "Authentique": C_HUMAN}
    sc_x, sc_y, sc_color, sc_size, sc_text = [], [], [], [], []
    for r, vd in zip(rows, verdicts):
        sc_x.append(float(r["bot_score"] or 0))
        sc_y.append(int(r.get("followers") or 0))
        sc_color.append(cmap[vd])
        sc_size.append(int(r.get("video_count") or 0))
        sc_text.append("@" + (r.get("unique_id") or ""))

    return {
        "total": total,
        "kpis": {
            "total": total,
            "pct_bot": round(100 * n_bot / total, 1),
            "avg_score": round(avg_score, 1),
            "n_highrisk": n_highrisk,
            "n_flagged": n_flagged,
            "n_verified": n_verified,
        },
        "thresholds": {"bot": T_BOT, "human": T_HUMAN},
        "score_hist": {"x": hist_x, "y": hist_y},
        "verdict": {
            "labels": ["Bot", "Zone grise", "Authentique"],
            "values": [vmap.get("Bot", 0), vmap.get("Zone grise", 0),
                       vmap.get("Authentique", 0)],
            "colors": [C_BOT, C_GREY, C_HUMAN],
        },
        "patterns": patterns_top,
        "regions": [{"region": d["region"], "count": d["len"]} for d in region_vc],
        "followers_scatter": {"x": sc_x, "y": sc_y, "color": sc_color,
                              "size": sc_size, "text": sc_text},
        "engine": "polars" if _HAS_POLARS else "stdlib",
    }


def get_overview(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"]) < _TTL:
        return _CACHE["data"]
    data = _compute()
    _CACHE["data"] = data
    _CACHE["ts"] = now
    return data


if __name__ == "__main__":
    import pprint
    d = get_overview(force=True)
    pprint.pprint({k: v for k, v in d.items()
                   if k not in ("followers_scatter", "patterns")})
