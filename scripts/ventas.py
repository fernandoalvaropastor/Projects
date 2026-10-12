#!/usr/bin/env python3
"""ventas.py - ritmo de salidas, curva de supervivencia, precios y censo de salidas.

ADITIVO: no toca contar.py ni registros.py. Lee data/propiedades.json, data/historico.csv y
data/precios.csv (que escribe contar.py), y escribe:
  - data/ventas.json          (el bloque que la pagina Inventory Watch lee como panel.ventas)
  - data/exit_census.json     (que dice la ficha de cada casa que salio del sitemap)
  - data/panel.json           (se le anade la clave "ventas"; el resto queda INTACTO)
Uso: python scripts/ventas.py        (VENTAS_CENSO=0 para saltar el censo; VENTAS_MAX_FICHAS=60)
Etiqueta honesta: una salida del sitemap es "left the listing (~contract)", NUNCA "sold".
"""
import gzip, json, os, re, sys, time, urllib.error, urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATOS = os.environ.get("VENTAS_DATOS", os.path.join(RAIZ, "data"))
UA = ("opendoor-inventory-counter/3.1 (investigacion personal; fichas de casas que salieron; "
      "github.com/fernandoalvaropastor/Projects)")
MAX_FICHAS = int(os.environ.get("VENTAS_MAX_FICHAS", "60"))
CENSO_ON = os.environ.get("VENTAS_CENSO", "1") != "0"
BINS = [(0, 7), (8, 14), (15, 30), (31, 60), (61, 90), (91, 120), (121, 180), (181, 9999)]
D = date.fromisoformat


def cargar(nombre, defecto):
    ruta = os.path.join(DATOS, nombre)
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return defecto


def csv_filas(nombre):
    ruta = os.path.join(DATOS, nombre)
    if not os.path.exists(ruta):
        return []
    with open(ruta, encoding="utf-8") as f:
        lineas = [l.rstrip("\n") for l in f if l.strip()]
    cab = lineas[0].split(",")
    return [dict(zip(cab, l.split(","))) for l in lineas[1:]]


# ------------------------------------------------------------------ censo de salidas
RE_CONTRATO = re.compile(r"\b(in contract|under contract|sale pending|contingent)\b", re.I)
RE_PENDIENTE = re.compile(r"(?<!price )\bpending\b", re.I)
RE_VENDIDA = re.compile(r"\bsold\b", re.I)
RE_FUERA = re.compile(r"\b(off market|no longer (?:available|for sale)|withdrawn)\b", re.I)
MESES = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
RE_FECHA = r"((?:%s)[a-z]*\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})" % MESES
RE_EVENTO_PRECIO = re.compile(RE_FECHA + r"\s*[-–|·:]?\s*(sale pending|pending|under contract|contingent|sold)\b[^$]{0,60}\$\s?([\d,]{5,9})", re.I)


def texto_visible(html):
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", html)
    for a, b in (("&amp;", "&"), ("&nbsp;", " "), ("&#36;", "$"), ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t)


def a_fecha(s):
    s = s.replace(".", "")
    from datetime import datetime
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:  # noqa: BLE001
            continue
    return None


def clasificar_ficha(html):
    """No decide 'vendida'. Devuelve evidencia: etiqueta de contrato, fragmentos y evento con precio."""
    t = texto_visible(html)
    sn = []
    for rx, nombre in ((RE_CONTRATO, "contrato"), (RE_PENDIENTE, "pendiente"), (RE_FUERA, "fuera"), (RE_VENDIDA, "vendida_palabra")):
        for m in rx.finditer(t):
            sn.append({"k": nombre, "t": t[max(0, m.start() - 70): m.end() + 90]})
            if len([s for s in sn if s["k"] == nombre]) >= 2:
                break
    ev = None
    for m in RE_EVENTO_PRECIO.finditer(t):
        f = a_fecha(m.group(1))
        if f:
            ev = {"f": f, "e": m.group(2).lower(), "p": int(m.group(3).replace(",", ""))}
            break  # el primero de la lista de historial es el mas reciente
    return {"contrato": bool(RE_CONTRATO.search(t)), "evento": ev, "snippets": sn[:6]}


def descargar(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=20) as r:
            crudo = r.read()
            if r.headers.get("Content-Encoding") == "gzip" or crudo[:2] == b"\x1f\x8b":
                crudo = gzip.decompress(crudo)
            return r.status, crudo.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def censo(props, hoy):
    reg = cargar("exit_census.json", {})
    if not CENSO_ON:
        return reg
    pend = [(a, v) for a, v in props.items() if not v.get("activa") and v.get("salida") and a not in reg
            and (hoy - D(v["salida"])).days <= 14 and v.get("url")]
    pend.sort(key=lambda x: x[1]["salida"], reverse=True)
    fallos = 0
    for aid, v in pend[:MAX_FICHAS]:
        st, html = descargar(v["url"])
        if st in (404, 410):
            reg[aid] = {"s": v["salida"], "http": st, "contrato": False, "evento": None, "snippets": []}
        elif st == 200 and html:
            r = clasificar_ficha(html); r.update({"s": v["salida"], "http": 200}); reg[aid] = r
        else:
            fallos += 1
            if fallos >= 8 and not any(x.get("http") == 200 for x in reg.values()):
                print("censo: demasiados fallos seguidos, se para", file=sys.stderr); break
        time.sleep(0.5)
    with open(os.path.join(DATOS, "exit_census.json"), "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return reg


# ------------------------------------------------------------------ calculo
def calcular(props, reg):
    hoy = D(max(v["ultima"] for v in props.values()))
    hist = {r["fecha"]: r for r in csv_filas("historico.csv")}
    fechas = sorted(hist)
    prim = fechas[0] if fechas else None
    serie = []
    for f in fechas:
        if f == prim:
            continue
        r = hist[f]; serie.append({"f": f, "off": int(r["retirados"]), "on": int(r["nuevos"]), "tot": int(r["total"])})
    for i, s in enumerate(serie):
        w = serie[max(0, i - 6): i + 1]; s["mm7"] = round(sum(x["off"] for x in w) / len(w), 1)
    wk = defaultdict(int); cn = defaultdict(int)
    for s in serie:
        d = D(s["f"]); m = (d - timedelta(days=d.weekday())).isoformat(); wk[m] += s["off"]; cn[m] += 1
    semanas = [{"s": m, "off": wk[m], "dias": cn[m]} for m in sorted(wk)]

    ex = [v for v in props.values() if not v.get("activa") and v.get("salida")]
    reapar = sum(1 for v in props.values() if v.get("reapariciones"))
    gross = sum(s["off"] for s in serie) or 1
    ret = min(0.5, reapar / gross)
    # --- supervivencia (hazard por edad, fecha de listado conocida)
    def b(a):
        for i, (lo, hi) in enumerate(BINS):
            if lo <= a <= hi:
                return i
    pd = Counter(); ev = Counter(); pdA = pdK = 0; evA = len(ex); evK = 0
    for v in props.values():
        ini = D(v["primera"]); fin = D(v["salida"]) if not v.get("activa") and v.get("salida") else hoy
        n = (fin - ini).days + (0 if not v.get("activa") else 1); pdA += max(n, 0)
        ld = v.get("fecha_listado_mls")
        if not ld:
            continue
        L = D(ld); pdK += max(n, 0)
        for k in range(max(n, 0)):
            pd[b(max((ini + timedelta(days=k) - L).days, 0))] += 1
        if not v.get("activa") and v.get("salida"):
            evK += 1; ev[b(max((D(v["salida"]) - L).days, 0))] += 1
    sup = None
    if evK and pdK and evA:
        k = (evA / evK) / (pdA / pdK)
        raw = [ev[i] / pd[i] if pd[i] else 0 for i in range(len(BINS))]
        cor = [h * k for h in raw]
        def curva(hz):
            S = 1; out = []
            for a in range(181):
                S *= (1 - min(hz[b(a)], 0.99)); out.append(round((1 - S) * 100, 2))
            return out
        cc, cr = curva(cor), curva(raw)
        med = lambda c: next((i for i, x in enumerate(c) if x >= 50), None)
        sup = {"edades": list(range(181)), "est": cc, "cota": cr,
               "hitos": {str(m): {"raw": round(cr[m], 1), "corr": round(cc[m], 1)} for m in (7, 14, 30, 60, 90, 120, 180)},
               "bins": [list(x) for x in BINS], "haz": [round(h * 100, 3) for h in cor], "haz_cota": [round(h * 100, 3) for h in raw],
               "mediana_est": med(cc), "mediana_cota": med(cr),
               "base": {"salidas": evA, "con_fecha": evK, "activas_con_fecha": sum(1 for v in props.values() if v.get("activa") and v.get("fecha_listado_mls")),
                        "activas": sum(1 for v in props.values() if v.get("activa"))}}
    # --- precios
    act = []
    for r in csv_filas("precios.csv"):
        try:
            if int(r["cobertura"]) >= 500:
                act.append({"f": r["fecha"], "medio": round(float(r["precio_medio"])), "mediana": round(float(r["precio_mediana"])),
                            "n": int(r["cobertura"]), "conc_pct": float(r["con_concesion_pct"] or 0), "conc_med": float(r["concesion_media"] or 0)})
        except Exception:  # noqa: BLE001
            continue
    por_dia = defaultdict(list)
    for v in ex:
        if v.get("precio_lista"):
            por_dia[v["salida"]].append(v["precio_lista"])
    dias = sorted(por_dia); rol = []
    for d in dias:
        w = [x for dd in dias if 0 <= (D(d) - D(dd)).days <= 6 for x in por_dia[dd]]
        if len(w) >= 15:
            rol.append({"f": d, "medio": round(sum(w) / len(w)), "n": len(w)})
    pe = [v["precio_lista"] for v in ex if v.get("precio_lista")]
    pa = [v["precio_lista"] for v in props.values() if v.get("activa") and v.get("precio_lista")]
    import statistics as st
    # precio de contrato leido de las fichas (censo)
    pv = defaultdict(list)
    for a, r in reg.items():
        e = r.get("evento")
        if e and e.get("p") and e.get("e") in ("sale pending", "pending", "under contract", "contingent"):
            pv[r["s"]].append(e["p"])
    venta = []
    for d in sorted(pv):
        w = [x for dd in pv if 0 <= (D(d) - D(dd)).days <= 6 for x in pv[dd]]
        if len(w) >= 10:
            venta.append({"f": d, "medio": round(sum(w) / len(w)), "n": len(w)})
    cs = Counter()
    for r in reg.values():
        cs["fichas"] += 1
        cs["contrato" if r.get("contrato") else ("404" if r.get("http") in (404, 410) else "sin_marca")] += 1
    g = [s["off"] for s in serie]
    esc = {"low": round(sum(g[-7:]) / max(len(g[-7:]), 1) * (1 - ret), 1), "base": round(sum(g) / max(len(g), 1) * (1 - ret), 1),
           "high": round(sum(g[:7]) / max(len(g[:7]), 1) * (1 - ret), 1)}
    out = {"version": 1, "generado": date.today().isoformat(), "ventana": [serie[0]["f"], serie[-1]["f"]] if serie else [],
           "serie": serie, "semanas": semanas, "supervivencia": sup,
           "precios": {"activas": act, "salidas": rol, "venta": venta,
                       "salidas_total": {"media": st.mean(pe) if pe else None, "mediana": st.median(pe) if pe else None, "n": len(pe)},
                       "activas_total": {"media": st.mean(pa) if pa else None, "mediana": st.median(pa) if pa else None, "n": len(pa)}},
           "escenarios": esc, "retorno_pct": round(ret * 100, 1), "censo": dict(cs)}
    return out


def main():
    props = cargar("propiedades.json", {})
    if not props:
        print("ventas.py: sin propiedades.json, nada que hacer"); return
    hoy = D(max(v["ultima"] for v in props.values()))
    reg = censo(props, hoy)
    out = calcular(props, reg)
    with open(os.path.join(DATOS, "ventas.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    panel = cargar("panel.json", None)
    if panel is not None:
        panel["ventas"] = out
        with open(os.path.join(DATOS, "panel.json"), "w", encoding="utf-8") as f:
            json.dump(panel, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    sup = out["supervivencia"] or {}
    print("ventas.py: %d dias, %d salidas, mediana %s d, censo %s" % (len(out["serie"]), (sup.get("base") or {}).get("salidas", 0), sup.get("mediana_est"), out["censo"]))


if __name__ == "__main__":
    main()
