"""
Tekkai — Générateur de rapport client (CIB TikTok).

Produit un document HTML autonome, calibré pour l'impression (Cmd/Ctrl-P → PDF),
à partir du résultat d'une analyse CIB TikTok (run_cib_analysis). Aucune dépendance
externe : pas de CDN, pas de capture d'écran, CSS d'impression propre et paginé.

Point d'entrée : build_html_report(result: dict, meta: dict|None) -> str
Toutes les sections sont défensives : rendues uniquement si les données existent.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

ACCENT = "#ff0050"   # rose TikTok
INK = "#0f172a"
MUTED = "#64748b"

# Libellés lisibles des signaux de coordination (clés issues de cib.py)
SIGNAL_LABELS = {
    "hashtag_overlap": "Hashtags communs",
    "temporal_burst": "Pics temporels synchronisés",
    "same_sound": "Même son / musique",
    "desc_similarity": "Descriptions similaires",
    "creation_cluster": "Comptes créés en grappe",
}


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f == int(f):
        return f"{int(f):,}".replace(",", " ")
    return f"{f:,.1f}".replace(",", " ")


def _pct(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if 0 <= f <= 1:
        f *= 100
    return f"{f:.0f} %"


def _get(d: Any, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def _sig_label(s: Any) -> str:
    return SIGNAL_LABELS.get(str(s), str(s).replace("_", " ").capitalize())


def _risk(coordinated: int, analyzed: int, max_cib: float) -> tuple[str, str]:
    rate = (coordinated / analyzed * 100) if analyzed else 0
    score = max(rate, max_cib or 0)
    if score >= 60:
        return "Élevé", "#dc2626"
    if score >= 30:
        return "Modéré", "#d97706"
    if score > 0:
        return "Faible", "#16a34a"
    return "Non déterminé", MUTED


def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{_esc(sub)}</div>' if sub else ""
    return (f'<div class="kpi"><div class="kpi-val">{value}</div>'
            f'<div class="kpi-lbl">{_esc(label)}</div>{sub_html}</div>')


def _chips(items, fmt) -> str:
    return '<div class="chips">' + "".join(f'<span class="chip">{fmt(x)}</span>' for x in items) + "</div>"


def _clusters_table(result: dict) -> str:
    clusters = _get(result, "clusters", default=[]) or []
    if not isinstance(clusters, list) or not clusters:
        return '<p class="empty">Aucun cluster coordonné détecté.</p>'
    clusters = sorted(clusters, key=lambda c: -(c.get("cib_score") or 0))
    trs = []
    for i, c in enumerate(clusters[:30], 1):
        accts = c.get("accounts") or []
        accts_html = ", ".join(f"@{_esc(a)}" for a in accts[:8]) + ("…" if len(accts) > 8 else "")
        sigs = _chips(c.get("signals") or [], lambda s: _esc(_sig_label(s)))
        score = c.get("cib_score")
        sc_color = "#dc2626" if (isinstance(score, (int, float)) and score >= 60) else (
            "#d97706" if (isinstance(score, (int, float)) and score >= 30) else INK)
        trs.append(
            f"<tr><td class='r'>{i}</td><td class='r'>{_num(c.get('size'))}</td>"
            f"<td class='r' style='color:{sc_color};font-weight:700'>{_num(score)}</td>"
            f"<td><b>{accts_html}</b></td><td>{sigs}</td></tr>"
        )
    return ('<table class="tbl"><thead><tr><th class="r">#</th><th class="r">Taille</th>'
            '<th class="r">Score CIB</th><th>Comptes</th><th>Signaux</th></tr></thead><tbody>'
            + "".join(trs) + "</tbody></table>")


def _accounts_table(result: dict) -> str:
    accounts = _get(result, "accounts", default={}) or {}
    if not isinstance(accounts, dict) or not accounts:
        return ""
    rows = [(uid, a) for uid, a in accounts.items()
            if isinstance(a, dict) and (a.get("cib_score") or 0) > 0]
    if not rows:
        return ""
    rows.sort(key=lambda kv: -(kv[1].get("cib_score") or 0))
    trs = []
    for i, (uid, a) in enumerate(rows[:50], 1):
        score = a.get("cib_score")
        sc_color = "#dc2626" if (isinstance(score, (int, float)) and score >= 60) else (
            "#d97706" if (isinstance(score, (int, float)) and score >= 30) else INK)
        sigs = _chips(a.get("signals") or [], lambda s: _esc(_sig_label(s)))
        trs.append(
            f"<tr><td class='r'>{i}</td><td><b>@{_esc(uid)}</b></td>"
            f"<td class='r' style='color:{sc_color};font-weight:700'>{_num(score)}</td>"
            f"<td class='r'>{_num(a.get('partners'))}</td>"
            f"<td class='r'>{_esc(a.get('cluster_id')) if a.get('cluster_id') is not None else '—'}</td>"
            f"<td>{sigs}</td></tr>"
        )
    note = '<p class="note">50 comptes affichés (triés par score CIB décroissant).</p>' if len(rows) > 50 else ""
    return ('<table class="tbl"><thead><tr><th class="r">#</th><th>Compte</th>'
            '<th class="r">Score CIB</th><th class="r">Partenaires</th>'
            '<th class="r">Cluster</th><th>Signaux</th></tr></thead><tbody>'
            + "".join(trs) + "</tbody></table>" + note)


def build_html_report(result: dict, meta: dict | None = None) -> str:
    result = result if isinstance(result, dict) else {}
    meta = meta or {}
    stats = _get(result, "stats", default={}) or {}

    total = _get(stats, "total", default=0)
    analyzed = _get(stats, "analyzed", default=0)
    n_clusters = _get(stats, "clusters", default=len(_get(result, "clusters", default=[]) or []))
    coordinated = _get(stats, "coordinated", default=0)

    clusters = _get(result, "clusters", default=[]) or []
    max_cib = max([c.get("cib_score") or 0 for c in clusters], default=0) if isinstance(clusters, list) else 0
    label, color = _risk(coordinated, analyzed, max_cib)

    company = _esc(meta.get("company", ""))
    analyst = _esc(meta.get("analyst", ""))
    now = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")
    ts = _get(result, "ts", default="")

    kpis = "".join([
        _kpi("Comptes suivis", _num(total)),
        _kpi("Comptes analysés", _num(analyzed)),
        _kpi("Clusters coordonnés", _num(n_clusters)),
        _kpi("Comptes coordonnés", _num(coordinated),
             _pct(coordinated / analyzed) + " des analysés" if analyzed else ""),
    ])

    meta_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>" for k, v in [
            ("Périmètre", f"<b>{_num(analyzed)} comptes TikTok analysés</b>"),
            ("Clusters détectés", _num(n_clusters)),
            ("Score CIB max", _num(max_cib) + " / 100"),
            ("Date de l'analyse", _esc(ts) or "—"),
            ("Date du rapport", _esc(now)),
        ]
    )

    verdict = (
        f'<div class="verdict" style="border-left:6px solid {color}">'
        f'<div class="verdict-row"><span class="verdict-lbl">Niveau de coordination détecté</span>'
        f'<span class="verdict-tag" style="background:{color}">{_esc(label)}</span></div>'
        f'<div class="verdict-sub">{_num(coordinated)} comptes répartis dans '
        f'{_num(n_clusters)} cluster(s) présentent des signaux de comportement coordonné.</div></div>'
    )

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rapport Tekkai — CIB TikTok</title>
<style>
  :root {{ --accent:{ACCENT}; --ink:{INK}; --muted:{MUTED}; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         color:var(--ink); margin:0; background:#f1f5f9; line-height:1.5; }}
  .page {{ background:#fff; max-width:880px; margin:24px auto; padding:48px 56px;
          box-shadow:0 1px 8px rgba(0,0,0,.08); }}
  .toolbar {{ position:sticky; top:0; text-align:center; padding:10px; background:#0f172a; }}
  .toolbar button {{ background:var(--accent); color:#fff; border:0; padding:9px 22px;
          border-radius:7px; font-size:14px; font-weight:700; cursor:pointer; }}
  .cover {{ border-bottom:3px solid var(--accent); padding-bottom:24px; margin-bottom:8px; }}
  .brand {{ font-size:13px; font-weight:800; letter-spacing:2px; color:var(--accent);
          text-transform:uppercase; }}
  h1 {{ font-size:30px; margin:6px 0 4px; letter-spacing:-.5px; }}
  .cover-sub {{ color:var(--muted); font-size:14px; }}
  .meta-tbl {{ width:100%; border-collapse:collapse; margin:18px 0 4px; font-size:13px; }}
  .meta-tbl td {{ padding:7px 10px; border-bottom:1px solid #e2e8f0; }}
  .meta-tbl td:first-child {{ color:var(--muted); width:200px; }}
  h2 {{ font-size:18px; margin:30px 0 12px; padding-bottom:6px; border-bottom:2px solid #e2e8f0; }}
  .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:16px 0; }}
  .kpi {{ border:1px solid #e2e8f0; border-radius:10px; padding:14px 16px; background:#f8fafc; }}
  .kpi-val {{ font-size:26px; font-weight:800; letter-spacing:-.5px; }}
  .kpi-lbl {{ font-size:12px; color:var(--muted); margin-top:2px; }}
  .kpi-sub {{ font-size:11px; color:var(--accent); margin-top:3px; font-weight:600; }}
  .verdict {{ background:#f8fafc; border-radius:10px; padding:16px 18px; margin:14px 0; }}
  .verdict-row {{ display:flex; align-items:center; justify-content:space-between; gap:14px; }}
  .verdict-lbl {{ font-size:13px; font-weight:700; }}
  .verdict-tag {{ color:#fff; font-weight:800; font-size:13px; padding:4px 14px; border-radius:99px; }}
  .verdict-sub {{ color:var(--muted); font-size:13px; margin-top:6px; }}
  .tbl {{ width:100%; border-collapse:collapse; font-size:12.5px; margin-top:8px; }}
  .tbl th {{ text-align:left; background:#0f172a; color:#fff; padding:8px 10px; font-size:11px;
          text-transform:uppercase; letter-spacing:.4px; }}
  .tbl td {{ padding:7px 10px; border-bottom:1px solid #eef2f7; vertical-align:top; }}
  .tbl tr:nth-child(even) td {{ background:#f8fafc; }}
  .tbl .r {{ text-align:right; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:4px; }}
  .chip {{ background:#ffe4ec; color:#9d174d; border:1px solid #fbcfe8; border-radius:6px;
          padding:2px 8px; font-size:11px; }}
  .note {{ color:var(--muted); font-size:11.5px; margin-top:8px; }}
  .empty {{ color:var(--muted); font-style:italic; }}
  .sec {{ break-inside:avoid; }}
  footer {{ margin-top:36px; padding-top:14px; border-top:1px solid #e2e8f0;
          color:var(--muted); font-size:11px; }}
  @media print {{
    body {{ background:#fff; }}
    .toolbar {{ display:none; }}
    .page {{ box-shadow:none; margin:0; max-width:none; padding:0; }}
    h2 {{ break-after:avoid; }}
    .sec, .kpi, .verdict, tr {{ break-inside:avoid; }}
    @page {{ margin:18mm 16mm; }}
  }}
</style></head>
<body>
<div class="toolbar"><button onclick="window.print()">Imprimer / Enregistrer en PDF</button></div>
<div class="page">
  <div class="cover">
    <div class="brand">Tekkai · Rapport CIB TikTok</div>
    <h1>Détection de comportement coordonné — TikTok</h1>
    <div class="cover-sub">{(company + ' · ') if company else ''}{('Analyste : ' + analyst) if analyst else 'Analyse d\'inauthenticité coordonnée'}</div>
  </div>
  <table class="meta-tbl">{meta_rows}</table>

  <section class="sec"><h2>Synthèse exécutive</h2>{verdict}<div class="kpi-grid">{kpis}</div></section>

  <section class="sec"><h2>Clusters coordonnés</h2>{_clusters_table(result)}</section>

  {f'<section class="sec"><h2>Comptes les plus coordonnés</h2>{_accounts_table(result)}</section>' if _accounts_table(result) else ''}

  <section class="sec"><h2>Méthodologie</h2>
    <p class="note">L'analyse CIB (Coordinated Inauthentic Behavior) compare les comptes deux
    à deux sur cinq signaux pondérés : <b>hashtags communs</b> (30), <b>pics temporels
    synchronisés</b> (25), <b>même son/musique</b> (20), <b>descriptions similaires</b> (15)
    et <b>comptes créés en grappe</b> (10). Les paires fortement liées sont regroupées en
    clusters. Le score CIB (0–100) reflète l'intensité moyenne de coordination d'un compte.
    Les verdicts sont des indicateurs d'aide à la décision, non des preuves d'automatisation.</p>
  </section>

  <footer>Généré le {_esc(now)} par Tekkai. Document confidentiel — diffusion restreinte.
  Les données reflètent l'état du corpus au moment de l'analyse.</footer>
</div>
</body></html>"""
