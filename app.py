"""
Tekkai — Détection de comportements inauthentiques sur TikTok
Standalone Flask app — Port 5006
API : TikFly (tiktok-api23.p.rapidapi.com)
"""
from flask import Flask, render_template, jsonify, request, session, Response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import json, os, uuid, secrets, time, re, gzip
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import requests as _requests

import db as _db
import llm as _llm
import tiktok_collector as _tk
import scoring as _scoring
import cib as _cib

import logging
import logging.handlers as _log_handlers

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = (
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.path.join(_BASE_DIR, 'data')
)
_LOG_DIR = os.path.join(_BASE_DIR, 'logs')
os.makedirs(_DATA_DIR, exist_ok=True)
os.makedirs(_LOG_DIR, exist_ok=True)
os.makedirs(os.path.join(_DATA_DIR, 'users'), exist_ok=True)
os.makedirs(os.path.join(_DATA_DIR, 'tk_results'), exist_ok=True)

_fmt  = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_ch   = logging.StreamHandler(); _ch.setFormatter(_fmt); _root.addHandler(_ch)
_fh   = _log_handlers.RotatingFileHandler(
    os.path.join(_LOG_DIR, 'app.log'), maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'
)
_fh.setFormatter(_fmt); _root.addHandler(_fh)
logger = logging.getLogger(__name__)

app = Flask(__name__)
_CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5006,http://127.0.0.1:5006').split(',')
CORS(app, supports_credentials=True, origins=_CORS_ORIGINS)
app.config['JSON_SORT_KEYS'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

_SK_FILE = os.path.join(_DATA_DIR, '.secret_key')
if os.environ.get('SECRET_KEY'):
    app.secret_key = os.environ['SECRET_KEY']
elif os.path.exists(_SK_FILE):
    with open(_SK_FILE) as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(_SK_FILE, 'w') as f:
        f.write(app.secret_key)
    try:
        os.chmod(_SK_FILE, 0o600)
    except Exception:
        pass

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# ─── Jobs async ───────────────────────────────────────────────────────────────

_jobs: dict = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL   = 900
_JOB_MAX   = 200
_executor  = ThreadPoolExecutor(max_workers=4)

def _jobs_gc():
    now = time.time()
    with _JOBS_LOCK:
        to_del = [jid for jid, j in _jobs.items()
                  if j.get('status') in ('done', 'error') and (now - j.get('ts', now)) > _JOB_TTL]
        for jid in to_del:
            _jobs.pop(jid, None)
        if len(_jobs) > _JOB_MAX:
            ordered = sorted(_jobs.items(), key=lambda kv: kv[1].get('ts', 0))
            for jid, _ in ordered[:len(_jobs) - _JOB_MAX]:
                _jobs.pop(jid, None)

# ─── Users / Auth ─────────────────────────────────────────────────────────────

USERS_FILE = os.path.join(_DATA_DIR, 'users.json')

def _load_users() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_users(users: dict):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

def _ensure_admin():
    users = _load_users()
    if not users:
        pwd = os.environ.get('ADMIN_PASSWORD', 'tekkai2024')
        users['admin'] = {
            'password_hash': generate_password_hash(pwd),
            'role': 'admin',
            'config': {},
        }
        _save_users(users)

def _get_user(uid: str) -> dict | None:
    return _load_users().get(uid)

def _save_user_config(uid: str, cfg: dict):
    users = _load_users()
    if uid not in users:
        return
    users[uid]['config'] = cfg
    _save_users(users)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return jsonify({'error': 'auth required'}), 401
        return f(*args, **kwargs)
    return decorated

def get_current_user() -> dict | None:
    uid = session.get('user_id')
    if not uid:
        return None
    u = _get_user(uid)
    if u:
        u['id'] = uid
    return u

def get_cfg() -> dict:
    u = get_current_user()
    base = u.get('config', {}) if u else {}
    # Fallback env vars
    for k, env in [('tikfly_key', 'TIKFLY_KEY'), ('rapidapi_key', 'RAPIDAPI_KEY'),
                   ('groq_key', 'GROQ_API_KEY'), ('openai_key', 'OPENAI_API_KEY'),
                   ('mistral_key', 'MISTRAL_API_KEY'), ('gemini_key', 'GEMINI_API_KEY')]:
        if not base.get(k) and os.environ.get(env):
            base[k] = os.environ[env]
    return base

# ─── Utils ────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'

def _safe_err(e, n=120) -> str:
    return re.sub(r'[A-Za-z0-9_\-]{32,}', '***', str(e)[:n])

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if not session.get('user_id'):
        return render_template('index.html', logged_in=False)
    return render_template('index.html', logged_in=True,
                           user_id=session['user_id'])

@app.route('/api/login', methods=['POST'])
def api_login():
    body = request.get_json(silent=True) or {}
    uid  = (body.get('username') or '').strip().lower()
    pwd  = (body.get('password') or '').strip()
    user = _get_user(uid)
    if not user or not check_password_hash(user.get('password_hash', ''), pwd):
        time.sleep(0.5)
        return jsonify({'error': 'Identifiants invalides'}), 401
    session.permanent = True
    session['user_id'] = uid
    return jsonify({'ok': True, 'user_id': uid, 'role': user.get('role', 'user')})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/me')
def api_me():
    u = get_current_user()
    if not u:
        return jsonify({'logged_in': False})
    cfg = get_cfg()
    return jsonify({
        'logged_in': True,
        'user_id':   u['id'],
        'role':      u.get('role', 'user'),
        'has_tikfly': bool(cfg.get('tikfly_key') or cfg.get('rapidapi_key')),
        'has_llm':    bool(cfg.get('groq_key') or cfg.get('openai_key') or
                          cfg.get('mistral_key') or cfg.get('gemini_key')),
    })

@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def api_config():
    u = get_current_user()
    if request.method == 'GET':
        cfg = dict(u.get('config', {}))
        # Inclure les clés provenant des variables d'environnement
        env_map = [
            ('tikfly_key', 'TIKFLY_KEY'), ('rapidapi_key', 'RAPIDAPI_KEY'),
            ('groq_key', 'GROQ_API_KEY'), ('openai_key', 'OPENAI_API_KEY'),
            ('mistral_key', 'MISTRAL_API_KEY'), ('gemini_key', 'GEMINI_API_KEY'),
        ]
        sources = {}
        for k, env in env_map:
            if not cfg.get(k) and os.environ.get(env):
                cfg[k] = os.environ[env]
                sources[k] = 'env'
            elif cfg.get(k):
                sources[k] = 'user'
        for k in ('tikfly_key', 'rapidapi_key', 'groq_key', 'openai_key', 'mistral_key', 'gemini_key'):
            if cfg.get(k):
                v = str(cfg[k])
                cfg[k] = v[:6] + '***' + v[-4:] if len(v) > 10 else '***'
        return jsonify({'ok': True, 'config': cfg, 'sources': sources})
    body = request.get_json(silent=True) or {}
    allowed = {'tikfly_key', 'rapidapi_key', 'groq_key', 'openai_key',
               'mistral_key', 'gemini_key', 'llm_model', 'max_videos'}
    current = u.get('config', {})
    for k, v in body.items():
        if k in allowed:
            if v == '' or v is None:
                current.pop(k, None)
            elif '***' not in str(v):
                current[k] = v
    _save_user_config(u['id'], current)
    return jsonify({'ok': True})

# ─── Stats ────────────────────────────────────────────────────────────────────

@app.route('/api/stats')
@login_required
def api_stats():
    return jsonify({'ok': True, 'stats': _db.tk_stats()})

# ─── Accounts ─────────────────────────────────────────────────────────────────

@app.route('/api/accounts')
@login_required
def api_accounts():
    flagged_only = request.args.get('flagged') == '1'
    verdict = request.args.get('verdict') or None
    limit   = min(int(request.args.get('limit', 200)), 500)
    offset  = int(request.args.get('offset', 0))
    rows = _db.tk_list(flagged_only=flagged_only, verdict=verdict,
                       limit=limit, offset=offset)
    for r in rows:
        for f in ('patterns', 'report_reasons'):
            if isinstance(r.get(f), str):
                try: r[f] = json.loads(r[f])
                except Exception: r[f] = []
        if isinstance(r.get('context'), str):
            try: r['context'] = json.loads(r['context'])
            except Exception: r['context'] = {}
    total = _db.tk_count(flagged_only=flagged_only)
    return jsonify({'ok': True, 'accounts': rows, 'total': total})

@app.route('/api/account/<unique_id>/flag', methods=['POST'])
@login_required
def api_flag(unique_id):
    body  = request.get_json(silent=True) or {}
    notes = body.get('notes', '')
    flagged = bool(body.get('flagged', True))
    ok = _db.tk_flag(unique_id, flagged, notes or None)
    return jsonify({'ok': ok})

@app.route('/api/account/<unique_id>/score', methods=['POST'])
@login_required
def api_set_score(unique_id):
    body = request.get_json(silent=True) or {}
    score   = float(body.get('score', 50))
    verdict = body.get('verdict', 'unclear')
    notes   = body.get('notes', '')
    ok = _db.tk_set_manual_score(unique_id, score, verdict, notes or None)
    return jsonify({'ok': ok})

@app.route('/api/account/<unique_id>', methods=['DELETE'])
@login_required
def api_delete_account(unique_id):
    ok = _db.tk_delete(unique_id)
    return jsonify({'ok': ok})

# ─── Analyse async ────────────────────────────────────────────────────────────

def _run_analysis(job_id: str, handles: list[str], cfg: dict):
    job = _jobs[job_id]
    try:
        results = []
        manual_overrides = _db.tk_get_manual_overrides()
        collector = _tk.get_collector(cfg)
        if not collector:
            job.update({'status': 'error', 'error': 'Aucune clé TikFly configurée.'})
            return

        max_videos = int(cfg.get('max_videos', 30))

        for i, handle in enumerate(handles):
            handle = handle.lstrip('@').strip()
            if not handle:
                continue
            job['msg'] = f'Analyse @{handle} ({i+1}/{len(handles)})…'
            job['progress'] = int((i / len(handles)) * 100)

            try:
                data, err = _tk.fetch_account(cfg, handle, max_videos=max_videos, job_state=job)
                if err or not data:
                    results.append({'handle': handle, 'error': err or 'Données vides'})
                    continue

                user   = data['user']
                videos = data['videos']

                result = _scoring.score_account(user, videos, manual_overrides=manual_overrides)

                _db.tk_upsert(
                    unique_id    = user['unique_id'],
                    bot_score    = result['bot_score'],
                    verdict      = result['verdict'],
                    display_name = user.get('display_name', ''),
                    avatar       = user.get('avatar', ''),
                    followers    = user.get('followers', 0),
                    following    = user.get('following', 0),
                    hearts       = user.get('hearts', 0),
                    video_count  = user.get('video_count', 0),
                    verified     = user.get('verified', False),
                    region       = user.get('region', ''),
                    patterns     = result['flags'],
                    posts_analyzed = result['posts_analyzed'],
                    context      = {
                        'layers':       result.get('layers', {}),
                        'confidence':   result.get('confidence'),
                        'engagement':   result.get('engagement_rate'),
                        'posting_freq': result.get('posting_freq'),
                        'user':         user,
                    },
                )

                # Sauvegarder les vidéos brutes
                result_file = os.path.join(_DATA_DIR, 'tk_results', f'{user["unique_id"]}.json.gz')
                with gzip.open(result_file, 'wt', encoding='utf-8') as gz:
                    json.dump({'user': user, 'videos': videos, 'result': result,
                               'ts': _utc_now()}, gz)

                results.append({
                    'handle':    user['unique_id'],
                    'bot_score': result['bot_score'],
                    'verdict':   result['verdict'],
                    'followers': user.get('followers', 0),
                    'flags_count': len(result['flags']),
                })

            except Exception as e:
                logger.warning(f'[tekkai] @{handle}: {e}')
                results.append({'handle': handle, 'error': _safe_err(e)})

        _db.sh_insert(
            user_id='admin',
            keyword=', '.join(handles),
            mode='account',
            account_count=len([r for r in results if not r.get('error')]),
        )

        job.update({'status': 'done', 'progress': 100, 'results': results,
                    'msg': f'{len(results)} compte(s) analysé(s)'})

    except Exception as e:
        logger.error(f'[tekkai] job {job_id}: {e}')
        job.update({'status': 'error', 'error': _safe_err(e)})


@app.route('/api/analyze', methods=['POST'])
@login_required
def api_analyze():
    body    = request.get_json(silent=True) or {}
    handles = body.get('handles') or []
    if isinstance(handles, str):
        handles = [h.strip() for h in re.split(r'[\n,;]+', handles) if h.strip()]
    handles = [h.lstrip('@').strip() for h in handles if h.strip()]
    if not handles:
        return jsonify({'error': 'Au moins un @handle requis'}), 400
    if len(handles) > 50:
        return jsonify({'error': 'Max 50 comptes par batch'}), 400

    _jobs_gc()
    job_id = str(uuid.uuid4())
    with _JOBS_LOCK:
        _jobs[job_id] = {
            'id': job_id, 'status': 'running', 'progress': 0,
            'msg': 'Démarrage…', 'ts': time.time(), 'results': [],
        }
    cfg = get_cfg()
    _executor.submit(_run_analysis, job_id, handles, cfg)
    return jsonify({'ok': True, 'job_id': job_id})


@app.route('/api/job/<job_id>')
@login_required
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job introuvable'}), 404
    return jsonify(job)

# ─── LLM deep analysis ────────────────────────────────────────────────────────

@app.route('/api/llm-analyze', methods=['POST'])
@login_required
def api_llm_analyze():
    body      = request.get_json(silent=True) or {}
    unique_id = (body.get('unique_id') or '').strip().lstrip('@')
    if not unique_id:
        return jsonify({'error': 'unique_id requis'}), 400
    cfg = get_cfg()

    # Charger les données stockées
    result_file = os.path.join(_DATA_DIR, 'tk_results', f'{unique_id}.json.gz')
    if not os.path.exists(result_file):
        return jsonify({'error': 'Compte non encore analysé — lancez d\'abord une analyse'}), 404

    with gzip.open(result_file, 'rt', encoding='utf-8') as gz:
        stored = json.load(gz)

    user   = stored.get('user', {})
    videos = stored.get('videos', [])
    result = stored.get('result', {})

    prompt = f"""Analyse le comportement d'un compte TikTok et détermine s'il est authentique, suspect ou inauthentique.

Compte : @{unique_id}
Nom : {user.get('display_name', '—')}
Abonnés : {user.get('followers', 0):,}
Following : {user.get('following', 0):,}
Vidéos : {user.get('video_count', 0):,}
Total likes : {user.get('hearts', 0):,}
Région : {user.get('region', '—')}
Vérifié : {'Oui' if user.get('verified') else 'Non'}
Bio : {user.get('signature', '(vide)')}
Score algo : {result.get('bot_score', '—')}/100 ({result.get('verdict', '—')})
Flags détectés : {', '.join(result.get('flags', [])) or 'Aucun'}

Vidéos analysées ({len(videos)}) — Engagement moyen : {result.get('engagement_rate', 0):.2%}
Fréquence de post : {result.get('posting_freq', 0):.1f} vidéos/jour

Donne un verdict structuré : verdict, niveau de confiance, analyse des signaux, recommandations."""

    try:
        llm_result = _llm.call_llm(prompt, '', cfg.get('llm_model', 'groq:llama-3.3-70b-versatile'), cfg)
        return jsonify({'ok': True, 'analysis': llm_result['result'],
                        'model_used': llm_result.get('model_used', ''),
                        'tokens': llm_result.get('tokens_used', 0)})
    except Exception as e:
        return jsonify({'error': _safe_err(e)}), 500

# ─── Scraping par hashtag ─────────────────────────────────────────────────────

def _run_hashtag_scrape(job_id: str, hashtag: str, max_videos: int, cfg: dict):
    job = _jobs[job_id]
    try:
        collector = _tk.get_collector(cfg)
        if not collector:
            job.update({'status': 'error', 'error': 'Aucune clé TikFly configurée.'})
            return

        job['msg'] = f'Scraping #{hashtag}…'
        try:
            videos = collector.search_hashtag(hashtag, max_videos=max_videos)
        except Exception as e:
            job.update({'status': 'error', 'error': f'Erreur API hashtag: {_safe_err(e, 200)}'})
            return

        if not videos:
            job.update({'status': 'error', 'error': f'Aucune vidéo trouvée pour #{hashtag}'})
            return

        # Extraire les handles uniques — _author en priorité, sinon author_unique_id
        author_ids: set[str] = set()
        for v in videos:
            uid = (v.get('_author') or {}).get('unique_id') or v.get('author_unique_id') or ''
            if uid:
                author_ids.add(uid)

        if not author_ids:
            job.update({'status': 'error',
                        'error': f'{len(videos)} vidéos trouvées mais aucun auteur extractible (données API incomplètes)'})
            return

        job['msg'] = f'{len(author_ids)} comptes trouvés depuis #{hashtag}, analyse en cours…'
        job['progress'] = 20

        results = []
        manual_overrides = _db.tk_get_manual_overrides()
        total = len(author_ids)

        for i, uid in enumerate(author_ids):
            job['msg'] = f'Analyse @{uid} ({i+1}/{total})…'
            job['progress'] = 20 + int((i / max(total, 1)) * 75)

            try:
                data, err = _tk.fetch_account(cfg, uid, max_videos=30, job_state=job)
                if err or not data:
                    results.append({'handle': uid, 'error': err or 'Données vides'})
                    continue

                user_full = data['user']
                vids = data['videos']
                result = _scoring.score_account(user_full, vids, manual_overrides=manual_overrides)

                _db.tk_upsert(
                    unique_id    = user_full['unique_id'],
                    bot_score    = result['bot_score'],
                    verdict      = result['verdict'],
                    display_name = user_full.get('display_name', ''),
                    avatar       = user_full.get('avatar', ''),
                    followers    = user_full.get('followers', 0),
                    following    = user_full.get('following', 0),
                    hearts       = user_full.get('hearts', 0),
                    video_count  = user_full.get('video_count', 0),
                    verified     = user_full.get('verified', False),
                    region       = user_full.get('region', ''),
                    patterns     = result['flags'],
                    posts_analyzed = result['posts_analyzed'],
                    context      = {
                        'layers': result.get('layers', {}),
                        'confidence': result.get('confidence'),
                        'engagement': result.get('engagement_rate'),
                        'posting_freq': result.get('posting_freq'),
                        'user': user_full,
                        'source_hashtag': hashtag,
                    },
                )

                result_file = os.path.join(_DATA_DIR, 'tk_results', f'{user_full["unique_id"]}.json.gz')
                with gzip.open(result_file, 'wt', encoding='utf-8') as gz:
                    json.dump({'user': user_full, 'videos': vids, 'result': result,
                               'ts': _utc_now()}, gz)

                results.append({
                    'handle': user_full['unique_id'],
                    'bot_score': result['bot_score'],
                    'verdict': result['verdict'],
                    'followers': user_full.get('followers', 0),
                    'flags_count': len(result['flags']),
                })

            except Exception as e:
                logger.warning(f'[tekkai/hashtag] @{uid}: {e}')
                results.append({'handle': uid, 'error': _safe_err(e)})

        _db.sh_insert(
            user_id='admin',
            keyword=f'#{hashtag}',
            mode='hashtag',
            account_count=len([r for r in results if not r.get('error')]),
        )

        job.update({'status': 'done', 'progress': 100, 'results': results,
                    'msg': f'{len(results)} compte(s) scrapé(s) depuis #{hashtag}'})

    except Exception as e:
        logger.error(f'[tekkai/hashtag] job {job_id}: {e}')
        job.update({'status': 'error', 'error': _safe_err(e)})


@app.route('/api/scrape/hashtag', methods=['POST'])
@login_required
def api_scrape_hashtag():
    body     = request.get_json(silent=True) or {}
    hashtag  = (body.get('hashtag') or '').lstrip('#').strip()
    max_vids = min(int(body.get('max_videos', 50)), 200)
    if not hashtag:
        return jsonify({'error': 'hashtag requis'}), 400

    _jobs_gc()
    job_id = str(uuid.uuid4())
    with _JOBS_LOCK:
        _jobs[job_id] = {
            'id': job_id, 'status': 'running', 'progress': 0,
            'msg': f'Scraping #{hashtag}…', 'ts': time.time(), 'results': [],
        }
    cfg = get_cfg()
    _executor.submit(_run_hashtag_scrape, job_id, hashtag, max_vids, cfg)
    return jsonify({'ok': True, 'job_id': job_id})


# ─── CIB ──────────────────────────────────────────────────────────────────────

_CIB_RESULT_FILE = os.path.join(_DATA_DIR if os.path.isabs(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
) else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'), 'cib_last.json')


def _cib_result_path() -> str:
    return os.path.join(_DATA_DIR, 'cib_last.json')


@app.route('/api/cib/run', methods=['POST'])
@login_required
def api_cib_run():
    rows = _db.tk_list(limit=500, offset=0)
    unique_ids = [r['unique_id'] for r in rows if r.get('unique_id')]
    if len(unique_ids) < 2:
        return jsonify({'error': 'Au moins 2 comptes analysés nécessaires'}), 400

    result = _cib.run_cib_analysis(_DATA_DIR, unique_ids)
    result['ts'] = _utc_now()

    try:
        with open(_cib_result_path(), 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f'[cib] save result: {e}')

    return jsonify({'ok': True, **result})


@app.route('/api/cib/result')
@login_required
def api_cib_result():
    path = _cib_result_path()
    if not os.path.exists(path):
        return jsonify({'ok': False, 'error': 'Aucune analyse CIB lancée'})
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify({'ok': True, **data})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── History ──────────────────────────────────────────────────────────────────

@app.route('/api/history')
@login_required
def api_history():
    u = get_current_user()
    return jsonify({'ok': True, 'history': _db.sh_list(u['id'])})

# ─── Health ───────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'ok': True, 'service': 'tekkai', 'ts': _utc_now()})

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    _ensure_admin()
    _db.init_db()
    port = int(os.environ.get('PORT', 5006))
    logger.info(f'Tekkai démarré sur port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
