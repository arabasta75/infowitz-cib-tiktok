"""
seed_demo.py — Peuple tekkai.db avec un corpus DÉMO réaliste de comptes TikTok.

Usage:  python seed_demo.py [n]      (défaut 600 comptes)

Génère 3 cohortes corrélées (bot / zone grise / humain) avec followers, hearts,
régions, patterns d'inauthenticité et vagues de coordination cohérents — de quoi
démontrer le dashboard /overview. La table data/ est gitignorée : rien n'est commité.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone

import db as _db

random.seed(42)

REGIONS = ["FR", "US", "MA", "DZ", "RU", "SN", "GB", "ES", "TN", "CI", "BE", "CA"]
BOT_PATTERNS = [
    "Bio absente", "Avatar par défaut", "Username avec suffixe numérique",
    "Très faible diversité de hashtags", "Bursts de publication synchronisés",
    "Ratio following/followers anormal", "Mot-clé suspect (follow4follow)",
    "Création récente du compte", "Engagement quasi nul",
    "Heures de publication mécaniques (rigidité)", "Légendes dupliquées",
    "Aucune vidéo originale (reposts)",
]
HUMAN_PATTERNS = [
    "Compte vérifié", "Audience établie", "Engagement organique élevé",
    "Diversité de contenu", "Historique de publication ancien",
    "Bio détaillée", "Interactions authentiques",
]
NAMES = ["lina", "yacine", "sofia", "max", "nadia", "leo", "ines", "omar",
         "clara", "sami", "jade", "noah", "maya", "adam", "lou", "rayan"]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _ts_days_ago(d):
    return (datetime.now(timezone.utc) - timedelta(days=d)).replace(
        tzinfo=None).isoformat() + "Z"


def _verdict(score):
    if score <= 40:
        return "bot"
    if score < 70:
        return "unclear"
    return "human"


def make_account(i, cohort):
    name = random.choice(NAMES)
    if cohort == "bot":
        score = round(random.triangular(5, 40, 22), 1)
        uid = f"{name}{random.randint(100000, 9999999)}"
        followers = random.randint(0, 400)
        following = random.randint(400, 4000)
        hearts = random.randint(0, 1500)
        videos = random.randint(0, 18)
        pats = random.sample(BOT_PATTERNS, k=random.randint(4, 7))
        verified = 0
        # bots concentrés sur quelques régions (coordination)
        region = random.choice(["MA", "DZ", "RU", "RU", "MA"])
    elif cohort == "unclear":
        score = round(random.triangular(40, 70, 55), 1)
        uid = f"{name}_{random.choice(['off','real','tv',''])}{random.randint(1,999)}"
        followers = random.randint(200, 12000)
        following = random.randint(150, 2500)
        hearts = random.randint(1000, 80000)
        videos = random.randint(10, 120)
        pats = random.sample(BOT_PATTERNS, k=random.randint(1, 3)) + \
            random.sample(HUMAN_PATTERNS, k=random.randint(0, 2))
        verified = 0
        region = random.choice(REGIONS)
    else:  # human
        score = round(random.triangular(70, 98, 82), 1)
        uid = f"{name}.{random.choice(['paris','officiel','media','news',''])}".strip(".")
        uid = uid or f"{name}{random.randint(1,99)}"
        followers = random.randint(5000, 3_000_000)
        following = random.randint(50, 1500)
        hearts = random.randint(50_000, 90_000_000)
        videos = random.randint(40, 1200)
        pats = random.sample(HUMAN_PATTERNS, k=random.randint(2, 5))
        verified = 1 if random.random() < 0.25 else 0
        region = random.choice(REGIONS)
    return {
        "unique_id": f"{uid}_{i}", "display_name": name.capitalize(),
        "followers": followers, "following": following, "hearts": hearts,
        "video_count": videos, "verified": verified, "region": region,
        "bot_score": score, "verdict": _verdict(score),
        "patterns": json.dumps(pats, ensure_ascii=False),
        "posts_analyzed": videos,
        "flagged": 1 if (cohort == "bot" and random.random() < 0.05) else 0,
        "reported": 1 if (cohort == "bot" and random.random() < 0.02) else 0,
    }


def seed(n=600):
    _db.init_db()
    c = _db._conn()
    c.execute("DELETE FROM tk_accounts")
    c.execute("DELETE FROM tk_search_history")
    # répartition réaliste : ~45% bot, ~35% gris, ~20% humain
    cohorts = (["bot"] * int(n * 0.45) + ["unclear"] * int(n * 0.35) +
               ["human"] * (n - int(n * 0.45) - int(n * 0.35)))
    random.shuffle(cohorts)
    for i, ch in enumerate(cohorts):
        a = make_account(i, ch)
        ts0 = _ts_days_ago(random.randint(0, 45))
        c.execute("""
            INSERT INTO tk_accounts
            (unique_id, display_name, avatar, followers, following, hearts,
             video_count, verified, region, first_seen_ts, last_seen_ts, runs,
             bot_score, verdict, patterns, posts_analyzed, flagged, notes,
             context, manual_override, reported, report_reasons, reported_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (a["unique_id"], a["display_name"], "", a["followers"], a["following"],
              a["hearts"], a["video_count"], a["verified"], a["region"], ts0, ts0, 1,
              a["bot_score"], a["verdict"], a["patterns"], a["posts_analyzed"],
              a["flagged"], "", "{}", 0, a["reported"], "[]", None))
    # historique de scans (hashtags suivis)
    tags = ["#presidentielle", "#gaza", "#bardella", "#maroc2030",
            "#standwith", "#stopmacron", "#football", "#crypto"]
    for k in range(14):
        c.execute("""INSERT INTO tk_search_history
            (user_id, keyword, mode, ts, account_count, params)
            VALUES (?,?,?,?,?,?)""",
                  ("demo", random.choice(tags), random.choice(["hashtag", "account"]),
                   _ts_days_ago(random.randint(0, 30)), random.randint(20, 300), "{}"))
    c.commit()
    print(f"✅ Seed démo : {n} comptes TikTok + 14 scans dans {_db._DB_PATH}")


if __name__ == "__main__":
    seed(int(sys.argv[1]) if len(sys.argv) > 1 else 600)
