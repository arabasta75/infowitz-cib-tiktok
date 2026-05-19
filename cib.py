"""
cib.py — Détection de comportements coordonnés inauthentiques (CIB) sur TikTok

5 signaux analysés :
  hashtag_overlap   — mêmes hashtags entre comptes (Jaccard ≥ 0.5)
  temporal_burst    — synchronisation des créneaux horaires de publication
  same_sound        — même musique dominante
  desc_similarity   — descriptions identiques entre vidéos
  creation_cluster  — comptes créés à moins de 30 jours d'intervalle

Score CIB 0-100 par compte + clustering union-find.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SIGNAL_WEIGHTS = {
    'hashtag_overlap':  30,
    'temporal_burst':   25,
    'same_sound':       20,
    'desc_similarity':  15,
    'creation_cluster': 10,
}

CIB_THRESHOLD = 25.0   # score pairwise minimum pour lier deux comptes


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def _load_account_data(data_dir: str, unique_ids: list[str]) -> dict[str, dict]:
    accounts: dict[str, dict] = {}
    results_dir = os.path.join(data_dir, 'tk_results')
    for uid in unique_ids:
        path = os.path.join(results_dir, f'{uid}.json.gz')
        if os.path.exists(path):
            try:
                with gzip.open(path, 'rt', encoding='utf-8') as f:
                    accounts[uid] = json.load(f)
            except Exception as e:
                logger.warning(f'[cib] load {uid}: {e}')
    return accounts


# ─── Extraction des signaux ────────────────────────────────────────────────────

def _extract_signals(account_data: dict) -> dict:
    user   = account_data.get('user', {})
    videos = account_data.get('videos', [])

    hashtags:   set[str] = set()
    music_ids:  list[str] = []
    time_slots: set[int] = set()
    descs:      set[str] = set()

    for v in videos:
        for t in v.get('hashtags', []):
            hashtags.add(t.lower())
        if v.get('music_id'):
            music_ids.append(v['music_id'])
        ts = v.get('create_ts') or 0
        if ts > 0:
            dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
            slot = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
            time_slots.add(slot)
        d = (v.get('desc') or '').strip()
        if len(d) > 10:
            descs.add(d[:100])

    top_music = Counter(music_ids).most_common(1)[0][0] if music_ids else None

    raw_user  = user.get('_raw_user') or {}
    create_ts = int(raw_user.get('createTime') or raw_user.get('createtime') or 0)

    return {
        'unique_id':   user.get('unique_id', ''),
        'hashtags':    hashtags,
        'top_music':   top_music,
        'music_ids':   music_ids,
        'time_slots':  time_slots,
        'descs':       descs,
        'create_ts':   create_ts,
        'video_count': len(videos),
    }


# ─── Similarité pairwise ──────────────────────────────────────────────────────

def _pairwise(a: dict, b: dict) -> dict:
    score   = 0.0
    reasons: list[str] = []

    ht = _jaccard(a['hashtags'], b['hashtags'])
    if ht >= 0.5:
        score += SIGNAL_WEIGHTS['hashtag_overlap'] * ht
        reasons.append(f'hashtags partagés {ht:.0%}')

    if a['top_music'] and b['top_music'] and a['top_music'] == b['top_music']:
        score += SIGNAL_WEIGHTS['same_sound']
        reasons.append(f'même son ({a["top_music"][:14]}…)')

    ts = _jaccard(a['time_slots'], b['time_slots'])
    if ts >= 0.5:
        score += SIGNAL_WEIGHTS['temporal_burst'] * ts
        reasons.append(f'synchro temporelle {ts:.0%}')

    common = a['descs'] & b['descs']
    if common:
        pts = SIGNAL_WEIGHTS['desc_similarity'] * min(len(common), 3) / 3
        score += pts
        reasons.append(f'{len(common)} description(s) identique(s)')

    if a['create_ts'] > 0 and b['create_ts'] > 0:
        delta = abs(a['create_ts'] - b['create_ts']) / 86400
        if delta < 30:
            score += SIGNAL_WEIGHTS['creation_cluster'] * (1 - delta / 30)
            reasons.append(f'créés à {delta:.0f}j d\'écart')

    return {'score': round(score, 1), 'reasons': reasons}


# ─── Clustering union-find ────────────────────────────────────────────────────

def _cluster(ids: list[str], pairs: dict[tuple, dict]) -> list[list[str]]:
    parent = {uid: uid for uid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str):
        parent[find(x)] = find(y)

    for (a, b), info in pairs.items():
        if info['score'] >= CIB_THRESHOLD:
            union(a, b)

    groups: dict[str, list[str]] = defaultdict(list)
    for uid in ids:
        groups[find(uid)].append(uid)

    return [g for g in groups.values() if len(g) >= 2]


# ─── Analyse CIB principale ───────────────────────────────────────────────────

def run_cib_analysis(data_dir: str, unique_ids: list[str]) -> dict:
    """
    Analyse CIB sur un ensemble de comptes.
    Retourne : clusters, scores par compte, stats globales.
    """
    empty = {
        'clusters': [], 'accounts': {},
        'stats': {'total': len(unique_ids), 'analyzed': 0, 'clusters': 0, 'coordinated': 0},
    }
    if len(unique_ids) < 2:
        return empty

    raw       = _load_account_data(data_dir, unique_ids)
    available = list(raw.keys())
    if len(available) < 2:
        empty['stats']['analyzed'] = len(available)
        return empty

    signals = {uid: _extract_signals(raw[uid]) for uid in available}
    ids     = list(signals.keys())

    # Matrice pairwise
    pairs: dict[tuple, dict] = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            key = (ids[i], ids[j])
            pairs[key] = _pairwise(signals[ids[i]], signals[ids[j]])

    clusters   = _cluster(available, pairs)
    cluster_map: dict[str, int] = {}
    for idx, cl in enumerate(clusters):
        for uid in cl:
            cluster_map[uid] = idx

    # Score CIB par compte
    account_cib: dict[str, dict] = {}
    for uid in available:
        cidx = cluster_map.get(uid)
        if cidx is None:
            account_cib[uid] = {
                'cib_score': 0, 'partners': 0, 'signals': [],
                'cluster_id': None, 'cluster_size': 1,
            }
            continue

        cl    = clusters[cidx]
        total = 0.0
        reasons: list[str] = []
        n_partners = 0

        for other in cl:
            if other == uid:
                continue
            key  = (min(uid, other), max(uid, other))
            info = pairs.get(key, {})
            if info.get('score', 0) >= CIB_THRESHOLD:
                total += info['score']
                reasons.extend(info.get('reasons', []))
                n_partners += 1

        account_cib[uid] = {
            'cib_score':    min(100, round(total / max(n_partners, 1), 1)),
            'partners':     n_partners,
            'signals':      list(set(reasons)),
            'cluster_id':   cidx,
            'cluster_size': len(cl),
        }

    # Résumés clusters (triés par score décroissant)
    summaries: list[dict] = []
    for idx, cl in enumerate(clusters):
        all_sig: list[str] = []
        max_score = 0.0
        for uid in cl:
            info = account_cib.get(uid, {})
            all_sig.extend(info.get('signals', []))
            max_score = max(max_score, info.get('cib_score', 0))
        summaries.append({
            'cluster_id': idx,
            'accounts':   cl,
            'size':       len(cl),
            'cib_score':  max_score,
            'signals':    list(set(all_sig)),
        })

    summaries.sort(key=lambda c: c['cib_score'], reverse=True)

    # Renuméroter après tri
    for new_idx, cs in enumerate(summaries):
        old_idx = cs['cluster_id']
        cs['cluster_id'] = new_idx
        for uid in cs['accounts']:
            if account_cib.get(uid, {}).get('cluster_id') == old_idx:
                account_cib[uid]['cluster_id'] = new_idx

    return {
        'clusters': summaries,
        'accounts': account_cib,
        'stats': {
            'total':       len(unique_ids),
            'analyzed':    len(available),
            'clusters':    len(clusters),
            'coordinated': sum(c['size'] for c in summaries),
        },
    }
