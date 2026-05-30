"""
Gunicorn — Tekkai (TikTok). Pas de SocketIO → config simple.
1 worker par défaut (caches & jobs en mémoire dans le process) ; gthread pour l'I/O
bound (scraping TikTok). _startup_init() de app.py tourne à l'import du worker.
"""
import os

_port = os.environ.get('PORT', '5006')
bind = os.environ.get('GUNICORN_BIND', f'0.0.0.0:{_port}')

worker_class = 'gthread'
workers      = int(os.environ.get('GUNICORN_WORKERS', 1))
threads      = int(os.environ.get('GUNICORN_THREADS', 8))

timeout          = int(os.environ.get('GUNICORN_TIMEOUT', 300))  # scraping TikTok long
graceful_timeout = 30
keepalive        = 5
preload_app = False

_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_log_dir, exist_ok=True)
accesslog = os.path.join(_log_dir, 'access.log')
errorlog  = os.path.join(_log_dir, 'gunicorn.log')
loglevel  = 'info'
