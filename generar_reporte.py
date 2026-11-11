"""
Genera el dashboard mensual de Meta Ads de UL Baja (y su historial desde enero).

- Junta los datos de TODAS las cuentas listadas en config.json ("account_ids").
- Separa los resultados por PROYECTO (The Park / Torre Corporativa Eugenia),
  segun el nombre de cada campaña (lista "projects" en config.json).
- El objetivo de cada campaña (whatsapp / formulario / reconocimiento) tambien
  se deduce del nombre de la campaña.

El token se lee de la variable de entorno META_ACCESS_TOKEN (GitHub Secret).
"""

import json
import os
import sys
import base64
import re
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from jinja2 import Template

GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
WA_ACTION = "onsite_conversion.messaging_conversation_started_7d"
CALL_PRIMARY = "click_to_call_native_call_placed"
CALL_FALLBACK = "click_to_call_call_confirm"
# Subir este numero fuerza a regenerar UNA vez todos los meses del historial.
BACKFILL_VERSION = "1"
OTROS = "Otras campañas"

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MESES_CORTOS = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul",
                "Ago", "Sep", "Oct", "Nov", "Dic"]
PROJECT_COLORS = ["#132A3E", "#E15241", "#1BAF7A", "#EDA100", "#7A8794"]


# ----------------------------------------------------------------- utilidades

def get_access_token():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        print("ERROR: no encontre la variable de entorno META_ACCESS_TOKEN.")
        sys.exit(1)
    return token


def get_account_ids(config):
    ids = config.get("account_ids") or [config["account_id"]]
    return [i if str(i).startswith("act_") else f"act_{i}" for i in ids]


def chunked(seq, n=50):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def money(x):
    return f"${x:,.2f}"


def graph_get_all(url, params, max_pages=20):
    """GET con paginacion. Si Meta responde con error (ej. falta de permisos
    sobre una cuenta) lo muestra y DETIENE el reporte, para no publicar
    numeros incompletos."""
    rows = []
    next_url, next_params = url, params
    page = 1
    while next_url:
        resp = requests.get(next_url, params=next_params, timeout=60)
        if not resp.ok:
            print(f"ERROR de la API de Meta ({resp.status_code}) en {url}: {resp.text[:300]}")
            resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
        next_params = None
        page += 1
        if page > max_pages:
            break
    return rows


# ------------------------------------------------- objetivos y proyectos

def count_actions(actions):
    """Un solo action_type canonico por metrica (si se suman todos los que
    dicen "lead" se duplican)."""
    wa, leads = 0, 0
    for a in actions or []:
        t = a.get("action_type", "")
        if t == WA_ACTION:
            wa = int(float(a.get("value", 0)))
        if t == "lead":
            leads = int(float(a.get("value", 0)))
    return wa, leads


def count_calls(actions):
    primary, fallback = 0, 0
    for a in actions or []:
        t = a.get("action_type", "")
        if t == CALL_PRIMARY:
            primary = int(float(a.get("value", 0)))
        elif t == CALL_FALLBACK:
            fallback = int(float(a.get("value", 0)))
    return primary or fallback


def objective_from_name(name):
    """Objetivo de la campaña segun su nombre (None si el nombre no lo dice)."""
    n = (name or "").lower()
    if "reconocimiento" in n or "alcance" in n or "awareness" in n:
        return "reconocimiento"
    if "llamada" in n:
        return "llamadas"
    if "whatsapp" in n:
        return "whatsapp"
    if "formulario" in n or "lead" in n:
        return "formulario"
    return None


def project_of(name, projects):
    """Proyecto de la campaña segun su nombre (lista 'projects' del config)."""
    n = (name or "").lower()
    for p in projects:
        if any(k.lower() in n for k in p.get("keywords", [])):
            return p["name"]
    return OTROS


def allocate_results(name, actions, spend):
    """Asigna resultados y gasto de UNA campaña a un solo canal segun su
    objetivo (como la columna "Resultados" de Ads Manager).
    Regresa (whatsapp, leads, llamadas, gasto_wa, gasto_leads, gasto_llamadas)."""
    wa, leads = count_actions(actions)
    calls = count_calls(actions)
    obj = objective_from_name(name)
    if obj == "reconocimiento":
        return 0, 0, 0, 0.0, 0.0, 0.0
    if obj == "llamadas":
        return 0, 0, calls, 0.0, 0.0, (spend if calls else 0.0)
    if obj == "whatsapp":
        return wa, 0, 0, (spend if wa else 0.0), 0.0, 0.0
    if obj == "formulario":
        return 0, leads, 0, 0.0, (spend if leads else 0.0), 0.0
    total = wa + leads + calls
    if total == 0:
        return 0, 0, 0, 0.0, 0.0, 0.0
    return (wa, leads, calls,
            spend * wa / total, spend * leads / total, spend * calls / total)


def classify_objective(name, wa_count, lead_count, reach, call_count=0):
    obj = objective_from_name(name)
    if obj == "reconocimiento":
        return "Reconocimiento", reach, True
    if obj == "llamadas":
        return "Llamadas", call_count, False
    if obj == "whatsapp":
        return "Whatsapp", wa_count, False
    if obj == "formulario":
        return "Formulario", lead_count, False
    if call_count > max(wa_count, lead_count):
        return "Llamadas", call_count, False
    if lead_count > wa_count:
        return "Formulario", lead_count, False
    if wa_count > 0:
        return "Whatsapp", wa_count, False
    return "Otro", reach, True


# ------------------------------------------------------- consultas a Meta

def fetch_account_totals(account_ids, token, since, until):
    tot = {"reach": 0, "spend": 0.0, "impressions": 0}
    for account_id in account_ids:
        rows = graph_get_all(f"{GRAPH_API_BASE}/{account_id}/insights", {
            "fields": "reach,spend,impressions",
            "time_range": json.dumps({"since": since, "until": until}),
            "access_token": token,
        })
        if rows:
            tot["reach"] += int(float(rows[0].get("reach", 0)))
            tot["spend"] += float(rows[0].get("spend", 0))
            tot["impressions"] += int(float(rows[0].get("impressions", 0)))
    return tot


def fetch_campaign_rows(account_ids, token, since, until, monthly=False):
    rows = []
    for account_id in account_ids:
        params = {
            "level": "campaign",
            "fields": "campaign_id,campaign_name,reach,spend,impressions,actions",
            "time_range": json.dumps({"since": since, "until": until}),
            "limit": 500,
            "access_token": token,
        }
        if monthly:
            params["time_increment"] = "monthly"
        for row in graph_get_all(f"{GRAPH_API_BASE}/{account_id}/insights", params):
            row["_account"] = account_id
            rows.append(row)
    return rows


def fetch_reach_for_campaigns(ids_by_account, token, since, until):
    """Alcance SIN duplicar de un grupo de campañas (ej. todas las de un
    proyecto): se pide a Meta filtrando por esos IDs, porque el alcance de
    varias campañas no se puede sumar."""
    tot = {"reach": 0, "impressions": 0}
    for account_id, ids in ids_by_account.items():
        if not ids:
            continue
        rows = graph_get_all(f"{GRAPH_API_BASE}/{account_id}/insights", {
            "fields": "reach,impressions",
            "time_range": json.dumps({"since": since, "until": until}),
            "filtering": json.dumps([{"field": "campaign.id", "operator": "IN",
                                      "value": sorted(set(ids))}]),
            "access_token": token,
        })
        if rows:
            tot["reach"] += int(float(rows[0].get("reach", 0)))
            tot["impressions"] += int(float(rows[0].get("impressions", 0)))
    return tot


def fetch_range_insights(config, account_ids, token, since, until, detailed=True):
    """KPIs de un rango. Alcance, gasto y frecuencia: total de la cuenta.
    Resultados y su costo: campaña por campaña segun su objetivo.
    Con detailed=True tambien calcula cada proyecto y el reconocimiento."""
    projects_cfg = config.get("projects", [])
    acct = fetch_account_totals(account_ids, token, since, until)
    rows = fetch_campaign_rows(account_ids, token, since, until)

    tot = {"wa": 0, "leads": 0, "calls": 0, "sp_wa": 0.0, "sp_leads": 0.0, "sp_calls": 0.0}
    proj = {}
    recog = {"spend": 0.0, "ids": {}}

    def empty_proj():
        return {"spend": 0.0, "wa": 0, "leads": 0, "calls": 0,
                "sp_wa": 0.0, "sp_leads": 0.0, "sp_calls": 0.0,
                "ids": {}, "recog_ids": {}, "recog_spend": 0.0}

    for row in rows:
        name = row.get("campaign_name", "")
        spend = float(row.get("spend", 0))
        w, l, c, sw, sl, sc = allocate_results(name, row.get("actions"), spend)
        for k, v in (("wa", w), ("leads", l), ("calls", c),
                     ("sp_wa", sw), ("sp_leads", sl), ("sp_calls", sc)):
            tot[k] += v
        p = proj.setdefault(project_of(name, projects_cfg), empty_proj())
        p["spend"] += spend
        for k, v in (("wa", w), ("leads", l), ("calls", c),
                     ("sp_wa", sw), ("sp_leads", sl), ("sp_calls", sc)):
            p[k] += v
        p["ids"].setdefault(row["_account"], []).append(row.get("campaign_id"))
        if objective_from_name(name) == "reconocimiento":
            recog["spend"] += spend
            recog["ids"].setdefault(row["_account"], []).append(row.get("campaign_id"))
            p["recog_spend"] += spend
            p["recog_ids"].setdefault(row["_account"], []).append(row.get("campaign_id"))

    freq = (acct["impressions"] / acct["reach"]) if acct["reach"] else 0.0
    result = {
        "reach": acct["reach"],
        "spend": round(acct["spend"], 2),
        "freq": round(freq, 1),
        "whatsapp": tot["wa"],
        "leads": tot["leads"],
        "calls": tot["calls"],
        "spend_wa": tot["sp_wa"],
        "spend_leads": tot["sp_leads"],
        "spend_calls": tot["sp_calls"],
        "campaign_rows": rows,
        "projects": [],
        "recog_spend": recog["spend"],
        "recog_reach": 0,
    }
    if not detailed:
        return result

    if recog["spend"] > 0:
        result["recog_reach"] = fetch_reach_for_campaigns(recog["ids"], token, since, until)["reach"]

    order = [p["name"] for p in projects_cfg] + [OTROS]
    for pname in order:
        p = proj.get(pname)
        if not p or p["spend"] <= 0:
            continue
        r = fetch_reach_for_campaigns(p["ids"], token, since, until)
        recog_reach = 0
        if p["recog_spend"] > 0:
            recog_reach = fetch_reach_for_campaigns(p["recog_ids"], token, since, until)["reach"]
        result["projects"].append({
            "name": pname,
            "spend": p["spend"],
            "reach": r["reach"],
            "freq": (r["impressions"] / r["reach"]) if r["reach"] else 0.0,
            "whatsapp": p["wa"],
            "leads": p["leads"],
            "calls": p["calls"],
            "cost_wa": (p["sp_wa"] / p["wa"]) if p["wa"] else None,
            "cost_lead": (p["sp_leads"] / p["leads"]) if p["leads"] else None,
            "cost_call": (p["sp_calls"] / p["calls"]) if p["calls"] else None,
            "recog_spend": p["recog_spend"],
            "recog_reach": recog_reach,
            "cost_1000": (p["recog_spend"] / recog_reach * 1000) if recog_reach else None,
        })
    return result


def build_campaigns_breakdown(rows, projects_cfg):
    """Tabla por campaña (reutiliza las filas ya descargadas)."""
    merged = {}
    for row in rows:
        name = row.get("campaign_name", "")
        m = merged.setdefault(name, {"reach": 0, "spend": 0.0, "impr": 0, "wa": 0, "leads": 0, "calls": 0})
        wa, leads = count_actions(row.get("actions"))
        m["reach"] += int(float(row.get("reach", 0)))
        m["spend"] += float(row.get("spend", 0))
        m["impr"] += int(float(row.get("impressions", 0)))
        m["wa"] += wa
        m["leads"] += leads
        m["calls"] += count_calls(row.get("actions"))

    campaigns = []
    for name, m in merged.items():
        if m["spend"] <= 0:
            continue
        reach = m["reach"]
        spend = round(m["spend"], 2)
        freq = round(m["impr"] / reach, 1) if reach else 0.0
        objetivo, resultado, es_awareness = classify_objective(name, m["wa"], m["leads"], reach, m["calls"])
        if es_awareness:
            costo = f"{money(spend / reach * 1000)} x 1,000 pers." if reach else "N/A"
            resultado_txt = f"{resultado:,} pers."
        else:
            costo = money(spend / resultado) if resultado else "N/A"
            resultado_txt = f"{resultado:,}"
        campaigns.append({
            "name": name,
            "proyecto": project_of(name, projects_cfg),
            "objetivo": objetivo,
            "reach": f"{reach:,}",
            "spend": f"{money(spend)} MXN",
            "freq": f"{freq:.1f}",
            "resultado": resultado_txt,
            "costo_resultado": costo,
            "_spend_raw": spend,
        })
    campaigns.sort(key=lambda c: c["_spend_raw"], reverse=True)
    return campaigns


def month_label(key, multi_year):
    d = datetime.strptime(key, "%Y-%m-%d")
    lab = MESES_CORTOS[d.month - 1]
    return f"{lab} {d.year % 100:02d}" if multi_year else lab


def fetch_monthly_insights(config, account_ids, token, since, until):
    """Historico mes a mes: alcance/gasto/frecuencia de la cuenta y
    resultados por objetivo y por proyecto."""
    projects_cfg = config.get("projects", [])
    by_month = {}

    def bucket(key):
        return by_month.setdefault(key, {"reach": 0, "spend": 0.0, "impr": 0,
                                         "wa": 0, "leads": 0, "calls": 0, "proj": {}})

    time_range = json.dumps({"since": since, "until": until})
    for account_id in account_ids:
        for row in graph_get_all(f"{GRAPH_API_BASE}/{account_id}/insights", {
            "fields": "reach,spend,impressions",
            "time_increment": "monthly",
            "time_range": time_range,
            "access_token": token,
        }):
            m = bucket(row["date_start"])
            m["reach"] += int(float(row.get("reach", 0)))
            m["spend"] += float(row.get("spend", 0))
            m["impr"] += int(float(row.get("impressions", 0)))

    for row in fetch_campaign_rows(account_ids, token, since, until, monthly=True):
        m = bucket(row["date_start"])
        name = row.get("campaign_name", "")
        w, l, c, _, _, _ = allocate_results(name, row.get("actions"), float(row.get("spend", 0)))
        m["wa"] += w
        m["leads"] += l
        m["calls"] += c
        pname = project_of(name, projects_cfg)
        m["proj"][pname] = m["proj"].get(pname, 0) + w + l + c

    keys = sorted(k for k in by_month if by_month[k]["spend"] > 0)
    multi_year = len({k[:4] for k in keys}) > 1
    out = {"months": [month_label(k, multi_year) for k in keys],
           "reach": [], "spend": [], "freq": [], "whatsapp": [], "leads": [], "calls": []}
    for k in keys:
        m = by_month[k]
        out["reach"].append(m["reach"])
        out["spend"].append(round(m["spend"], 2))
        out["freq"].append(round(m["impr"] / m["reach"], 1) if m["reach"] else 0.0)
        out["whatsapp"].append(m["wa"])
        out["leads"].append(m["leads"])
        out["calls"].append(m["calls"])

    names = [p["name"] for p in projects_cfg] + [OTROS]
    series = []
    for i, pname in enumerate(names):
        data = [by_month[k]["proj"].get(pname, 0) for k in keys]
        if any(data):
            series.append({"label": pname, "data": data,
                           "backgroundColor": PROJECT_COLORS[i % len(PROJECT_COLORS)],
                           "borderRadius": 4})
    out["projects_series"] = series
    return out


# ------------------------------------------------------- anuncios top

def fetch_video_thumbnail(video_id, token):
    resp = requests.get(f"{GRAPH_API_BASE}/{video_id}",
                        params={"fields": "thumbnails", "access_token": token}, timeout=30)
    if not resp.ok:
        return None
    thumbs = resp.json().get("thumbnails", {}).get("data", [])
    if not thumbs:
        return None
    return max(thumbs, key=lambda t: t.get("width", 0)).get("uri")


def fetch_boosted_post_media(post_id, token):
    resp = requests.get(f"{GRAPH_API_BASE}/{post_id}", params={
        "fields": "full_picture,attachments{media_type,media,url}",
        "access_token": token,
    }, timeout=30)
    if not resp.ok:
        return None
    data = resp.json()
    for att in data.get("attachments", {}).get("data", []):
        src = att.get("media", {}).get("image", {}).get("src")
        if src:
            return src
    return data.get("full_picture")


def download_and_embed_image(url):
    """Descarga la imagen y la guarda en base64 dentro del HTML, porque los
    links de imagenes de Meta caducan."""
    if not url or url.startswith("data:"):
        return url
    try:
        resp = requests.get(url, timeout=20)
        if not resp.ok:
            return url
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        data = base64.b64encode(resp.content).decode("ascii")
        return f"data:{content_type};base64,{data}"
    except requests.RequestException:
        return url


def load_manual_image(prefix, index):
    folder = Path(__file__).parent / "images_manual"
    for ext in ("jpg", "jpeg", "png", "webp"):
        candidate = folder / f"{prefix}_{index}.{ext}"
        if candidate.exists():
            mime = "jpeg" if ext == "jpg" else ext
            data = base64.b64encode(candidate.read_bytes()).decode("ascii")
            return f"data:image/{mime};base64,{data}"
    return None


def fetch_ad_level_metrics(account_ids, token, since, until):
    rows = []
    for account_id in account_ids:
        rows.extend(graph_get_all(f"{GRAPH_API_BASE}/{account_id}/insights", {
            "level": "ad",
            "fields": "ad_id,ad_name,campaign_id,campaign_name,reach,spend,actions",
            "time_range": json.dumps({"since": since, "until": until}),
            "limit": 200,
            "access_token": token,
        }))
    return rows


def fetch_current_campaign_names(token, campaign_ids):
    result = {}
    for chunk in chunked(campaign_ids, 50):
        resp = requests.get(f"{GRAPH_API_BASE}/", params={
            "ids": ",".join(str(c) for c in chunk), "fields": "name", "access_token": token,
        }, timeout=30)
        resp.raise_for_status()
        result.update({cid: info.get("name", cid) for cid, info in resp.json().items()})
    return result


def fetch_ads_creative_info(token, ad_ids):
    result = {}
    for chunk in chunked(ad_ids, 50):
        resp = requests.get(f"{GRAPH_API_BASE}/", params={
            "ids": ",".join(chunk),
            "fields": (
                "name,creative{id,thumbnail_url.width(1080).height(1350),body,"
                "object_type,image_url,object_story_spec,effective_object_story_id,"
                "asset_feed_spec{images{url},videos{video_id,thumbnail_url}}}"
            ),
            "access_token": token,
        }, timeout=30)
        resp.raise_for_status()
        result.update(resp.json())
    return result


def enrich_ad(ad, creative_by_id, prefix, i, token):
    creative = creative_by_id.get(ad["best_ad_id"], {}).get("creative", {})
    image = creative.get("image_url") or creative.get("thumbnail_url", "")
    video_id = creative.get("object_story_spec", {}).get("video_data", {}).get("video_id")
    afs = creative.get("asset_feed_spec", {})
    if not video_id and afs.get("videos"):
        video_id = afs["videos"][0].get("video_id")
        if not image:
            image = afs["videos"][0].get("thumbnail_url") or image
    if afs.get("images") and (creative.get("object_type") or "").upper() != "VIDEO":
        image = afs["images"][0].get("url") or image
    story_id = creative.get("effective_object_story_id")

    manual = load_manual_image(prefix, i)
    if manual:
        image = manual
    elif video_id:
        image = fetch_video_thumbnail(video_id, token) or image
    elif story_id:
        image = fetch_boosted_post_media(story_id, token) or image

    ad["name"] = (creative.get("body") or ad["name"])[:90]
    ad["format"] = (creative.get("object_type") or "ANUNCIO").upper()
    ad["thumbnail_url"] = download_and_embed_image(image)
    ad["reach"] = f"{ad['reach']:,}"
    ad["spend"] = f"{money(ad['spend'])} MXN"
    ad["whatsapp"] = f"{ad['whatsapp']:,}"
    ad["leads"] = f"{ad['leads']:,}"
    ad["calls"] = f"{ad.get('calls', 0):,}"
    return ad


def fetch_top_ad_per_campaign(account_ids, token, since, until, projects_cfg, top_n=3):
    rows = fetch_ad_level_metrics(account_ids, token, since, until)

    grouped, campaign_totals = {}, {}
    for row in rows:
        campaign_id = row.get("campaign_id") or row.get("campaign_name", "sin_campaña")
        campaign_name = row.get("campaign_name", "Sin campaña")
        ad_name = row.get("ad_name", "")
        reach = int(float(row.get("reach", 0)))
        spend = round(float(row.get("spend", 0)), 2)
        wa_count, lead_count = count_actions(row.get("actions"))
        call_count = count_calls(row.get("actions"))

        ct = campaign_totals.setdefault(campaign_id, {"reach": 0, "spend": 0.0, "whatsapp": 0,
                                                      "leads": 0, "calls": 0, "nombres": {}})
        ct["reach"] += reach
        ct["spend"] += spend
        ct["whatsapp"] += wa_count
        ct["leads"] += lead_count
        ct["calls"] += call_count
        ct["nombres"][campaign_name] = ct["nombres"].get(campaign_name, 0) + spend

        normalized = re.sub(r"\s*-\s*copia(\s*\d+)?\s*$", "", ad_name, flags=re.IGNORECASE).strip().lower()
        key = (campaign_id, normalized or ad_name.strip() or row.get("ad_id"))
        g = grouped.setdefault(key, {"campaign_id": campaign_id, "name": ad_name, "reach": 0,
                                     "spend": 0.0, "whatsapp": 0, "leads": 0, "calls": 0,
                                     "best_ad_id": row.get("ad_id"), "best_reach": -1})
        g["reach"] += reach
        g["spend"] += spend
        g["whatsapp"] += wa_count
        g["leads"] += lead_count
        g["calls"] += call_count
        if reach > g["best_reach"]:
            g["best_reach"] = reach
            g["best_ad_id"] = row.get("ad_id")

    names = fetch_current_campaign_names(token, list(campaign_totals.keys()))

    winners = {}
    for campaign_id, totals in campaign_totals.items():
        if totals["spend"] <= 0:
            continue
        current_name = names.get(campaign_id) or max(totals["nombres"], key=totals["nombres"].get)
        objetivo, _, es_awareness = classify_objective(
            current_name, totals["whatsapp"], totals["leads"], totals["reach"], totals["calls"])
        metric_key = ("reach" if es_awareness else
                      {"Whatsapp": "whatsapp", "Llamadas": "calls"}.get(objetivo, "leads"))
        ads = [dict(a) for a in grouped.values() if a["campaign_id"] == campaign_id]
        ads.sort(key=lambda a: a[metric_key], reverse=True)
        top = ads[:top_n]
        for ad in top:
            ad["objetivo"] = objetivo
        winners[campaign_id] = top

    winner_ids = list({a["best_ad_id"] for ads in winners.values() for a in ads if a.get("best_ad_id")})
    creative_by_id = fetch_ads_creative_info(token, winner_ids)

    by_theme, i = {}, 1
    for campaign_id, ads in winners.items():
        campaign_name = names.get(campaign_id, str(campaign_id))
        enriched = [enrich_ad(ad, creative_by_id, "campaign", i + idx, token) for idx, ad in enumerate(ads)]
        i += len(ads)
        if not enriched:
            continue
        # Se agrupa por proyecto; si la campaña no tiene proyecto, por su nombre sin el objetivo.
        theme = project_of(campaign_name, projects_cfg)
        if theme == OTROS:
            theme = re.sub(r"\b(whatsapp|formulario|reconocimiento|alcance|awareness|leads?)\b",
                           "", campaign_name, flags=re.IGNORECASE)
            theme = re.sub(r"[/\-_|]+", " ", theme)
            theme = re.sub(r"\s+", " ", theme).strip(" /-")
        key = theme.lower() or campaign_name.lower()
        by_theme.setdefault(key, {"campaign_name": theme or campaign_name, "ads": []})["ads"].extend(enriched)
    return list(by_theme.values())


# ------------------------------------------------------------- insights

def pct_change(prev, cur):
    return round((cur - prev) / prev * 100, 1)


def build_insights(current, previous):
    insights = []
    r_prev, r_cur = previous["reach"], current["reach"]
    res_prev = previous["whatsapp"] + previous["leads"] + previous["calls"]
    res_cur = current["whatsapp"] + current["leads"] + current["calls"]
    sp_res_prev = previous["spend_wa"] + previous["spend_leads"] + previous["spend_calls"]
    sp_res_cur = current["spend_wa"] + current["spend_leads"] + current["spend_calls"]

    if r_prev and r_cur:
        p = pct_change(r_prev, r_cur)
        insights.append(f"El alcance {'subió' if p >= 0 else 'bajó'} {abs(p)}% contra el mes anterior "
                        f"({r_prev:,} → {r_cur:,} personas).")
    elif r_cur:
        insights.append(f"Se alcanzaron (semana) {r_cur:,} personas este mes (no hay mes anterior para comparar).")

    if res_prev and res_cur:
        p = pct_change(res_prev, res_cur)
        insights.append(f"Los resultados (whatsapps + formularios) {'aumentaron' if p >= 0 else 'bajaron'} "
                        f"{abs(p)}% contra el mes anterior ({res_prev} → {res_cur}).")
    elif res_cur:
        insights.append(f"Se generaron (semana) {res_cur} resultados (whatsapps + formularios) este mes.")

    if res_cur and sp_res_cur:
        cpr = sp_res_cur / res_cur
        line = f"El costo promedio por resultado fue de {money(cpr)} MXN"
        if res_prev and sp_res_prev:
            cpr_prev = sp_res_prev / res_prev
            p = pct_change(cpr_prev, cpr)
            line += f", {abs(p)}% {'más caro' if p >= 0 else 'más barato'} que el mes anterior ({money(cpr_prev)} MXN)."
        else:
            line += "."
        insights.append(line)

    projs = [p for p in current.get("projects", []) if p["name"] != OTROS]
    for p in projs:
        partes = []
        if p["whatsapp"]:
            partes.append(f"{p['whatsapp']:,} whatsapps (a {money(p['cost_wa'])} c/u)")
        if p["leads"]:
            partes.append(f"{p['leads']:,} formularios (a {money(p['cost_lead'])} c/u)")
        if p["recog_reach"]:
            partes.append(f"{p['recog_reach']:,} personas con reconocimiento "
                          f"(a {money(p['cost_1000'])} por cada 1,000)")
        if partes:
            insights.append(f"{p['name']}: invirtió {money(p['spend'])} MXN y generó " + ", ".join(partes) + ".")

    if len(projs) >= 2:
        con_res = [p for p in projs if p["whatsapp"] + p["leads"] > 0]
        if len(con_res) >= 2:
            top = max(con_res, key=lambda p: p["whatsapp"] + p["leads"])
            total = sum(p["whatsapp"] + p["leads"] for p in con_res)
            share = round((top["whatsapp"] + top["leads"]) / total * 100)
            insights.append(f"{top['name']} concentró el {share}% de los resultados del mes.")

    return insights or ["Sin cambios significativos respecto al mes anterior."]


# ------------------------------------------------------------- historial

def fix_archive_links(archive_dir):
    """Las copias de archivo/ viven una carpeta mas adentro: ajusta sus
    links relativos. Es seguro correrla varias veces."""
    for f in archive_dir.glob("*.html"):
        if f.name == "index.html" or f.name.startswith("_"):
            continue
        text = f.read_text(encoding="utf-8")
        fixed = text.replace('href="archivo/index.html"', 'href="index.html"')
        if fixed != text:
            f.write_text(fixed, encoding="utf-8")


def build_archive_index(archive_dir, client_name, main_filename):
    archivos = sorted((f for f in archive_dir.glob("*.html")
                       if f.name != "index.html" and not f.name.startswith("_")), reverse=True)
    
    # Separar en semanal (nov+) y mensual (ene-oct)
    semanal = []
    mensual = []
    for f in archivos:
        try:
            year, month = f.stem.split("-")
            month_int = int(month)
            label = f"{MESES[month_int - 1].capitalize()} {year}"
            if month_int >= 11:
                semanal.append(f'<li><a href="{f.name}">{label}</a></li>')
            else:
                mensual.append(f'<li><a href="{f.name}">{label}</a></li>')
        except (ValueError, IndexError):
            label = f.stem
            mensual.append(f'<li><a href="{f.name}">{label}</a></li>')

    semanal_html = "".join(semanal) if semanal else "<li>Sin reportes semanales aún.</li>"
    mensual_html = "".join(mensual) if mensual else "<li>Sin reportes mensuales.</li>"

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Historial de reportes — {client_name}</title>
<style>
  body{{font-family:Calibri,Arial,sans-serif;background:#FAFAF8;color:#1B2A3B;max-width:600px;margin:60px auto;padding:0 20px;}}
  h1{{font-size:22px;}}
  h2{{font-size:16px;color:#132A3E;margin-top:30px;margin-bottom:12px;}}
  h2:first-of-type{{margin-top:0;}}
  ul{{list-style:none;padding:0;}}
  li{{margin-bottom:10px;}}
  a{{color:#E15241;font-size:16px;text-decoration:none;}}
  a:hover{{text-decoration:underline;}}
  .back{{margin-top:30px;display:inline-block;font-size:13px;color:#63707D;}}
</style>
</head>
<body>
  <h1>Historial de reportes — {client_name}</h1>
  <h2>Reportes semanales</h2>
  <ul>
    {semanal_html}
  </ul>
  <h2>Reportes mensuales anteriores</h2>
  <ul>
    {mensual_html}
  </ul>
  <a class="back" href="../{main_filename}">← Volver al reporte más reciente</a>
</body>
</html>"""
    (archive_dir / "index.html").write_text(html, encoding="utf-8")


def month_bounds(year, month):
    start = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return start, nxt - timedelta(days=1)


def prev_month_bounds(start_str):
    d = datetime.strptime(start_str, "%Y-%m-%d").date()
    prev_end = d.replace(day=1) - timedelta(days=1)
    return prev_end.replace(day=1).strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d")


# ------------------------------------------------------------- reporte

def build_report(config, token, account_ids, since, chart_until,
                 kpi_start, kpi_until, kpi_period_label, write_main=True):
    """Genera el reporte de kpi_start -> kpi_until. write_main=False se usa
    al rellenar meses anteriores: solo guarda la copia en archivo/."""
    monthly = fetch_monthly_insights(config, account_ids, token, since, chart_until)
    cur = fetch_range_insights(config, account_ids, token, kpi_start, kpi_until, detailed=True)
    p_since, p_until = prev_month_bounds(kpi_start)
    prev = fetch_range_insights(config, account_ids, token, p_since, p_until, detailed=False)
    insights = build_insights(cur, prev)

    campaigns_breakdown = None
    if config.get("show_campaign_breakdown", True):
        campaigns_breakdown = build_campaigns_breakdown(cur["campaign_rows"], config.get("projects", []))

    top_ads_by_campaign = None
    if config.get("top_ads_per_campaign", True):
        top_ads_by_campaign = fetch_top_ad_per_campaign(
            account_ids, token, kpi_start, kpi_until, config.get("projects", []),
            top_n=config.get("top_ads_per_campaign_count", 3))

    wa, leads, calls = cur["whatsapp"], cur["leads"], cur["calls"]
    projects_view = [{
        "name": p["name"],
        "spend": f"{money(p['spend'])} MXN",
        "reach": f"{p['reach']:,}",
        "freq": f"{p['freq']:.1f}",
        "whatsapp": f"{p['whatsapp']:,}",
        "leads": f"{p['leads']:,}",
        "calls": f"{p['calls']:,}",
        "show_calls": p["calls"] > 0,
        "show_wa": p["whatsapp"] > 0,
        "show_leads": p["leads"] > 0,
        "cost_wa": money(p["cost_wa"]) if p["cost_wa"] else "N/A",
        "cost_lead": money(p["cost_lead"]) if p["cost_lead"] else "N/A",
        "cost_call": money(p["cost_call"]) if p["cost_call"] else "N/A",
        "show_recog": p["recog_reach"] > 0,
        "recog_reach": f"{p['recog_reach']:,}",
        "cost_1000": money(p["cost_1000"]) if p["cost_1000"] else "N/A",
    } for p in cur["projects"]]

    template = Template((Path(__file__).parent / "template.html").read_text(encoding="utf-8"))
    archive_url = "archivo/index.html" if config.get("enable_history", True) else None
    recog_reach, recog_spend = cur["recog_reach"], cur["recog_spend"]

    html = template.render(
        agency_name=config.get("agency_name", "Quattro Marketing"),
        client_name=config["client_name"],
        period_label=config.get("period_label", chart_until[:4]),
        kpi_period_label=kpi_period_label,
        kpi_reach=f"{cur['reach']:,}",
        kpi_spend=f"{money(cur['spend'])} MXN",
        kpi_whatsapp=f"{wa:,}",
        kpi_leads=f"{leads:,}",
        kpi_frequency=f"{cur['freq']:.1f}",
        kpi_cost_per_wa=money(cur["spend_wa"] / wa) if wa else "N/A",
        kpi_cost_per_lead=money(cur["spend_leads"] / leads) if leads else "N/A",
        kpi_calls=f"{calls:,}",
        kpi_cost_per_call=money(cur["spend_calls"] / calls) if calls else "N/A",
        show_calls=calls > 0,
        show_recog=recog_reach > 0,
        kpi_recog_reach=f"{recog_reach:,}",
        kpi_cost_1000=money(recog_spend / recog_reach * 1000) if recog_reach else "N/A",
        projects=projects_view,
        project_names=" + ".join(p["name"] for p in config.get("projects", [])) or "todas las campañas",
        show_projects=len(projects_view) > 0,
        campaigns_breakdown=campaigns_breakdown,
        top_ads_by_campaign=top_ads_by_campaign,
        insights=insights,
        archive_url=archive_url,
        months_json=json.dumps(monthly["months"]),
        reach_json=json.dumps(monthly["reach"]),
        cost_json=json.dumps(monthly["spend"]),
        freq_json=json.dumps(monthly["freq"]),
        whatsapp_json=json.dumps(monthly["whatsapp"]),
        leads_json=json.dumps(monthly["leads"]),
        calls_json=json.dumps(monthly["calls"]),
        show_calls_chart=any(monthly["calls"]),
        projects_series_json=json.dumps(monthly["projects_series"], ensure_ascii=False),
        projects_legend=monthly["projects_series"],
        generated_date=date.today().strftime("%d/%m/%Y"),
    )

    output_dir = Path(__file__).parent / config.get("output_dir", "docs")
    output_dir.mkdir(exist_ok=True, parents=True)
    main_filename = config.get("output_filename", "dashboard.html")
    if write_main:
        (output_dir / main_filename).write_text(html, encoding="utf-8")
        print(f"Listo! Dashboard generado en: {output_dir / main_filename}")

    if archive_url:
        archive_dir = output_dir / "archivo"
        archive_dir.mkdir(exist_ok=True, parents=True)
        (archive_dir / f"{kpi_start[:7]}.html").write_text(html, encoding="utf-8")
        fix_archive_links(archive_dir)
        build_archive_index(archive_dir, config["client_name"], main_filename)


def month_has_data(account_ids, token, start, end):
    return fetch_account_totals(account_ids, token, start, end)["spend"] > 0


def main():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        print("ERROR: no encontre config.json.")
        sys.exit(1)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    token = get_access_token()
    account_ids = get_account_ids(config)
    since = config["since"]
    until = config["until"]
    if str(until).strip().lower() == "today":
        until = date.today().strftime("%Y-%m-%d")
    until_date = datetime.strptime(until, "%Y-%m-%d").date()

    print(f"Jalando datos de {', '.join(account_ids)}...")

    # A partir de noviembre: semana completa. Antes: mes completo.
    if until_date.month >= 11:
        # Semana completa (lunes a domingo)
        days_since_sunday = (until_date.weekday() + 1) % 7
        if days_since_sunday == 0:
            last_sunday = until_date
        else:
            last_sunday = until_date - timedelta(days=days_since_sunday)
        last_monday = last_sunday - timedelta(days=6)
        kpi_start = last_monday.strftime("%Y-%m-%d")
        kpi_until = last_sunday.strftime("%Y-%m-%d")
        label = f"{last_monday.strftime('%d/%m')} al {last_sunday.strftime('%d/%m/%Y')}"
    else:
        # Mes completo (enero-octubre)
        last_end = until_date.replace(day=1) - timedelta(days=1)
        kpi_start = last_end.replace(day=1).strftime("%Y-%m-%d")
        kpi_until = last_end.strftime("%Y-%m-%d")
        label = f"{MESES[last_end.month - 1]} {last_end.year}"

    # Rellenar meses anteriores (solo los que faltan en archivo/ y que tienen datos).
    backfill_from = config.get("backfill_from")
    if backfill_from:
        archive_dir = Path(__file__).parent / config.get("output_dir", "docs") / "archivo"
        archive_dir.mkdir(exist_ok=True, parents=True)
        marker = archive_dir / "_version.html"
        marker_text = marker.read_text(encoding="utf-8") if marker.exists() else ""
        force = f"backfill v{BACKFILL_VERSION}" not in marker_text
        if force:
            print("Generando todo el historial (primera vez o cambio de version del calculo)...")
        cur = datetime.strptime(backfill_from[:7] + "-01", "%Y-%m-%d").date()
        last_full = datetime.strptime(kpi_start, "%Y-%m-%d").date()
        while cur < last_full:
            slug = cur.strftime("%Y-%m")
            start, end = month_bounds(cur.year, cur.month)
            if (archive_dir / f"{slug}.html").exists() and not force:
                print(f"Archivo {slug} ya existe, se omite.")
            elif not month_has_data(account_ids, token, start.isoformat(), end.isoformat()):
                print(f"{slug}: sin inversion en Meta, no se genera pagina.")
            else:
                print(f"Generando archivo de {slug}...")
                build_report(config, token, account_ids, since, end.isoformat(),
                             start.isoformat(), end.isoformat(),
                             f"{MESES[cur.month - 1]} {cur.year}", write_main=False)
            cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        marker.write_text(f"<!-- backfill v{BACKFILL_VERSION} -->", encoding="utf-8")

    build_report(config, token, account_ids, since, until, kpi_start, kpi_until, label, write_main=True)


if __name__ == "__main__":
    main()
