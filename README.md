# Infowitz · CIB TikTok

Détection de **comportements inauthentiques coordonnés (CIB)** sur TikTok.

Module de la suite **[Infowitz](https://github.com/arabasta75?tab=repositories&q=infowitz)** — plateforme OSINT/CIB souveraine. Base visuelle « Tactical Void » avec accent plateforme TikTok (rose `#fe2c55`).

## Capacités
- Scoring d'inauthenticité par compte (heures de publication, bursts, coordination).
- Analyse de hashtags et de vidéos.
- Funnel demo public (lead-gate + quota) pour la prospection.

## Stack
- Python · Flask · gunicorn
- Déploiement : Railway (`Procfile` + `railway.toml`)

## Lancement local
```bash
pip install -r requirements.txt
python app.py
```

---
*Infowitz · La Warroom — audit informationnel & guerre cognitive.*
