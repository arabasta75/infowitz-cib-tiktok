"""
tiktok_collector.py — Couche d'accès aux données TikTok via TikFly (RapidAPI)
=============================================================================
API : tiktok-api23.p.rapidapi.com  (TikFly)
Auth: x-rapidapi-key

Endpoints utilisés :
  GET /api/user/info?uniqueId={handle}            → profil + stats
  GET /api/user/info-with-region?uniqueId={handle}→ profil étendu avec région
  GET /api/user/posts?uniqueId={handle}&cursor={} → vidéos du compte (pagination)

Format de réponse :
  { "userInfo": { "user": {...}, "stats": {...} } }
  { "data": { "itemList": [...], "hasMore": bool, "cursor": str } }
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ─── Cache disque ──────────────────────────────────────────────────────────────

_DATA_DIR = (
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.environ.get("DATA_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
)
_CACHE_PATH = os.path.join(_DATA_DIR, 'tiktok_api_cache.db')
_cache_lock = threading.Lock()


def _cache_conn() -> sqlite3.Connection:
    c = sqlite3.connect(_CACHE_PATH, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_cache (
            key      TEXT PRIMARY KEY,
            body     TEXT NOT NULL,
            saved_at REAL NOT NULL
        )
    """)
    c.commit()
    return c


def _cache_get(key: str, ttl_seconds: int = 3600 * 6) -> dict | None:
    with _cache_lock:
        try:
            c = _cache_conn()
            row = c.execute("SELECT body, saved_at FROM api_cache WHERE key=?", (key,)).fetchone()
            if row and (time.time() - row[1]) < ttl_seconds:
                return json.loads(row[0])
        except Exception:
            pass
    return None


def _cache_set(key: str, data: dict):
    with _cache_lock:
        try:
            c = _cache_conn()
            c.execute(
                "INSERT OR REPLACE INTO api_cache (key, body, saved_at) VALUES (?,?,?)",
                (key, json.dumps(data), time.time())
            )
            c.commit()
        except Exception as e:
            logger.warning(f'[tk_cache] write error: {e}')


def _cache_key(endpoint: str, params: dict) -> str:
    raw = endpoint + '|' + json.dumps(params, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── Normalisation ────────────────────────────────────────────────────────────

def normalize_tiktok_user(raw_user: dict, raw_stats: dict) -> dict:
    """Normalise un profil TikTok brut vers le format Tekkai."""
    def _int(v):
        try: return int(v or 0)
        except (ValueError, TypeError): return 0

    uid      = raw_user.get('id') or raw_user.get('uid') or ''
    unique_id = raw_user.get('uniqueId') or raw_user.get('unique_id') or ''
    nickname  = raw_user.get('nickname') or ''
    signature = raw_user.get('signature') or ''
    avatar    = (raw_user.get('avatarMedium') or raw_user.get('avatarThumb')
                 or raw_user.get('avatarLarger') or '')
    verified  = bool(raw_user.get('verified') or raw_user.get('isVerified'))
    region    = raw_user.get('region') or raw_user.get('language') or ''
    sec_uid   = raw_user.get('secUid') or raw_user.get('sec_uid') or ''
    private   = bool(raw_user.get('privateAccount') or raw_user.get('isPrivate'))
    create_ts = _int(raw_user.get('createTime') or raw_user.get('createtime') or 0)

    followers  = _int(raw_stats.get('followerCount') or raw_stats.get('followers') or 0)
    following  = _int(raw_stats.get('followingCount') or raw_stats.get('following') or 0)
    hearts     = _int(raw_stats.get('heartCount') or raw_stats.get('heart') or raw_stats.get('diggCount') or 0)
    video_count = _int(raw_stats.get('videoCount') or raw_stats.get('aweme_count') or 0)
    friend_count = _int(raw_stats.get('friendCount') or 0)

    created_at = ''
    if create_ts > 0:
        try:
            created_at = datetime.fromtimestamp(create_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            pass

    return {
        'uid':         uid,
        'unique_id':   unique_id,
        'display_name': nickname,
        'signature':   signature,
        'avatar':      avatar,
        'verified':    verified,
        'private':     private,
        'region':      region,
        'sec_uid':     sec_uid,
        'followers':   followers,
        'following':   following,
        'hearts':      hearts,
        'video_count': video_count,
        'friend_count': friend_count,
        'created_at':  created_at,
        '_raw_user':   raw_user,
        '_raw_stats':  raw_stats,
    }


def normalize_tiktok_video(raw: dict) -> dict:
    """Normalise un objet vidéo TikTok brut."""
    def _int(v):
        try: return int(v or 0)
        except (ValueError, TypeError): return 0

    stats = raw.get('stats') or raw.get('statistics') or {}
    author = raw.get('author') or {}
    music  = raw.get('music') or {}
    desc   = raw.get('desc') or raw.get('description') or ''
    hashtags = re.findall(r'#([A-Za-zÀ-ÿ0-9_]{2,})', desc)

    create_ts = _int(raw.get('createTime') or raw.get('create_time') or 0)
    created_at = ''
    if create_ts > 0:
        try:
            created_at = datetime.fromtimestamp(create_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            pass

    return {
        'video_id':     raw.get('id') or raw.get('aweme_id') or '',
        'desc':         desc,
        'created_at':   created_at,
        'create_ts':    create_ts,
        'plays':        _int(stats.get('playCount') or stats.get('play_count') or 0),
        'likes':        _int(stats.get('diggCount') or stats.get('digg_count') or 0),
        'comments':     _int(stats.get('commentCount') or stats.get('comment_count') or 0),
        'shares':       _int(stats.get('shareCount') or stats.get('share_count') or 0),
        'duration':     _int((raw.get('video') or {}).get('duration') or 0),
        'hashtags':     hashtags,
        'music_id':     music.get('id') or '',
        'music_title':  music.get('title') or '',
        'is_original_sound': bool(music.get('original') or False),
        'author_unique_id': author.get('uniqueId') or '',
        '_raw':         raw,
    }


# ─── TikFly Collector ─────────────────────────────────────────────────────────

class TikFlyCollector:
    """
    Collecteur principal : tiktok-api23.p.rapidapi.com (TikFly)
    Cache disque 6h pour préserver le quota RapidAPI.
    """

    BASE_URL = 'https://tiktok-api23.p.rapidapi.com'
    HOST     = 'tiktok-api23.p.rapidapi.com'
    CACHE_TTL = 3600 * 6
    TIMEOUT   = 30

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            'x-rapidapi-key':  api_key,
            'x-rapidapi-host': self.HOST,
        })

    def _get(self, endpoint: str, params: dict, use_cache: bool = True) -> dict:
        key = _cache_key(endpoint, params)
        if use_cache:
            cached = _cache_get(key, self.CACHE_TTL)
            if cached is not None:
                logger.info(f'[tikfly] cache hit → {endpoint}')
                return cached
        url = f'{self.BASE_URL}{endpoint}'
        logger.info(f'[tikfly] GET {endpoint} {params}')
        try:
            resp = self.session.get(url, params=params, timeout=self.TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if use_cache:
                _cache_set(key, data)
            return data
        except requests.HTTPError as e:
            raise RuntimeError(f'HTTP {resp.status_code}: {resp.text[:300]}') from e
        except Exception as e:
            raise RuntimeError(f'Erreur réseau: {e}') from e

    def get_user_info(self, unique_id: str) -> dict:
        """Récupère le profil + stats d'un compte TikTok par @handle."""
        data = self._get('/api/user/info', {'uniqueId': unique_id})
        user_info = data.get('userInfo') or data.get('data') or data
        user  = user_info.get('user')  or user_info.get('User')  or {}
        stats = user_info.get('stats') or user_info.get('Stats') or {}
        if not user:
            raise RuntimeError(f'Compte introuvable : @{unique_id}')
        return normalize_tiktok_user(user, stats)

    def get_user_posts(self, unique_id: str, max_videos: int = 30) -> list[dict]:
        """Récupère les N dernières vidéos d'un compte (avec pagination cursor)."""
        videos = []
        cursor = None
        pages = 0
        max_pages = min(5, max(1, (max_videos // 10) + 1))

        while len(videos) < max_videos and pages < max_pages:
            params: dict = {'uniqueId': unique_id, 'count': 20}
            if cursor:
                params['cursor'] = cursor
            try:
                data = self._get('/api/user/posts', params)
            except Exception as e:
                logger.warning(f'[tikfly] posts pagination arrêtée p{pages}: {e}')
                break

            raw_items = (
                (data.get('data') or {}).get('itemList')
                or data.get('itemList')
                or data.get('items')
                or []
            )
            for item in raw_items:
                videos.append(normalize_tiktok_video(item))

            has_more = (
                (data.get('data') or {}).get('hasMore')
                or data.get('hasMore')
                or False
            )
            cursor = (
                (data.get('data') or {}).get('cursor')
                or data.get('cursor')
            )
            pages += 1
            if not has_more or not cursor:
                break

        return videos[:max_videos]

    def get_user_full(self, unique_id: str, max_videos: int = 30,
                      job_state: dict = None) -> dict:
        """
        Récupère profil + vidéos pour un compte.
        Retourne { 'user': dict, 'videos': list }
        """
        if job_state:
            job_state['msg'] = f'Récupération profil @{unique_id}…'

        user = self.get_user_info(unique_id)

        if job_state:
            job_state['msg'] = f'Récupération vidéos @{unique_id}…'

        videos = []
        if not user.get('private'):
            try:
                videos = self.get_user_posts(unique_id, max_videos=max_videos)
            except Exception as e:
                logger.warning(f'[tikfly] get_user_posts @{unique_id}: {e}')

        return {'user': user, 'videos': videos}


    def get_challenge_id(self, hashtag: str) -> str:
        """Résout un nom de hashtag en challengeId numérique via /api/challenge/info."""
        hashtag = hashtag.lstrip('#').strip()
        data = self._get('/api/challenge/info', {'challengeName': hashtag}, use_cache=True)
        info = (
            data.get('challengeInfo')
            or (data.get('data') or {}).get('challengeInfo')
            or data.get('data') or {}
        )
        cid = (
            info.get('id')
            or info.get('challengeId')
            or (info.get('challenge') or {}).get('id')
            or (info.get('challenge') or {}).get('challengeId')
            or ''
        )
        if not cid:
            raise RuntimeError(f'challengeId introuvable pour #{hashtag} (réponse: {str(data)[:200]})')
        return str(cid)

    def search_hashtag(self, hashtag: str, max_videos: int = 50) -> list[dict]:
        """
        Vidéos d'un hashtag via TikFly.
        Flux : /api/challenge/info (→ challengeId) → /api/challenge/posts (→ vidéos)
        """
        hashtag = hashtag.lstrip('#').strip()

        # Étape 1 : résoudre le challengeId
        challenge_id = self.get_challenge_id(hashtag)
        logger.info(f'[tikfly] #{hashtag} → challengeId={challenge_id}')

        # Étape 2 : récupérer les vidéos
        videos: list[dict] = []
        cursor = '0'
        pages = 0
        max_pages = min(5, max(1, (max_videos // 20) + 1))

        while len(videos) < max_videos and pages < max_pages:
            params: dict = {'challengeId': challenge_id, 'count': 30, 'cursor': cursor}
            try:
                data = self._get('/api/challenge/posts', params)
            except Exception as e:
                logger.warning(f'[tikfly] challenge/posts p{pages}: {e}')
                if pages == 0:
                    raise RuntimeError(f'Erreur récupération posts #{hashtag}: {e}') from e
                break

            raw_items = (
                data.get('itemList')
                or (data.get('data') or {}).get('itemList')
                or data.get('items') or []
            )
            for item in raw_items:
                v = normalize_tiktok_video(item)
                author_raw   = item.get('author') or {}
                author_stats = item.get('authorStats') or item.get('stats') or {}
                if author_raw:
                    v['_author'] = normalize_tiktok_user(author_raw, author_stats)
                videos.append(v)

            has_more = data.get('hasMore') or (data.get('data') or {}).get('hasMore') or False
            cursor   = str(data.get('cursor') or (data.get('data') or {}).get('cursor') or '0')
            pages += 1
            if not has_more or cursor == '0':
                break

        return videos[:max_videos]

    def get_video_comments(self, aweme_id: str, max_comments: int = 50) -> list[dict]:
        """Commentaires d'une vidéo TikTok."""
        comments: list[dict] = []
        cursor = None
        pages = 0

        while len(comments) < max_comments and pages < 3:
            params: dict = {'aweme_id': aweme_id, 'count': 30}
            if cursor:
                params['cursor'] = cursor
            try:
                data = self._get('/api/post/comments', params)
            except Exception as e:
                logger.warning(f'[tikfly] comments p{pages}: {e}')
                break

            raw_items = (
                (data.get('data') or {}).get('comments')
                or data.get('comments') or []
            )
            for c in raw_items:
                u = c.get('user') or {}
                comments.append({
                    'cid':       c.get('cid') or c.get('id') or '',
                    'text':      c.get('text') or '',
                    'like_count': int(c.get('digg_count') or c.get('like_count') or 0),
                    'create_ts': int(c.get('create_time') or 0),
                    'user_id':   u.get('uid') or u.get('id') or '',
                    'unique_id': u.get('unique_id') or u.get('uniqueId') or '',
                    'nickname':  u.get('nickname') or '',
                })

            has_more = (data.get('data') or {}).get('has_more') or data.get('has_more') or False
            cursor   = (data.get('data') or {}).get('cursor') or data.get('cursor')
            pages += 1
            if not has_more or not cursor:
                break

        return comments[:max_comments]

    def search_keyword(self, keyword: str, max_users: int = 30) -> list[dict]:
        """Recherche de comptes par mot-clé."""
        try:
            data = self._get('/api/search/general', {'keyword': keyword, 'type': 1, 'count': max_users})
            items = (
                (data.get('data') or {}).get('user_list')
                or data.get('user_list')
                or data.get('users') or []
            )
            users = []
            for item in items:
                u = item.get('user_info') or item.get('user') or item
                s = item.get('stats') or {}
                if u:
                    users.append(normalize_tiktok_user(u, s))
            return users[:max_users]
        except Exception as e:
            logger.warning(f'[tikfly] keyword search {keyword}: {e}')
            return []


# ─── DISPATCHER ───────────────────────────────────────────────────────────────

def get_collector(user_cfg: dict) -> TikFlyCollector | None:
    key = (user_cfg.get('tikfly_key') or user_cfg.get('rapidapi_key')
           or os.environ.get('TIKFLY_KEY') or os.environ.get('RAPIDAPI_KEY') or '').strip()
    if key:
        return TikFlyCollector(key)
    return None


def fetch_account(user_cfg: dict, unique_id: str,
                  max_videos: int = 30, job_state: dict = None) -> tuple[dict | None, str | None]:
    """Point d'entrée principal. Retourne (account_data, error_or_None)."""
    unique_id = unique_id.lstrip('@').strip()
    collector = get_collector(user_cfg)
    if collector is None:
        return None, "Aucune clé TikFly configurée. Ajoutez tikfly_key dans Configuration."
    try:
        data = collector.get_user_full(unique_id, max_videos=max_videos, job_state=job_state)
        return data, None
    except Exception as e:
        logger.error(f'[tiktok_collector] erreur @{unique_id}: {e}')
        return None, f'Erreur: {e}'
