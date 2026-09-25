#!/usr/bin/env python3
"""Importa las PARADAS REALES de los mapas de Google de Tiadhe (uno por línea).

    python importar_mapas.py                       # todas las líneas (descarga de tiadhe.com)
    python importar_mapas.py 12 01                 # solo esas líneas
    python importar_mapas.py --mid 01=1cPK1PhZdYUCRRMaxQGcglNZIybFh1mo
    python importar_mapas.py --kml 01=linea01.kml  # KML/KMZ descargado a mano (recomendado si la descarga falla)

KML de un mapa:  https://www.google.com/maps/d/kml?mid=<ID>&forcekml=1

Cada línea se VALIDA: si el mapa no encaja (trazado incompleto, ramas, paradas lejos del trazado)
la línea se guarda como no válida y la app sigue usando el esqueleto aproximado, avisando.
Resultado: datos/paradas_reales.json
"""
import io, json, math, re, sys, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

DATOS = Path(__file__).parent / "datos"
UA = {"User-Agent": "Mozilla/5.0 guaguas-fuerteventura"}
LINEAS = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "14", "15", "16", "18", "25", "111"]
KML = "https://www.google.com/maps/d/kml?mid={}&forcekml=1"
MAX_LEJOS_KM = 0.3      # una parada más lejos que esto del trazado no se usa
MAX_RAMAS = 1.8         # suma de saltos entre paradas / longitud del trazado (1.0 = recorrido lineal)


def km(a, b):
    p = math.pi / 180
    h = math.sin((b[0] - a[0]) * p / 2) ** 2 + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin((b[1] - a[1]) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def buscar_mids(num):
    import requests
    for url in (f"https://tiadhe.com/en/line-{num}/", f"https://tiadhe.com/linea-{num}/"):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            mids = list(dict.fromkeys(re.findall(r"maps/d/(?:embed|viewer)\?mid=([\w-]+)", r.text)))
            if mids:
                return mids
        except requests.RequestException:
            pass
    return []


def leer_kml(datos):
    """bytes de KML o KMZ → (paradas [(nombre, lat, lon)], trazados [[(lat, lon), ...], ...])"""
    if datos[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(datos))
        datos = z.read(next(n for n in z.namelist() if n.lower().endswith(".kml")))
    raiz = ET.fromstring(datos)
    paradas, trazados = [], []
    for pm in raiz.iter():
        if not pm.tag.endswith("Placemark"):
            continue
        nombre = next((e.text.strip() for e in pm if e.tag.endswith("name") and e.text), "")
        for e in pm.iter():
            if e.tag.endswith("coordinates") and e.text:
                pts = [tuple(map(float, c.split(",")[:2])) for c in e.text.split()]
                pts = [(la, lo) for lo, la in pts]  # KML: lon,lat
                if len(pts) == 1:
                    paradas.append((nombre, *pts[0]))
                elif len(pts) > 1:
                    trazados.append(pts)
    return paradas, trazados


def longitud(t):
    return sum(km(a, b) for a, b in zip(t, t[1:]))


def encadenar(trazados, origen=None):
    """Une todos los tramos del mapa en un único recorrido (crece por ambos extremos desde el tramo
    más largo). Devuelve (recorrido, tramos_sueltos_sin_conectar)."""
    restos = [list(t) for t in trazados if len(t) > 1]
    cadena = restos.pop(max(range(len(restos)), key=lambda i: longitud(restos[i])))
    while restos:
        mejor = None
        for j, t in enumerate(restos):
            for ec, pc in ((0, cadena[0]), (1, cadena[-1])):
                for et, pt in ((0, t[0]), (1, t[-1])):
                    d = km(pc, pt)
                    if mejor is None or d < mejor[0]:
                        mejor = (d, j, ec, et)
        d, j, ec, et = mejor
        if d > 1.0:
            break
        t = restos.pop(j)
        cadena = cadena + (t if et == 0 else t[::-1]) if ec == 1 else (t if et == 1 else t[::-1]) + cadena
    if origen and km(origen, cadena[0]) > km(origen, cadena[-1]):
        cadena = cadena[::-1]
    return cadena, restos


def proyectar(traza, acum, lat, lon):
    """(distancia_al_trazado_km, km_recorridos_hasta_el_punto_más_cercano)"""
    k, mejor = math.cos(math.radians(lat)), (1e9, 0.0)
    for j in range(len(traza) - 1):
        (ay, ax), (by, bx) = traza[j], traza[j + 1]
        dx, dy = (bx - ax) * k, by - ay
        L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0, min(1, (((lon - ax) * k) * dx + (lat - ay) * dy) / L))
        d = km((ay + t * dy, ax + t * (bx - ax)), (lat, lon))
        if d < mejor[0]:
            mejor = (d, acum[j] + t * (acum[j + 1] - acum[j]))
    return mejor


def decimar(t, n=500):
    """Reduce el trazado a ~n puntos sin duplicar el último."""
    paso = max(1, len(t) // n)
    m = [[round(a, 5), round(b, 5)] for a, b in t[::paso]]
    ult = [round(t[-1][0], 5), round(t[-1][1], 5)]
    if m[-1] != ult:
        m.append(ult)
    return m


def construir(paradas, trazados, origen=None):
    if not trazados:
        return dict(valida=False, avisos=["el mapa no tiene ningún trazado (línea)"], paradas=[], trazado=[])
    avisos, invalida = [], []
    cadena, sueltos = encadenar(trazados, origen)
    acum = [0.0]
    for a, b in zip(cadena, cadena[1:]):
        acum.append(acum[-1] + km(a, b))
    vistos, res, lejos = [], [], []
    for n, la, lo in paradas:
        if any(v[0] == n and km((v[1], v[2]), (la, lo)) < 0.6 for v in vistos):
            continue  # misma parada en el otro sentido de la calzada
        vistos.append((n, la, lo))
        d, k = proyectar(cadena, acum, la, lo)
        if d > MAX_LEJOS_KM:
            lejos.append((n, d))
        else:
            res.append(dict(n=n or "(sin nombre)", lat=la, lon=lo, k=round(k, 3)))
    res.sort(key=lambda p: p["k"])
    total = len(vistos)
    if not res:
        invalida.append("ninguna parada está cerca del trazado")
    if lejos:
        msg = f"{len(lejos)} de {total} paradas a más de {int(MAX_LEJOS_KM * 1000)} m del trazado (p. ej. {lejos[0][0]!r} a {lejos[0][1]:.1f} km)"
        (invalida if len(lejos) > 0.25 * total else avisos).append(msg)
    if origen:
        d0 = km(origen, cadena[0])
        (invalida if d0 > 6 else avisos).extend([f"el trazado empieza a {d0:.1f} km del origen esperado"] if d0 > 3 else [])
    if len(res) > 1:
        saltos = sum(km((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(res, res[1:]))
        ramas = saltos / max(res[-1]["k"] - res[0]["k"], 0.1)
        if ramas > MAX_RAMAS:
            invalida.append(f"el recorrido no es lineal (índice {ramas:.1f}): tiene ramas o bucles")
    if sueltos:
        avisos.append(f"{len(sueltos)} tramos del mapa no conectan con el recorrido")
    return dict(valida=not invalida, avisos=invalida + avisos, km_total=round(acum[-1], 1),
                descartadas=[n for n, _ in lejos][:20], paradas=res,
                trazado=[decimar(cadena)] + [decimar(t, 200) for t in sueltos])


def main(args):
    manual, kmls, lineas, i = {}, {}, [], 0
    while i < len(args):
        if args[i] in ("--mid", "--kml"):
            n, v = args[i + 1].split("=", 1)
            (manual if args[i] == "--mid" else kmls)[n] = v
            i += 2
        else:
            lineas.append(args[i]); i += 1
    lineas = lineas or sorted(set(manual) | set(kmls)) or LINEAS
    base = json.loads((DATOS / "lineas_base.json").read_text(encoding="utf-8"))["lineas"]
    p_out = DATOS / "paradas_reales.json"
    salida = json.loads(p_out.read_text(encoding="utf-8")) if p_out.exists() else {}
    for num in lineas:
        try:
            if num in kmls:
                datos, fuente = Path(kmls[num]).read_bytes(), kmls[num]
            else:
                import requests
                mids = [manual[num]] if num in manual else buscar_mids(num)
                if not mids:
                    print(f"L{num}: no encuentro el mapa en la web (usa --mid {num}=ID o --kml {num}=archivo.kml)"); continue
                if len(mids) > 1:
                    print(f"L{num}: la página tiene {len(mids)} mapas; uso el primero ({mids[0]}). Otros: {', '.join(mids[1:])}")
                r = requests.get(KML.format(mids[0]), headers=UA, timeout=60); r.raise_for_status()
                datos, fuente = r.content, mids[0]
            paradas, trazados = leer_kml(datos)
            o = base.get(num, {}).get("paradas", [None])[0]
            salida[num] = construir(paradas, trazados, (o["lat"], o["lon"]) if o else None)
            s = salida[num]
            print(f"L{num}: {'OK ' if s['valida'] else 'DESCARTADA'} {len(s['paradas'])}/{len(paradas)} paradas, {len(trazados)} tramos ({fuente})")
            for a in s["avisos"]:
                print("     ·", a)
        except Exception as e:
            print(f"L{num}: error: {e}")
    p_out.write_text(json.dumps(salida, ensure_ascii=False), encoding="utf-8")
    print("Guardado", p_out)


if __name__ == "__main__":
    main(sys.argv[1:])