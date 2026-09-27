#!/usr/bin/env python3
"""
Contador propio del inventario publicado de Opendoor ($OPEN).

Lee el sitemap publico de listados, cuenta las casas que la empresa tiene
anunciadas, las reparte por estado y codigo postal, y mantiene un registro
por propiedad con la primera y la ultima vez que se vio.

Eso ultimo es lo que no publica nadie: permite saber cuantas casas entran
y salen cada dia, y cuanto tiempo lleva listada cada una.

Ademas (desde la v2), visita hasta MAX_FICHAS_POR_DIA fichas individuales de
propiedad cada dia -- las nuevas de hoy primero, luego las que llevan mas
tiempo sin comprobarse -- y extrae de ahi precio de lista, precio por pie
cuadrado, pago mensual estimado, la concesion de hipoteca si la hay, y la
fecha real de publicacion segun el historial de precios de la propia ficha.
Con eso se calculan medias de precio, tasa de recortes y una version mas
exacta de "dias en el mercado" que la que solo cuenta desde que medimos
nosotros. La cobertura crece poco a poco: no se visita el inventario entero
de golpe, por respeto al servidor y porque no hace falta.

Fuente: https://www.opendoor.com/sitemaps/listings.xml (permitido por robots.txt)
Las fichas de propiedad tambien estan permitidas por robots.txt (solo bloquea
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
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone, date

SITEMAP = "https://www.opendoor.com/sitemaps/listings.xml"
UA = "opendoor-inventory-counter/2.0 (investigacion personal; 1 sitemap + hasta 250 fichas de propiedad al dia)"
DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Cuantas fichas de propiedad se visitan como maximo en cada pasada.
# A este ritmo el inventario activo entero se cubre en unas tres semanas,
# y luego se va refrescando solo (para detectar recortes de precio).
MAX_FICHAS_POR_DIA = 250
PAUSA_ENTRE_FICHAS = 0.4  # segundos, por no golpear el servidor sin necesidad

# /properties/<Calle>-<Ciudad>-<ST>-<CP>/aid_<uuid>
# El estado y el codigo postal son fiables. La ciudad NO se extrae:
# calle y ciudad van ambas con guiones y no hay forma segura de separarlas.
PATRON = re.compile(r"-([A-Z]{2})-(\d{5})(?:-\d{4})?/aid_([0-9a-fA-F-]{8,})\s*$")


def descargar(url, intentos=4):
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={
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


def descargar_ficha(url, intentos=2, espera=15):
    """Descarga una ficha de propiedad. A diferencia de descargar(), falla
    en silencio (devuelve None) porque esto se hace cientos de veces por
    pasada y una ficha que falla hoy se reintenta manana sola."""
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=espera) as r:
                crudo = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or crudo[:2] == b"\x1f\x8b":
                    crudo = gzip.decompress(crudo)
                return crudo
        except Exception:  # noqa: BLE001
            if i < intentos - 1:
                time.sleep(2)
    return None


def extraer_precio(cuerpo):
    """Extrae datos de precio de una ficha de propiedad a partir del HTML
    crudo, sin depender de libreria externa (solo texto y regex).

    Opendoor ensena el precio de venta con un descuento estandar del 1%
    sobre el precio de lista ("$X below list"). Cuando aparecen los dos
    numeros juntos, el precio de lista es la suma; si solo aparece el
    descuento, se estima como el 100x de esa cifra y se marca como tal.
    """
    try:
        texto = cuerpo.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None

    plano = re.sub(r"<[^>]+>", " ", texto)
    plano = html_mod.unescape(plano)
    plano = re.sub(r"\s+", " ", plano)

    resultado = {}

    m = re.search(r"\$([\d,]{4,9})[^$]{0,60}?\$([\d,]{2,7})\s+below list", plano, re.I)
    if m:
        mostrado = int(m.group(1).replace(",", ""))
        descuento = int(m.group(2).replace(",", ""))
        resultado["precio_lista"] = mostrado + descuento
        resultado["precio_estimado"] = False
    else:
        m2 = re.search(r"\$([\d,]{2,7})\s+below list", plano, re.I)
        if m2:
            descuento = int(m2.group(1).replace(",", ""))
            resultado["precio_lista"] = descuento * 100
            resultado["precio_estimado"] = True

    if "precio_lista" not in resultado or not (20000 <= resultado["precio_lista"] <= 5000000):
        return None  # sin precio fiable (o fuera de rango razonable): no se guarda nada

    m = re.search(r"\$([\d,]{2,6})\s*/\s*sqft", plano, re.I)
    if m:
        ppsf = int(m.group(1).replace(",", ""))
        if 10 <= ppsf <= 2000:
            resultado["precio_por_sqft"] = ppsf
            resultado["sqft"] = round(resultado["precio_lista"] / ppsf)

    m = re.search(r"\$([\d,]{3,6})\s*/\s*month", plano, re.I)
    if m:
        resultado["pago_estimado_mes"] = int(m.group(1).replace(",", ""))

    m = re.search(r"\$([\d,]{3,6})\s+MORTGAGE CONCESSION", plano, re.I)
    if m:
        resultado["concesion_hipoteca"] = int(m.group(1).replace(",", ""))

    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})[^a-zA-Z]{0,12}for sale", plano)
    if m:
        try:
            resultado["fecha_listado_mls"] = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except Exception:  # noqa: BLE001
            pass

    return resultado


def elegir_para_precio(registro, ids_hoy, nuevos_hoy, maximo):
    """Prioriza lo nuevo de hoy (precio nunca visto) y luego lo que lleva
    mas tiempo sin comprobarse, para que la cobertura crezca sin repetirse."""
    nuevas = list(nuevos_hoy)
    resto = [aid for aid in ids_hoy if aid not in nuevos_hoy]
    resto.sort(key=lambda aid: registro[aid].get("precio_comprobado") or "0000-00-00")
    return (nuevas + resto)[:maximo]


def parsear(crudo):
    """Devuelve (lista de propiedades, lastmod del sitemap si lo trae)."""
    if crudo[:2] == b"\x1f\x8b":          # por si llega comprimido sin cabecera
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
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def guardar(nombre, obj):
    ruta = os.path.join(DATOS, nombre)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def anadir_csv(nombre, cabecera, filas, fecha):
    """Escribe las filas del dia. Si ya habia filas de esa fecha (relanzamiento
    manual del flujo), las sustituye en vez de duplicarlas."""
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


def main():
    ahora = datetime.now(timezone.utc)
    hoy = ahora.date().isoformat()

    crudo = descargar(SITEMAP)
    props, sin_patron = parsear(crudo)
    if not props:
        raise SystemExit("El sitemap no devolvio ninguna propiedad: no se escribe nada.")

    # Duplicados: nos quedamos con una entrada por identificador.
    unicas = {p["aid"]: p for p in props}
    ids_hoy = set(unicas)

    registro = cargar("propiedades.json", {})
    ids_ayer = {a for a, v in registro.items() if v.get("activa")}

    nuevos = sorted(ids_hoy - ids_ayer)
    retirados = sorted(ids_ayer - ids_hoy)

    # Actualiza el registro por propiedad.
    for aid, p in unicas.items():
        r = registro.get(aid)
        if r is None:
            registro[aid] = {
                "primera": hoy, "ultima": hoy, "activa": True,
                "estado": p["estado"], "cp": p["cp"], "url": p["url"],
                "dias": 1, "veces_retirada": 0,
            }
        else:
            if r.get("ultima") != hoy:          # no contar dos veces si se relanza
                r["dias"] = r.get("dias", 0) + 1
            r["activa"] = True
            r["ultima"] = hoy
            r.pop("salida", None)
            r["estado"] = p["estado"]
            r["cp"] = p["cp"]
            r["url"] = p["url"]

    salidas, salidas_reales = [], []
    for aid in retirados:
        r = registro[aid]
        r["activa"] = False
        r["salida"] = hoy
        r["veces_retirada"] = r.get("veces_retirada", 0) + 1
        try:
            d = (date.fromisoformat(hoy) - date.fromisoformat(r["primera"])).days
        except Exception:  # noqa: BLE001
            d = None
        if d is not None:
            r["dias_hasta_salir"] = d
            salidas.append(d)
        if r.get("fecha_listado_mls"):
            try:
                dr = (date.fromisoformat(hoy) - date.fromisoformat(r["fecha_listado_mls"])).days
                if dr >= 0:
                    r["dias_mercado_real"] = dr
                    salidas_reales.append(dr)
            except Exception:  # noqa: BLE001
                pass

    por_estado = Counter(p["estado"] for p in unicas.values())
    por_cp = Counter(p["cp"] for p in unicas.values())

    # Antiguedad de lo que sigue listado, solo desde que contamos nosotros.
    antiguedad = []
    for aid in ids_hoy:
        try:
            antiguedad.append((date.fromisoformat(hoy) - date.fromisoformat(registro[aid]["primera"])).days)
        except Exception:  # noqa: BLE001
            pass

    # --- Precios: se visitan hasta MAX_FICHAS_POR_DIA fichas individuales ---
    objetivo = elegir_para_precio(registro, ids_hoy, nuevos, MAX_FICHAS_POR_DIA)
    consultadas = con_precio = errores = 0
    for aid in objetivo:
        url = unicas[aid]["url"]
        cuerpo = descargar_ficha(url)
        consultadas += 1
        r = registro[aid]
        r["precio_comprobado"] = hoy
        if cuerpo is None:
            errores += 1
            time.sleep(PAUSA_ENTRE_FICHAS)
            continue
        datos = extraer_precio(cuerpo)
        if datos is None:
            errores += 1
            time.sleep(PAUSA_ENTRE_FICHAS)
            continue
        con_precio += 1
        anterior = r.get("precio_lista")
        if anterior is not None and datos["precio_lista"] < anterior:
            r["recortes"] = r.get("recortes", 0) + 1
            r["ultimo_recorte"] = hoy
            r["ultimo_recorte_de_a"] = [anterior, datos["precio_lista"]]
        r["precio_lista"] = datos["precio_lista"]
        r["precio_estimado"] = datos.get("precio_estimado", False)
        if r.get("precio_lista_inicial") is None:
            r["precio_lista_inicial"] = datos["precio_lista"]
        for campo in ("precio_por_sqft", "sqft", "pago_estimado_mes", "concesion_hipoteca", "fecha_listado_mls"):
            if campo in datos:
                r[campo] = datos[campo]
        time.sleep(PAUSA_ENTRE_FICHAS)

    con_datos = [registro[aid] for aid in ids_hoy if registro[aid].get("precio_lista")]
    precios = [r["precio_lista"] for r in con_datos]
    ppsf_l = [r["precio_por_sqft"] for r in con_datos if r.get("precio_por_sqft")]
    pagos = [r["pago_estimado_mes"] for r in con_datos if r.get("pago_estimado_mes")]
    concesiones = [r["concesion_hipoteca"] for r in con_datos if r.get("concesion_hipoteca")]
    con_recorte = sum(1 for r in con_datos if r.get("recortes"))

    resumen_precios = None
    if con_datos:
        resumen_precios = {
            "cobertura": len(con_datos),
            "cobertura_pct": round(len(con_datos) / len(unicas) * 100, 1),
            "precio_medio": round(statistics.mean(precios)),
            "precio_mediana": round(statistics.median(precios)),
            "precio_por_sqft_medio": (round(statistics.mean(ppsf_l)) if ppsf_l else None),
            "pago_estimado_medio": (round(statistics.mean(pagos)) if pagos else None),
            "con_concesion_pct": round(len(concesiones) / len(con_datos) * 100, 1),
            "concesion_media": (round(statistics.mean(concesiones)) if concesiones else None),
            "con_recorte_pct": round(con_recorte / len(con_datos) * 100, 1),
            "consultadas_hoy": consultadas,
            "con_precio_hoy": con_precio,
            "errores_hoy": errores,
            "nota": ("Cobertura parcial y creciente: se comprueban hasta "
                     f"{MAX_FICHAS_POR_DIA} fichas de propiedad al dia, asi que "
                     "el conjunto tarda semanas en cubrir todo el inventario activo. "
                     "El precio de venta que ensena Opendoor ya lleva su descuento "
                     "estandar del 1% sobre el precio de lista; aqui se deshace ese "
                     "descuento para dar el precio de lista real."),
        }

    snapshot = {
        "fecha": hoy,
        "obtenido_utc": ahora.isoformat(timespec="seconds"),
        "fuente": SITEMAP,
        "total": len(unicas),
        "nuevos": len(nuevos),
        "retirados": len(retirados),
        "primera_pasada": not ids_ayer,
        "por_estado": dict(sorted(por_estado.items(), key=lambda x: -x[1])),
        "cp_top_25": dict(por_cp.most_common(25)),
        "estados_distintos": len(por_estado),
        "cp_distintos": len(por_cp),
        "urls_sin_patron": sin_patron,
        "dias_listadas_mediana": (statistics.median(antiguedad) if antiguedad else None),
        "dias_hasta_salir_mediana": (statistics.median(salidas) if salidas else None),
        "dias_mercado_real_mediana": (statistics.median(salidas_reales) if salidas_reales else None),
        "precios": resumen_precios,
        "aviso": ("Conteo propio sobre el sitemap publico de Opendoor. Solo ve lo que esta "
                  "anunciado a la venta: no ve lo comprado y aun sin listar ni lo que esta "
                  "bajo contrato. Una casa que desaparece puede haberse vendido, retirado o "
                  "vuelto a listar con otra direccion: NO afirmar que se ha vendido. "
                  "La antiguedad se cuenta desde que empezamos a medir, no desde que se listo, "
                  "salvo 'dias_mercado_real_mediana', que usa la fecha de publicacion real "
                  "cuando la ficha de la propiedad la trae."),
    }

    guardar("latest.json", snapshot)
    guardar(os.path.join("dias", f"{hoy}.json"), snapshot)
    guardar("propiedades.json", registro)

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
          f"estados={len(por_estado)}  sin_patron={sin_patron}")
    print(f"precios: consultadas={consultadas}  con_precio={con_precio}  errores={errores}  "
          f"cobertura_total={len(con_datos)}/{len(unicas)}"
          + (f"  precio_medio=${resumen_precios['precio_medio']:,}" if resumen_precios else ""))
    if consultadas and con_precio == 0:
        print("AVISO: ninguna ficha dio precio legible. Puede que Opendoor haya cambiado el "
              "formato de la pagina, o que el precio se cargue con JavaScript y no este en "
              "el HTML crudo. Revisar extraer_precio() antes de fiarse de 'precios' en latest.json.")
    if snapshot["primera_pasada"]:
        print("Primera pasada: 'nuevos' es el inventario entero, no entradas reales. "
              "El primer dato de altas y bajas util llega manana.")


if __name__ == "__main__":
    sys.exit(main())
