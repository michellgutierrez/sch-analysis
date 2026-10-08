#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flowline_lbm.py - Reporte ejecutivo de lineas de flujo (Location-Based Management)
a partir de dos cronogramas Primavera P6 en formato XER.

  Eje Y = ubicacion (nivel)      Eje X = tiempo
  Cada banda = una tarea resumen que avanza piso por piso
  (borde izquierdo = inicio en el piso, borde derecho = fin).

Solo usa la libreria estandar de Python 3.8+ (no requiere pandas ni internet).
Genera un HTML autonomo e interactivo y, opcionalmente, un CSV con los datos.

USO
  python flowline_lbm.py LINEA_BASE.xer COMPARADO.xer -o reporte.html
  python flowline_lbm.py a.xer b.xer --label-a "Linea base" --label-b "Phased TCO" \
         --today 2026-10-05 --csv datos.csv
  python flowline_lbm.py a.xer b.xer --lang en --label-a "Baseline" --label-b "Phased TCO"
  python flowline_lbm.py a.xer b.xer --dump-config > mi_config.json   # para editar reglas

  --csv datos.csv escribe dos archivos:
    datos.csv             una fila por banda y nivel, con la lista de Activity IDs
    datos_activities.csv  una fila por actividad de cada cronograma: banda asignada
                          o motivo de exclusion, nombre, WBS, fechas, duracion y holgura
  python flowline_lbm.py a.xer b.xer --config mi_config.json

COMO SE ARMAN LAS LINEAS
  1. El nivel de cada actividad sale del Activity ID (regex "level_regex_code").
  2. Cada actividad se asigna a una tarea resumen con las reglas de "tasks"
     (regex sobre el Activity ID y, opcional, sobre el nombre). Gana la primera que calza.
  3. Por cada tarea y nivel se toma el inicio mas temprano y el fin mas tardio.
     Si la actividad tiene fechas reales (actual start/finish) se usan esas;
     si no, las fechas tempranas (early start/finish).
Las reglas por defecto estan afinadas para la codificacion de Domus II
(C-<nivel>-<zona>-<disciplina>-<nnn>). Para otro proyecto edite el JSON de config.
"""
import argparse
import csv
import datetime as dt
import json
import re
import statistics
import sys

# --------------------------------------------------------------------------- #
# Configuracion por defecto (editable con --config)
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "title": "Domus II Flowline",
    "subtitle": "Líneas de flujo por nivel · gestión basada en ubicación",
    "subtitle_en": "Flowlines by level · location-based management",
    # nivel desde el Activity ID; el grupo 1 debe ser el numero de nivel
    "level_regex_code": r"^C-(\d\d)-",
    # respaldo: nivel desde el nombre (se usa si el ID no calza). "" para desactivar
    "level_regex_name": r"",
    "level_min": 1,
    "level_max": 99,
    "level_labels": {"36": "L36 Pool", "37": "Roof", "38": "Upper roof"},
    # tareas resumen, en orden de construccion. Maximo recomendado: 7
    "tasks": [
        {"id": "estructura", "label": "Estructura",
         "label_en": "Structure", "desc_en": "Formwork, rebar, post-tensioning, slab pour and verticals",
         "desc": "Encofrado, acero, postensado, vaciado de losa y verticales",
         "code": r"^C-\d\d-(ST-1\d\d|MP-120)$"},
        {"id": "envolvente", "label": "Envolvente",
         "label_en": "Envelope", "desc_en": "Exterior masonry and glazing",
         "desc": "Mampostería exterior y vidriería",
         "code": r"^C-\d\d-(ST-4\d\d|EX-1\d\d)$"},
        {"id": "roughin", "label": "Tabiquería y MEP rough-in",
         "label_en": "Framing and MEP rough-in", "desc_en": "Layout, framing, risers and MEP rough-in through inspection",
         "desc": "Trazo, framing, montantes y rough-in MEP hasta inspección",
         "code": r"^C-\d\d-(U|UC|IN)-(FN-1\d\d|MP-1\d\d)$"},
        {"id": "drywall", "label": "Drywall y pintura base",
         "label_en": "Drywall and prime paint", "desc_en": "Drywall, ceilings, taping, doors and first coat",
         "desc": "Cerramiento de drywall, cielos, masillado, puertas y primera mano",
         "code": r"^C-\d\d-(U|UC|IN)-FN-2[0-7]\d$"},
        {"id": "acabados", "label": "Acabados y equipos",
         "label_en": "Finishes and equipment", "desc_en": "Tile, flooring, kitchens, fixtures, equipment, MEP trim and corridors",
         "desc": "Enchapes, pisos, cocinas, aparatos, equipos, trim MEP y corredores",
         "code": r"^C-\d\d-((U|UC|IN)-(FN-(2[89]|3\d|4\d)\d|MP-[2-4]\d\d)|C-(FN-1[0-8]\d|MP-\d+))$"},
        {"id": "punch_gc", "label": "Inspección y punch contratista",
         "label_en": "Inspection and contractor punch", "desc_en": "Partial MEP inspection, subcontractor punch and GC punch",
         "desc": "Inspección parcial MEP, punch de subcontratistas y punch del contratista general",
         "code": r"^C-\d\d-(U|UC|IN)-PC-9[0-2]\d$"},
        {"id": "punch_owner", "label": "Limpieza final y punch Owner",
         "label_en": "Final clean and Owner punch", "desc_en": "Final cleanup and Owner/Architect punch list",
         "desc": "Limpieza final y punch list del propietario y arquitecto",
         "code": r"^C-\d\d-((U|UC|IN)-PC-9[3-9]\d|C-FN-19\d)$"},
    ],
    # tarea cuyo fin representa "piso listo" y tarea previa para medir la espera
    "ready_task": "punch_gc",
    "wait_from_task": "acabados",
    # hitos: codigo de actividad, etiqueta, rango de niveles que cubre (null = todos),
    # "only": "A" o "B" si aplica a un solo cronograma
    "milestones": [
        {"code": "PR-MS-540", "label": "Energía perm.", "label_en": "Perm. power", "levels": None},
        {"code": "PR-MS-200", "label": "MS#1", "levels": [2, 15]},
        {"code": "PR-MS-300", "label": "MS#2", "levels": [16, 25]},
        {"code": "PR-MS-400", "label": "MS#3", "levels": [26, 35]},
        {"code": "PR-MS-800", "label": "TCO", "levels": None, "only": "A"},
        {"code": "PR-MS-800", "label": "TCO#1", "levels": [1, 14], "only": "B"},
        {"code": "PR-MS-910", "label": "TCO#2", "levels": [15, 99], "only": "B"},
    ],
    # comparativo de hitos: "code" si el Activity ID es el mismo en ambos cronogramas,
    # o "code_a" / "code_b" si el hito equivalente tiene distinto ID en cada uno
    "milestone_compare": [
        {"label": "Tope de estructura", "label_en": "Structure top-out", "code": "PR-MS-520"},
        {"label": "Permiso lobby/cocina", "label_en": "Kitchen/lobby permit", "code": "MS-KL-100"},
        {"label": "Energía permanente", "label_en": "Permanent power", "code": "PR-MS-540"},
        {"label": "Retiro de grúa torre", "label_en": "Tower crane removed", "code": "PR-MS-550"},
        {"label": "Vidriería exterior completa", "label_en": "Exterior glazing complete", "code": "PR-MS-570"},
        {"label": "Retiro de buckhoists", "label_en": "Buckhoists removed", "code": "PR-MS-580"},
        {"label": "MS#1 llaves L2–L15", "label_en": "MS#1 key delivery L2–L15", "code": "PR-MS-200"},
        {"label": "MS#2 llaves L16–L25", "label_en": "MS#2 key delivery L16–L25", "code": "PR-MS-300"},
        {"label": "MS#3 llaves L26–L35", "label_en": "MS#3 key delivery L26–L35", "code": "PR-MS-400"},
        {"label": "Medidor de agua y conveyance", "label_en": "Conveyance & water meter", "code": "PR-MS-600"},
        {"label": "TCO pisos 1–14", "label_en": "TCO levels 1–14", "code_a": "PR-MS-800", "code_b": "PR-MS-800"},
        {"label": "TCO pisos 15–36", "label_en": "TCO levels 15–36", "code_a": "PR-MS-800", "code_b": "PR-MS-910"},
        {"label": "Final Completion", "label_en": "Final completion", "code": "PR-MS-900"},
    ],
    # lineas horizontales que separan zonas de entrega (se dibujan encima del nivel indicado)
    "zones": [
        {"above_level": 15, "label": "L2–L15"},
        {"above_level": 25, "label": "L16–L25"},
        {"above_level": 35, "label": "L26–L35"},
    ],
}


# --------------------------------------------------------------------------- #
# Lectura del XER
# --------------------------------------------------------------------------- #
def parse_xer(path, wanted=("PROJECT", "TASK", "PROJWBS")):
    """Devuelve {tabla: [dict, ...]} solo con las tablas pedidas."""
    tables, cur, cols = {}, None, []
    with open(path, encoding="cp1252", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\r\n").split("\t")
            tag = parts[0]
            if tag == "%T":
                cur = parts[1] if parts[1] in wanted else None
                if cur:
                    tables[cur] = []
            elif tag == "%F" and cur:
                cols = parts[1:]
            elif tag == "%R" and cur:
                vals = parts[1:] + [""] * (len(cols) - len(parts) + 1)
                tables[cur].append(dict(zip(cols, vals)))
    if "TASK" not in tables:
        sys.exit("ERROR: %s no contiene la tabla TASK (¿es un XER valido?)" % path)
    return tables


def pdate(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def fnum(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Construccion de las lineas de flujo
# --------------------------------------------------------------------------- #
def build_schedule(path, cfg):
    tb = parse_xer(path)
    proj = tb.get("PROJECT", [{}])[0]
    lv_code = re.compile(cfg["level_regex_code"]) if cfg.get("level_regex_code") else None
    lv_name = re.compile(cfg["level_regex_name"]) if cfg.get("level_regex_name") else None
    rules = []
    for t in cfg["tasks"]:
        rules.append((t["id"],
                      re.compile(t["code"]) if t.get("code") else None,
                      re.compile(t["name"], re.I) if t.get("name") else None))

    wbs = {w.get("wbs_id"): w for w in tb.get("PROJWBS", [])}

    def wbs_path(wid):
        out, seen = [], set()
        while wid in wbs and wid not in seen:
            seen.add(wid)
            w = wbs[wid]
            if w.get("proj_node_flag") != "Y":
                out.append(w.get("wbs_name", ""))
            wid = w.get("parent_wbs_id")
        return " > ".join(reversed(out))

    cells = {}           # (task_id, level) -> dict
    codes = {}           # task_code -> (start, finish, name, cstr, cstr_date, float_hr)
    acts = []            # una fila por actividad, con su banda o el motivo de exclusion
    n_total = n_level = n_class = n_progress = 0
    for r in tb["TASK"]:
        code, name = r.get("task_code", ""), r.get("task_name", "")
        tf = fnum(r.get("total_float_hr_cnt"), None)
        rec = {"code": code, "name": name, "wbs": wbs_path(r.get("wbs_id")), "type": r.get("task_type", ""),
               "status": r.get("status_code", ""), "od": round(fnum(r.get("target_drtn_hr_cnt")) / 8.0, 1),
               "tf": None if tf is None else round(tf / 8.0, 1), "level": None, "band": None, "reason": None}
        acts.append(rec)
        start = pdate(r.get("act_start_date")) or pdate(r.get("early_start_date")) or pdate(r.get("target_start_date"))
        end = pdate(r.get("act_end_date")) or pdate(r.get("early_end_date")) or pdate(r.get("target_end_date"))
        if r.get("status_code") in ("TK_Active", "TK_Complete"):
            n_progress += 1
        n_total += 1
        codes[code] = (start, end, name, r.get("cstr_type", ""), pdate(r.get("cstr_date")), tf)
        rec["start"], rec["finish"] = start, end
        if not start or not end:
            rec["reason"] = "no_dates"
            continue
        level = None
        m = lv_code.search(code) if lv_code else None
        if m:
            level = int(m.group(1))
        elif lv_name:
            m = lv_name.search(name)
            if m:
                level = int(m.group(1))
        rec["level"] = level
        if level is None:
            rec["reason"] = "no_level"
            continue
        if not (cfg["level_min"] <= level <= cfg["level_max"]):
            rec["reason"] = "level_out_of_range"
            continue
        n_level += 1
        for tid, rc, rn in rules:
            if rc and not rc.search(code):
                continue
            if rn and not rn.search(name):
                continue
            if not rc and not rn:
                continue
            n_class += 1
            rec["band"] = tid
            c = cells.setdefault((tid, level), {"s": start, "f": end, "n": 0, "wd": 0.0, "codes": []})
            c["codes"].append((start, code))
            c["s"] = min(c["s"], start)
            c["f"] = max(c["f"], end)
            c["n"] += 1
            c["wd"] += fnum(r.get("target_drtn_hr_cnt")) / 8.0
            break
        else:
            rec["reason"] = "no_band_rule"

    milestones = []
    return {
        "file": path.replace("\\", "/").split("/")[-1],
        "project": proj.get("proj_short_name", ""),
        "data_date": (proj.get("last_recalc_date") or "")[:10],
        "counts": {"total": n_total, "with_level": n_level, "classified": n_class,
                   "with_progress": n_progress},
        "cells": cells, "codes": codes, "milestones": milestones, "acts": acts,
    }


def task_stats(cells, tid):
    """Ritmo de una tarea a traves de los niveles."""
    lv = sorted(l for (t, l) in cells if t == tid)
    if not lv:
        return None
    starts = [cells[(tid, l)]["s"] for l in lv]
    fins = [cells[(tid, l)]["f"] for l in lv]
    durs = [(cells[(tid, l)]["f"] - cells[(tid, l)]["s"]).days + 1 for l in lv]
    # ritmo: dias calendario entre inicios de niveles consecutivos (mediana, robusta a saltos)
    steps = [(cells[(tid, b)]["s"] - cells[(tid, a)]["s"]).days / float(b - a)
             for a, b in zip(lv, lv[1:])]
    return {
        "levels": len(lv), "first": min(starts).isoformat(), "last": max(fins).isoformat(),
        "takt": round(statistics.median(steps), 1) if steps else None,
        "dur": round(statistics.median(durs), 1),
        "work": round(statistics.median([cells[(tid, l)]["wd"] for l in lv]), 1),
    }


def loc(obj, key, lang):
    """Texto en el idioma pedido: usa <key>_<lang> si existe, si no <key>."""
    return obj.get("%s_%s" % (key, lang)) or obj.get(key, "")


def ms_side(s, code):
    hit = s["codes"].get(code) if code else None
    if not hit or not (hit[1] or hit[0]):
        return None
    tf = hit[5]
    return {"code": code, "date": (hit[1] or hit[0]).isoformat(), "name": hit[2],
            "cstr": hit[3] or "", "cstrDate": hit[4].isoformat() if hit[4] else None,
            "tf": None if tf is None else round(tf / 8.0, 1)}


def assemble(a, b, cfg, label_a, label_b, today, lang="es"):
    levels = sorted({l for s in (a, b) for (_, l) in s["cells"]})
    compare = []
    for m in cfg.get("milestone_compare", []):
        sa = ms_side(a, m.get("code_a") or m.get("code"))
        sb = ms_side(b, m.get("code_b") or m.get("code"))
        if sa or sb:
            compare.append({"label": loc(m, "label", lang), "a": sa, "b": sb})
    out = {
        "lang": lang, "msCompare": compare,
        "title": cfg["title"], "subtitle": loc(cfg, "subtitle", lang),
        "generated": dt.date.today().isoformat(), "today": today,
        "levels": levels,
        "levelLabels": {str(l): cfg.get("level_labels", {}).get(str(l), "L%d" % l) for l in levels},
        "tasks": [{"id": t["id"], "label": loc(t, "label", lang), "desc": loc(t, "desc", lang)} for t in cfg["tasks"]],
        "zones": cfg.get("zones", []),
        "readyTask": cfg.get("ready_task"), "waitFrom": cfg.get("wait_from_task"),
        "sched": [],
    }
    for key, s, label in (("A", a, label_a), ("B", b, label_b)):
        cells = {}
        for (tid, l), c in s["cells"].items():
            cells.setdefault(tid, {})[str(l)] = [c["s"].isoformat(), c["f"].isoformat(), c["n"], round(c["wd"], 1)]
        ms = []
        for m in cfg.get("milestones", []):
            if m.get("only") and m["only"] != key:
                continue
            hit = s["codes"].get(m["code"])
            if hit and hit[1]:
                ms.append({"label": loc(m, "label", lang), "date": hit[1].isoformat(),
                           "levels": m.get("levels"), "name": hit[2]})
        out["sched"].append({
            "key": key, "label": label, "file": s["file"], "project": s["project"],
            "dataDate": s["data_date"], "counts": s["counts"], "cells": cells,
            "milestones": ms,
            "stats": {t["id"]: task_stats(s["cells"], t["id"]) for t in cfg["tasks"]},
        })
    return out


CSV_TXT = {
    "en": {"sum": ["schedule", "band", "level", "start", "finish", "activities", "work_days", "activity_ids"],
           "act": ["schedule", "activity_id", "activity_name", "in_band", "band", "level", "excluded_reason",
                   "wbs_path", "activity_type", "status", "start", "finish", "original_duration_d", "total_float_d"],
           "yes": "yes", "no": "no",
           "why": {"no_dates": "no dates", "no_level": "no level in the Activity ID",
                   "level_out_of_range": "level outside the configured range",
                   "no_band_rule": "has a level but matches no band rule"}},
    "es": {"sum": ["cronograma", "banda", "nivel", "inicio", "fin", "actividades", "dias_trabajo", "activity_ids"],
           "act": ["cronograma", "activity_id", "nombre_actividad", "en_banda", "banda", "nivel", "motivo_exclusion",
                   "ruta_wbs", "tipo_actividad", "estado", "inicio", "fin", "duracion_original_d", "holgura_total_d"],
           "yes": "si", "no": "no",
           "why": {"no_dates": "sin fechas", "no_level": "sin nivel en el Activity ID",
                   "level_out_of_range": "nivel fuera del rango configurado",
                   "no_band_rule": "tiene nivel pero no calza con ninguna regla de banda"}},
}


def write_csv(path, data, raws):
    """Escribe el resumen por banda y nivel (con la lista de Activity IDs) y el detalle por actividad."""
    tx = CSV_TXT.get(data.get("lang"), CSV_TXT["es"])
    names = {t["id"]: t["label"] for t in data["tasks"]}
    order = {t["id"]: i for i, t in enumerate(data["tasks"])}
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(tx["sum"])
        for s, raw in zip(data["sched"], raws):
            for (tid, l) in sorted(raw["cells"], key=lambda k: (order.get(k[0], 99), k[1])):
                c = raw["cells"][(tid, l)]
                ids = "; ".join(code for _, code in sorted(c["codes"]))
                w.writerow([s["label"], names[tid], l, c["s"].isoformat(), c["f"].isoformat(), c["n"], round(c["wd"], 1), ids])
    root = path[:-4] if path.lower().endswith(".csv") else path
    path2 = root + "_activities.csv"
    with open(path2, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(tx["act"])
        for s, raw in zip(data["sched"], raws):
            rows = sorted(raw["acts"], key=lambda a: (a["band"] is None, order.get(a["band"], 99),
                                                      a["level"] if a["level"] is not None else 999,
                                                      a.get("start") or dt.date.max, a["code"]))
            for a in rows:
                w.writerow([s["label"], a["code"], a["name"], tx["yes"] if a["band"] else tx["no"],
                            names.get(a["band"], ""), "" if a["level"] is None else a["level"],
                            tx["why"].get(a["reason"], "") if a["reason"] else "", a["wbs"],
                            a["type"], a["status"],
                            a["start"].isoformat() if a.get("start") else "",
                            a["finish"].isoformat() if a.get("finish") else "",
                            a["od"], "" if a["tf"] is None else a["tf"]])
    return path2


# --------------------------------------------------------------------------- #
# Plantilla HTML (autonoma: CSS y JS embebidos)
# --------------------------------------------------------------------------- #
PAGE = r"""<title>__TITLE__</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: lámina de planificación. Encabezado compacto, leyenda única arriba,
   dos paneles de flujo apilados con la misma escala de tiempo, tablas al pie. */
:root{
  --bg:#f3f5f7; --panel:#fcfcfb; --ink:#12171c; --ink2:#46515c; --muted:#77828d;
  --rule:#dde2e7; --band:#f1f4f6; --accent:#1c5cab; --warn:#b4541a;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7;
  --f-body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --f-cond:"IBM Plex Sans Condensed","IBM Plex Sans","Arial Narrow",system-ui,sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#111315; --panel:#1a1a19; --ink:#f1f3f5; --ink2:#bcc3ca; --muted:#8b949d;
    --rule:#2b2f34; --band:#202327; --accent:#6da7ec; --warn:#e89a62;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300; --s7:#9085e9;
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#111315; --panel:#1a1a19; --ink:#f1f3f5; --ink2:#bcc3ca; --muted:#8b949d;
  --rule:#2b2f34; --band:#202327; --accent:#6da7ec; --warn:#e89a62;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300; --s7:#9085e9;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--f-body);font-size:14px;line-height:1.5;margin:0}
.wrap{max-width:1280px;margin:0 auto;padding-inline:20px;padding-block:28px 56px;display:flex;flex-direction:column;gap:22px}
header.top{display:flex;flex-wrap:wrap;gap:8px 28px;align-items:flex-end;justify-content:space-between;border-bottom:2px solid var(--ink);padding-bottom:12px}
h1{font-family:var(--f-cond);font-weight:600;font-size:30px;line-height:1.1;margin:0;letter-spacing:.01em;text-wrap:balance}
.sub{color:var(--ink2);margin:4px 0 0;font-size:14px}
.meta{font-family:var(--f-mono);font-size:11.5px;color:var(--muted);text-align:right;line-height:1.6}
h2{font-family:var(--f-cond);font-weight:600;font-size:13px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink2);margin:0 0 10px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:0;border:1px solid var(--rule);background:var(--panel)}
.kpi{padding:14px 16px;border-right:1px solid var(--rule);min-width:0}
.kpi:last-child{border-right:0}
.kpi .v{font-family:var(--f-cond);font-weight:600;font-size:28px;line-height:1.1;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--ink2);font-size:12.5px;margin-top:4px}
.kpi .d{color:var(--muted);font-size:11.5px;font-family:var(--f-mono);margin-top:4px}
.read{color:var(--ink2);max-width:78ch;margin:0}
.controls{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;justify-content:space-between}
.legend{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--rule);background:var(--panel);color:var(--ink);
  font:500 12.5px var(--f-body);padding:5px 10px 5px 8px;border-radius:3px;cursor:pointer}
.chip i{width:16px;height:3px;border-radius:2px;display:block}
.chip[aria-pressed="false"]{color:var(--muted);background:transparent}
.chip[aria-pressed="false"] i{opacity:.25}
.chip:focus-visible,.tog input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tog{display:inline-flex;gap:7px;align-items:center;color:var(--ink2);font-size:12.5px;cursor:pointer}
.panel{background:var(--panel);border:1px solid var(--rule)}
.panel-h{display:flex;flex-wrap:wrap;gap:4px 16px;justify-content:space-between;align-items:baseline;padding:10px 14px;border-bottom:1px solid var(--rule)}
.panel-h b{font-family:var(--f-cond);font-weight:600;font-size:17px}
.panel-h span{font-family:var(--f-mono);font-size:11px;color:var(--muted)}
.scroll{overflow-x:auto;position:relative}
svg{display:block}
svg text{font-family:var(--f-mono);font-size:10px;fill:var(--muted)}
svg .lvl{fill:var(--ink2)}
svg .yr{font-family:var(--f-cond);font-size:12px;font-weight:600;fill:var(--ink2)}
svg .grid{stroke:var(--rule);stroke-width:1}
svg .gridy{stroke:var(--ink2);stroke-width:1;opacity:.35}
svg .band{fill:var(--band)}
svg .zone{stroke:var(--ink2);stroke-width:1;stroke-dasharray:2 3;opacity:.7}
svg .zl{fill:var(--ink2);font-family:var(--f-cond);font-size:10.5px;font-weight:500;letter-spacing:.04em}
svg .ms{stroke:var(--ink);stroke-width:1.25}
svg .msl{fill:var(--ink);font-family:var(--f-cond);font-size:11px;font-weight:600}
svg .msd{fill:var(--muted);font-size:9.5px}
svg .today{stroke:var(--warn);stroke-width:1;stroke-dasharray:4 3}
svg .todayl{fill:var(--warn);font-family:var(--f-cond);font-size:11px;font-weight:600}
svg .fb{stroke:none;fill-opacity:.17}
svg .fs{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .ff{fill:none;stroke-width:1;stroke-linejoin:round;opacity:.75}
svg .fo{fill:none;stroke-width:1.5;stroke-dasharray:4 3;stroke-linejoin:round}
svg.dim g.fl:not(.hot){opacity:.22}
svg g.fl.hot .fb{fill-opacity:.34}
svg g.fl.hot .fs{stroke-width:3}
svg .hl{fill:var(--ink);opacity:.06}
.tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--ink2);padding:8px 10px;font-size:12px;
  min-width:210px;max-width:280px;box-shadow:0 4px 14px rgba(0,0,0,.14);z-index:5}
.tip b{display:flex;align-items:center;gap:6px;font-family:var(--f-cond);font-size:13.5px}
.tip b i{width:12px;height:3px;border-radius:2px;display:block}
.tip table{border-collapse:collapse;margin-top:5px;width:100%}
.tip td{padding:1px 0;font-family:var(--f-mono);font-size:11px;color:var(--ink2);white-space:nowrap}
.tip td:first-child{padding-right:10px;color:var(--muted)}
.tip .dl{margin-top:5px;font-family:var(--f-mono);font-size:11px;color:var(--ink)}
.tblwrap{overflow-x:auto;background:var(--panel);border:1px solid var(--rule)}
table.t{border-collapse:collapse;width:100%;font-size:12.5px}
table.t th{font:600 11px var(--f-cond);letter-spacing:.07em;text-transform:uppercase;color:var(--ink2);text-align:right;padding:8px 12px;border-bottom:1px solid var(--ink2);white-space:nowrap}
table.t th.g{text-align:center;border-bottom:1px solid var(--rule)}
table.t td{padding:6px 12px;border-bottom:1px solid var(--rule);text-align:right;font-family:var(--f-mono);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
table.t th:first-child,table.t td:first-child{text-align:left}
table.t td:first-child{font-family:var(--f-body);font-size:12.5px}
table.t td .sw{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:8px;vertical-align:middle}
table.t tr:last-child td{border-bottom:0}
.late{color:var(--warn);font-weight:500}
.early{color:var(--accent);font-weight:500}
.lv-scroll{max-height:420px;overflow:auto}
.lv-scroll thead th{position:sticky;top:0;background:var(--panel)}
.note{color:var(--muted);font-size:12px;max-width:90ch;margin:0}
.mskey{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12.5px;color:var(--ink2);margin:0 0 10px}
.mskey span{display:inline-flex;align-items:center;gap:6px}
.mskey svg{display:inline-block}
svg .mrow{fill:var(--ink);font-family:var(--f-body);font-size:12px}
svg .mdelta{fill:var(--ink);font-family:var(--f-mono);font-size:11px;font-weight:500}
svg .mA{fill:var(--panel);stroke:var(--ink);stroke-width:1.75}
svg .mlate{stroke:var(--warn);fill:var(--warn)}
svg .mearly{stroke:var(--accent);fill:var(--accent)}
svg .msame{stroke:var(--ink2);fill:var(--ink2)}
svg .mlink{stroke-width:2.5;stroke-linecap:round}
table.t td.txt{font-family:var(--f-body);font-size:12px;white-space:normal;min-width:150px}
.grid2{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:22px}
@media (max-width:900px){.grid2{grid-template-columns:minmax(0,1fr)} .meta{text-align:left} .kpi{border-right:0;border-bottom:1px solid var(--rule)}}
</style>

<div class="wrap">
  <header class="top">
    <div><h1 id="ttl"></h1><p class="sub" id="sub"></p></div>
    <div class="meta" id="meta"></div>
  </header>

  <section class="kpis" id="kpis" aria-label="KPIs"></section>

  <p class="read" id="read"></p>

  <div class="controls">
    <div class="legend" id="legend" role="group" aria-label="Tasks"></div>
    <label class="tog" for="ovl"><input type="checkbox" id="ovl"> <span id="ovlLbl"></span></label>
  </div>

  <div id="charts" style="display:flex;flex-direction:column;gap:18px"></div>

  <section id="msSec">
    <h2 id="h2m"></h2>
    <p class="read" id="msRead" style="margin-bottom:10px"></p>
    <div class="mskey" id="msKey"></div>
    <div class="panel"><div class="scroll" id="msChart"></div></div>
    <div class="tblwrap" style="margin-top:14px"><table class="t" id="tMs"></table></div>
  </section>

  <div class="grid2">
    <section>
      <h2 id="h2a"></h2>
      <div class="tblwrap"><table class="t" id="tRitmo"></table></div>
    </section>
    <section>
      <h2 id="h2b"></h2>
      <div class="tblwrap lv-scroll"><table class="t" id="tNivel"></table></div>
    </section>
  </div>
  <p class="note" id="note"></p>
</div>

<script>
const D = __DATA__;
(function(){
const TX={
es:{mes:['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'],vs:' frente a ',gen:'Generado ',days:' días',
  read:'Cada banda es una tarea resumen que sube por el edificio. El borde grueso de la izquierda marca cuándo empieza en cada piso y el borde fino de la derecha cuándo termina. Una banda más empinada avanza más rápido; una más ancha permanece más tiempo en cada piso. El espacio en blanco entre dos bandas es un piso sin trabajo de esas tareas.',
  ovl:'Superponer el otro cronograma (punteado)',
  kReady:(B,A)=>'Piso listo en '+B+' frente a '+A,kReadyD:(n,lo,hi)=>'mediana de '+n+' niveles · rango '+lo+' a '+hi,
  kStart:'Inicio de interiores por piso',kMed:'mediana, ',kWait:(a,b)=>'Espera entre '+a+' y '+b,kWaitD:(A,B)=>'mediana por piso, '+A+' → '+B,
  kCyc:'Ciclo de estructura',dfl:'d/piso',kCycD:'días calendario entre pisos, mediana',
  cnt:(c,t)=>c+' de '+t+' actividades en las bandas',prog:n=>n+' con avance real',noprog:'sin avance real cargado',
  aria:'Bandas de flujo por nivel, ',today:'Hoy ',
  tipEnd:(df,O)=>'Fin '+(df===0?'sin cambio':df+' días frente a '+O),tipActs:(n,w)=>n+' actividades · '+w+' días de trabajo',
  task:'Tarea',start:'Inicio',fin:'Fin',dEnd:'Δ fin',level:'Nivel',dDays:'Δ días',wait:'Espera ',h2a:'Ritmo por tarea',h2b:'Piso listo, por nivel',
  note:(rt,wf,B)=>'«Piso listo» es el fin de «'+rt+'». «Espera» son los días calendario entre el fin de «'+wf+'» y el inicio de «'+rt+'» en el mismo piso. d/piso es la mediana de días calendario entre inicios de pisos consecutivos. Fechas tempranas del cronograma, o reales cuando la actividad tiene avance. Δ positivo significa más tarde en '+B+'.',
  h2m:'Movimiento de hitos',msRead:(A,B)=>'Cada fila es un hito. El círculo vacío es su fecha en '+A+' y el círculo lleno su fecha en '+B+'. La línea que los une es el desplazamiento, en días calendario.',
  later:'más tarde',earlier:'antes',same:'sin cambio',ms:'Hito',shift:'Desplazamiento',cstr:'Restricción',flt:'Holgura (d)',none:'ninguna',msAria:'Movimiento de hitos',
  onlyIn:s=>'solo en '+s,
  C:{CS_MEOB:'Terminar a más tardar el',CS_MEO:'Terminar el',CS_MEOA:'Terminar no antes del',CS_MSO:'Iniciar el',CS_MSOA:'Iniciar no antes del',CS_MSOB:'Iniciar a más tardar el',CS_MANDFIN:'Fin obligatorio',CS_MANDSTART:'Inicio obligatorio',CS_ALAP:'Lo más tarde posible'}},
en:{mes:['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],vs:' vs ',gen:'Generated ',days:' days',
  read:'Each band is a summary task moving up the building. The thick left edge marks when it starts on each floor and the thin right edge when it finishes. A steeper band moves faster; a wider band stays longer on each floor. White space between two bands is a floor with none of those tasks under way.',
  ovl:'Overlay the other schedule (dashed)',
  kReady:(B,A)=>'Floor ready in '+B+' vs '+A,kReadyD:(n,lo,hi)=>'median of '+n+' levels · range '+lo+' to '+hi,
  kStart:'Interior start per floor',kMed:'median, ',kWait:(a,b)=>'Wait between '+a+' and '+b,kWaitD:(A,B)=>'median per floor, '+A+' → '+B,
  kCyc:'Structure cycle',dfl:'d/floor',kCycD:'calendar days between floors, median',
  cnt:(c,t)=>c+' of '+t+' activities in the bands',prog:n=>n+' with actual progress',noprog:'no actual progress loaded',
  aria:'Flow bands by level, ',today:'Today ',
  tipEnd:(df,O)=>'Finish '+(df===0?'unchanged':df+' days vs '+O),tipActs:(n,w)=>n+' activities · '+w+' work days',
  task:'Task',start:'Start',fin:'Finish',dEnd:'Δ finish',level:'Level',dDays:'Δ days',wait:'Wait, ',h2a:'Pace by task',h2b:'Floor ready, by level',
  note:(rt,wf,B)=>'“Floor ready” is the finish of “'+rt+'”. “Wait” is the calendar days between the finish of “'+wf+'” and the start of “'+rt+'” on the same floor. d/floor is the median calendar days between starts on consecutive floors. Dates are early dates from the schedule, or actual dates where an activity has progress. A positive Δ means later in '+B+'.',
  h2m:'Milestone movement',msRead:(A,B)=>'Each row is a milestone. The hollow circle is its date in '+A+' and the filled circle its date in '+B+'. The line between them is the shift, in calendar days.',
  later:'later',earlier:'earlier',same:'no change',ms:'Milestone',shift:'Shift',cstr:'Constraint',flt:'Float (d)',none:'none',msAria:'Milestone movement',
  onlyIn:s=>'only in '+s,
  C:{CS_MEOB:'Finish on or before',CS_MEO:'Finish on',CS_MEOA:'Finish on or after',CS_MSO:'Start on',CS_MSOA:'Start on or after',CS_MSOB:'Start on or before',CS_MANDFIN:'Mandatory finish',CS_MANDSTART:'Mandatory start',CS_ALAP:'As late as possible'}}
}[D.lang]||null;
const MES=TX.mes;
const day=864e5, P=s=>{const a=s.split('-');return Date.UTC(+a[0],+a[1]-1,+a[2])};
const fd=s=>{if(!s)return '–';const a=s.split('-');return a[2]+'-'+MES[+a[1]-1]+'-'+a[0].slice(2)};
const dd=(a,b)=>Math.round((P(b)-P(a))/day);
const sgn=n=>(n>0?'+':'')+n;
const NS='http://www.w3.org/2000/svg';
const el=(n,at,tx)=>{const e=document.createElementNS(NS,n);for(const k in at)e.setAttribute(k,at[k]);if(tx!=null)e.textContent=tx;return e};
const col=i=>'var(--s'+(i+1)+')';
const T=D.tasks, L=D.levels, A=D.sched[0], B=D.sched[1];
const on={}; T.forEach(t=>on[t.id]=true);
let overlay=false;

document.getElementById('ttl').textContent=D.title;
document.getElementById('sub').textContent=D.subtitle+' · '+A.label+TX.vs+B.label;
document.getElementById('read').textContent=TX.read;document.getElementById('ovlLbl').textContent=TX.ovl;
document.getElementById('h2a').textContent=TX.h2a;document.getElementById('h2b').textContent=TX.h2b;
document.getElementById('meta').innerHTML=[A,B].map(s=>s.label+': '+s.file.replace(/^[0-9a-f]{8}-/,'')+' · data date '+fd(s.dataDate)).join('<br>')+'<br>'+TX.gen+fd(D.generated);

/* ---------- dominio de tiempo ---------- */
let t0=Infinity,t1=-Infinity;
D.sched.forEach(s=>{for(const k in s.cells)for(const l in s.cells[k]){t0=Math.min(t0,P(s.cells[k][l][0]));t1=Math.max(t1,P(s.cells[k][l][1]))}
  s.milestones.forEach(m=>{t1=Math.max(t1,P(m.date))})});
{const a=new Date(t0);t0=Date.UTC(a.getUTCFullYear(),a.getUTCMonth(),1);const b=new Date(t1);t1=Date.UTC(b.getUTCFullYear(),b.getUTCMonth()+1,1)}

/* ---------- indicadores ---------- */
function med(a){if(!a.length)return null;a=a.slice().sort((x,y)=>x-y);const m=a.length>>1;return a.length%2?a[m]:(a[m-1]+a[m])/2}
const rt=D.readyTask, wf=D.waitFrom;
function cell(s,t,l){return (s.cells[t]||{})[l]}
const dReady=[],waitA=[],waitB=[],dStart=[];
L.forEach(l=>{const a=cell(A,rt,l),b=cell(B,rt,l);if(a&&b)dReady.push(dd(a[1],b[1]));
  const wa=cell(A,wf,l),wb=cell(B,wf,l);if(a&&wa)waitA.push(dd(wa[1],a[0]));if(b&&wb)waitB.push(dd(wb[1],b[0]));
  const ra=cell(A,'roughin',l),rb=cell(B,'roughin',l);if(ra&&rb)dStart.push(dd(ra[0],rb[0]))});
const tl=id=>{const s=(T.find(t=>t.id===id)||{label:id}).label;return s.charAt(0).toLowerCase()+s.slice(1)};
const k=[];
if(dReady.length){const m=med(dReady);k.push([sgn(Math.round(m))+TX.days,TX.kReady(B.label,A.label),TX.kReadyD(dReady.length,sgn(Math.min(...dReady)),sgn(Math.max(...dReady)))])}
if(dStart.length){const m=med(dStart);k.push([sgn(Math.round(m))+TX.days,TX.kStart,TX.kMed+tl('roughin')])}
if(waitA.length&&waitB.length){k.push([Math.round(med(waitA))+' → '+Math.round(med(waitB))+TX.days,TX.kWait(tl(wf),tl(rt)),TX.kWaitD(A.label,B.label)])}
{const sa=A.stats.estructura,sb=B.stats.estructura;if(sa&&sb&&sa.takt!=null)k.push([sa.takt+' → '+sb.takt+' '+TX.dfl,TX.kCyc,TX.kCycD])}
document.getElementById('kpis').innerHTML=k.map(x=>'<div class="kpi"><div class="v">'+x[0]+'</div><div class="l">'+x[1]+'</div><div class="d">'+x[2]+'</div></div>').join('');

/* ---------- leyenda ---------- */
const lg=document.getElementById('legend');
T.forEach((t,i)=>{const b=document.createElement('button');b.className='chip';b.type='button';b.setAttribute('aria-pressed','true');b.title=t.desc;
  b.innerHTML='<i style="background:'+col(i)+'"></i>'+t.label;
  b.onclick=()=>{on[t.id]=!on[t.id];b.setAttribute('aria-pressed',on[t.id]);draw()};lg.appendChild(b)});
document.getElementById('ovl').onchange=e=>{overlay=e.target.checked;draw()};

/* ---------- gráfico ---------- */
const host=document.getElementById('charts');
const RH=13, M={l:76,r:64,t:76,b:30};
function draw(){
  host.innerHTML='';
  const W=Math.max(900,host.clientWidth-2);
  D.sched.forEach((S,si)=>host.appendChild(panel(S,D.sched[1-si],W)));
}
function panel(S,O,W){
  const H=M.t+L.length*RH+M.b, iw=W-M.l-M.r;
  const x=t=>M.l+(t-t0)/(t1-t0)*iw;
  const yTop=i=>M.t+(L.length-1-i)*RH, yBot=i=>yTop(i)+RH;
  const box=document.createElement('section');box.className='panel';
  const c=S.counts;
  box.innerHTML='<div class="panel-h"><b>'+S.label+'</b><span>'+TX.cnt(c.classified,c.total)+' · '+(c.with_progress?TX.prog(c.with_progress):TX.noprog)+'</span></div>';
  const sc=document.createElement('div');sc.className='scroll';box.appendChild(sc);
  const svg=el('svg',{width:W,height:H,viewBox:'0 0 '+W+' '+H,role:'img','aria-label':TX.aria+S.label});sc.appendChild(svg);
  /* bandas y niveles */
  L.forEach((l,i)=>{if(i%2===0)svg.appendChild(el('rect',{class:'band',x:M.l,y:yTop(i),width:iw,height:RH}));
    svg.appendChild(el('text',{class:'lvl',x:M.l-8,y:yTop(i)+RH/2+3.5,'text-anchor':'end'},D.levelLabels[l]))});
  /* tiempo */
  for(let d=new Date(t0);d.getTime()<=t1;d.setUTCMonth(d.getUTCMonth()+1)){
    const m=d.getUTCMonth(),X=x(d.getTime());
    if(m%3===0)svg.appendChild(el('line',{class:m===0?'gridy':'grid',x1:X,x2:X,y1:M.t,y2:H-M.b}));
    if(m%3===0&&d.getTime()<t1)svg.appendChild(el('text',{x:X+3,y:H-M.b+12},MES[m]));
    if((m===0||d.getTime()===t0)&&d.getTime()<t1)svg.appendChild(el('text',{class:'yr',x:X+3,y:H-M.b+25},d.getUTCFullYear()));
  }
  svg.appendChild(el('line',{class:'gridy',x1:M.l,x2:M.l+iw,y1:H-M.b,y2:H-M.b}));
  /* zonas */
  let prevY=H-M.b;
  D.zones.forEach(z=>{const i=L.indexOf(z.above_level);if(i<0)return;const Y=yTop(i);
    svg.appendChild(el('line',{class:'zone',x1:M.l,x2:M.l+iw+6,y1:Y,y2:Y}));
    svg.appendChild(el('text',{class:'zl',x:M.l+iw+8,y:(Y+prevY)/2+4},z.label));prevY=Y});
  /* resaltado de fila */
  const hl=el('rect',{class:'hl',x:M.l,y:0,width:iw,height:RH,visibility:'hidden'});svg.appendChild(hl);
  /* hoy */
  if(D.today&&P(D.today)>t0&&P(D.today)<t1){const X=x(P(D.today));
    svg.appendChild(el('line',{class:'today',x1:X,x2:X,y1:M.t-6,y2:H-M.b}));
    svg.appendChild(el('text',{class:'todayl',x:X,y:H-4,'text-anchor':'middle'},TX.today+fd(D.today)))}
  /* líneas de flujo */
  /* tramos contiguos de niveles donde la tarea existe */
  function runs(sch,tid){const out=[];let cur=null;
    L.forEach((l,i)=>{const c=cell(sch,tid,l);if(!c){cur=null;return}if(!cur){cur=[];out.push(cur)}cur.push([i,x(P(c[0])),x(P(c[1])+day)])});return out}
  /* frente de avance: pasa por la mitad de cada nivel y se prolonga a los bordes del tramo */
  function front(run,k){const n=run.length,p=[[run[0][k],yBot(run[0][0])]];
    run.forEach(r=>p.push([r[k],yTop(r[0])+RH/2]));p.push([run[n-1][k],yTop(run[n-1][0])]);return p}
  const pts=p=>p.map(q=>q[0].toFixed(1)+' '+q[1].toFixed(1)).join('L');
  const lines={};
  if(overlay)T.forEach((t,i)=>{if(!on[t.id])return;runs(O,t.id).forEach(r=>{
    svg.appendChild(el('path',{class:'fo',d:'M'+pts(front(r,1)),stroke:col(i),opacity:.5}));
    svg.appendChild(el('path',{class:'fo',d:'M'+pts(front(r,2)),stroke:col(i),opacity:.9}))})});
  T.forEach((t,i)=>{if(!on[t.id])return;const rs=runs(S,t.id);if(!rs.length)return;const g=el('g',{class:'fl'});
    rs.forEach(r=>{const a=front(r,1),b=front(r,2);
      g.appendChild(el('path',{class:'fb',d:'M'+pts(a)+'L'+pts(b.slice().reverse())+'Z',fill:col(i)}));
      g.appendChild(el('path',{class:'ff',d:'M'+pts(b),stroke:col(i)}));
      g.appendChild(el('path',{class:'fs',d:'M'+pts(a),stroke:col(i)}))});
    svg.appendChild(g);lines[t.id]=g});
  /* hitos */
  const rows=[[],[],[]];
  S.milestones.slice().sort((a,b)=>P(a.date)-P(b.date)).forEach(m=>{
    const X=x(P(m.date)+day);let lo=0,hi=L.length-1;
    if(m.levels){const inr=L.map((l,i)=>l>=m.levels[0]&&l<=m.levels[1]?i:-1).filter(i=>i>=0);if(!inr.length)return;lo=inr[0];hi=inr[inr.length-1]}
    svg.appendChild(el('line',{class:'ms',x1:X,x2:X,y1:yTop(hi),y2:yBot(lo)}));
    svg.appendChild(el('line',{class:'grid',x1:X,x2:X,y1:M.t-4,y2:yTop(hi)}));
    const w=Math.max(m.label.length*6.4,52)+8;let r=0;
    while(r<2&&rows[r].some(p=>Math.abs(p[0]-X)<(p[1]+w)/2))r++;
    rows[r].push([X,w]);const Y=M.t-20-r*23;
    svg.appendChild(el('line',{class:'grid',x1:X,x2:X,y1:Y+12,y2:M.t-4}));
    svg.appendChild(el('text',{class:'msl',x:X,y:Y,'text-anchor':'middle'},m.label));
    svg.appendChild(el('text',{class:'msd',x:X,y:Y+10,'text-anchor':'middle'},fd(m.date)))});
  /* interacción */
  const tip=document.createElement('div');tip.className='tip';tip.hidden=true;sc.appendChild(tip);
  const hit=el('rect',{x:M.l,y:M.t,width:iw,height:L.length*RH,fill:'transparent'});svg.appendChild(hit);
  function leave(){tip.hidden=true;hl.setAttribute('visibility','hidden');svg.classList.remove('dim');for(const k in lines)lines[k].classList.remove('hot')}
  hit.addEventListener('pointerleave',leave);
  hit.addEventListener('pointermove',ev=>{
    const r=svg.getBoundingClientRect(),px=ev.clientX-r.left,py=ev.clientY-r.top;
    const i=L.length-1-Math.floor((py-M.t)/RH);if(i<0||i>=L.length){leave();return}
    const l=L[i],tt=t0+(px-M.l)/iw*(t1-t0);let best=null,bd=Infinity;
    T.forEach((t,ti)=>{if(!on[t.id])return;const c=cell(S,t.id,l);if(!c)return;
      const a=P(c[0]),b=P(c[1])+day;let d;
      if(tt>=a&&tt<=b)d=-1e15+(b-a);            /* dentro de la banda: gana la más angosta */
      else d=Math.min(Math.abs(tt-a),Math.abs(tt-b));
      if(d<bd){bd=d;best=[t,ti,c]}});
    if(!best||(bd>0&&bd/(t1-t0)*iw>40)){leave();return}
    const [t,ti,c]=best,o=cell(O,t.id,l);
    hl.setAttribute('y',yTop(i));hl.setAttribute('visibility','visible');
    svg.classList.add('dim');for(const k in lines)lines[k].classList.toggle('hot',k===t.id);
    let h='<b><i style="background:'+col(ti)+'"></i>'+t.label+' · '+D.levelLabels[l]+'</b><table>'+
      '<tr><td>'+S.label+'</td><td>'+fd(c[0])+' → '+fd(c[1])+'</td></tr>';
    if(o)h+='<tr><td>'+O.label+'</td><td>'+fd(o[0])+' → '+fd(o[1])+'</td></tr>';
    h+='</table>';
    if(o){const df=dd(o[1],c[1]);h+='<div class="dl">'+TX.tipEnd(df===0?0:sgn(df),O.label)+'</div>'}
    h+='<div class="dl" style="color:var(--muted)">'+TX.tipActs(c[2],c[3])+'</div>';
    tip.innerHTML=h;tip.hidden=false;
    const tw=tip.offsetWidth,th=tip.offsetHeight;let lx=px+sc.scrollLeft+14,ly=py+14;
    if(px+14+tw>sc.clientWidth)lx=px+sc.scrollLeft-tw-14;if(ly+th>H)ly=py-th-10;
    tip.style.left=Math.max(4,lx)+'px';tip.style.top=Math.max(4,ly)+'px'});
  return box;
}
draw();
let rz;window.addEventListener('resize',()=>{clearTimeout(rz);rz=setTimeout(draw,120)});

/* ---------- tablas ---------- */
const cls=n=>n>0?'late':(n<0?'early':'');
let h='<thead><tr><th rowspan="2">'+TX.task+'</th><th class="g" colspan="3">'+A.label+'</th><th class="g" colspan="3">'+B.label+'</th><th rowspan="2">'+TX.dEnd+'</th></tr>'+
  '<tr>'+('<th>'+TX.start+'</th><th>'+TX.fin+'</th><th>'+TX.dfl+'</th>').repeat(2)+'</tr></thead><tbody>';
T.forEach((t,i)=>{const a=A.stats[t.id],b=B.stats[t.id];if(!a&&!b)return;const df=a&&b?dd(a.last,b.last):null;
  h+='<tr><td title="'+t.desc+'"><span class="sw" style="background:'+col(i)+'"></span>'+t.label+'</td>'+
  [a,b].map(s=>s?'<td>'+fd(s.first)+'</td><td>'+fd(s.last)+'</td><td>'+(s.takt==null?'–':s.takt)+'</td>':'<td>–</td><td>–</td><td>–</td>').join('')+
  '<td class="'+(df==null?'':cls(df))+'">'+(df==null?'–':sgn(df))+'</td></tr>'});
document.getElementById('tRitmo').innerHTML=h+'</tbody>';

h='<thead><tr><th>'+TX.level+'</th><th>'+A.label+'</th><th>'+B.label+'</th><th>'+TX.dDays+'</th><th>'+TX.wait+B.label+'</th></tr></thead><tbody>';
L.slice().reverse().forEach(l=>{const a=cell(A,rt,l),b=cell(B,rt,l);if(!a&&!b)return;const df=a&&b?dd(a[1],b[1]):null;
  const wb=cell(B,wf,l),w=b&&wb?dd(wb[1],b[0]):null;
  h+='<tr><td>'+D.levelLabels[l]+'</td><td>'+(a?fd(a[1]):'–')+'</td><td>'+(b?fd(b[1]):'–')+'</td><td class="'+(df==null?'':cls(df))+'">'+(df==null?'–':sgn(df))+'</td><td>'+(w==null?'–':w+' d')+'</td></tr>'});
document.getElementById('tNivel').innerHTML=h+'</tbody>';
document.getElementById('note').textContent=TX.note(tl(rt),tl(wf),B.label);

/* ---------- movimiento de hitos ---------- */
(function(){
  const MC=(D.msCompare||[]).slice().sort((p,q)=>P((p.a||p.b).date)-P((q.a||q.b).date));
  const sec=document.getElementById('msSec');if(!MC.length){sec.hidden=true;return}
  document.getElementById('h2m').textContent=TX.h2m;document.getElementById('msRead').textContent=TX.msRead(A.label,B.label);
  const dot=(c)=>'<svg width="14" height="14"><circle cx="7" cy="7" r="4.5" class="'+c+'"/></svg>';
  const bar=(c)=>'<svg width="22" height="14"><line x1="2" y1="7" x2="20" y2="7" class="mlink '+c+'"/></svg>';
  document.getElementById('msKey').innerHTML='<span>'+dot('mA')+A.label+'</span><span>'+dot('msame')+B.label+'</span><span>'+bar('mlate')+TX.later+'</span><span>'+bar('mearly')+TX.earlier+'</span>';
  const cst=s=>!s?'–':(s.cstr?(TX.C[s.cstr]||s.cstr)+(s.cstrDate?' '+fd(s.cstrDate):''):TX.none);
  const dl=m=>m.a&&m.b?dd(m.a.date,m.b.date):null;
  const kc=n=>n>0?'mlate':(n<0?'mearly':'msame');
  const host2=document.getElementById('msChart');
  function drawMs(){
    host2.innerHTML='';
    const W=Math.max(820,host2.clientWidth),RH2=28,m2={l:210,r:84,t:24,b:34},H=m2.t+MC.length*RH2+m2.b,iw=W-m2.l-m2.r;
    let a0=Infinity,a1=-Infinity;MC.forEach(m=>[m.a,m.b].forEach(s=>{if(s){a0=Math.min(a0,P(s.date));a1=Math.max(a1,P(s.date))}}));
    {const a=new Date(a0);a0=Date.UTC(a.getUTCFullYear(),a.getUTCMonth()-1,1);const b=new Date(a1);a1=Date.UTC(b.getUTCFullYear(),b.getUTCMonth()+2,1)}
    const x=t=>m2.l+(t-a0)/(a1-a0)*iw;
    const svg=el('svg',{width:W,height:H,viewBox:'0 0 '+W+' '+H,role:'img','aria-label':TX.msAria});host2.appendChild(svg);
    MC.forEach((m,i)=>{if(i%2===0)svg.appendChild(el('rect',{class:'band',x:0,y:m2.t+i*RH2,width:W,height:RH2}))});
    for(let d=new Date(a0);d.getTime()<=a1;d.setUTCMonth(d.getUTCMonth()+1)){
      const mo=d.getUTCMonth(),X=x(d.getTime());
      if(mo%3===0){svg.appendChild(el('line',{class:mo===0?'gridy':'grid',x1:X,x2:X,y1:m2.t,y2:H-m2.b}));
        if(d.getTime()<a1)svg.appendChild(el('text',{x:X+3,y:H-m2.b+12},MES[mo]))}
      if((mo===0||d.getTime()===a0)&&d.getTime()<a1)svg.appendChild(el('text',{class:'yr',x:X+3,y:H-m2.b+26},d.getUTCFullYear()));
    }
    if(D.today&&P(D.today)>a0&&P(D.today)<a1){const X=x(P(D.today));svg.appendChild(el('line',{class:'today',x1:X,x2:X,y1:m2.t,y2:H-m2.b}));
      svg.appendChild(el('text',{class:'todayl',x:X+5,y:m2.t-8},TX.today+fd(D.today)))}
    const tip=document.createElement('div');tip.className='tip';tip.hidden=true;host2.appendChild(tip);
    MC.forEach((m,i)=>{
      const y=m2.t+i*RH2+RH2/2,n=dl(m),c=n==null?'msame':kc(n);
      svg.appendChild(el('text',{class:'mrow',x:m2.l-12,y:y+4,'text-anchor':'end'},m.label));
      const xa=m.a?x(P(m.a.date)):null,xb=m.b?x(P(m.b.date)):null;
      const near=xa!=null&&xb!=null&&Math.abs(xa-xb)<9;
      if(xa!=null&&xb!=null&&!near)svg.appendChild(el('line',{class:'mlink '+c,x1:xa,x2:xb,y1:y,y2:y}));
      if(xb!=null)svg.appendChild(el('circle',{class:c,cx:xb,cy:y,r:5,stroke:'var(--panel)','stroke-width':2,style:'stroke:var(--panel)'}));
      if(xa!=null)svg.appendChild(el('circle',{class:'mA',cx:near?xb:xa,cy:y,r:near?8:5,style:near?'fill:none':''}));
      const xr=Math.max(xa==null?-1:xa,xb==null?-1:xb)+(near?3:0);
      svg.appendChild(el('text',{class:'mdelta',x:xr+11,y:y+4},n==null?TX.onlyIn(m.a?A.label:B.label):(n===0?TX.same:sgn(n)+' d')));
      const hit=el('rect',{x:0,y:m2.t+i*RH2,width:W,height:RH2,fill:'transparent'});svg.appendChild(hit);
      hit.addEventListener('pointerleave',()=>{tip.hidden=true});
      hit.addEventListener('pointermove',ev=>{
        const r=svg.getBoundingClientRect(),px=ev.clientX-r.left,py=ev.clientY-r.top;
        let h='<b>'+m.label+'</b><table>';
        [[A,m.a],[B,m.b]].forEach(([S,s])=>{h+='<tr><td>'+S.label+'</td><td>'+(s?fd(s.date):'–')+'</td></tr>'});h+='</table>';
        if(n!=null)h+='<div class="dl">'+(n===0?TX.same:sgn(n)+TX.days+' · '+(n>0?TX.later:TX.earlier))+'</div>';
        [[A,m.a],[B,m.b]].forEach(([S,s])=>{if(s)h+='<div class="dl" style="color:var(--muted);white-space:normal">'+S.label+': '+cst(s)+(s.tf==null?'':' · '+TX.flt.replace(' (d)','')+' '+s.tf+' d')+'</div>'});
        tip.innerHTML=h;tip.hidden=false;const tw=tip.offsetWidth,th=tip.offsetHeight;
        let lx=px+host2.scrollLeft+14,ly=py+14;if(px+14+tw>host2.clientWidth)lx=px+host2.scrollLeft-tw-14;if(ly+th>H)ly=py-th-10;
        tip.style.left=Math.max(4,lx)+'px';tip.style.top=Math.max(4,ly)+'px'});
    });
  }
  drawMs();let rz2;window.addEventListener('resize',()=>{clearTimeout(rz2);rz2=setTimeout(drawMs,140)});
  let h='<thead><tr><th rowspan="2">'+TX.ms+'</th><th rowspan="2">'+A.label+'</th><th rowspan="2">'+B.label+'</th><th rowspan="2">'+TX.shift+'</th>'+
    '<th class="g" colspan="2">'+TX.cstr+'</th><th class="g" colspan="2">'+TX.flt+'</th></tr><tr><th>'+A.label+'</th><th>'+B.label+'</th><th>'+A.label+'</th><th>'+B.label+'</th></tr></thead><tbody>';
  MC.forEach(m=>{const n=dl(m);
    h+='<tr><td>'+m.label+'</td><td>'+(m.a?fd(m.a.date):'–')+'</td><td>'+(m.b?fd(m.b.date):'–')+'</td>'+
      '<td class="'+(n==null?'':cls(n))+'">'+(n==null?'–':(n===0?'0':sgn(n)+' d · '+(n>0?TX.later:TX.earlier)))+'</td>'+
      '<td class="txt">'+cst(m.a)+'</td><td class="txt">'+cst(m.b)+'</td>'+
      '<td>'+(m.a&&m.a.tf!=null?m.a.tf:'–')+'</td><td>'+(m.b&&m.b.tf!=null?m.b.tf:'–')+'</td></tr>'});
  document.getElementById('tMs').innerHTML=h+'</tbody>';
})();
})();
</script>
"""

DOC = """<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head><body>
%s
</body></html>
"""


def render(data, fragment=False):
    page = PAGE.replace("__TITLE__", data["title"]).replace(
        "__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    return page if fragment else DOC.replace("__LANG__", data.get("lang", "es")) % page


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lineas de flujo (LBM) comparando dos cronogramas XER.")
    ap.add_argument("xer_a", help="XER de referencia (p. ej. linea base)")
    ap.add_argument("xer_b", help="XER a comparar (p. ej. cronograma revisado)")
    ap.add_argument("-o", "--output", default="flowline_lbm.html", help="HTML de salida")
    ap.add_argument("--label-a", default="Línea base")
    ap.add_argument("--label-b", default="Cronograma revisado")
    ap.add_argument("--title", help="Titulo del reporte")
    ap.add_argument("--lang", choices=("es", "en"), default="es", help="Idioma del reporte (es o en)")
    ap.add_argument("--today", help="Fecha AAAA-MM-DD para la linea 'Hoy' (omitir para no dibujarla)")
    ap.add_argument("--config", help="JSON con reglas (ver --dump-config)")
    ap.add_argument("--dump-config", action="store_true", help="Imprime la configuracion por defecto y termina")
    ap.add_argument("--csv", help="Exporta tambien los datos de las lineas a CSV")
    ap.add_argument("--fragment", action="store_true", help="HTML sin <html>/<body> (para incrustar)")
    args = ap.parse_args(argv)

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.dump_config:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    if args.title:
        cfg["title"] = args.title
    if args.today:
        dt.datetime.strptime(args.today, "%Y-%m-%d")

    a = build_schedule(args.xer_a, cfg)
    b = build_schedule(args.xer_b, cfg)
    data = assemble(a, b, cfg, args.label_a, args.label_b, args.today, args.lang)
    if not data["levels"]:
        sys.exit("ERROR: ninguna actividad calzo con las reglas. Revise level_regex_code y tasks (--dump-config).")

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(render(data, args.fragment))
    if args.csv:
        p2 = write_csv(args.csv, data, (a, b))
        print("CSV: %s  +  %s" % (args.csv, p2))

    for s in data["sched"]:
        c = s["counts"]
        print("%-22s %5d actividades | %5d con nivel | %5d en lineas de flujo | data date %s"
              % (s["label"], c["total"], c["with_level"], c["classified"], s["dataDate"]))
    print("Niveles: %s a %s (%d)  ->  %s" % (data["levels"][0], data["levels"][-1], len(data["levels"]), args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
