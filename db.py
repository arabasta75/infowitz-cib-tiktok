"""
db.py — SQLite pour Tekkai (TikTok inauthenticity engine)
Tables : tk_accounts, tk_search_history
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

_DATA_DIR = (
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.environ.get("DATA_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
)
_DB_PATH = os.path.join(_DATA_DIR, 'tekkai.db')
os.makedirs(_DATA_DIR, exist_ok=True)
_LOCAL = threading.local()


def _conn() -> sqlite3.Connection:
    if not getattr(_LOCAL, 'conn', None):
        c = sqlite3.connect(_DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _LOCAL.conn = c
    return _LOCAL.conn


def init_db():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS tk_accounts (
        unique_id       TEXT PRIMARY KEY,
        display_name    TEXT DEFAULT '',
        avatar          TEXT DEFAULT '',
        followers       INTEGER DEFAULT 0,
        following       INTEGER DEFAULT 0,
        hearts          INTEGER DEFAULT 0,
        video_count     INTEGER DEFAULT 0,
        verified        INTEGER DEFAULT 0,
        region          TEXT DEFAULT '',
        first_seen_ts   TEXT NOT NULL,
        last_seen_ts    TEXT NOT NULL,
        runs            INTEGER NOT NULL DEFAULT 1,
        bot_score       REAL    NOT NULL DEFAULT 50,
        verdict         TEXT    NOT NULL DEFAULT 'unclear',
        patterns        TEXT    DEFAULT '[]',
        posts_analyzed  INTEGER DEFAULT 0,
        llm_verdict     TEXT,
        flagged         INTEGER NOT NULL DEFAULT 0,
        notes           TEXT    DEFAULT '',
        context         TEXT    DEFAULT '{}',
        manual_override INTEGER NOT NULL DEFAULT 0,
        reported        INTEGER NOT NULL DEFAULT 0,
        report_reasons  TEXT    DEFAULT '[]',
        reported_ts     TEXT    DEFAULT NULL
    );

    CREATE INDEX IF NOT EXISTS ix_tk_score   ON tk_accounts(bot_score);
    CREATE INDEX IF NOT EXISTS ix_tk_flagged ON tk_accounts(flagged);
    CREATE INDEX IF NOT EXISTS ix_tk_verdict ON tk_accounts(verdict);

    CREATE TABLE IF NOT EXISTS tk_search_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       TEXT    NOT NULL DEFAULT 'default',
        keyword       TEXT    NOT NULL,
        mode          TEXT    NOT NULL DEFAULT 'account',
        ts            TEXT    NOT NULL,
        account_count INTEGER NOT NULL DEFAULT 0,
        params        TEXT    DEFAULT '{}'
    );

    CREATE INDEX IF NOT EXISTS ix_tksh_user ON tk_search_history(user_id);
    CREATE INDEX IF NOT EXISTS ix_tksh_ts   ON tk_search_history(ts DESC);
    """)
    c.commit()


def tk_upsert(unique_id: str, bot_score: float, verdict: str,
              display_name: str = '', avatar: str = '',
              followers: int = 0, following: int = 0,
              hearts: int = 0, video_count: int = 0,
              verified: bool = False, region: str = '',
              patterns: list = None, posts_analyzed: int = 0,
              llm_verdict: str = None, context: dict = None):
    c = _conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
    existing = c.execute("SELECT * FROM tk_accounts WHERE unique_id=?", (unique_id,)).fetchone()
    if existing:
        c.execute("""
            UPDATE tk_accounts SET
                last_seen_ts=?, runs=runs+1, bot_score=?, verdict=?,
                patterns=?, posts_analyzed=posts_analyzed+?,
                llm_verdict=COALESCE(?,llm_verdict),
                context=COALESCE(?,context),
                followers=?, following=?, hearts=?, video_count=?, verified=?, region=?,
                display_name=CASE WHEN ? != '' THEN ? ELSE display_name END,
                avatar=CASE WHEN ? != '' THEN ? ELSE avatar END
            WHERE unique_id=?
        """, (now, bot_score, verdict,
              json.dumps(patterns or []), posts_analyzed,
              llm_verdict, json.dumps(context) if context else None,
              followers, following, hearts, video_count, 1 if verified else 0, region,
              display_name or '', display_name or '',
              avatar or '', avatar or '',
              unique_id))
    else:
        c.execute("""
            INSERT INTO tk_accounts
            (unique_id, display_name, avatar, followers, following, hearts,
             video_count, verified, region,
             first_seen_ts, last_seen_ts, runs, bot_score, verdict,
             patterns, posts_analyzed, llm_verdict, context)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)
        """, (unique_id, display_name, avatar, followers, following, hearts,
              video_count, 1 if verified else 0, region,
              now, now, bot_score, verdict,
              json.dumps(patterns or []), posts_analyzed,
              llm_verdict, json.dumps(context or {})))
    c.commit()


def tk_list(flagged_only: bool = False, limit: int = 500,
            min_score: float = None, max_score: float = None,
            verdict: str = None, offset: int = 0) -> list:
    c = _conn()
    conditions, args = [], []
    if flagged_only:
        conditions.append("flagged=1")
    if verdict:
        conditions.append("verdict=?"); args.append(verdict)
    if min_score is not None:
        conditions.append("bot_score>=?"); args.append(min_score)
    if max_score is not None:
        conditions.append("bot_score<=?"); args.append(max_score)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = c.execute(
        f"SELECT * FROM tk_accounts {where} ORDER BY bot_score ASC LIMIT ? OFFSET ?",
        (*args, limit, offset)
    ).fetchall()
    return [dict(r) for r in rows]


def tk_count(flagged_only: bool = False,
             min_score: float = None, max_score: float = None) -> int:
    c = _conn()
    conditions, args = [], []
    if flagged_only:
        conditions.append("flagged=1")
    if min_score is not None:
        conditions.append("bot_score>=?"); args.append(min_score)
    if max_score is not None:
        conditions.append("bot_score<=?"); args.append(max_score)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return c.execute(f"SELECT COUNT(*) FROM tk_accounts {where}", args).fetchone()[0]


def tk_stats() -> dict:
    c = _conn()
    row = c.execute("""
        SELECT
            COUNT(*)                                          AS total,
            SUM(CASE WHEN bot_score <= 40  THEN 1 ELSE 0 END) AS bots,
            SUM(CASE WHEN bot_score > 40 AND bot_score < 70 THEN 1 ELSE 0 END) AS unclear,
            SUM(CASE WHEN bot_score >= 70  THEN 1 ELSE 0 END) AS legit,
            SUM(CASE WHEN flagged = 1      THEN 1 ELSE 0 END) AS flagged,
            SUM(CASE WHEN verified = 1     THEN 1 ELSE 0 END) AS verified
        FROM tk_accounts
    """).fetchone()
    return {
        'total':    row[0] or 0,
        'bots':     row[1] or 0,
        'unclear':  row[2] or 0,
        'legit':    row[3] or 0,
        'flagged':  row[4] or 0,
        'verified': row[5] or 0,
    }


def tk_flag(unique_id: str, flagged: bool, notes: str = None) -> bool:
    c = _conn()
    if notes is not None:
        cur = c.execute("UPDATE tk_accounts SET flagged=?, notes=? WHERE unique_id=?",
                        (1 if flagged else 0, notes, unique_id))
    else:
        cur = c.execute("UPDATE tk_accounts SET flagged=? WHERE unique_id=?",
                        (1 if flagged else 0, unique_id))
    c.commit()
    return cur.rowcount > 0


def tk_set_manual_score(unique_id: str, bot_score: float, verdict: str, notes: str = None) -> bool:
    c = _conn()
    verdict = verdict.strip().lower()
    if verdict not in ('bot', 'human', 'unclear'):
        verdict = 'unclear'
    bot_score = max(0.0, min(100.0, float(bot_score)))
    if notes is not None:
        cur = c.execute(
            "UPDATE tk_accounts SET bot_score=?, verdict=?, manual_override=1, notes=? WHERE unique_id=?",
            (bot_score, verdict, str(notes)[:500], unique_id))
    else:
        cur = c.execute(
            "UPDATE tk_accounts SET bot_score=?, verdict=?, manual_override=1 WHERE unique_id=?",
            (bot_score, verdict, unique_id))
    c.commit()
    return cur.rowcount > 0


def tk_get_manual_overrides() -> dict:
    c = _conn()
    rows = c.execute(
        "SELECT unique_id, bot_score, verdict FROM tk_accounts WHERE manual_override=1"
    ).fetchall()
    return {r['unique_id'].lower(): {'bot_score': r['bot_score'], 'verdict': r['verdict']} for r in rows}


def tk_delete(unique_id: str) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM tk_accounts WHERE unique_id=?", (unique_id,))
    c.commit()
    return cur.rowcount > 0


def sh_insert(user_id: str, keyword: str, mode: str = 'account',
              account_count: int = 0, params: dict = None) -> int:
    c = _conn()
    ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
    cur = c.execute(
        "INSERT INTO tk_search_history (user_id, keyword, mode, ts, account_count, params) VALUES (?,?,?,?,?,?)",
        (user_id, keyword, mode, ts, account_count, json.dumps(params or {}))
    )
    c.commit()
    return cur.lastrowid


def sh_list(user_id: str, limit: int = 200) -> list:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM tk_search_history WHERE user_id=? ORDER BY ts DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def sh_delete(record_id: int, user_id: str) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM tk_search_history WHERE id=? AND user_id=?", (record_id, user_id))
    c.commit()
    return cur.rowcount > 0
