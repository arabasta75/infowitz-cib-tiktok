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

    stats  = raw.get('stats') or raw.get('statistics') or {}
    author = raw.get('author') or {}
    music  = raw.get('music') or {}
    video  = raw.get('video') or {}
    desc   = raw.get('desc') or raw.get('description') or ''
    hashtags = re.findall(r'#([A-Za-zÀ-ÿ0-9_]{2,})', desc)

    create_ts = _int(raw.get('createTime') or raw.get('create_time') or 0)
    created_at = ''
    if create_ts > 0:
        try:
            created_at = datetime.fromtimestamp(create_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            pass

    # Cover / thumbnail
    cover = (video.get('cover') or video.get('originCover')
             or video.get('dynamicCover') or video.get('shareCover', [None])[0] or '')

    return {
        'video_id':          raw.get('id') or raw.get('aweme_id') or '',
        'desc':              desc,
        'created_at':        created_at,
        'create_ts':         create_ts,
        'plays':             _int(stats.get('playCount') or stats.get('play_count') or 0),
        'likes':             _int(stats.get('diggCount') or stats.get('digg_count') or 0),
        'comments':          _int(stats.get('commentCount') or stats.get('comment_count') or 0),
        'shares':            _int(stats.get('shareCount') or stats.get('share_count') or 0),
        'collects':          _int(stats.get('collectCount') or stats.get('collect_count') or 0),
        'duration':          _int(video.get('duration') or 0),
        'cover':             cover,
        'share_url':         raw.get('shareUrl') or raw.get('share_url') or '',
        'hashtags':          hashtags,
        'music_id':          music.get('id') or '',
        'music_title':       music.get('title') or '',
        'music_author':      music.get('authorName') or music.get('author') or '',
        'is_original_sound': bool(music.get('original') or False),
        'author_unique_id':  author.get('uniqueId') or author.get('unique_id') or '',
        'share_url':         raw.get('shareUrl') or raw.get('share_url') or '',
        '_raw':              raw,
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
            if not resp.text or not resp.text.strip():
                raise RuntimeError(f'Réponse vide (HTTP {resp.status_code}) pour {endpoint}')
            data = resp.json()
            if use_cache:
                _cache_set(key, data)
            return data
        except requests.HTTPError as e:
            body = resp.text[:300] if resp.text else '(vide)'
            raise RuntimeError(f'HTTP {resp.status_code}: {body}') from e
        except RuntimeError:
            raise
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


    def _extract_videos_from_response(self, data: dict, hashtag_filter: str = '') -> list[dict]:
        """Extrait et normalise les vidéos depuis n'importe quelle réponse TikFly."""
        raw_items = (
            data.get('itemList')
            or (data.get('data') or {}).get('itemList')
            or data.get('item_list')
            or data.get('items') or []
        )
        ht_lower = hashtag_filter.lower() if hashtag_filter else ''
        videos = []
        for item in raw_items:
            if not item.get('id') and not item.get('aweme_id'):
                continue
            v = normalize_tiktok_video(item)
            # Post-filtre strict : on rejette les vidéos qui ne contiennent pas le hashtag cherché
            if ht_lower and ht_lower not in [h.lower() for h in v['hashtags']]:
                continue
            author_raw   = item.get('author') or {}
            author_stats = item.get('authorStats') or item.get('stats') or {}
            if author_raw:
                v['_author'] = normalize_tiktok_user(author_raw, author_stats)
            videos.append(v)
        return videos

    def _extract_challenge_id(self, hashtag: str, items: list[dict]) -> str | None:
        """Extrait le challengeId depuis le payload des vidéos (textExtra ou challenges)."""
        ht_lower = hashtag.lower()
        for video in items:
            for te in (video.get('textExtra') or video.get('text_extra') or []):
                if ht_lower in (te.get('hashtagName') or '').lower() and te.get('hashtagId'):
                    return str(te['hashtagId'])
            for ch in (video.get('challenges') or []):
                if ht_lower in (ch.get('title') or '').lower() and ch.get('id'):
                    return str(ch['id'])
        return None

    def search_hashtag(self, hashtag: str, max_videos: int = 100) -> list[dict]:
        """
        Vidéos d'un hashtag via TikFly.
        Stratégie 1 : search/video (1 page) → extraire challengeId → challenge/posts (exhaustif)
        Fallback    : search/video paginé si challengeId introuvable
        """
        hashtag = hashtag.lstrip('#').strip()
        videos: list[dict] = []
        last_error: Exception | None = None

        # ── Seed : une page search/video pour récupérer le challengeId ────────
        challenge_id: str | None = None
        seed_items: list[dict] = []
        try:
            data = self._get('/api/search/video', {'keyword': f'#{hashtag}', 'cursor': 0, 'search_id': '0'})
            seed_items = data.get('item_list') or data.get('itemList') or []
            challenge_id = self._extract_challenge_id(hashtag, seed_items)
            logger.info(f'[tikfly] #{hashtag} challengeId={challenge_id}')
        except Exception as e:
            last_error = e
            logger.warning(f'[tikfly] seed search/video failed for #{hashtag}: {e}')

        # ── Stratégie principale : challenge/posts (exhaustif) ────────────────
        if challenge_id:
            try:
                cursor    = '0'
                pages     = 0
                max_pages = max(5, (max_videos // 30) + 2)

                while len(videos) < max_videos and pages < max_pages:
                    params = {'challengeId': challenge_id, 'count': 30, 'cursor': cursor}
                    data   = self._get('/api/challenge/posts', params)
                    # challenge/posts est déjà filtré par TikTok — pas de filtre supplémentaire
                    batch  = self._extract_videos_from_response(data)
                    videos.extend(batch)

                    has_more = data.get('hasMore') or data.get('has_more') or False
                    cursor   = str(data.get('cursor') or '0')
                    pages   += 1
                    if not has_more or not batch:
                        break

                if videos:
                    logger.info(f'[tikfly] challenge/posts #{hashtag} → {len(videos)} vidéos en {pages} pages')
                    return videos[:max_videos]

            except Exception as e:
                last_error = e
                logger.warning(f'[tikfly] challenge/posts failed for #{hashtag}: {e}')

        # ── Fallback : search/video paginé ────────────────────────────────────
        try:
            cursor      = 0
            search_id   = '0'
            pages       = 0
            empty_pages = 0

            if seed_items:
                batch = self._extract_videos_from_response({'item_list': seed_items}, hashtag_filter=hashtag)
                videos.extend(batch)
                pages = 1

            while len(videos) < max_videos and pages < 10:
                params: dict = {'keyword': f'#{hashtag}', 'cursor': cursor, 'search_id': search_id}
                data  = self._get('/api/search/video', params)
                batch = self._extract_videos_from_response(data, hashtag_filter=hashtag)
                videos.extend(batch)

                cursor    = data.get('cursor') or 0
                search_id = (data.get('log_pb') or {}).get('impr_id') or search_id
                has_more  = data.get('has_more') or data.get('hasMore') or False
                pages    += 1

                if not batch:
                    empty_pages += 1
                    if empty_pages >= 3:
                        break
                else:
                    empty_pages = 0

                if not has_more:
                    break

        except Exception as e:
            last_error = e
            logger.warning(f'[tikfly] search/video failed for #{hashtag}: {e}')

        if not videos:
            raise RuntimeError(str(last_error) if last_error else f'Aucune vidéo pour #{hashtag}')

        logger.info(f'[tikfly] search/video fallback #{hashtag} → {len(videos)} vidéos')
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
                avatar_obj = u.get('avatar_thumb') or {}
                avatar_url = (avatar_obj.get('url_list') or [None])[0] or u.get('avatarThumb') or ''
                comments.append({
                    'cid':          c.get('cid') or c.get('id') or '',
                    'text':         c.get('text') or '',
                    'like_count':   int(c.get('digg_count') or 0),
                    'reply_count':  int(c.get('reply_comment_total') or 0),
                    'create_ts':    int(c.get('create_time') or 0),
                    'lang':         c.get('comment_language') or '',
                    'pinned':       bool(c.get('author_pin') or False),
                    'user_id':      u.get('uid') or u.get('id') or '',
                    'unique_id':    u.get('unique_id') or u.get('uniqueId') or '',
                    'nickname':     u.get('nickname') or '',
                    'user_avatar':  avatar_url,
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
