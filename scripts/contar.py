#!/usr/bin/env python3
"""
Contador propio del inventario publicado de Opendoor ($OPEN).

Lee el sitemap publico de listados, cuenta las casas que la empresa tiene
anunciadas, las reparte por estado y codigo postal, y mantiene un registro
por propiedad con la primera y la ultima vez que se vio.

Eso ultimo es lo que no publica nadie: permite saber cuantas casas entran
y salen cada dia, y cuanto tiempo lleva listada cada una.

Fuente: https://www.opendoor.com/sitemaps/listings.xml (permitido por robots.txt)
Se ejecuta una vez al dia. No golpea la web: es una sola peticion.
"""

import gzip
import io
import json
import os
import re
import statistics
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone, date

SITEMAP = "https://www.opendoor.com/sitemaps/listings.xml"
UA = "opendoor-inventory-counter/1.0 (personal research; one request per day)"
DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

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
                import time
                time.sleep(5 * (i + 1))
    raise SystemExit(f"No se pudo descargar {url}: {ultimo}")


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

    salidas = []
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

    por_estado = Counter(p["estado"] for p in unicas.values())
    por_cp = Counter(p["cp"] for p in unicas.values())

    # Antiguedad de lo que sigue listado, solo desde que contamos nosotros.
    antiguedad = []
    for aid in ids_hoy:
        try:
            antiguedad.append((date.fromisoformat(hoy) - date.fromisoformat(registro[aid]["primera"])).days)
        except Exception:  # noqa: BLE001
            pass

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
        "aviso": ("Conteo propio sobre el sitemap publico de Opendoor. Solo ve lo que esta "
                  "anunciado a la venta: no ve lo comprado y aun sin listar ni lo que esta "
                  "bajo contrato. Una casa que desaparece puede haberse vendido, retirado o "
                  "vuelto a listar con otra direccion: NO afirmar que se ha vendido. "
                  "La antiguedad se cuenta desde que empezamos a medir, no desde que se listo."),
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

    print(f"{hoy}  total={len(unicas)}  nuevos={len(nuevos)}  retirados={len(retirados)}  "
          f"estados={len(por_estado)}  sin_patron={sin_patron}")
    if snapshot["primera_pasada"]:
        print("Primera pasada: 'nuevos' es el inventario entero, no entradas reales. "
              "El primer dato de altas y bajas util llega manana.")


if __name__ == "__main__":
    sys.exit(main())
