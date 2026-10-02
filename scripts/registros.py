#!/usr/bin/env python3
"""
Registros publicos de $OPEN: lo que queda escrito en los condados.

El sitemap de Opendoor solo ensena lo que esta anunciado. Los registros de la
propiedad ensenan lo que de verdad se compra, se vende y se hipoteca. Este
script mira en ellos, con fuentes publicas y sin autenticacion:

1. MARICOPA (Phoenix), el condado con la mejor ventana:
   a) Registro de documentos (publicapi.recorder.maricopa.gov): cada
      escritura, hipoteca (deed of trust) y liberacion donde aparece una
      entidad de Opendoor. Ahi salen:
        - compras y ventas de casas de Opendoor (escrituras de traspaso),
        - las hipotecas nuevas de OPENDOOR HOME LOANS LLC (relanzada en 2026)
          y las liberaciones de su cartera antigua,
        - OS NATIONAL LLC (su filial de title & escrow) como fiduciario
          ("trustee") en deeds of trust, que es la huella de sus cierres.
   b) Catastro del asesor (gis.mcassessor.maricopa.gov): todas las parcelas
      cuyo dueno es una entidad de Opendoor, con su precio de compra, fecha
      de escritura, metros y valor fiscal. Es el inventario que TIENE, no el
      que anuncia: incluye lo comprado y aun sin listar.
   Cruzando (a) con (b) por numero de escritura se sabe si cada traspaso es
   una compra o una venta y a que precio. Cuando una parcela sale de la
   cartera de Opendoor se empareja su precio de compra con el de venta:
   margen bruto realizado y dias en cartera, casa a casa.
   Y cruzando los anuncios de Arizona del sitemap con el catastro se mide
   que parte de lo anunciado es de Opendoor segun los registros.

2. OTROS CONDADOS con catastro abierto consultable por nombre de dueno:
   Pima (Tucson), Harris (Houston), Collin, Denton, Williamson y Fort Bend
   (Texas). Solo el recuento de parcelas a nombre de Opendoor y sus altas y
   bajas: Texas no publica precios de venta (es estado de "no divulgacion").

3. PANEL: al final se compone data/panel.json, un unico JSON compacto con
   todo lo que necesita la pagina «$OPEN Inventory Watch».

Todo lo que falla aqui falla en silencio y por partes: un condado caido no
impide los demas, y nada de esto toca el recuento principal (contar.py).

Limites que hay que decir siempre:
  * El catastro va con retraso respecto al registro (unas dos semanas).
  * Una escritura sin parcela emparejada todavia queda como "pendiente".
  * Una hipoteca de Opendoor Home Loans solo se ve en los condados donde se
    registra; aqui solo se mira Maricopa. No es el volumen nacional.
  * El margen bruto realizado es precio de venta menos precio de compra; no
    descuenta reformas, comisiones, impuestos ni costes de tenencia.
"""

import glob
import json
import os
import re
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATOS = os.path.join(RAIZ, "data")
UA = ("opendoor-public-records/1.0 (investigacion personal sobre registros publicos; "
      "github.com/fernandoalvaropastor/Projects)")
PAUSA = float(os.environ.get("REGISTROS_PAUSA", "0.6"))
MOCK = os.environ.get("REGISTROS_MOCK", "").rstrip("/")   # solo para pruebas

REC = "https://publicapi.recorder.maricopa.gov"
PARCELAS_MARICOPA = "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/Parcels/MapServer/0/query"
INICIO_REGISTRO = date(2026, 1, 1)
RELANZAMIENTO_OHL = date(2026, 2, 1)       # vuelta de Opendoor Home Loans (beta)
RELECTURA_DIAS = 45                        # se repasa esta ventana cada dia (retraso del indice)
MAX_DETALLES_POR_PASADA = 1400
MAX_DIRECCIONES_POR_PASADA = 160
CP_MARICOPA = ("850", "852", "853")        # aproximado: fuera quedan Pinal (851xx) y Pima (856-857)
DIRECCIONALES = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST"}

CONDADOS = [
    {"id": "maricopa_az", "nombre": "Maricopa", "ciudad": "Phoenix", "estado": "AZ",
     "url": PARCELAS_MARICOPA, "dueno": "OWNER_NAME", "pid": "APN", "fecha": "DEED_DATE",
     "cp": "PHYSICAL_ZIP", "precio": "SALE_PRICE"},
    {"id": "pima_az", "nombre": "Pima", "ciudad": "Tucson", "estado": "AZ",
     "url": "https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/LandRecords/MapServer/12/query",
     "dueno": "MAIL1", "pid": "PARCEL", "fecha": "RECORDDATE", "cp": "ZIP"},
    {"id": "harris_tx", "nombre": "Harris", "ciudad": "Houston", "estado": "TX",
     "url": "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0/query",
     "dueno": "owner_name_1", "pid": "HCAD_NUM", "fecha": "new_owner_date", "cp": "site_zip"},
    {"id": "collin_tx", "nombre": "Collin", "ciudad": "Dallas norte", "estado": "TX",
     "url": ("https://gismaps.cityofallen.org/arcgis/rest/services/ReferenceData/"
             "Collin_County_Appraisal_District_Parcels/MapServer/1/query"),
     "dueno": "GIS_DBO_AD_Entity_file_as_name", "pid": "GIS_DBO_Parcel_PROP_ID",
     "fecha": "GIS_DBO_AD_Entity_deed_dt", "cp": "GIS_DBO_AD_Entity_situs_zip"},
    {"id": "denton_tx", "nombre": "Denton", "ciudad": "Dallas norte", "estado": "TX",
     "url": "https://gis.dentoncounty.gov/arcgis/rest/services/Parcels/MapServer/0/query",
     "dueno": "OWNER_NAME", "pid": ("PROP_ID", "prop_id", "PID", "PARCEL_ID", "GEO_ID"),
     "fecha": ("DEED_DATE", "deed_dt", "DEED_DT"), "cp": ("SITUS_ZIP", "situs_zip", "ZIP")},
    {"id": "williamson_tx", "nombre": "Williamson", "ciudad": "Austin norte", "estado": "TX",
     "url": "https://gis.wilco.org/arcgis/rest/services/public/county_wcad_parcels/MapServer/0/query",
     "dueno": "FullName", "pid": "PARCELID", "fecha": None, "cp": "Szip"},
    {"id": "fortbend_tx", "nombre": "Fort Bend", "ciudad": "Houston sur", "estado": "TX",
     "url": ("https://gisportal.fortbendcountytx.gov/arcgis/rest/services/InteractiveMap/"
             "Parcels_Public/FeatureServer/1/query"),
     "dueno": "Owner_Name", "pid": "Property_Number", "fecha": "Deed_Date", "cp": None},
]


# ----------------------------------------------------------------- red y utilidades

class FuenteCaida(Exception):
    pass


LLAMADAS = Counter()


def url_real(url):
    if not MOCK:
        return url
    p = urllib.parse.urlparse(url)
    return f"{MOCK}/{p.netloc}{p.path}" + (f"?{p.query}" if p.query else "")


def get_json(url, params=None, intentos=4, fuente="?"):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url_real(url), headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                LLAMADAS[fuente] += 1
                datos = json.loads(r.read().decode("utf-8", "ignore") or "null")
            time.sleep(PAUSA)
            if isinstance(datos, dict) and "error" in datos and len(datos) <= 2:
                raise FuenteCaida(f"error de ArcGIS: {datos['error']}")
            return datos
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code in (400, 401, 403, 404):
                break
            time.sleep(3 * (i + 1) + (10 if e.code == 429 else 0))
        except FuenteCaida as e:
            ultimo = e
            break
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(3 * (i + 1))
    raise FuenteCaida(f"{fuente}: {type(ultimo).__name__}: {ultimo}")


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


def a_int(s):
    try:
        return int(float(str(s).replace(",", "").replace("$", "").strip()))
    except Exception:  # noqa: BLE001
        return None


def a_fecha(v):
    """Epoch en ms, 'M-DD-YYYY', 'MM/DD/YYYY' o ISO -> date."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"-?\d{10,13}", v.strip())):
        try:
            ms = float(v)
            if ms > 1e11:
                ms /= 1000
            return datetime.fromtimestamp(ms, tz=timezone.utc).date()
        except Exception:  # noqa: BLE001
            return None
    s = str(v).strip()[:10]
    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:  # noqa: BLE001
            continue
    return None


def iso(d):
    return d.isoformat() if d else None


def lunes(d):
    return d - timedelta(days=d.weekday())


def mediana(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def es_od(nombre):
    return "OPENDOOR" in (nombre or "").upper()


def publico(nombre):
    """Entidades que se guardan en claro: Opendoor, su title & escrow y MERS."""
    n = (nombre or "").upper()
    return "OPENDOOR" in n or "OS NATIONAL" in n or "MORTGAGE ELECTRONIC" in n


def anon(nombre):
    """Los nombres de particulares NO se guardan: solo una huella (hash) que
    permite cruzar documentos entre si sin publicar quien es nadie."""
    import hashlib
    n = re.sub(r"\s+", " ", (nombre or "").upper().strip())
    if not n:
        return ""
    if publico(n):
        return n
    return "h:" + hashlib.sha1(n.encode("utf-8")).hexdigest()[:14]


def anon_dueno(dueno):
    """Primer titular de un OWNER_NAME del catastro ('APELLIDO NOMBRE/OTRO')."""
    primero = re.split(r"[/&]", (dueno or "").upper())[0].strip()
    return anon(primero)


def campo(attrs, nombres):
    if nombres is None:
        return None
    if isinstance(nombres, str):
        nombres = (nombres,)
    for n in nombres:
        if n in attrs and attrs[n] not in (None, ""):
            return attrs[n]
    return None


def arcgis_todos(url, where, fuente, campos="*", pagina=1000, tope=20000):
    salida, offset = [], 0
    while True:
        datos = get_json(url, {"where": where, "outFields": campos, "returnGeometry": "false",
                               "resultOffset": offset, "resultRecordCount": pagina, "f": "json"},
                         fuente=fuente)
        feats = (datos or {}).get("features") or []
        salida += [f.get("attributes") or {} for f in feats]
        if not feats or not datos.get("exceededTransferLimit") or len(salida) >= tope:
            break
        offset += len(feats)
    return salida


def clave_direccion(num, calle, cp):
    num = re.sub(r"\D", "", str(num or ""))
    calle = (calle or "").upper().strip()
    cp = re.sub(r"\D", "", str(cp or ""))[:5]
    if not (num and calle and cp):
        return None
    return f"{num}|{cp}|{calle}"


def clave_de_slug(url, cp):
    m = re.search(r"/properties/([^/]+)/", url or "")
    if not m:
        return None
    t = m.group(1).split("-")
    if len(t) < 3 or not t[0].isdigit():
        return None
    i = 1
    if t[i].upper() in DIRECCIONALES and len(t) > i + 1:
        i += 1
    return clave_direccion(t[0], t[i], cp)


def clave_de_direccion_parcela(p):
    d = (p.get("dir") or "").split()
    if len(d) < 3 or not d[0].isdigit():
        return clave_direccion(p.get("num"), (p.get("calle") or "").split(" ")[0] if p.get("calle") else None, p.get("cp"))
    i = 1
    if d[i].upper() in DIRECCIONALES and len(d) > i + 1:
        i += 1
    return clave_direccion(d[0], d[i], p.get("cp") or d[-1])


# ----------------------------------------------------------------- Maricopa: catastro

def parcela_de(a):
    return {
        "apn": str(a.get("APN") or "").strip(),
        "dueno": (a.get("OWNER_NAME") or "").strip(),
        "escritura": str(a.get("DEED_NUMBER") or "").strip(),
        "f_escritura": iso(a_fecha(a.get("DEED_DATE"))),
        "precio": a_int(a.get("SALE_PRICE")),
        "dir": re.sub(r"\s+", " ", (a.get("PHYSICAL_ADDRESS") or "").strip()),
        "ciudad": (a.get("PHYSICAL_CITY") or "").strip().title() or None,
        "cp": re.sub(r"\D", "", str(a.get("PHYSICAL_ZIP") or ""))[:5] or None,
        "num": str(a.get("PHYSICAL_STREET_NUM") or "").strip(),
        "calle": (a.get("PHYSICAL_STREET_NAME") or "").strip(),
        "sqft": a_int(a.get("LIVING_SPACE")),
        "ano": a_int(a.get("CONST_YEAR")),
        "fcv": a_int(a.get("FCV_CUR")),
    }


CAMPOS_PARCELA = ("APN,OWNER_NAME,DEED_NUMBER,DEED_DATE,SALE_DATE,SALE_PRICE,PHYSICAL_ADDRESS,"
                  "PHYSICAL_CITY,PHYSICAL_ZIP,PHYSICAL_STREET_NUM,PHYSICAL_STREET_NAME,"
                  "LIVING_SPACE,CONST_YEAR,FCV_CUR")


def parcelas_por_escritura(numeros):
    salida = {}
    numeros = [n for n in numeros if re.fullmatch(r"\d{6,14}", n)]
    for i in range(0, len(numeros), 40):
        trozo = numeros[i:i + 40]
        where = "DEED_NUMBER IN (" + ",".join(f"'{n}'" for n in trozo) + ")"
        for a in arcgis_todos(PARCELAS_MARICOPA, where, "maricopa_catastro", CAMPOS_PARCELA):
            p = parcela_de(a)
            salida.setdefault(p["escritura"], []).append(p)
    return salida


def parcela_por_apn(apn):
    a = arcgis_todos(PARCELAS_MARICOPA, f"APN = '{apn}'", "maricopa_catastro", CAMPOS_PARCELA)
    return parcela_de(a[0]) if a else None


def parcela_por_direccion(num, calle, cp):
    num = re.sub(r"\D", "", num)
    calle = re.sub(r"[^A-Z0-9]", "", calle.upper())
    if not (num and calle and cp):
        return None
    where = f"PHYSICAL_ADDRESS LIKE '{num} %{calle}%{cp}%'"
    a = arcgis_todos(PARCELAS_MARICOPA, where, "maricopa_catastro", CAMPOS_PARCELA, pagina=10, tope=10)
    return [parcela_de(x) for x in a]


# ----------------------------------------------------------------- Maricopa: registro

CODIGOS_ESCRITURA = {"WAR DEED", "SPEC/W D", "Q/CL DEED", "DEED", "GRANT DEED", "SP WAR DD", "JT TEN DD"}


def es_escritura(cod):
    c = (cod or "").upper()
    if c in CODIGOS_ESCRITURA:
        return True
    return ("DEED" in c or "W D" in c) and "TRST" not in c and "D/T" not in c and "TRSTE" not in c


def rec_buscar(nombre, desde, hasta, nivel=0):
    salida, pagina, total = [], 1, 0
    while True:
        datos = get_json(REC + "/documents/search", {
            "businessNames": nombre, "beginDate": desde.isoformat(), "endDate": hasta.isoformat(),
            "pageSize": 200, "pageNumber": pagina, "maxResults": 200,
        }, fuente="maricopa_registro") or {}
        res = datos.get("searchResults") or []
        total = datos.get("totalResults") or len(res)
        salida += res
        if len(res) < 200 or len(salida) >= total or pagina >= 5:
            break
        pagina += 1
    if total >= 500 and (hasta - desde).days >= 1 and nivel < 5:
        mitad = desde + (hasta - desde) // 2
        return (rec_buscar(nombre, desde, mitad, nivel + 1) +
                rec_buscar(nombre, mitad + timedelta(days=1), hasta, nivel + 1))
    return salida


def clasificar(doc):
    nombres = doc.get("n") or []
    cod = (doc.get("c") or "").upper()
    od = [n for n in nombres if es_od(n)]
    osn = [n for n in nombres if "OS NATIONAL" in n.upper()]
    otros = [n for n in nombres if not es_od(n) and "OS NATIONAL" not in n.upper()
             and "MORTGAGE ELECTRONIC" not in n.upper()]
    ohl = any("HOME LOANS" in n.upper() for n in od)
    if ohl:
        if "DEED TRST" in cod or cod in ("D/T", "DEED OF TRUST"):
            return "hipoteca_ohl"
        if "REL" in cod or "C/N" in cod:
            return "liberacion_ohl"
        return "otro_ohl"
    if od and es_escritura(cod):
        if not otros:
            return "interna"
        return doc.get("dir") or "traspaso"          # compra / venta si ya se sabe
    if osn:
        if "DEED TRST" in cod:
            return "osn_fiduciario"
        if "SUB" in cod:
            return "osn_sustitucion"
        if "REL" in cod or "C/N" in cod or "PART" in cod:
            return "osn_liberacion"
        return "osn_otro"
    if od:
        return "otro_od"
    return "otro"


def actualizar_registro(docs, hoy, estado):
    """Busca en el registro y trae el detalle (nombres) de lo nuevo."""
    ult = estado.get("registro_hasta")
    desde = INICIO_REGISTRO if not ult else max(INICIO_REGISTRO, date.fromisoformat(ult) - timedelta(days=RELECTURA_DIAS))
    nuevos = 0
    for nombre in ("OPENDOOR", "OS NATIONAL"):
        d = desde
        while d <= hoy:
            h = min(hoy, d + timedelta(days=6))
            for r in rec_buscar(nombre, d, h):
                rn = str(r.get("recordingNumber") or "").strip()
                if not rn:
                    continue
                if rn not in docs:
                    docs[rn] = {"f": iso(a_fecha(r.get("recordingDate"))), "c": r.get("documentCode") or "",
                                "b": [nombre]}
                    nuevos += 1
                elif nombre not in docs[rn].get("b", []):
                    docs[rn].setdefault("b", []).append(nombre)
            d = h + timedelta(days=1)
    # detalle: nombres de todas las partes
    pendientes = [rn for rn, v in docs.items()
                  if "n" not in v or (not v["n"] and v.get("f") and v["f"] >= iso(hoy - timedelta(days=30)))]
    pendientes.sort(key=lambda rn: docs[rn].get("f") or "", reverse=True)
    hechos = 0
    for rn in pendientes[:MAX_DETALLES_POR_PASADA]:
        try:
            det = get_json(f"{REC}/documents/{rn}", fuente="maricopa_registro") or {}
        except FuenteCaida:
            continue
        v = docs[rn]
        v["n"] = [anon(str(x)) for x in (det.get("names") or []) if str(x).strip()]
        cods = det.get("documentCodes") or []
        if cods:
            v["c"] = cods[0]
            if len(cods) > 1:
                v["cs"] = cods
        f = a_fecha(det.get("recordingDate"))
        if f:
            v["f"] = iso(f)
        v["pg"] = det.get("pageAmount")
        hechos += 1
    estado["registro_hasta"] = iso(hoy)
    return nuevos, hechos, max(0, len(pendientes) - MAX_DETALLES_POR_PASADA)


def resolver_direccion(docs, hoy):
    """Compra o venta: se busca la parcela cuyo numero de escritura vigente es
    el del documento. Si la parcela es hoy de Opendoor, fue una compra; si es
    de otro, fue una venta de Opendoor a ese otro."""
    candidatos = [rn for rn, v in docs.items()
                  if clasificar(v) == "traspaso" and v.get("f")
                  and (v.get("intentos_dir", 0) < 8 or v["f"] >= iso(hoy - timedelta(days=120)))]
    if not candidatos:
        return 0
    halladas = parcelas_por_escritura(candidatos)
    resueltos = 0
    for rn in candidatos:
        v = docs[rn]
        ps = halladas.get(rn)
        if not ps:
            v["intentos_dir"] = v.get("intentos_dir", 0) + 1
            continue
        p = ps[0]
        v["apn"] = p["apn"]
        v["precio"] = p["precio"]
        v["ciudad"] = p["ciudad"]
        v["cp"] = p["cp"]
        v["sqft"] = p["sqft"]
        v["np"] = len(ps)
        if es_od(p["dueno"]):
            v["dir"] = "compra"
        else:
            v["dir"] = "venta"
        resueltos += 1
    return resueltos


def cruzar_financiacion(docs):
    """Una venta de Opendoor esta financiada por Opendoor Home Loans si hay
    una hipoteca de OHL a nombre de los compradores en torno a esa fecha."""
    hipotecas = [v for v in docs.values() if clasificar(v) == "hipoteca_ohl" and v.get("f")]
    usadas = set()
    for v in docs.values():
        if clasificar(v) != "venta" or not v.get("f"):
            continue
        compradores = {n for n in v.get("n", []) if not es_od(n)}
        fv = date.fromisoformat(v["f"])
        v.pop("ohl", None)
        for h in hipotecas:
            fh = date.fromisoformat(h["f"])
            if -3 <= (fh - fv).days <= 10 and compradores & set(h.get("n", [])):
                v["ohl"] = True
                usadas.add(id(h))
                break
    for h in hipotecas:
        h["casa_od"] = id(h) in usadas


# ----------------------------------------------------------------- Maricopa: cartera

def actualizar_cartera(cartera, ventas, hoy):
    filas = arcgis_todos(PARCELAS_MARICOPA, "OWNER_NAME LIKE 'OPENDOOR%'", "maricopa_catastro", CAMPOS_PARCELA)
    if not filas:
        raise FuenteCaida("catastro de Maricopa: 0 parcelas de Opendoor (se ignora la pasada)")
    hoy_s = iso(hoy)
    antes = sum(1 for p in cartera.values() if p.get("activa"))
    if antes and len(filas) < 0.7 * antes:
        raise FuenteCaida(f"catastro de Maricopa: {len(filas)} parcelas frente a {antes} ayer; "
                          "respuesta sospechosa, no se registran salidas hoy")
    vistas = set()
    for a in filas:
        p = parcela_de(a)
        if not p["apn"]:
            continue
        vistas.add(p["apn"])
        prev = cartera.get(p["apn"])
        if prev and prev.get("activa") and prev.get("escritura") == p["escritura"]:
            prev.update({"visto": hoy_s, "dueno": p["dueno"]})
            continue
        p.update({"desde": (prev or {}).get("desde", hoy_s) if prev and prev.get("activa") else hoy_s,
                  "visto": hoy_s, "activa": True})
        cartera[p["apn"]] = p
    salidas = 0
    for apn, p in cartera.items():
        if not p.get("activa") or apn in vistas:
            continue
        # ha dejado de ser de Opendoor: se mira quien la tiene ahora
        try:
            nueva = parcela_por_apn(apn)
        except FuenteCaida:
            continue
        if nueva and es_od(nueva["dueno"]):
            continue                      # sigue siendo suya (cambio de entidad); se vera manana
        p["activa"] = False
        p["salida"] = hoy_s
        salidas += 1
        if nueva and nueva["escritura"] and nueva["escritura"] != p.get("escritura"):
            venta = {
                "apn": apn, "ciudad": p.get("ciudad"), "cp": p.get("cp"), "sqft": p.get("sqft"),
                "compra_f": p.get("f_escritura"), "compra_p": p.get("precio"),
                "venta_f": nueva.get("f_escritura"), "venta_p": nueva.get("precio"),
                "venta_escritura": nueva.get("escritura"), "detectada": hoy_s,
            }
            try:
                venta["dias"] = (date.fromisoformat(venta["venta_f"]) - date.fromisoformat(venta["compra_f"])).days
            except Exception:  # noqa: BLE001
                venta["dias"] = None
            if venta["compra_p"] and venta["venta_p"]:
                venta["margen_bruto"] = venta["venta_p"] - venta["compra_p"]
                venta["margen_bruto_pct"] = round((venta["venta_p"] / venta["compra_p"] - 1) * 100, 2)
            if not any(v["apn"] == apn and v.get("venta_escritura") == venta["venta_escritura"] for v in ventas):
                ventas.append(venta)
    return len(vistas), salidas


# ----------------------------------------------------------------- Maricopa: anuncios vs titulo

def cruzar_anuncios(cartera, docs, cache, hoy):
    registro = cargar("propiedades.json", {})
    activas = {aid: r for aid, r in registro.items()
               if r.get("activa") and r.get("estado") == "AZ" and str(r.get("cp", ""))[:3] in CP_MARICOPA}
    claves_cartera = {}
    for apn, p in cartera.items():
        if p.get("activa"):
            k = clave_de_direccion_parcela(p)
            if k:
                claves_cartera[k] = apn
    # nombres que aparecen en escrituras de Opendoor (para detectar compras aun sin catastro)
    nombres_escrituras = defaultdict(list)
    for v in docs.values():
        if clasificar(v) in ("compra", "traspaso", "venta") and v.get("f"):
            for n in v.get("n", []):
                if not es_od(n):
                    nombres_escrituras[n].append(v["f"])
    hoy_s = iso(hoy)
    consultas = 0
    for aid, r in activas.items():
        k = clave_de_slug(r.get("url"), r.get("cp"))
        c = cache.get(aid)
        if k and k in claves_cartera:
            cache[aid] = {"clase": "en_cartera", "apn": claves_cartera[k], "comprobado": hoy_s}
            continue
        caduca = {"en_cartera": 3, "comprada_reciente": 7, "sin_titulo_od": 7, "sin_parcela": 21}
        if c and c.get("comprobado") and (hoy - date.fromisoformat(c["comprobado"])).days < caduca.get(c.get("clase"), 7):
            continue
        if consultas >= MAX_DIRECCIONES_POR_PASADA or not k:
            if not k:
                cache[aid] = {"clase": "sin_parcela", "comprobado": hoy_s, "motivo": "direccion ilegible"}
            continue
        num, cp, calle = k.split("|")[0], k.split("|")[1], k.split("|")[2]
        try:
            ps = parcela_por_direccion(num, calle, cp)
        except FuenteCaida:
            continue
        consultas += 1
        if not ps:
            cache[aid] = {"clase": "sin_parcela", "comprobado": hoy_s}
            continue
        p = ps[0]
        if es_od(p["dueno"]):
            clase = "en_cartera"
        else:
            huella = anon_dueno(p["dueno"])
            fechas = nombres_escrituras.get(huella, []) if huella else []
            reciente = [f for f in fechas if not p.get("f_escritura") or f >= p["f_escritura"]]
            clase = "comprada_reciente" if reciente else "sin_titulo_od"
        cache[aid] = {"clase": clase, "apn": p["apn"], "comprobado": hoy_s,
                      "f_escritura": p.get("f_escritura")}
    # limpieza de lo que ya no esta anunciado
    for aid in list(cache):
        if aid not in activas:
            cache.pop(aid)
    clases = Counter(c["clase"] for aid, c in cache.items() if aid in activas)
    apns_anunciados = {c.get("apn") for c in cache.values() if c.get("clase") == "en_cartera"}
    activos = {a for a, p in cartera.items() if p.get("activa")}
    return {
        "anuncios_maricopa": len(activas),
        "comprobados": sum(clases.values()),
        "clases": dict(clases),
        "pct_titulo_od": (round((clases["en_cartera"] + clases["comprada_reciente"]) /
                                max(1, sum(clases.values()) - clases["sin_parcela"]) * 100, 1)
                          if sum(clases.values()) else None),
        "cartera_sin_anunciar": len(activos - apns_anunciados),
        "cartera_total": len(activos),
        "consultas_hoy": consultas,
        "nota": ("Anuncios de Arizona con CP 850/852/853 (aprox. Maricopa). 'en_cartera': la parcela "
                 "es de Opendoor en el catastro. 'comprada_reciente': el catastro aun no lo refleja pero "
                 "hay una escritura reciente de Opendoor con el dueno anterior. 'sin_titulo_od': "
                 "Opendoor la anuncia pero los registros no la ponen a su nombre (posible venta por "
                 "cuenta del propietario, o escritura aun no indexada). Retraso del catastro: ~2 semanas."),
    }


# ----------------------------------------------------------------- Maricopa: resumen

TRAMOS_CARTERA = [(0, 30, "0-30"), (31, 60, "31-60"), (61, 90, "61-90"), (91, 180, "91-180"),
                  (181, 365, "181-365"), (366, 100000, "365+")]
TRAMOS_PRECIO = [(0, 250000, "<250k"), (250000, 300000, "250-300k"), (300000, 400000, "300-400k"),
                 (400000, 500000, "400-500k"), (500000, 750000, "500-750k"), (750000, 10 ** 9, "750k+")]


def reparto(vals, tramos, inclusivo=True):
    c = Counter()
    for v in vals:
        for lo, hi, et in tramos:
            if (lo <= v <= hi) if inclusivo else (lo <= v < hi):
                c[et] += 1
                break
    return {et: c.get(et, 0) for _, _, et in tramos}


def resumen_maricopa(docs, cartera, ventas, cruce, hoy):
    activas = [p for p in cartera.values() if p.get("activa")]
    edades = [(hoy - date.fromisoformat(p["f_escritura"])).days for p in activas if p.get("f_escritura")]
    precios = [p["precio"] for p in activas if p.get("precio")]
    ppsf = [p["precio"] / p["sqft"] for p in activas if p.get("precio") and p.get("sqft")]
    sobre_fcv = [p["precio"] / p["fcv"] for p in activas if p.get("precio") and p.get("fcv")]
    ultima = max((p["f_escritura"] for p in activas if p.get("f_escritura")), default=None)

    clases = {rn: clasificar(v) for rn, v in docs.items()}
    semanas = defaultdict(Counter)
    meses = defaultdict(Counter)
    precios_mes = defaultdict(lambda: defaultdict(list))
    for rn, v in docs.items():
        if not v.get("f"):
            continue
        f = date.fromisoformat(v["f"])
        k = clases[rn]
        grupo = {"compra": "compras", "venta": "ventas", "traspaso": "pendientes", "interna": "internas",
                 "hipoteca_ohl": "hipotecas_ohl", "liberacion_ohl": "liberaciones_ohl",
                 "osn_fiduciario": "osn_fiduciario", "osn_sustitucion": "osn_otros",
                 "osn_liberacion": "osn_otros", "osn_otro": "osn_otros"}.get(k)
        if not grupo:
            continue
        semanas[iso(lunes(f))][grupo] += 1
        meses[v["f"][:7]][grupo] += 1
        if k in ("compra", "venta") and v.get("precio"):
            precios_mes[v["f"][:7]][k].append(v["precio"])
        if k == "venta" and v.get("ohl"):
            semanas[iso(lunes(f))]["ventas_ohl"] += 1
            meses[v["f"][:7]]["ventas_ohl"] += 1

    lista_semanas = []
    for s in sorted(semanas)[-26:]:
        fila = {"s": s}
        fila.update(semanas[s])
        lista_semanas.append(fila)
    lista_meses = []
    for m in sorted(meses):
        fila = {"m": m}
        fila.update(meses[m])
        for k in ("compra", "venta"):
            xs = precios_mes[m][k]
            fila[f"p_{k}"] = round(statistics.median(xs)) if xs else None
        lista_meses.append(fila)

    ohl = sorted([dict(v, rn=rn) for rn, v in docs.items() if clasificar(v) == "hipoteca_ohl" and v.get("f")],
                 key=lambda v: v["f"], reverse=True)
    ohl_lib = [v for v in docs.values() if clasificar(v) == "liberacion_ohl"]
    relanz = iso(RELANZAMIENTO_OHL)
    ventas_desde = [v for v in docs.values() if clasificar(v) == "venta" and (v.get("f") or "") >= "2026-07-01"]
    primera_ohl = ohl[-1]["f"] if ohl else None
    ventas_ohl_base = [v for v in ventas_desde if primera_ohl and v["f"] >= primera_ohl]
    ventas_pairs = sorted(ventas, key=lambda v: v.get("venta_f") or "", reverse=True)
    margenes = [v["margen_bruto_pct"] for v in ventas_pairs if v.get("margen_bruto_pct") is not None]
    dias = [v["dias"] for v in ventas_pairs if v.get("dias") is not None]

    d90 = iso(hoy - timedelta(days=90))
    c90 = Counter(clases[rn] for rn, v in docs.items() if (v.get("f") or "") >= d90)

    return {
        "cartera": {
            "casas": len(activas),
            "por_entidad": dict(Counter(p["dueno"] for p in activas).most_common()),
            "coste_total": sum(precios) if precios else None,
            "coste_base": len(precios),
            "precio_compra_mediana": round(statistics.median(precios)) if precios else None,
            "precio_compra_sqft_mediana": round(statistics.median(ppsf)) if ppsf else None,
            "sqft_mediana": mediana([p["sqft"] for p in activas if p.get("sqft")]),
            "ano_mediana": mediana([p["ano"] for p in activas if p.get("ano")]),
            "precio_sobre_valor_fiscal": round(statistics.median(sobre_fcv), 3) if sobre_fcv else None,
            "reparto_precio": reparto(precios, TRAMOS_PRECIO, inclusivo=False),
            "dias_en_cartera_mediana": mediana(edades),
            "reparto_dias_en_cartera": reparto(edades, TRAMOS_CARTERA),
            "mas_de_90_dias_pct": round(sum(1 for e in edades if e > 90) / len(edades) * 100, 1) if edades else None,
            "ciudades": dict(Counter(p.get("ciudad") for p in activas if p.get("ciudad")).most_common(12)),
            "ultima_escritura_catastro": ultima,
            "retraso_catastro_dias": (hoy - date.fromisoformat(ultima)).days if ultima else None,
        },
        "registro": {
            "documentos": len(docs),
            "desde": iso(INICIO_REGISTRO),
            "ultimos_90d": dict(c90),
            "semanas": lista_semanas,
            "meses": lista_meses,
        },
        "hipotecas_ohl": {
            "total_desde_relanzamiento": sum(1 for v in ohl if v["f"] >= relanz),
            "primera": primera_ohl,
            "ultimos_30d": sum(1 for v in ohl if v["f"] >= iso(hoy - timedelta(days=30))),
            "en_casas_de_opendoor": sum(1 for v in ohl if v.get("casa_od")),
            "liberaciones_cartera_antigua": len(ohl_lib),
            "lista": [{"f": v["f"], "rn": v["rn"], "od": bool(v.get("casa_od")), "pg": v.get("pg")}
                      for v in ohl[:60]],
            "ventas_od_desde_primera": len(ventas_ohl_base),
            "ventas_od_con_ohl": sum(1 for v in ventas_ohl_base if v.get("ohl")),
            "attach_pct": (round(sum(1 for v in ventas_ohl_base if v.get("ohl")) / len(ventas_ohl_base) * 100, 1)
                           if len(ventas_ohl_base) >= 10 else None),
        },
        "osn": {
            "fiduciario_90d": c90.get("osn_fiduciario", 0),
            "otros_90d": c90.get("osn_otros", 0) + c90.get("osn_sustitucion", 0) + c90.get("osn_liberacion", 0),
        },
        "ventas_emparejadas": {
            "n": len(ventas_pairs),
            "margen_bruto_pct_mediana": round(statistics.median(margenes), 2) if margenes else None,
            "dias_mediana": mediana(dias),
            "con_perdida": sum(1 for m in margenes if m < 0),
            "lista": ventas_pairs[:60],
        },
        "anuncios_vs_titulo": cruce,
    }


# ----------------------------------------------------------------- otros condados

def actualizar_condados(estado_c, hoy, cartera_maricopa_n=None):
    salida = []
    hoy_s = iso(hoy)
    for cfg in CONDADOS:
        fila = {"id": cfg["id"], "nombre": cfg["nombre"], "ciudad": cfg["ciudad"], "estado": cfg["estado"]}
        st = estado_c.setdefault(cfg["id"], {"ids": {}, "historial": []})
        try:
            dueno = cfg["dueno"]
            filas = arcgis_todos(cfg["url"], f"{dueno} LIKE 'OPENDOOR%'", cfg["id"])
            ids_hoy = {}
            fechas = []
            for a in filas:
                pid = campo(a, cfg["pid"]) or campo(a, ("OBJECTID", "objectid", "FID"))
                pid = str(pid).strip()
                if not pid:
                    continue
                f = a_fecha(campo(a, cfg.get("fecha")))
                if f and f <= hoy:
                    fechas.append(f)
                ids_hoy[pid] = {"f": iso(f), "cp": str(campo(a, cfg.get("cp")) or "")[:5] or None}
            if not ids_hoy:
                raise FuenteCaida("0 parcelas (puede ser un cambio de formato)")
            previas = {k for k, v in st["ids"].items() if v.get("activa")}
            altas = [k for k in ids_hoy if k not in previas]
            bajas = [k for k in previas if k not in ids_hoy]
            primera = not previas
            for k, v in ids_hoy.items():
                reg = st["ids"].get(k) or {"desde": hoy_s}
                if not reg.get("activa"):
                    reg["desde"] = hoy_s
                reg.update({"activa": True, "visto": hoy_s, "f": v["f"], "cp": v["cp"]})
                st["ids"][k] = reg
            for k in bajas:
                st["ids"][k]["activa"] = False
                st["ids"][k]["salida"] = hoy_s
            st["historial"] = [h for h in st["historial"] if h[0] != hoy_s] + [[hoy_s, len(ids_hoy)]]
            st["historial"] = st["historial"][-400:]
            edades = [(hoy - f).days for f in fechas]
            fila.update({
                "casas": len(ids_hoy),
                "altas": None if primera else len(altas),
                "bajas": None if primera else len(bajas),
                "ultima_fecha": iso(max(fechas)) if fechas else None,
                "dias_en_cartera_mediana": mediana(edades),
                "mas_de_90_dias_pct": round(sum(1 for e in edades if e > 90) / len(edades) * 100, 1) if edades else None,
                "historial": st["historial"][-120:],
                "ok": True,
            })
        except Exception as e:  # noqa: BLE001
            fila.update({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}",
                         "casas": (st["historial"][-1][1] if st["historial"] else None),
                         "historial": st["historial"][-120:]})
        salida.append(fila)
    return salida


# ----------------------------------------------------------------- mercado (FRED)

# Las areas metropolitanas donde Opendoor tiene mas casas (codigo CBSA).
METROS = [
    ("38060", "Phoenix", "AZ"), ("19100", "Dallas-Fort Worth", "TX"), ("26420", "Houston", "TX"),
    ("12060", "Atlanta", "GA"), ("16740", "Charlotte", "NC"), ("39580", "Raleigh", "NC"),
    ("45300", "Tampa", "FL"), ("36740", "Orlando", "FL"), ("27260", "Jacksonville", "FL"),
    ("34980", "Nashville", "TN"), ("41700", "San Antonio", "TX"), ("12420", "Austin", "TX"),
    ("US", "United States", "US"),
]
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


def get_texto(url, fuente, intentos=3):
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url_real(url), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                LLAMADAS[fuente] += 1
                t = r.read().decode("utf-8", "ignore")
            time.sleep(PAUSA)
            return t
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(3 * (i + 1))
    raise FuenteCaida(f"{fuente}: {ultimo}")


def csv_fred(texto, meses=25):
    filas = [l.strip().split(",") for l in texto.strip().splitlines() if l.strip()]
    if len(filas) < 2:
        return {}
    cab = filas[0]
    out = {c: [] for c in cab[1:]}
    for f in filas[1:]:
        for i, c in enumerate(cab[1:], start=1):
            if i < len(f) and f[i] not in (".", ""):
                try:
                    out[c].append([f[0][:10], float(f[i])])
                except ValueError:
                    pass
    return {c: v[-meses:] for c, v in out.items()}


def actualizar_mercado():
    """Realtor.com (via FRED): anuncios activos, dias en mercado, anuncios con rebaja y
    precio mediano de lista en los mercados de Opendoor, mas la hipoteca a 30 anos."""
    metros = []
    for cbsa, nombre, st in METROS:
        ids = [f"ACTLISCOU{cbsa}", f"MEDDAYONMAR{cbsa}", f"PRIREDCOU{cbsa}", f"MEDLISPRI{cbsa}"]
        try:
            d = csv_fred(get_texto(FRED_CSV + ",".join(ids), "fred"))
        except FuenteCaida:
            continue
        act, dom, red, pre = (d.get(i, []) for i in ids)
        if not act:
            continue
        fila = {"cbsa": cbsa, "nombre": nombre, "estado": st, "mes": act[-1][0][:7],
                "activos": act[-1][1], "activos_serie": [v for _, v in act],
                "dom": dom[-1][1] if dom else None, "dom_serie": [v for _, v in dom],
                "precio": pre[-1][1] if pre else None}
        if len(act) >= 13 and act[-13][1]:
            fila["activos_yoy_pct"] = round((act[-1][1] / act[-13][1] - 1) * 100, 1)
        if len(dom) >= 13:
            fila["dom_hace_1a"] = dom[-13][1]
        if red and act and red[-1][0] == act[-1][0] and act[-1][1]:
            fila["con_rebaja_pct"] = round(red[-1][1] / act[-1][1] * 100, 1)
            if len(red) >= 13 and len(act) >= 13 and act[-13][1]:
                fila["con_rebaja_pct_hace_1a"] = round(red[-13][1] / act[-13][1] * 100, 1)
        metros.append(fila)
    hip = []
    try:
        hip = csv_fred(get_texto(FRED_CSV + "MORTGAGE30US", "fred"), meses=60).get("MORTGAGE30US", [])
    except FuenteCaida:
        pass
    if not metros and not hip:
        raise FuenteCaida("FRED no respondio")
    return {"metros": metros, "hipoteca30": hip,
            "fuente": "Realtor.com via FRED (monthly) · Freddie Mac PMMS via FRED (weekly)"}


# ----------------------------------------------------------------- panel

def construir_panel(hoy):
    latest = cargar("latest.json", {})
    serie = []
    for ruta in sorted(glob.glob(os.path.join(DATOS, "dias", "*.json")))[-365:]:
        try:
            with open(ruta, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        pr = d.get("precios") or {}
        emp = d.get("empleo") or {}
        serie.append({
            "f": d.get("fecha"), "total": d.get("total"), "altas": d.get("nuevos"),
            "bajas": d.get("retirados"), "reap": d.get("reapariciones"),
            "primera": d.get("primera_pasada"), "lastmod": d.get("lastmod_hoy"),
            "valor_salido": d.get("valor_salido_hoy"), "dias_med": d.get("dias_listadas_mediana"),
            "cob": pr.get("cobertura_pct"), "p_med": pr.get("precio_mediana"),
            "p_medio": pr.get("precio_medio"), "recorte_pct": pr.get("con_recorte_pct"),
            "recortes": pr.get("recortes_hoy"), "valor_est": pr.get("valor_listado_estimado"),
            "empleo": emp.get("total"),
        })
    reg = cargar(os.path.join("condado", "latest.json"), None)
    inventario = dict(latest)
    if isinstance(inventario.get("empleo"), dict):
        inventario["empleo"] = dict(inventario["empleo"])
    panel = {
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inventario": inventario,
        "serie": serie,
        "registros": reg,
        "version": 3,
    }
    # la base de datos de la pagina admite documentos de hasta 256 KiB
    def tam():
        return len(json.dumps(panel, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    recortes = [("serie", 240), ("serie", 150), ("serie", 90)]
    while tam() > 200_000 and recortes:
        k, n = recortes.pop(0)
        panel[k] = panel[k][-n:]
    if tam() > 200_000 and isinstance(inventario.get("empleo"), dict):
        inventario["empleo"].pop("puestos", None)
    if tam() > 200_000 and reg and reg.get("maricopa"):
        for sec, campo_lista in (("ventas_emparejadas", "lista"), ("hipotecas_ohl", "lista")):
            try:
                reg["maricopa"][sec][campo_lista] = reg["maricopa"][sec][campo_lista][:25]
            except Exception:  # noqa: BLE001
                pass
    guardar("panel.json", panel, compacto=True)
    return os.path.getsize(os.path.join(DATOS, "panel.json"))


# ----------------------------------------------------------------- principal

def main():
    hoy = datetime.now(timezone.utc).date()
    if os.environ.get("HOY_PRUEBA"):                  # solo pruebas
        hoy = date.fromisoformat(os.environ["HOY_PRUEBA"])
    estado = cargar(os.path.join("condado", "estado.json"), {})
    errores = {}
    resumen = {"fecha": iso(hoy), "obtenido_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # ---- Maricopa
    docs = cargar(os.path.join("condado", "maricopa_documentos.json"), {})
    cartera = cargar(os.path.join("condado", "maricopa_cartera.json"), {})
    ventas = cargar(os.path.join("condado", "maricopa_ventas.json"), [])
    cache = cargar(os.path.join("condado", "maricopa_anuncios.json"), {})
    cruce = None
    try:
        n, det, resto = actualizar_registro(docs, hoy, estado)
        print(f"Maricopa registro: {n} documentos nuevos, {det} detalles leidos, {resto} pendientes para manana")
    except Exception as e:  # noqa: BLE001
        errores["maricopa_registro"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    try:
        casas, salidas = actualizar_cartera(cartera, ventas, hoy)
        print(f"Maricopa catastro: {casas} parcelas de Opendoor, {salidas} salidas hoy")
    except Exception as e:  # noqa: BLE001
        errores["maricopa_catastro"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    try:
        r = resolver_direccion(docs, hoy)
        cruzar_financiacion(docs)
        print(f"Maricopa: {r} traspasos clasificados hoy como compra o venta")
    except Exception as e:  # noqa: BLE001
        errores["maricopa_cruce"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    try:
        cruce = cruzar_anuncios(cartera, docs, cache, hoy)
        print(f"Maricopa anuncios vs titulo: {cruce['clases']}")
    except Exception as e:  # noqa: BLE001
        errores["maricopa_anuncios"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    guardar(os.path.join("condado", "maricopa_documentos.json"), docs, compacto=True)
    guardar(os.path.join("condado", "maricopa_cartera.json"), cartera, compacto=True)
    guardar(os.path.join("condado", "maricopa_ventas.json"), ventas)
    guardar(os.path.join("condado", "maricopa_anuncios.json"), cache, compacto=True)

    try:
        resumen["maricopa"] = resumen_maricopa(docs, cartera, ventas, cruce, hoy)
    except Exception as e:  # noqa: BLE001
        errores["maricopa_resumen"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    # ---- otros condados
    estado_c = cargar(os.path.join("condado", "condados.json"), {})
    try:
        resumen["condados"] = actualizar_condados(estado_c, hoy)
        guardar(os.path.join("condado", "condados.json"), estado_c, compacto=True)
    except Exception as e:  # noqa: BLE001
        errores["condados"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    # ---- contexto de mercado
    try:
        resumen["mercado"] = actualizar_mercado()
        print(f"Mercado: {len(resumen['mercado']['metros'])} areas, "
              f"{len(resumen['mercado']['hipoteca30'])} semanas de hipoteca")
    except Exception as e:  # noqa: BLE001
        errores["mercado"] = f"{type(e).__name__}: {e}"

    resumen["errores"] = errores
    resumen["llamadas"] = dict(LLAMADAS)
    resumen["aviso"] = ("Registros publicos de los condados. El catastro va con ~2 semanas de retraso "
                        "sobre el registro. Hipotecas de Opendoor Home Loans: solo Maricopa, no es el "
                        "volumen nacional. Margen bruto = venta - compra, sin reformas, comisiones ni "
                        "costes de tenencia. Texas no publica precios de venta.")
    guardar(os.path.join("condado", "estado.json"), estado)
    guardar(os.path.join("condado", "latest.json"), resumen)

    hist = cargar(os.path.join("condado", "historial.csv.json"), [])
    m = resumen.get("maricopa") or {}
    fila = {"f": iso(hoy),
            "cartera_maricopa": (m.get("cartera") or {}).get("casas"),
            "ohl_total": (m.get("hipotecas_ohl") or {}).get("total_desde_relanzamiento")}
    for c in resumen.get("condados") or []:
        fila[c["id"]] = c.get("casas")
    hist = [h for h in hist if h.get("f") != fila["f"]] + [fila]
    guardar(os.path.join("condado", "historial.csv.json"), hist[-500:])

    tam = construir_panel(hoy)
    print(f"panel.json: {tam / 1024:.0f} KB   llamadas={dict(LLAMADAS)}   errores={list(errores)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        # aunque algo reviente, se intenta dejar el panel compuesto con lo que haya
        try:
            construir_panel(datetime.now(timezone.utc).date())
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
