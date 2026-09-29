#!/usr/bin/env python3
"""
Contador propio del inventario publicado de Opendoor ($OPEN) -- v3.

Lee el sitemap publico de listados, cuenta las casas que la empresa tiene
anunciadas, las reparte por estado y codigo postal, y mantiene un registro
por propiedad con la primera y la ultima vez que se vio.

v2: visita hasta MAX_FICHAS_POR_DIA fichas de propiedad al dia y saca precio
de lista, precio por pie cuadrado, pago estimado, concesion de hipoteca y la
fecha real de publicacion.

v3 (28-sep-2026) anade, sin tocar lo anterior:
  * Flujos por estado (altas y bajas de cada estado) y reapariciones.
  * Reparto de antiguedad de lo que sigue listado y de lo que sale.
  * Cohortes semanales: de lo que entro cada semana, que % ha salido del
    listado a los 7 / 30 / 60 / 90 dias (la "velocidad de salida").
  * Valor que sale del listado (suma del ultimo precio de lista conocido),
    retencion de precio (precio final / precio inicial) y recortes del dia.
  * Actividad del sitemap: cuantas fichas cambiaron su <lastmod> hoy.
  * De cada ficha, ademas: habitaciones, banos, ano de construccion, tipo,
    quien la lista, estado (en venta / pendiente...), historial de precios
    y si la casa ya se habia intentado vender antes y se retiro.
  * Empleo: las vacantes abiertas en opendoor.com/careers, por departamento
    y con marcador de las que son de hipoteca / escrow / title.
  * Protecciones: un cortacircuitos (si las primeras fichas fallan todas,
    se deja de insistir) y un tope de tiempo, para que el recuento principal
    nunca se quede sin guardar por culpa de las fichas.
  * Muestras de depuracion en data/debug/ (una ficha y la pagina de empleo)
    para poder afinar los extractores leyendo el HTML real desde fuera.

Fuente: https://www.opendoor.com/sitemaps/listings.xml (permitido por robots.txt)
Las fichas y /careers tambien estan permitidas por robots.txt (solo bloquea
cuentas, paneles y /api/).
"""

import gzip
import html as html_mod
import io
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone, date, timedelta

SITEMAP = "https://www.opendoor.com/sitemaps/listings.xml"
CARRERAS = "https://www.opendoor.com/careers/open-positions"
UA = ("opendoor-inventory-counter/3.0 (investigacion personal; 1 sitemap + "
      "hasta 600 fichas de propiedad al dia; github.com/fernandoalvaropastor/Projects)")
DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

MAX_FICHAS_POR_DIA = 600
PAUSA_ENTRE_FICHAS = float(os.environ.get("REGISTROS_PAUSA", "0.3"))  # segundos
TOPE_MINUTOS_FICHAS = 35        # como mucho este tiempo visitando fichas
CORTACIRCUITOS = 20             # si las primeras N fichas fallan TODAS, se para

# /properties/<Calle>-<Ciudad>-<ST>-<CP>/aid_<uuid>
PATRON = re.compile(r"-([A-Z]{2})-(\d{5})(?:-\d{4})?/aid_([0-9a-fA-F-]{8,})\s*$")

TRAMOS_EDAD = [(0, 7, "0-7"), (8, 30, "8-30"), (31, 60, "31-60"),
               (61, 90, "61-90"), (91, 120, "91-120"), (121, 100000, "120+")]
TRAMOS_PRECIO = [(0, 200000, "<200k"), (200000, 300000, "200-300k"),
                 (300000, 400000, "300-400k"), (400000, 500000, "400-500k"),
                 (500000, 750000, "500-750k"), (750000, 10 ** 9, "750k+")]

MESES = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
EVENTOS = ("for sale|listed|relisted|withdrawn|delisted|expired|cancell?ed|"
           "pending|contingent|under contract|sold|price change|price decrease|"
           "price increase|price cut|price reduced|off market|coming soon")
PATRON_EVENTO = re.compile(
    r"((?:" + MESES + r")[a-z]*\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"
    r"\s*[-–|·:]?\s*(" + EVENTOS + r")\b\s*(?:[-–|·:]\s*[A-Z][A-Za-z0-9&]{1,20}\s*)?[-–|·:]?\s*(?:\$\s?([\d,]{4,9}))?",
    re.I)
RETIRADAS = ("withdrawn", "delisted", "expired", "cancelled", "canceled", "off market")


# ----------------------------------------------------------------- red

MOCK = os.environ.get("REGISTROS_MOCK", "").rstrip("/")   # solo para pruebas locales


def url_real(url):
    if not MOCK:
        return url
    import urllib.parse as up
    p = up.urlparse(url)
    return f"{MOCK}/{p.netloc}{p.path}" + (f"?{p.query}" if p.query else "")


def descargar(url, intentos=4):
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url_real(url), headers={
                "User-Agent": UA,
                "Accept": "application/xml,text/xml,*/*",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=120) as r:
                crudo = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or crudo[:2] == b"\x1f\x8b":
                    crudo = gzip.decompress(crudo)
                return crudo
        except Exception as e:  # noqa: BLE001
            ultimo = e
            if i < intentos - 1:
                time.sleep(5 * (i + 1))
    raise SystemExit(f"No se pudo descargar {url}: {ultimo}")


CODIGOS_HTTP = Counter()


def descargar_ficha(url, intentos=2, espera=15):
    """Descarga una pagina. Falla en silencio (None): una ficha que falla hoy
    se reintenta manana sola. Apunta el codigo de error para el resumen."""
    for i in range(intentos):
        try:
            req = urllib.request.Request(url_real(url), headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=espera) as r:
                crudo = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or crudo[:2] == b"\x1f\x8b":
                    crudo = gzip.decompress(crudo)
                CODIGOS_HTTP[str(r.status)] += 1
                return crudo
        except urllib.error.HTTPError as e:
            CODIGOS_HTTP[str(e.code)] += 1
            if e.code in (403, 404, 410):
                return None          # no sirve reintentar
        except Exception as e:  # noqa: BLE001
            CODIGOS_HTTP[type(e).__name__] += 1
        if i < intentos - 1:
            time.sleep(2)
    return None


# ----------------------------------------------------------------- utilidades

def a_int(s):
    try:
        return int(str(s).replace(",", "").replace("$", "").strip().split(".")[0])
    except Exception:  # noqa: BLE001
        return None


def a_fecha(s):
    s = (s or "").strip().replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:  # noqa: BLE001
            continue
    # "Sept 3, 2026"
    m = re.match(r"([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4})", s)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%b %d, %Y").date()
        except Exception:  # noqa: BLE001
            return None
    return None


def tramo(v, tramos):
    """Tramos de edad: limites inclusivos (0-7, 8-30...). Tramos de precio:
    semiabiertos [lo, hi)."""
    inclusivo = tramos is TRAMOS_EDAD
    for lo, hi, et in tramos:
        if (lo <= v <= hi) if inclusivo else (lo <= v < hi):
            return et
    return tramos[-1][2]


def reparto(valores, tramos):
    c = Counter(tramo(v, tramos) for v in valores)
    return {et: c.get(et, 0) for _, _, et in tramos}


def mediana(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def media(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def scripts_json(texto):
    """Todos los bloques JSON embebidos: JSON-LD y __NEXT_DATA__ / similares."""
    ld, otros = [], []
    for m in re.finditer(r'<script([^>]*)>(.*?)</script>', texto, re.S | re.I):
        attrs, cuerpo = m.group(1), m.group(2).strip()
        if not cuerpo or cuerpo[0] not in "{[":
            continue
        try:
            obj = json.loads(cuerpo)
        except Exception:  # noqa: BLE001
            continue
        if "ld+json" in attrs.lower():
            ld.append(obj)
        else:
            otros.append((attrs, obj))
    return ld, otros


def recorrer(obj, ruta="", profundidad=0):
    """Genera (ruta, clave, valor) para todo el arbol JSON."""
    if profundidad > 14:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            r = f"{ruta}.{k}" if ruta else str(k)
            yield r, k, v
            yield from recorrer(v, r, profundidad + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:200]):
            yield from recorrer(v, f"{ruta}[{i}]", profundidad + 1)


CLAVES = {
    "precio_lista_json": ("listprice", "list_price", "listingprice"),
    "habitaciones": ("beds", "bedrooms", "numberofbedrooms", "bedroomcount"),
    "banos": ("baths", "bathrooms", "numberofbathroomstotal", "bathroomcount", "bathstotal"),
    "sqft_json": ("sqft", "squarefeet", "square_feet", "livingarea", "livingareasqft", "floorsize"),
    "ano_construccion": ("yearbuilt", "year_built"),
    "estado_json": ("listingstatus", "homestatus", "listing_status", "status"),
    "tipo_casa": ("hometype", "home_type", "propertytype", "property_type"),
}


def valor_escalar(v):
    if isinstance(v, dict):
        for k in ("value", "amount", "price"):
            if k in v and not isinstance(v[k], (dict, list)):
                return v[k]
        return None
    if isinstance(v, list):
        return None
    return v


# ----------------------------------------------------------------- fichas

def extraer_ficha(cuerpo):
    """Extrae todo lo que se pueda de una ficha. Devuelve un dict con precio de
    lista, o un dict con estado_ficha='proximamente' (casa de Opendoor aun sin
    precio), o None si la pagina no se puede interpretar."""
    try:
        texto = cuerpo.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None

    plano = re.sub(r"<script.*?</script>|<style.*?</style>", " ", texto, flags=re.S | re.I)
    plano = re.sub(r"<[^>]+>", " ", plano)
    plano = html_mod.unescape(plano)
    plano = re.sub(r"\s+", " ", plano)

    r = {}

    # --- precio (misma logica que v2: el "$X below list" es el 1% fijo de marketing)
    m = re.search(r"\$([\d,]{4,9})[^$]{0,60}?\$([\d,]{2,7})\s+below list", plano, re.I)
    if m:
        r["precio_lista"] = a_int(m.group(1)) + a_int(m.group(2))
        r["precio_estimado"] = False
    else:
        m2 = re.search(r"\$([\d,]{2,7})\s+below list", plano, re.I)
        if m2:
            r["precio_lista"] = a_int(m2.group(1)) * 100
            r["precio_estimado"] = True

    # --- JSON embebido (JSON-LD y datos de la app): complementa al texto
    ld, otros = scripts_json(texto)
    encontrados = {}
    for bloque in ld + [o for _, o in otros]:
        for _, k, v in recorrer(bloque):
            kl = str(k).lower()
            for campo, alias in CLAVES.items():
                if campo in encontrados:
                    continue
                if kl in alias:
                    val = valor_escalar(v)
                    if val not in (None, "", 0):
                        encontrados[campo] = val
            if kl == "offers" and isinstance(v, dict) and "precio_ld" not in encontrados:
                p = a_int(v.get("price"))
                if p:
                    encontrados["precio_ld"] = p

    if "precio_lista" not in r:
        for campo in ("precio_lista_json", "precio_ld"):
            p = a_int(encontrados.get(campo))
            if p and 20000 <= p <= 5000000:
                r["precio_lista"] = p
                r["precio_estimado"] = False
                r["precio_fuente"] = campo
                break

    if "precio_lista" in r and not (20000 <= r["precio_lista"] <= 5000000):
        r.pop("precio_lista", None)

    # --- resto de campos: primero texto, si no, JSON
    m = re.search(r"\$([\d,]{2,6})\s*/\s*sq\.?\s?ft", plano, re.I)
    if m and r.get("precio_lista"):
        ppsf = a_int(m.group(1))
        if ppsf and 10 <= ppsf <= 2000:
            r["precio_por_sqft"] = ppsf
            r["sqft"] = round(r["precio_lista"] / ppsf)
    if "sqft" not in r:
        m = re.search(r"(?<![\$/\d,])([\d,]{3,6})\s*(?:sq\.?\s?ft|sqft|square feet)", plano, re.I)
        s = a_int(m.group(1)) if m else a_int(encontrados.get("sqft_json"))
        if s and 300 <= s <= 15000:
            r["sqft"] = s
            if r.get("precio_lista"):
                r["precio_por_sqft"] = round(r["precio_lista"] / s)

    m = re.search(r"\$([\d,]{3,6})\s*/\s*mo(?:nth)?\b", plano, re.I)
    if m:
        r["pago_estimado_mes"] = a_int(m.group(1))

    m = re.search(r"\$([\d,]{3,6})\s+MORTGAGE CONCESSION", plano, re.I)
    if m:
        r["concesion_hipoteca"] = a_int(m.group(1))

    m = re.search(r"(\d{1,2}(?:\.\d)?)\s*(?:bd|beds?|bedrooms?)\b", plano, re.I)
    v = m.group(1) if m else encontrados.get("habitaciones")
    try:
        if v is not None and 0 < float(v) < 20:
            r["habitaciones"] = float(v)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:ba|baths?|bathrooms?)\b", plano, re.I)
    v = m.group(1) if m else encontrados.get("banos")
    try:
        if v is not None and 0 < float(v) < 20:
            r["banos"] = float(v)
    except Exception:  # noqa: BLE001
        pass

    m = re.search(r"(?:year built|built in)\s*:?\s*(1[89]\d\d|20[0-3]\d)", plano, re.I)
    ano = a_int(m.group(1)) if m else a_int(encontrados.get("ano_construccion"))
    if ano and 1850 <= ano <= 2030:
        r["ano_construccion"] = ano

    m = re.search(r"(?:home type|property type)\s*:?\s*(single[- ]family|townhouse|townhome|condo|"
                  r"multi[- ]family|manufactured)", plano, re.I)
    if m:
        r["tipo_casa"] = m.group(1).lower().replace("-", " ")
    elif encontrados.get("tipo_casa"):
        r["tipo_casa"] = str(encontrados["tipo_casa"]).lower()[:40]

    m = re.search(r"Listed by\s*:?\s*((?:[A-Z][\w&.'-]*,?\s?){1,5})", plano)
    if m:
        quien = re.sub(r"\s+(?:Price|Status|Est|Year|Home|Tour|Schedule|Contact|Listing|Days)\b.*$", "",
                       m.group(1)).strip(" ,.")
        r["listada_por"] = "Opendoor" if quien.lower().startswith("opendoor") else quien[:60]

    # estado de la ficha: solo si viene rotulado, para no confundirlo con el historial
    m = re.search(r"\b(?:Status|Listing status)\s*:?\s*(For sale|Active|Pending|Under contract|Contingent|"
                  r"Sold|Off market|Coming soon)", plano, re.I)
    est = m.group(1) if m else encontrados.get("estado_json")
    if est:
        e = str(est).lower()
        if "pend" in e or "contract" in e or "contingent" in e:
            r["estado_ficha"] = "pendiente"
        elif "sold" in e or "closed" in e:
            r["estado_ficha"] = "vendida"
        elif "off" in e or "withdraw" in e or "inactive" in e:
            r["estado_ficha"] = "fuera"
        elif "coming" in e:
            r["estado_ficha"] = "proximamente"
        elif "sale" in e or "active" in e or "listed" in e:
            r["estado_ficha"] = "en_venta"

    # historial de precios
    eventos = []
    for m in PATRON_EVENTO.finditer(plano):
        f = a_fecha(m.group(1))
        if not f:
            continue
        eventos.append({"f": f.isoformat(), "e": m.group(2).lower(), "p": a_int(m.group(3)) if m.group(3) else None})
    vistos, unicos = set(), []
    for ev in eventos:
        clave = (ev["f"], ev["e"])
        if clave not in vistos:
            vistos.add(clave)
            unicos.append(ev)
    unicos.sort(key=lambda x: x["f"], reverse=True)
    if unicos:
        r["historial"] = unicos[:12]
        altas = [ev for ev in unicos if ev["e"] in ("for sale", "listed", "relisted")]
        if altas:
            r["fecha_listado_mls"] = altas[0]["f"]
            fl = date.fromisoformat(altas[0]["f"])
            previas = [ev for ev in unicos
                       if ev["e"] in RETIRADAS and date.fromisoformat(ev["f"]) < fl
                       and (fl - date.fromisoformat(ev["f"])).days <= 3 * 365]
            r["retirada_previa"] = bool(previas)
            if previas:
                r["retirada_previa_fecha"] = previas[0]["f"]
    if "fecha_listado_mls" not in r:
        m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})[^a-zA-Z]{0,12}for sale", plano)
        if m:
            f = a_fecha(m.group(1))
            if f:
                r["fecha_listado_mls"] = f.isoformat()

    # --- v3.1: casas "Available soon" (compradas por Opendoor, aun sin precio ni a la venta)
    # Verificado el 29-sep-2026: la ficha dice "Price pending" y "Available soon", con el
    # aviso de que esas casas son propiedad de Opendoor. Casi la mitad de las fichas sin
    # precio de la v2 eran de este tipo.
    proxima = bool(re.search(r"\bprice pending\b", plano, re.I)) or (
        "precio_lista" not in r and bool(re.search(r"\bavailable soon\b", plano, re.I)))
    if proxima:
        r["estado_ficha"] = "proximamente"
        r.pop("precio_lista", None)
        r.pop("precio_estimado", None)
        previsto = [ev for ev in unicos if ev["e"] == "coming soon" and ev.get("p")]
        if previsto:
            r["precio_previsto"] = previsto[0]["p"]
            r["fecha_proximamente"] = previsto[0]["f"]
        return r

    # --- si no hay precio con el "below list", el ultimo precio del historial de venta
    if "precio_lista" not in r and unicos:
        hoy_d = date.today()
        for ev in unicos:
            if ev["e"] in ("for sale", "listed", "relisted", "price change", "price decrease",
                           "price increase", "price cut", "price reduced") and ev.get("p"):
                if (hoy_d - date.fromisoformat(ev["f"])).days <= 400 and 20000 <= ev["p"] <= 5000000:
                    r["precio_lista"] = ev["p"]
                    r["precio_estimado"] = False
                    r["precio_fuente"] = "historial"
                break
    if "precio_lista" not in r:
        return None
    if r.get("sqft") and not r.get("precio_por_sqft"):
        r["precio_por_sqft"] = round(r["precio_lista"] / r["sqft"])
    r.setdefault("estado_ficha", "en_venta")
    return r


def estructura_debug(cuerpo):
    """Resumen de la estructura de una pagina, para afinar extractores."""
    texto = cuerpo.decode("utf-8", "ignore")
    ld, otros = scripts_json(texto)
    rutas = []
    for attrs, obj in otros[:4]:
        for ruta, k, v in recorrer(obj):
            if isinstance(v, (dict, list)):
                continue
            muestra = str(v)[:60]
            rutas.append(f"{ruta} = {muestra}")
            if len(rutas) > 1500:
                break
    return {
        "bytes": len(cuerpo),
        "jsonld": ld[:5],
        "scripts_json_attrs": [a for a, _ in otros][:10],
        "rutas_json": rutas,
        "texto_plano_inicio": re.sub(r"\s+", " ", html_mod.unescape(
            re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", " ",
                                             texto, flags=re.S | re.I))))[:6000],
    }


def guardar_debug(nombre, cuerpo, hoy, cada_dias=7):
    meta = cargar(os.path.join("debug", "meta.json"), {})
    ult = meta.get(nombre)
    if ult and (date.fromisoformat(hoy) - date.fromisoformat(ult)).days < cada_dias:
        return
    ruta = os.path.join(DATOS, "debug")
    os.makedirs(ruta, exist_ok=True)
    with open(os.path.join(ruta, nombre + ".html"), "wb") as f:
        f.write(cuerpo[:400_000])
    guardar(os.path.join("debug", nombre + "_estructura.json"), estructura_debug(cuerpo))
    meta[nombre] = hoy
    guardar(os.path.join("debug", "meta.json"), meta)


# ----------------------------------------------------------------- empleo

DEPTOS = ("Research & Development", "Operations", "Finance", "Legal", "Pricing", "People",
          "Growth & Marketing", "Sales & Partnership", "Open Services", "Business Analytics & Development",
          "Design", "Engineering", "Product", "Customer Experience", "Mortgage", "Title", "Escrow")


def contar_empleo(hoy):
    cuerpo = descargar_ficha(CARRERAS, espera=30)
    if not cuerpo:
        return None
    guardar_debug("empleo_muestra", cuerpo, hoy)
    texto = cuerpo.decode("utf-8", "ignore")
    puestos = []

    # 1) datos estructurados embebidos: la lista de dicts mas larga con titulo
    _, otros = scripts_json(texto)
    mejor = []
    for _, obj in otros:
        for _, k, v in recorrer(obj):
            if isinstance(v, list) and len(v) > len(mejor) and v and all(isinstance(x, dict) for x in v[:5]):
                claves = {str(c).lower() for c in v[0].keys()}
                if ({"title", "name", "text"} & claves) and (
                        {"location", "locations", "department", "departments", "team", "categories",
                         "locationname", "departmentname"} & claves):
                    mejor = v
    for x in mejor:
        t = x.get("title") or x.get("name") or x.get("text")
        d = x.get("department") or x.get("departmentName") or x.get("team")
        if not d and isinstance(x.get("categories"), dict):
            d = x["categories"].get("team") or x["categories"].get("department")
        if isinstance(d, dict):
            d = d.get("name")
        if isinstance(d, list):
            d = d[0].get("name") if d and isinstance(d[0], dict) else (d[0] if d else None)
        loc = x.get("location") or x.get("locationName") or x.get("locations")
        if isinstance(loc, dict):
            loc = loc.get("name")
        if isinstance(loc, list):
            loc = ", ".join(str(l.get("name") if isinstance(l, dict) else l) for l in loc[:4])
        if t:
            puestos.append({"t": str(t)[:90], "d": (str(d)[:50] if d else None), "l": (str(loc)[:80] if loc else None)})
    metodo = "json embebido" if puestos else None

    # 2) texto: titulos de departamento seguidos de puestos (enlaces)
    if not puestos:
        dep_actual = None
        for m in re.finditer(r"<(h[1-6])[^>]*>(.*?)</\1>|<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", texto, re.S | re.I):
            if m.group(1):
                h = html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).strip()
                h = re.sub(r"\s*\(\d+.*\)$", "", re.sub(r"\s+", " ", h))
                if any(h.lower().startswith(d.lower()) for d in DEPTOS):
                    dep_actual = h
                continue
            href, a = m.group(3), m.group(4)
            if not re.search(r"(careers/|jobs?/|ashbyhq|greenhouse|lever\.co)", href, re.I):
                continue
            t = re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", a))).strip()
            if not t or len(t) > 140 or t.lower() in ("browse careers", "apply today", "open positions",
                                                      "careers", "apply"):
                continue
            puestos.append({"t": t[:90], "d": dep_actual, "l": None})
        metodo = "enlaces" if puestos else None

    if not puestos:
        return {"total": None, "metodo": None,
                "nota": "No se pudo leer la lista de vacantes; ver data/debug/empleo_muestra_estructura.json"}

    vistos, unicos = set(), []
    for p in puestos:
        k = (p["t"], p.get("l"))
        if k not in vistos:
            vistos.add(k)
            unicos.append(p)
    claves = Counter()
    for p in unicos:
        t = (p["t"] + " " + (p.get("d") or "")).lower()
        if "mortgage" in t or "loan" in t or "lending" in t or "underwrit" in t:
            claves["hipoteca"] += 1
        if "escrow" in t or "title" in t or "os national" in t or "closing" in t:
            claves["escrow_title"] += 1
        if "pricing" in t or "portfolio" in t:
            claves["pricing"] += 1
        if "renovation" in t or "homes project" in t or "field" in t:
            claves["operaciones_casas"] += 1
    return {
        "total": len(unicos),
        "por_departamento": dict(Counter((p.get("d") or "Sin departamento") for p in unicos).most_common()),
        "claves": dict(claves),
        "puestos": unicos[:80],
        "metodo": metodo,
    }


# ----------------------------------------------------------------- sitemap

def parsear(crudo):
    if crudo[:2] == b"\x1f\x8b":
        crudo = gzip.decompress(crudo)
    props, sin_patron = [], 0
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    for _, elem in ET.iterparse(io.BytesIO(crudo), events=("end",)):
        if elem.tag != ns + "url" and elem.tag != "url":
            continue
        loc = elem.findtext(ns + "loc") or elem.findtext("loc") or ""
        lastmod = elem.findtext(ns + "lastmod") or elem.findtext("lastmod") or ""
        m = PATRON.search(loc.strip())
        if m:
            props.append({
                "aid": m.group(3).lower(),
                "estado": m.group(1),
                "cp": m.group(2),
                "url": loc.strip(),
                "lastmod": lastmod.strip(),
            })
        elif loc.strip():
            sin_patron += 1
        elem.clear()
    return props, sin_patron


def cargar(nombre, defecto):
    ruta = os.path.join(DATOS, nombre)
    if not os.path.exists(ruta):
        return defecto
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return defecto


def guardar(nombre, obj, compacto=False):
    ruta = os.path.join(DATOS, nombre)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        if compacto:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def anadir_csv(nombre, cabecera, filas, fecha):
    ruta = os.path.join(DATOS, nombre)
    previas = []
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            lineas = [l.rstrip("\n") for l in f if l.strip()]
        previas = [l for l in lineas[1:] if not l.startswith(fecha + ",")]
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(cabecera + "\n")
        for l in previas + list(filas):
            f.write(l + "\n")


def elegir_para_precio(registro, ids_hoy, nuevos_hoy, maximo, hoy=None):
    """Orden: lo nuevo de hoy; lo nunca comprobado; las 'Available soon' que llevan
    3+ dias sin mirarse (para cazar el dia en que salen a la venta); y el resto,
    de la comprobacion mas antigua a la mas reciente."""
    nuevas = list(nuevos_hoy)
    nunca = [a for a in ids_hoy if a not in nuevos_hoy and not registro[a].get("precio_comprobado")]
    limite = (date.fromisoformat(hoy) - timedelta(days=3)).isoformat() if hoy else "0000-00-00"
    proximas = [a for a in ids_hoy if a not in nuevos_hoy and registro[a].get("estado_ficha") == "proximamente"
                and (registro[a].get("precio_comprobado") or "") <= limite]
    ya = set(nuevas) | set(nunca) | set(proximas)
    resto = [a for a in ids_hoy if a not in ya]
    resto.sort(key=lambda aid: registro[aid].get("precio_comprobado") or "0000-00-00")
    return (nuevas + nunca + proximas + resto)[:maximo]


def lunes(d):
    return d - timedelta(days=d.weekday())


# ----------------------------------------------------------------- principal

def main():
    ahora = datetime.now(timezone.utc)
    if os.environ.get("HOY_PRUEBA"):                  # solo pruebas
        ahora = datetime.fromisoformat(os.environ["HOY_PRUEBA"] + "T15:00:00+00:00")
    hoy = ahora.date().isoformat()
    d_hoy = ahora.date()

    crudo = descargar(SITEMAP)
    props, sin_patron = parsear(crudo)
    if not props:
        raise SystemExit("El sitemap no devolvio ninguna propiedad: no se escribe nada.")

    unicas = {p["aid"]: p for p in props}
    ids_hoy = set(unicas)

    registro = cargar("propiedades.json", {})
    meta = cargar("meta.json", {})
    ids_ayer = {a for a, v in registro.items() if v.get("activa")}
    primera_pasada = not ids_ayer
    if primera_pasada and "inicio_medicion" not in meta:
        meta["inicio_medicion"] = hoy
    if "inicio_medicion" not in meta:
        # instalaciones anteriores a v3: el inicio es la primera fecha del registro
        meta["inicio_medicion"] = min((v.get("primera") for v in registro.values() if v.get("primera")),
                                      default=hoy)
    inicio = meta["inicio_medicion"]

    nuevos = sorted(ids_hoy - ids_ayer)
    retirados = sorted(ids_ayer - ids_hoy)
    reapariciones = [aid for aid in nuevos if aid in registro and registro[aid].get("salida")]

    for aid, p in unicas.items():
        r = registro.get(aid)
        if r is None:
            registro[aid] = {
                "primera": hoy, "ultima": hoy, "activa": True,
                "estado": p["estado"], "cp": p["cp"], "url": p["url"],
                "dias": 1, "veces_retirada": 0, "lastmod": p["lastmod"],
            }
        else:
            if r.get("ultima") != hoy:
                r["dias"] = r.get("dias", 0) + 1
            if r.get("salida") and not r.get("activa"):
                r["reapariciones"] = r.get("reapariciones", 0) + 1
            r["activa"] = True
            r["ultima"] = hoy
            r.pop("salida", None)
            r["estado"] = p["estado"]
            r["cp"] = p["cp"]
            r["url"] = p["url"]
            if p["lastmod"] and p["lastmod"] != r.get("lastmod"):
                r["lastmod_cambios"] = r.get("lastmod_cambios", 0) + 1
            r["lastmod"] = p["lastmod"]

    salidas, salidas_reales, valor_salido, retencion = [], [], [], []
    for aid in retirados:
        r = registro[aid]
        r["activa"] = False
        r["salida"] = hoy
        r["veces_retirada"] = r.get("veces_retirada", 0) + 1
        try:
            d = (d_hoy - date.fromisoformat(r["primera"])).days
        except Exception:  # noqa: BLE001
            d = None
        if d is not None:
            r["dias_hasta_salir"] = d
            if r["primera"] != inicio:          # los de la primera foto no tienen edad real
                salidas.append(d)
        if r.get("fecha_listado_mls"):
            try:
                dr = (d_hoy - date.fromisoformat(r["fecha_listado_mls"])).days
                if dr >= 0:
                    r["dias_mercado_real"] = dr
                    salidas_reales.append(dr)
            except Exception:  # noqa: BLE001
                pass
        if r.get("precio_lista"):
            valor_salido.append(r["precio_lista"])
            if r.get("precio_lista_inicial"):
                retencion.append(r["precio_lista"] / r["precio_lista_inicial"])

    por_estado = Counter(p["estado"] for p in unicas.values())
    por_cp = Counter(p["cp"] for p in unicas.values())
    altas_estado = Counter(unicas[a]["estado"] for a in nuevos)
    bajas_estado = Counter(registro[a].get("estado") for a in retirados)

    # antiguedad (desde que medimos) de lo que sigue listado
    antiguedad, antiguedad_real = [], []
    for aid in ids_hoy:
        r = registro[aid]
        try:
            antiguedad.append((d_hoy - date.fromisoformat(r["primera"])).days)
        except Exception:  # noqa: BLE001
            pass
        if r.get("fecha_listado_mls"):
            try:
                x = (d_hoy - date.fromisoformat(r["fecha_listado_mls"])).days
                if 0 <= x < 3650:
                    antiguedad_real.append(x)
            except Exception:  # noqa: BLE001
                pass

    lastmod_hoy = sum(1 for p in unicas.values() if p["lastmod"][:10] == hoy)

    # ------------------------------------------------ fichas (protegido)
    consultadas = con_precio = errores = recortes_hoy = proximas_hoy = a_venta_hoy = 0
    cortes_hoy_pct = []
    bloqueado = False
    try:
        objetivo = elegir_para_precio(registro, ids_hoy, nuevos, MAX_FICHAS_POR_DIA, hoy)
        t0 = time.time()
        for aid in objetivo:
            if time.time() - t0 > TOPE_MINUTOS_FICHAS * 60:
                print(f"Tope de {TOPE_MINUTOS_FICHAS} min en fichas: se sigue manana.")
                break
            if consultadas >= CORTACIRCUITOS and con_precio == 0 and proximas_hoy == 0 and errores == consultadas:
                bloqueado = True
                print(f"Cortacircuitos: las primeras {consultadas} fichas fallaron todas. "
                      f"Codigos: {dict(CODIGOS_HTTP)}")
                break
            url = unicas[aid]["url"]
            cuerpo = descargar_ficha(url)
            consultadas += 1
            r = registro[aid]
            r["precio_comprobado"] = hoy
            if cuerpo is None:
                errores += 1
                time.sleep(PAUSA_ENTRE_FICHAS)
                continue
            if con_precio == 0:
                guardar_debug("ficha_muestra", cuerpo, hoy)
            datos = extraer_ficha(cuerpo)
            if datos is None:
                errores += 1
                if errores <= 3:
                    guardar_debug(f"ficha_rara_{errores}", cuerpo, hoy)
                time.sleep(PAUSA_ENTRE_FICHAS)
                continue
            if datos.get("estado_ficha") == "proximamente":
                proximas_hoy += 1
                if r.get("estado_ficha") != "proximamente" or not r.get("proximamente_desde"):
                    r["proximamente_desde"] = datos.get("fecha_proximamente") or hoy
                r["estado_ficha"] = "proximamente"
                r.pop("precio_lista", None)
                for campo in ("precio_previsto", "fecha_proximamente", "historial", "sqft",
                              "habitaciones", "banos", "ano_construccion", "tipo_casa"):
                    if campo in datos:
                        r[campo] = datos[campo]
                if guardar_debug and proximas_hoy == 1:
                    guardar_debug("ficha_proximamente", cuerpo, hoy)
                time.sleep(PAUSA_ENTRE_FICHAS)
                continue
            con_precio += 1
            if r.get("estado_ficha") == "proximamente":
                # sale a la venta hoy: fin de la fase "Available soon"
                a_venta_hoy += 1
                r["salio_a_venta"] = hoy
                try:
                    r["dias_proximamente"] = (d_hoy - date.fromisoformat(r.get("proximamente_desde") or hoy)).days
                except Exception:  # noqa: BLE001
                    pass
            anterior = r.get("precio_lista")
            if anterior is not None and datos["precio_lista"] < anterior:
                r["recortes"] = r.get("recortes", 0) + 1
                r["ultimo_recorte"] = hoy
                r["ultimo_recorte_de_a"] = [anterior, datos["precio_lista"]]
                recortes_hoy += 1
                cortes_hoy_pct.append((anterior - datos["precio_lista"]) / anterior * 100)
            r["precio_lista"] = datos["precio_lista"]
            r["precio_estimado"] = datos.get("precio_estimado", False)
            if r.get("precio_lista_inicial") is None:
                r["precio_lista_inicial"] = datos["precio_lista"]
            for campo in ("precio_por_sqft", "sqft", "pago_estimado_mes", "concesion_hipoteca",
                          "fecha_listado_mls", "habitaciones", "banos", "ano_construccion", "tipo_casa",
                          "listada_por", "estado_ficha", "historial", "retirada_previa",
                          "retirada_previa_fecha"):
                if campo in datos:
                    r[campo] = datos[campo]
            if "concesion_hipoteca" not in datos:
                r.pop("concesion_hipoteca", None)
            time.sleep(PAUSA_ENTRE_FICHAS)
    except Exception as e:  # noqa: BLE001
        print(f"AVISO: fallo en la fase de fichas ({type(e).__name__}: {e}). "
              "El recuento principal se guarda igual.")

    # ------------------------------------------------ agregados de precio
    con_datos = [registro[aid] for aid in ids_hoy if registro[aid].get("precio_lista")
                 and registro[aid].get("estado_ficha") != "proximamente"]
    precios = [r["precio_lista"] for r in con_datos]
    ppsf_l = [r["precio_por_sqft"] for r in con_datos if r.get("precio_por_sqft")]
    pagos = [r["pago_estimado_mes"] for r in con_datos if r.get("pago_estimado_mes")]
    concesiones = [r["concesion_hipoteca"] for r in con_datos if r.get("concesion_hipoteca")]
    con_recorte = sum(1 for r in con_datos if r.get("recortes"))

    resumen_precios = None
    if con_datos:
        n = len(con_datos)
        habs = [r["habitaciones"] for r in con_datos if r.get("habitaciones")]
        anos = [r["ano_construccion"] for r in con_datos if r.get("ano_construccion")]
        sqfts = [r["sqft"] for r in con_datos if r.get("sqft")]
        con_hist = [r for r in con_datos if "retirada_previa" in r]
        estados_ficha = Counter(r.get("estado_ficha") for r in con_datos if r.get("estado_ficha"))
        listadores = Counter(r.get("listada_por") for r in con_datos if r.get("listada_por"))
        por_estado_precio = defaultdict(list)
        for r in con_datos:
            por_estado_precio[r.get("estado")].append(r["precio_lista"])
        resumen_precios = {
            "cobertura": n,
            "cobertura_pct": round(n / len(unicas) * 100, 1),
            "precio_medio": round(statistics.mean(precios)),
            "precio_mediana": round(statistics.median(precios)),
            "valor_listado_muestra": sum(precios),
            "valor_listado_estimado": round(statistics.mean(precios) * len(unicas)),
            "reparto_precio": reparto(precios, TRAMOS_PRECIO),
            "precio_mediana_por_estado": {e: round(statistics.median(v)) for e, v in
                                          sorted(por_estado_precio.items(), key=lambda x: -len(x[1]))
                                          if e and len(v) >= 10},
            "precio_por_sqft_medio": (round(statistics.mean(ppsf_l)) if ppsf_l else None),
            "sqft_mediana": (round(statistics.median(sqfts)) if sqfts else None),
            "habitaciones_mediana": mediana(habs),
            "ano_construccion_mediana": (round(statistics.median(anos)) if anos else None),
            "pago_estimado_medio": (round(statistics.mean(pagos)) if pagos else None),
            "con_concesion_pct": round(len(concesiones) / n * 100, 1),
            "concesion_media": (round(statistics.mean(concesiones)) if concesiones else None),
            "con_recorte_pct": round(con_recorte / n * 100, 1),
            "recortes_hoy": recortes_hoy,
            "recorte_medio_hoy_pct": (round(statistics.mean(cortes_hoy_pct), 2) if cortes_hoy_pct else None),
            "retirada_previa_pct": (round(sum(1 for r in con_hist if r["retirada_previa"]) / len(con_hist) * 100, 1)
                                    if len(con_hist) >= 20 else None),
            "retirada_previa_base": len(con_hist),
            "estado_ficha": dict(estados_ficha),
            "listada_por_top": dict(listadores.most_common(6)),
            "consultadas_hoy": consultadas,
            "con_precio_hoy": con_precio,
            "errores_hoy": errores,
            "codigos_http": dict(CODIGOS_HTTP),
            "bloqueado": bloqueado,
            "nota": ("Cobertura parcial y creciente: se comprueban hasta "
                     f"{MAX_FICHAS_POR_DIA} fichas de propiedad al dia. "
                     "El precio de venta que ensena Opendoor ya lleva su descuento "
                     "estandar del 1% sobre el precio de lista; aqui se deshace ese "
                     "descuento para dar el precio de lista real. Ese 1% NO es un recorte."),
        }

    # ------------------------------------------------ v3.1: a la venta vs "Available soon"
    activas_r = [registro[a] for a in ids_hoy]
    def clase_de(r):
        e = r.get("estado_ficha")
        if e in ("proximamente", "pendiente"):
            return e
        if r.get("precio_lista"):
            return "en_venta"
        return None
    clases_act = Counter(c for c in (clase_de(r) for r in activas_r) if c)
    n_clas = sum(clases_act.values())
    d30s = (d_hoy - timedelta(days=30)).isoformat()
    pasos = [r for r in registro.values() if (r.get("salio_a_venta") or "") > d30s]
    prox_r = [r for r in activas_r if r.get("estado_ficha") == "proximamente"]
    clasificacion = None
    if n_clas:
        cuota = clases_act.get("proximamente", 0) / n_clas
        clasificacion = {
            "clasificadas": n_clas,
            "cobertura_pct": round(n_clas / len(unicas) * 100, 1),
            "en_venta": clases_act.get("en_venta", 0),
            "proximamente": clases_act.get("proximamente", 0),
            "pendiente": clases_act.get("pendiente", 0),
            "proximamente_pct": round(cuota * 100, 1),
            "proximamente_estimadas": round(cuota * len(unicas)),
            "en_venta_estimadas": round((1 - cuota) * len(unicas)),
            "proximamente_por_estado": dict(Counter(r.get("estado") for r in prox_r).most_common(12)),
            "precio_previsto_mediana": mediana([r.get("precio_previsto") for r in prox_r if r.get("precio_previsto")]),
            "vistas_hoy": proximas_hoy,
            "a_venta_hoy": a_venta_hoy,
            "a_venta_30d": len(pasos),
            "dias_proximamente_mediana": mediana([r.get("dias_proximamente") for r in pasos
                                                  if r.get("dias_proximamente") is not None]),
            "nota": ("'Available soon': casas que ya son de Opendoor (lo dice la propia ficha) pero "
                     "aun sin precio ni a la venta, normalmente en reforma. Estan en el sitemap. "
                     "Las estimadas extrapolan la proporcion de las fichas ya leidas al total."),
        }

    # ------------------------------------------------ salidas acumuladas y cohortes
    d30 = (d_hoy - timedelta(days=30)).isoformat()
    d7 = (d_hoy - timedelta(days=7)).isoformat()
    sal_30 = [r for r in registro.values() if not r.get("activa") and (r.get("salida") or "") > d30]
    sal_7 = [r for r in sal_30 if r["salida"] > d7]
    edades_salida_30 = [r["dias_hasta_salir"] for r in sal_30
                        if r.get("dias_hasta_salir") is not None and r.get("primera") != inicio]

    cohortes = []
    por_semana = defaultdict(list)
    for r in registro.values():
        p = r.get("primera")
        if not p or p == inicio:
            continue
        por_semana[lunes(date.fromisoformat(p)).isoformat()].append(r)
    for sem in sorted(por_semana)[-12:]:
        grupo = por_semana[sem]
        fila = {"semana": sem, "n": len(grupo)}
        for h in (7, 30, 60, 90):
            if (d_hoy - date.fromisoformat(sem)).days < h + 6:
                fila[f"s{h}"] = None             # aun no ha pasado el plazo para toda la semana
                continue
            fuera = sum(1 for r in grupo if r.get("dias_hasta_salir") is not None
                        and not r.get("activa") and r["dias_hasta_salir"] <= h)
            fila[f"s{h}"] = round(fuera / len(grupo) * 100, 1)
        cohortes.append(fila)

    empleo = None
    try:
        empleo = contar_empleo(hoy)
    except Exception as e:  # noqa: BLE001
        print(f"AVISO: empleo no leido ({type(e).__name__}: {e})")

    snapshot = {
        "version": 3,
        "fecha": hoy,
        "obtenido_utc": ahora.isoformat(timespec="seconds"),
        "fuente": SITEMAP,
        "inicio_medicion": inicio,
        "total": len(unicas),
        "nuevos": len(nuevos),
        "retirados": len(retirados),
        "reapariciones": len(reapariciones),
        "primera_pasada": primera_pasada,
        "lastmod_hoy": lastmod_hoy,
        "por_estado": dict(sorted(por_estado.items(), key=lambda x: -x[1])),
        "altas_por_estado": dict(altas_estado.most_common()) if not primera_pasada else {},
        "bajas_por_estado": dict(bajas_estado.most_common()),
        "cp_top_25": dict(por_cp.most_common(25)),
        "estados_distintos": len(por_estado),
        "cp_distintos": len(por_cp),
        "urls_sin_patron": sin_patron,
        "dias_listadas_mediana": mediana(antiguedad),
        "edad_listadas": reparto(antiguedad, TRAMOS_EDAD),
        "edad_real_listadas": reparto(antiguedad_real, TRAMOS_EDAD) if len(antiguedad_real) >= 50 else None,
        "edad_real_listadas_mediana": mediana(antiguedad_real) if len(antiguedad_real) >= 50 else None,
        "edad_real_base": len(antiguedad_real),
        "dias_hasta_salir_mediana": mediana(salidas),
        "dias_mercado_real_mediana": mediana(salidas_reales),
        "valor_salido_hoy": sum(valor_salido) if valor_salido else None,
        "valor_salido_hoy_base": len(valor_salido),
        "retencion_precio_hoy": (round(statistics.median(retencion) * 100, 2) if retencion else None),
        "salidas_7d": len(sal_7),
        "salidas_30d": len(sal_30),
        "valor_salido_30d": sum(r["precio_lista"] for r in sal_30 if r.get("precio_lista")) or None,
        "valor_salido_30d_base": sum(1 for r in sal_30 if r.get("precio_lista")),
        "edad_salidas_30d": reparto(edades_salida_30, TRAMOS_EDAD) if edades_salida_30 else None,
        "cohortes": cohortes,
        "precios": resumen_precios,
        "clasificacion": clasificacion,
        "empleo": empleo,
        "aviso": ("Conteo propio sobre el sitemap publico de Opendoor. Solo ve lo que esta "
                  "anunciado a la venta: no ve lo comprado y aun sin listar ni lo que esta "
                  "bajo contrato. Una casa que desaparece puede haberse vendido, retirado o "
                  "vuelto a listar con otra direccion: NO afirmar que se ha vendido. "
                  "La antiguedad se cuenta desde que empezamos a medir, salvo los campos "
                  "'_real_', que usan la fecha de publicacion de la propia ficha. "
                  "Las casas de la primera foto no cuentan para edades ni cohortes."),
    }

    guardar("latest.json", snapshot)
    guardar(os.path.join("dias", f"{hoy}.json"), snapshot)
    guardar("propiedades.json", registro, compacto=True)
    guardar("meta.json", meta)

    anadir_csv("historico.csv", "fecha,total,nuevos,retirados,estados,dias_listadas_mediana",
               [f"{hoy},{len(unicas)},{len(nuevos)},{len(retirados)},{len(por_estado)},"
                f"{snapshot['dias_listadas_mediana'] if snapshot['dias_listadas_mediana'] is not None else ''}"],
               hoy)
    anadir_csv("estados.csv", "fecha,estado,casas",
               [f"{hoy},{e},{n}" for e, n in sorted(por_estado.items())], hoy)
    if resumen_precios:
        anadir_csv(
            "precios.csv",
            "fecha,cobertura,cobertura_pct,precio_medio,precio_mediana,precio_por_sqft_medio,"
            "pago_estimado_medio,con_concesion_pct,concesion_media,con_recorte_pct",
            [f"{hoy},{resumen_precios['cobertura']},{resumen_precios['cobertura_pct']},"
             f"{resumen_precios['precio_medio']},{resumen_precios['precio_mediana']},"
             f"{resumen_precios['precio_por_sqft_medio'] or ''},"
             f"{resumen_precios['pago_estimado_medio'] or ''},"
             f"{resumen_precios['con_concesion_pct']},"
             f"{resumen_precios['concesion_media'] or ''},"
             f"{resumen_precios['con_recorte_pct']}"],
            hoy,
        )

    print(f"{hoy}  total={len(unicas)}  nuevos={len(nuevos)}  retirados={len(retirados)}  "
          f"reapariciones={len(reapariciones)}  estados={len(por_estado)}  sin_patron={sin_patron}  "
          f"lastmod_hoy={lastmod_hoy}")
    print(f"fichas: consultadas={consultadas}  con_precio={con_precio}  errores={errores}  "
          f"codigos={dict(CODIGOS_HTTP)}  cobertura_total={len(con_datos)}/{len(unicas)}"
          + (f"  precio_medio=${resumen_precios['precio_medio']:,}" if resumen_precios else ""))
    if clasificacion:
        print(f"clasificacion: {clasificacion['en_venta']} a la venta, {clasificacion['proximamente']} available soon, "
              f"{clasificacion['pendiente']} pendientes ({clasificacion['proximamente_pct']}% proximamente, "
              f"~{clasificacion['proximamente_estimadas']} en total); {a_venta_hoy} salieron a la venta hoy")
    if empleo:
        print(f"empleo: total={empleo.get('total')}  metodo={empleo.get('metodo')}  claves={empleo.get('claves')}")
    if consultadas and con_precio == 0:
        print("AVISO: ninguna ficha dio precio legible. Mirar data/debug/ficha_muestra_estructura.json.")
    if primera_pasada:
        print("Primera pasada: 'nuevos' es el inventario entero, no entradas reales.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

