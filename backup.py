"""
Sauvegardes SQLite — opt-in, sûres, hors-process-bloquant.

Pourquoi : sur Railway un volume n'est PAS une sauvegarde (même domaine de panne).
Ce module fait un snapshot COHÉRENT de chaque *.db (online backup, OK sous WAL),
le gzip, applique une rotation, et l'envoie en option vers un stockage objet
S3-compatible (Backblaze B2 / Cloudflare R2 / S3) pour une vraie reprise après sinistre.

Activation (sinon NO-OP total) :
  BACKUP_ENABLED=1
  BACKUP_INTERVAL_HOURS=24        (défaut 24)
  BACKUP_KEEP=7                   (snapshots locaux conservés)
  # off-site optionnel :
  BACKUP_S3_BUCKET=...  BACKUP_S3_ENDPOINT=https://...  (R2/B2)
  BACKUP_S3_ACCESS_KEY=...  BACKUP_S3_SECRET_KEY=...  BACKUP_S3_PREFIX=pulse/

CLI : `python -m core.backup`  (snapshot unique, pour cron/manuel/restore-drill).
"""
from __future__ import annotations
import gzip
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STARTED = False


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _iter_dbs(data_dir: Path):
    """Tous les fichiers *.db sous data_dir (hors le dossier backups/)."""
    for p in data_dir.rglob("*.db"):
        if "backups" in p.parts:
            continue
        yield p


def _online_copy(src: Path, dst: Path) -> None:
    """Copie cohérente d'une base SQLite via l'API backup (sûre même en écriture concurrente)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def snapshot(data_dir: str | os.PathLike, keep: int = 7) -> Path | None:
    """Crée un snapshot gzip de toutes les bases. Retourne le dossier créé (ou None)."""
    data_dir = Path(data_dir)
    dbs = list(_iter_dbs(data_dir))
    if not dbs:
        logger.info("[backup] aucune base trouvée sous %s", data_dir)
        return None
    backups_root = data_dir / "backups"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backups_root / ts
    dest.mkdir(parents=True, exist_ok=True)
    for db in dbs:
        rel = db.relative_to(data_dir).as_posix().replace("/", "__")
        tmp = dest / rel
        try:
            _online_copy(db, tmp)
            with open(tmp, "rb") as f_in, gzip.open(f"{tmp}.gz", "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)
            tmp.unlink(missing_ok=True)
        except Exception as e:  # une base qui échoue ne doit pas casser le reste
            logger.warning("[backup] échec %s : %s", db, e)
    # rotation locale
    try:
        snaps = sorted([p for p in backups_root.iterdir() if p.is_dir()])
        for old in snaps[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass
    _maybe_upload_s3(dest)
    logger.info("[backup] snapshot OK : %s (%d bases)", dest, len(dbs))
    return dest


def _maybe_upload_s3(dest: Path) -> None:
    bucket = os.environ.get("BACKUP_S3_BUCKET", "").strip()
    if not bucket:
        return
    try:
        import boto3  # dépendance optionnelle, importée seulement si configurée
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("BACKUP_S3_ENDPOINT") or None,
            aws_access_key_id=os.environ.get("BACKUP_S3_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("BACKUP_S3_SECRET_KEY"),
        )
        prefix = os.environ.get("BACKUP_S3_PREFIX", "").strip().strip("/")
        for f in dest.rglob("*.gz"):
            key = "/".join(filter(None, [prefix, dest.name, f.name]))
            s3.upload_file(str(f), bucket, key)
        logger.info("[backup] upload S3 OK → s3://%s/%s/%s", bucket, prefix, dest.name)
    except Exception as e:
        logger.warning("[backup] upload S3 échoué (snapshot local conservé) : %s", e)


def _loop(data_dir: str, interval_h: float, keep: int) -> None:
    # premier backup ~2 min après le boot, puis toutes les interval_h
    time.sleep(120)
    while True:
        try:
            snapshot(data_dir, keep=keep)
        except Exception as e:
            logger.warning("[backup] boucle : %s", e)
        time.sleep(max(1.0, interval_h) * 3600)


def maybe_start_backups(data_dir: str | os.PathLike) -> bool:
    """Démarre le thread de sauvegarde si BACKUP_ENABLED=1. Idempotent. NO-OP sinon."""
    global _STARTED
    if _STARTED or not _env_bool("BACKUP_ENABLED"):
        return False
    _STARTED = True
    try:
        interval = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
    except Exception:
        interval = 24.0
    try:
        keep = int(os.environ.get("BACKUP_KEEP", "7"))
    except Exception:
        keep = 7
    t = threading.Thread(target=_loop, args=(str(data_dir), interval, keep),
                         daemon=True, name="sqlite-backup")
    t.start()
    logger.info("[backup] activé : toutes les %sh, %d snapshots conservés", interval, keep)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _dd = (os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
           or str(Path(__file__).resolve().parent.parent / "data"))
    out = snapshot(_dd, keep=int(os.environ.get("BACKUP_KEEP", "7")))
    print(f"snapshot → {out}")
