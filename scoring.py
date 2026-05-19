"""
scoring.py — Moteur de scoring inauthenticité TikTok pour Tekkai

Score 0-100 : 100 = compte authentique, 0 = bot certain
4 couches d'analyse :
  surface      (25 pts) — signaux visuels / username
  stats        (35 pts) — anomalies follower/following/engagement
  content      (25 pts) — qualité et régularité du contenu
  network      (15 pts) — patterns CIB et comportement coordonné
"""

from __future__ import annotations
import math
import re
from datetime import datetime, timezone
from collections import Counter
from typing import Any


# ─── Patterns suspects ────────────────────────────────────────────────────────

_RANDOM_USERNAME_RE = re.compile(r'^[a-z0-9]{8,}$')
_DIGITS_HEAVY_RE    = re.compile(r'\d{4,}')
_BOT_KEYWORDS_RE    = re.compile(r'\b(bot|auto|spam|fake|clone|follower|follow4|f4f)\b', re.I)

_HIGH_BOT_REGIONS   = {'KP', 'IR', 'RU', 'BY', 'VE', 'NG'}

LAYER_WEIGHTS = {
    'surface': 25,
    'stats':   35,
    'content': 25,
    'network': 15,
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_ratio(a: float, b: float, default: float = 0.0) -> float:
    try:
        if b == 0:
            return default
        return a / b
    except Exception:
        return default


def _engagement_rate(videos: list) -> float:
    """Taux d'engagement moyen : (likes + comments + shares) / plays."""
    if not videos:
        return 0.0
    rates = []
    for v in videos:
        plays = v.get('plays', 0) or 0
        if plays > 0:
            interactions = (v.get('likes', 0) + v.get('comments', 0) + v.get('shares', 0))
            rates.append(interactions / plays)
    if not rates:
        return 0.0
    return sum(rates) / len(rates)


def _posting_frequency_per_day(videos: list) -> float:
    """Fréquence de publication moyenne en vidéos/jour."""
    if len(videos) < 2:
        return 0.0
    ts_list = sorted(
        [v['create_ts'] for v in videos if v.get('create_ts', 0) > 0]
    )
    if len(ts_list) < 2:
        return 0.0
    span_days = (ts_list[-1] - ts_list[0]) / 86400
    if span_days < 1:
        return len(ts_list)
    return len(ts_list) / span_days


def _hashtag_diversity(videos: list) -> float:
    """0 = toujours les mêmes hashtags (bot), 1 = grande diversité."""
    if not videos:
        return 1.0
    all_tags = [t for v in videos for t in v.get('hashtags', [])]
    if not all_tags:
        return 1.0
    total = len(all_tags)
    unique = len(set(all_tags))
    return unique / total


# ─── Couches de scoring ───────────────────────────────────────────────────────

def _score_surface(user: dict) -> tuple[float, list[str]]:
    """
    Surface (25 pts max) : username, bio, avatar, vérification.
    Score = points gagnés (partant de 0, max 25).
    """
    score = 25.0
    flags = []

    unique_id = (user.get('unique_id') or '').lower()
    nickname  = (user.get('display_name') or '').strip()
    signature = (user.get('signature') or '').strip()
    avatar    = (user.get('avatar') or '').strip()
    verified  = bool(user.get('verified'))

    # Vérification officielle
    if verified:
        score = min(25.0, score + 5)
        flags.append('✅ Compte vérifié TikTok')

    # Pas d'avatar
    if not avatar or 'default' in avatar.lower():
        score -= 8
        flags.append('Pas de photo de profil')

    # Pas de bio
    if not signature:
        score -= 5
        flags.append('Bio vide')
    elif len(signature) < 10:
        score -= 2

    # Username suspect : chiffres lourds
    if _DIGITS_HEAVY_RE.search(unique_id):
        score -= 8
        flags.append(f'Username contient une séquence numérique longue (@{unique_id})')

    # Username aléatoire (lettres+chiffres mélangés, 10+ chars sans sens)
    if len(unique_id) >= 12 and _RANDOM_USERNAME_RE.match(unique_id):
        score -= 6
        flags.append('Username généré automatiquement (pattern aléatoire)')

    # Mots-clés bot dans username ou bio
    if _BOT_KEYWORDS_RE.search(unique_id) or _BOT_KEYWORDS_RE.search(signature):
        score -= 12
        flags.append('Mot-clé suspect dans username ou bio (bot/fake/follow4follow…)')

    # Nickname = unique_id (pas de nom personnalisé)
    if nickname.lower() == unique_id.lower():
        score -= 3
        flags.append('Nom affiché identique au username (non personnalisé)')

    # Compte privé
    if user.get('private'):
        score -= 3
        flags.append('Compte privé — analyse incomplète')

    return max(0.0, min(25.0, score)), flags


def _score_stats(user: dict) -> tuple[float, list[str]]:
    """
    Stats (35 pts max) : follower/following ratio, achats de likes, anomalies.
    """
    score = 35.0
    flags = []

    followers   = int(user.get('followers', 0) or 0)
    following   = int(user.get('following', 0) or 0)
    hearts      = int(user.get('hearts', 0) or 0)
    video_count = int(user.get('video_count', 0) or 0)
    region      = (user.get('region') or '').upper()

    # ── Ratio following/followers ──────────────────────────────────────────────
    if followers > 0:
        ff_ratio = _safe_ratio(following, followers)
        if ff_ratio > 10:
            score -= 20
            flags.append(f'Ratio following/followers très élevé ({ff_ratio:.0f}x) — pattern mass-follow')
        elif ff_ratio > 3:
            score -= 10
            flags.append(f'Ratio following/followers anormal ({ff_ratio:.1f}x)')
        elif ff_ratio > 1.5:
            score -= 5
            flags.append(f'Following > Followers ({following:,} vs {followers:,})')
    elif followers == 0 and following > 100:
        score -= 15
        flags.append(f'Aucun abonné mais suit {following:,} comptes')

    # ── Mass following absolu ─────────────────────────────────────────────────
    if following > 10_000:
        score -= 10
        flags.append(f'Following très élevé ({following:,}) — comportement mass-follow')
    elif following > 5_000:
        score -= 5
        flags.append(f'Following élevé ({following:,})')

    # ── Likes achetés ─────────────────────────────────────────────────────────
    if video_count > 0 and followers > 0:
        avg_hearts_per_follower = _safe_ratio(hearts, followers)
        if avg_hearts_per_follower > 100:
            score -= 12
            flags.append(f'Total likes ({hearts:,}) disproportionné vs abonnés ({followers:,}) — likes potentiellement achetés')
        elif avg_hearts_per_follower > 30:
            score -= 5
            flags.append(f'Ratio likes/abonnés élevé ({avg_hearts_per_follower:.0f}x)')

    # ── Compte sans vidéos mais avec followers ─────────────────────────────────
    if video_count == 0 and followers > 1000:
        score -= 8
        flags.append(f'Aucune vidéo mais {followers:,} abonnés — followers achetés probable')

    # ── Région à risque ───────────────────────────────────────────────────────
    if region in _HIGH_BOT_REGIONS:
        score -= 5
        flags.append(f'Région associée à activité inauthentique ({region})')

    return max(0.0, min(35.0, score)), flags


def _score_content(user: dict, videos: list) -> tuple[float, list[str]]:
    """
    Content (25 pts max) : engagement, fréquence de posting, diversité.
    """
    score = 25.0
    flags = []

    if not videos:
        # Pas de vidéos analysées
        if not user.get('private'):
            score -= 10
            flags.append('Aucune vidéo récente accessible')
        return max(0.0, score), flags

    # ── Taux d'engagement ─────────────────────────────────────────────────────
    eng = _engagement_rate(videos)
    if eng == 0.0:
        score -= 12
        flags.append('Engagement nul (0 vues comptabilisées) — vues peut-être gonflées')
    elif eng < 0.001:
        score -= 8
        flags.append(f'Taux d\'engagement très faible ({eng*100:.3f}%)')
    elif eng > 0.3:
        # Engagement anormalement haut peut indiquer boosting
        score -= 3
        flags.append(f'Taux d\'engagement très élevé ({eng*100:.1f}%) — possible boosting')

    # ── Fréquence de posting ──────────────────────────────────────────────────
    freq = _posting_frequency_per_day(videos)
    if freq > 20:
        score -= 15
        flags.append(f'Fréquence de publication extrême ({freq:.0f} vidéos/jour) — automatisation probable')
    elif freq > 10:
        score -= 8
        flags.append(f'Fréquence de publication très élevée ({freq:.0f} vidéos/jour)')
    elif freq > 5:
        score -= 3
        flags.append(f'Fréquence de publication élevée ({freq:.1f} vidéos/jour)')

    # ── Diversité des hashtags ─────────────────────────────────────────────────
    diversity = _hashtag_diversity(videos)
    if diversity < 0.15:
        score -= 8
        flags.append(f'Hashtags quasi-identiques sur toutes les vidéos (diversité: {diversity:.0%})')
    elif diversity < 0.30:
        score -= 3
        flags.append(f'Faible diversité de hashtags ({diversity:.0%})')

    # ── Musiques identiques ────────────────────────────────────────────────────
    music_ids = [v.get('music_id') for v in videos if v.get('music_id')]
    if music_ids:
        top_music, top_count = Counter(music_ids).most_common(1)[0]
        music_ratio = top_count / len(music_ids)
        if music_ratio > 0.8 and len(music_ids) >= 5:
            score -= 5
            flags.append(f'Même musique utilisée dans {music_ratio:.0%} des vidéos')

    # ── Durées identiques ─────────────────────────────────────────────────────
    durations = [v.get('duration', 0) for v in videos if v.get('duration', 0) > 0]
    if len(durations) >= 5:
        most_common_dur, dur_count = Counter(durations).most_common(1)[0]
        if dur_count / len(durations) > 0.9:
            score -= 4
            flags.append(f'Toutes les vidéos ont la même durée ({most_common_dur}s) — template automation')

    return max(0.0, min(25.0, score)), flags


def _score_network(user: dict, videos: list, peer_data: list = None) -> tuple[float, list[str]]:
    """
    Network (15 pts max) : CIB, coordination, patterns suspects inter-comptes.
    peer_data : liste d'autres comptes du même batch (pour détection CIB future).
    """
    score = 15.0
    flags = []

    # ── Patterns de hashtags narratifs connus ─────────────────────────────────
    all_hashtags = [t.lower() for v in videos for t in v.get('hashtags', [])]
    disinfo_tags = {
        'deepstate', 'globalistvaccine', 'plandemic', 'greatreset',
        'nwo', 'qanon', 'wwg1wga', 'electionfraud', 'stolenelection',
        'pizzagate', 'soros', 'bilderberg', 'chemtrails',
    }
    found_disinfo = set(all_hashtags) & disinfo_tags
    if found_disinfo:
        score -= 10
        flags.append(f'Hashtags narratifs suspects : {", ".join(f"#{t}" for t in found_disinfo)}')

    # ── Descriptions identiques sur plusieurs vidéos ──────────────────────────
    descs = [v.get('desc', '').strip() for v in videos if v.get('desc')]
    if len(descs) >= 3:
        dup_descs = [d for d, c in Counter(descs).items() if c > 1 and len(d) > 10]
        if dup_descs:
            score -= 8
            flags.append(f'{len(dup_descs)} descriptions identiques sur plusieurs vidéos')

    # ── Peer CIB (si données batch disponibles) ────────────────────────────────
    if peer_data:
        same_hashtags = 0
        my_tags = set(all_hashtags)
        for peer in peer_data:
            peer_tags = set(t.lower() for v in peer.get('videos', []) for t in v.get('hashtags', []))
            overlap = len(my_tags & peer_tags) / max(len(my_tags), 1)
            if overlap > 0.7:
                same_hashtags += 1
        if same_hashtags >= 3:
            score -= 10
            flags.append(f'CIB : {same_hashtags} comptes partagent +70% des mêmes hashtags')

    return max(0.0, min(15.0, score)), flags


# ─── Score global ─────────────────────────────────────────────────────────────

def score_account(user: dict, videos: list, peer_data: list = None,
                  manual_overrides: dict = None) -> dict:
    """
    Calcule le score d'authenticité d'un compte TikTok.
    Retourne un dict complet avec score, verdict, flags, couches.
    """
    unique_id = (user.get('unique_id') or '').lower()

    # Override manuel
    if manual_overrides:
        ov = manual_overrides.get(unique_id)
        if ov:
            return {
                'bot_score':    ov['bot_score'],
                'verdict':      ov['verdict'],
                'flags':        ['⚙️ Score corrigé manuellement'],
                'layers':       {},
                'confidence':   'manual',
                'posts_analyzed': len(videos),
            }

    s_surface,  f_surface  = _score_surface(user)
    s_stats,    f_stats    = _score_stats(user)
    s_content,  f_content  = _score_content(user, videos)
    s_network,  f_network  = _score_network(user, videos, peer_data)

    raw_score = s_surface + s_stats + s_content + s_network  # max = 100
    bot_score = round(max(0.0, min(100.0, raw_score)), 1)

    if bot_score >= 70:
        verdict = 'human'
    elif bot_score >= 40:
        verdict = 'unclear'
    else:
        verdict = 'bot'

    all_flags = f_surface + f_stats + f_content + f_network

    # Couverture : combien de signaux disponibles
    signals_available = sum([
        1 if user.get('avatar') else 0,
        1 if user.get('signature') else 0,
        1 if user.get('followers', 0) > 0 else 0,
        1 if len(videos) > 0 else 0,
    ])
    confidence = 'high' if signals_available >= 3 else ('medium' if signals_available >= 2 else 'low')

    return {
        'bot_score':    bot_score,
        'verdict':      verdict,
        'flags':        all_flags,
        'layers': {
            'surface':  {'score': s_surface,  'max': 25, 'flags': f_surface},
            'stats':    {'score': s_stats,    'max': 35, 'flags': f_stats},
            'content':  {'score': s_content,  'max': 25, 'flags': f_content},
            'network':  {'score': s_network,  'max': 15, 'flags': f_network},
        },
        'confidence':     confidence,
        'posts_analyzed': len(videos),
        'engagement_rate': _engagement_rate(videos),
        'posting_freq':   _posting_frequency_per_day(videos),
    }
