#!/usr/bin/env python3
"""Guaguas de Fuerteventura (Tiadhe): ¿qué guagua pasa por mi parada?

    python guaguas.py                       # modo interactivo
    python guaguas.py -p "Las Playitas"     # consulta directa
    python guaguas.py -p "Antigua" --mapa   # además abre el mapa de la isla
    python guaguas.py --actualizar-osm      # vuelve a descargar las paradas de OpenStreetMap
    python guaguas.py -p X --hora "2026-09-25 10:15"   # simular otra fecha/hora

Datos (carpeta datos/): horarios.json, lineas_base.json (esqueleto aproximado + duraciones),
paradas_reales.json (importar_mapas.py), paradas_osm.json (caché de OpenStreetMap).
"""
import argparse, json, math, re, unicodedata, webbrowser
from datetime import datetime, timedelta
from difflib import get_close_matches
from pathlib import Path

try:
    import holidays
    FESTIVOS = holidays.Spain(subdiv="CN")
except ImportError:
    FESTIVOS = {}

DATOS = Path(__file__).parent / "datos"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
BBOX = "28.03,-14.60,28.80,-13.80"
FACTOR_CARRETERA, MIN_POR_PARADA, RADIO_OSM_KM = 1.3, 0.7, 1.5
COLORES = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000",
           "#808000", "#000075", "#f032e6", "#469990", "#bfa100", "#aa6e28", "#d2054e", "#2a7f62", "#5a5a5a", "#0b6e99"]

_RECORRIDOS = {}  # caché: (linea, sentido, h, via, inicio, fin) → lista de paradas del tramo


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def limpiar(n):
    """'999 Estacion Puerto' → 'Estacion Puerto' (los mapas de Tiadhe llevan el nº de parada delante)."""
    return re.sub(r"\s+", " ", re.sub(r"^\s*\d+\s+", "", n or "")).strip() or "(sin nombre)"


def km(a, b):
    p = math.pi / 180
    h = math.sin((b["lat"] - a["lat"]) * p / 2) ** 2 + math.cos(a["lat"] * p) * math.cos(b["lat"] * p) * math.sin((b["lon"] - a["lon"]) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def tipo_dia(fecha):
    if fecha in FESTIVOS or fecha.weekday() == 6:
        return "DF"
    return "S" if fecha.weekday() == 5 else "LV"


# --------------------------------------------------------------------- datos
def cargar():
    horarios = json.loads((DATOS / "horarios.json").read_text(encoding="utf-8"))
    base = json.loads((DATOS / "lineas_base.json").read_text(encoding="utf-8"))["lineas"]
    for l in base.values():
        l["esqueleto"] = l["paradas"]  # núcleos con coordenadas aproximadas (para localizar "Gran Tarajal", etc.)
    real = DATOS / "paradas_reales.json"
    if real.exists():
        for num, r in json.loads(real.read_text(encoding="utf-8")).items():
            if num not in base:
                continue
            if not r.get("valida", True) or not r.get("paradas"):
                print(f"(Línea {num}: datos reales descartados, uso el esqueleto aproximado. {(r.get('avisos') or ['vacío'])[0]})")
                continue
            reales = [{**p, "orig": p["n"], "n": limpiar(p["n"])} for p in r["paradas"]]
            desvios = []  # variantes (Playitas, Triquivijate, Ajuy…) que no salen en el mapa general
            for p in base[num]["esqueleto"]:
                if "opt" in p:
                    cerca = min(reales, key=lambda q: km(p, q))
                    desvios.append({**p, "k": cerca["k"], "d": km(p, cerca)})
            tr = r["trazado"]
            if tr and isinstance(tr[0][0], (int, float)):
                tr = [tr]
            base[num] = {**base[num], "paradas": sorted(reales + desvios, key=lambda p: (p["k"], "d" in p)), "trazado": tr, "real": True}
    for num, l in base.items():
        for i, p in enumerate(l["paradas"]):
            p["id"] = f"{num}:{i}"
    _RECORRIDOS.clear()
    return horarios, base


def paradas_osm(refrescar=False):
    cache = DATOS / "paradas_osm.json"
    if cache.exists() and not refrescar:
        return json.loads(cache.read_text(encoding="utf-8"))
    import requests
    q = f'[out:json][timeout:90];node["highway"="bus_stop"]({BBOX});out body;'
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": q}, headers={"User-Agent": "guaguas-fuerteventura/1.0"}, timeout=120)
            r.raise_for_status()
            paradas = [{"nombre": e.get("tags", {}).get("name", ""), "lat": e["lat"], "lon": e["lon"]} for e in r.json()["elements"]]
            cache.write_text(json.dumps(paradas, ensure_ascii=False), encoding="utf-8")
            print(f"Descargadas {len(paradas)} paradas de OpenStreetMap.")
            return paradas
        except Exception as e:
            err = e
    print(f"(No pude descargar las paradas de OpenStreetMap: {err})")
    return []


# ------------------------------------------------------- recorrido y tiempos
def _indice(pts, nombre, esq):
    """Posición de un lugar ('Gran Tarajal') en el recorrido: por nombre o, si no, la parada real más cercana."""
    if not nombre:
        return None
    for i, p in enumerate(pts):
        if p["n"] == nombre:
            return i
    e = next((q for q in esq if q["n"] == nombre), None)
    if e is None:
        return None
    i = min(range(len(pts)), key=lambda i: km(pts[i], e))
    return i if km(pts[i], e) < 4 else None


def recorrido(linea, sentido, salida, base):
    """Paradas de una salida concreta con distancia (km) y, si la línea está calibrada, tiempo (min).
    El resultado se cachea por (línea, sentido, hora, variante, inicio, fin) durante toda la vida del proceso."""
    clave = (linea, sentido, salida.get("h"), tuple(salida.get("via", [])),
             salida.get("inicio"), salida.get("fin"))
    if clave in _RECORRIDOS:
        return _RECORRIDOS[clave]

    b = base[linea]
    extra = set(salida.get("via", [])) | ({salida["inicio"]} if "inicio" in salida else set())
    pts = [p for p in b["paradas"] if "opt" not in p or p["opt"] in extra]
    kms, shift = [], 0.0
    for i, p in enumerate(pts):  # km en sentido "ida"
        if "k" in p:
            d = p.get("d", 0.0)
            kms.append(p["k"] + shift + d)
            shift += 2 * d
        else:
            kms.append(kms[-1] + km(pts[i - 1], p) if i else 0.0)
    T = b.get("duracion_min", {}).get(sentido)  # duración total conocida (Moovit) repartida por posición
    ts = fr = None
    if T:
        gen, acc, kg = [p for p in b["paradas"] if "opt" not in p], 0.0, {}
        for i, p in enumerate(gen):
            acc = p["k"] if "k" in p else (acc + km(gen[i - 1], p) if i else 0.0)
            kg[id(p)] = acc
        fr, last = [], 0.0
        for p in pts:
            last = kg.get(id(p), last)
            fr.append(last / acc if acc else 0.0)
    if sentido == "vuelta":
        pts, kms = pts[::-1], [kms[-1] - x for x in kms[::-1]]
        if T:
            fr = [1 - f for f in fr[::-1]]
    if T:
        ts, ext = [], 0.0
        for p, f in zip(pts, fr):
            dv = b.get("desvio_min", {}).get(p.get("opt"), 0) if "opt" in p else 0
            ts.append(T * f + ext + dv / 2)
            ext += dv
    esq = b.get("esqueleto", b["paradas"])
    i0, i1 = _indice(pts, salida.get("inicio"), esq), _indice(pts, salida.get("fin"), esq)
    i0, i1 = (0 if i0 is None else i0), (len(pts) - 1 if i1 is None else i1)
    if i0 >= i1:
        i0, i1 = 0, len(pts) - 1
    res = []
    for i, p in enumerate(pts):
        if i0 <= i <= i1:
            r = {**p, "km": abs(kms[i] - kms[i0]), "i": i - i0}
            if ts:
                r["t"] = max(0.0, ts[i] - ts[i0])
            res.append(r)
    _RECORRIDOS[clave] = res
    return res


def minutos(punto, b):
    if "t" in punto:
        return punto["t"]
    real = b.get("real")
    return punto["km"] * (1.0 if real else FACTOR_CARRETERA) / b["velocidad_kmh"] * 60 + (0.25 if real else MIN_POR_PARADA) * punto["i"]


def proyecta(seq, lat, lon):
    """Proyecta una coordenada sobre el recorrido → (km_recorridos, distancia_al_recorrido_km)."""
    mejor = (1e9, 0.0)
    for j in range(len(seq) - 1):
        a, b = seq[j], seq[j + 1]
        k = math.cos(math.radians(lat))
        ax, ay, bx, by, px, py = a["lon"] * k, a["lat"], b["lon"] * k, b["lat"], lon * k, lat
        dx, dy = bx - ax, by - ay
        t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        d = km({"lat": ay + t * dy, "lon": (ax + t * dx) / k}, {"lat": lat, "lon": lon})
        if d < mejor[0]:
            mejor = (d, a["km"] + t * (b["km"] - a["km"]))
    return mejor[1], mejor[0]


def nombre_destino(p, b, cache={}):
    k = (id(b), p["id"])
    if k not in cache:
        cerca = min((q for q in b.get("esqueleto", []) if "opt" not in q), key=lambda q: km(p, q), default=None)
        cache[k] = cerca["n"] if cerca and km(p, cerca) < 3 else p["n"]
    return cache[k]


def variante_de(s):
    return f"{','.join(s.get('via', []))}|{s.get('inicio') or ''}|{s.get('fin') or ''}"


def clave_viaje(fecha, num, s):
    return f"{fecha:%Y%m%d}|{num}|{s['s']}|{s['h']}|{variante_de(s)}"


def prediccion_viaje(viaje, ids, horarios, base, com=None):
    """Hora prevista (sin correcciones en vivo) a la que un viaje concreto pasa por una parada (conjunto de ids)."""
    try:
        f, num, sent, h, via, ini, fin = viaje.split("|")
        fecha = datetime.strptime(f, "%Y%m%d").date()
        hh, mm = map(int, h.split(":"))
    except ValueError:
        return None
    s = next((x for x in horarios.get(num, {}).get("salidas", []) if x["s"] == sent and x["h"] == h
              and variante_de(x) == f"{via}|{ini}|{fin}" and tipo_dia(fecha) in x["d"].split(",")), None)
    if s is None:
        return None
    seq = recorrido(num, sent, s, base)
    hit = next((p for p in seq if p.get("id") in ids), None)
    if hit is None or hit["km"] >= seq[-1]["km"] - 1e-6:
        return None
    off = minutos(hit, base[num])
    if com:
        off = com.aprendido(num, sent, variante_de(s), hit["id"], off) or off
    inicio = datetime(fecha.year, fecha.month, fecha.day, hh, mm)
    return dict(linea=num, sentido=sent, variante=variante_de(s), off=off, inicio=inicio,
                llegada=inicio + timedelta(minutes=off), destino=nombre_destino(seq[-1], base[num]))


def proximas(objetivo, ahora, horarios, base, com=None, gracia=1):
    """objetivo: {'nombre', 'ids' (paradas concretas) | 'nombres' | 'lat','lon'}. Devuelve las salidas ordenadas."""
    res, memo = [], {}
    ids, nombres = objetivo.get("ids"), objetivo.get("nombres") or {norm(objetivo["nombre"])}
    for dia_off in (-1, 0, 1):
        fecha = ahora.date() + timedelta(days=dia_off)
        dia = tipo_dia(fecha)
        for num, lin in horarios.items():
            for s in lin["salidas"]:
                if dia not in s["d"].split(","):
                    continue
                clave = (num, s["s"], tuple(s.get("via", [])), s.get("inicio"), s.get("fin"))
                if clave not in memo:
                    memo[clave] = recorrido(num, s["s"], s, base)
                seq = memo[clave]
                if len(seq) < 2:
                    continue
                hit = next((p for p in seq if (p.get("id") in ids if ids else norm(p["n"]) in nombres)), None)
                aprox = False
                if hit is None and "lat" in objetivo:
                    kmr, d = proyecta(seq, objetivo["lat"], objetivo["lon"])
                    if d <= RADIO_OSM_KM:
                        hit, aprox = {"km": kmr, "i": sum(1 for p in seq if p["km"] <= kmr)}, True
                if hit is None or hit["km"] >= seq[-1]["km"] - 1e-6:
                    continue  # no pasa por aquí, o es el final del trayecto
                off = minutos(hit, base[num])
                viaje, usable = clave_viaje(fecha, num, s), com is not None and not aprox and hit.get("id")
                if usable:  # lo aprendido por la comunidad mejora la estimación del modelo
                    off = com.aprendido(num, s["s"], variante_de(s), hit["id"], off) or off
                h, m = map(int, s["h"].split(":"))
                llegada = datetime(fecha.year, fecha.month, fecha.day, h, m) + timedelta(minutes=off)
                prec = "oficial" if hit["i"] == 0 and not aprox else ("aprox" if aprox else "estimado")
                n = 0
                if usable:  # ¿ya la han confirmado en esta parada o en una anterior?
                    aj = com.ajuste(viaje, [p.get("id") for p in seq], hit["id"], llegada)
                    if aj:
                        llegada, prec, n = aj["llegada"], aj["prec"], aj["n"]
                if llegada >= ahora - timedelta(minutes=gracia):
                    res.append(dict(llegada=llegada, linea=num, destino=nombre_destino(seq[-1], base[num]), off=off, prec=prec,
                                    cal="t" in hit, nota=s.get("nota", ""), viaje=viaje, n=n))
    return sorted(res, key=lambda r: r["llegada"])


def texto_espera(r, ahora):
    dif = (r["llegada"] - ahora).total_seconds() / 60
    if r["prec"] == "confirmada":
        return f"pasó a las {r['llegada']:%H:%M}" if dif < -1 else ("pasando ahora" if dif < 1 else f"a las {r['llegada']:%H:%M}")
    if dif < 1 and r["prec"] != "oficial":
        return "pasando ahora" if dif > -1 else f"pasó hace {round(-dif)} min"
    if dif < 1:
        return "ahora mismo" if dif > -1 else f"salió hace {round(-dif)} min"
    margen = 2 if r["prec"] == "vivo" else 0 if r["prec"] == "oficial" else max(2, round((0.07 if r.get("cal") else 0.12) * r["off"])) + (2 if r["prec"] == "aprox" else 0)
    if dif < 60:
        if margen == 0:
            return "ahora mismo" if dif < 1 else f"en {round(dif)} min"
        lo, hi = max(0, round(dif - margen)), round(dif + margen)
        if hi < 60:
            return f"en {lo}-{hi} min"
    dia = " (mañana)" if r["llegada"].date() > ahora.date() else ""
    return f"{'' if margen == 0 else 'sobre las '}{r['llegada']:%H:%M}{dia}"


def resumen(objetivo, ahora, horarios, base, por_linea=2):
    cuenta, out = {}, []
    for r in proximas(objetivo, ahora, horarios, base):
        k = (r["linea"], r["destino"])
        if cuenta.get(k, 0) < por_linea:
            cuenta[k] = cuenta.get(k, 0) + 1
            out.append(r)
    return out


def mostrar(objetivo, ahora, horarios, base):
    print(f"\n🚏 {objetivo['nombre']} — {ahora:%A %d/%m/%Y %H:%M}")
    lista = resumen(objetivo, ahora, horarios, base)
    for r in lista:
        nota = f"  · {r['nota']}" if r["nota"] else ""
        print(f"  Línea {r['linea']:>3} → {r['destino']:<30} {texto_espera(r, ahora)}{nota}")
    if not lista:
        print("  No hay más guaguas registradas para esta parada.")
    return lista


# ------------------------------------------------------------------ búsqueda
def nombres_linea(base):
    return sorted({p["n"] for l in base.values() for p in l["paradas"]})


def buscar(texto, base, osm):
    t = norm(texto)
    cand = [{"nombre": n} for n in nombres_linea(base) if t in norm(n)]
    if not cand:
        cand = [{"nombre": p["nombre"], "lat": p["lat"], "lon": p["lon"]} for p in osm if p["nombre"] and t in norm(p["nombre"])][:8]
    if not cand:
        cerca = get_close_matches(t, [norm(n) for n in nombres_linea(base)], n=4, cutoff=0.5)
        cand = [{"nombre": n} for n in nombres_linea(base) if norm(n) in cerca]
    return cand


# ---------------------------------------------------------------------- mapa
def crear_mapa(base, osm, horarios, objetivo=None, ahora=None, ruta="mapa_fuerteventura.html"):
    import folium
    m = folium.Map(location=[28.35, -14.0], zoom_start=9)
    grupo = folium.FeatureGroup(name=f"Paradas OpenStreetMap ({len(osm)})").add_to(m)
    for p in osm:
        folium.CircleMarker([p["lat"], p["lon"]], radius=2, color="#777", fill=True, tooltip=p["nombre"] or "(sin nombre)").add_to(grupo)
    for i, (num, l) in enumerate(base.items()):
        c = COLORES[i % len(COLORES)]
        capa = folium.FeatureGroup(name=f"Línea {num} · {horarios[num]['nombre']}").add_to(m)
        for tr in (l["trazado"] if "trazado" in l else [[(p["lat"], p["lon"]) for p in l["paradas"]]]):
            folium.PolyLine([tuple(x) for x in tr], color=c, weight=4, opacity=0.7).add_to(capa)
        for p in l["paradas"]:
            folium.CircleMarker([p["lat"], p["lon"]], radius=3 if l.get("real") else 5, color=c, fill=True, tooltip=f"{p['n']} (línea {num})").add_to(capa)
    if objetivo:
        lat, lon = objetivo.get("lat"), objetivo.get("lon")
        if lat is None:
            nombres = objetivo.get("nombres") or {norm(objetivo["nombre"])}
            for l in base.values():
                for p in l["paradas"]:
                    if norm(p["n"]) in nombres:
                        lat, lon = p["lat"], p["lon"]
        html = f"<b>{objetivo['nombre']}</b><br>" + "<br>".join(f"L{r['linea']} → {r['destino']}: {texto_espera(r, ahora)}" for r in resumen(objetivo, ahora, horarios, base))
        folium.Marker([lat, lon], popup=folium.Popup(html, max_width=320, show=True), icon=folium.Icon(color="red", icon="bus", prefix="fa")).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(ruta)
    return ruta


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Guaguas de Fuerteventura (Tiadhe)")
    ap.add_argument("-p", "--parada")
    ap.add_argument("--mapa", action="store_true")
    ap.add_argument("--hora", help='"AAAA-MM-DD HH:MM"')
    ap.add_argument("--actualizar-osm", action="store_true")
    a = ap.parse_args()
    horarios, base = cargar()
    osm = paradas_osm(a.actualizar_osm)
    ahora = datetime.strptime(a.hora, "%Y-%m-%d %H:%M") if a.hora else datetime.now()

    def consulta(texto):
        cand = buscar(texto, base, osm)
        if not cand:
            print("No encuentro esa parada. Núcleos con servicio:", ", ".join(nombres_linea(base)))
            return
        if len(cand) > 1:
            print("Hay varias paradas con ese nombre:")
            for i, c in enumerate(cand, 1):
                print(f"  {i}. {c['nombre']}")
            try:
                r = input("Número de parada (Enter = todas juntas): ").strip()
            except EOFError:
                r = ""
            if r.isdigit() and 1 <= int(r) <= len(cand):
                cand = [cand[int(r) - 1]]
            else:
                cand = [{"nombre": texto.strip().title(), "nombres": {norm(c["nombre"]) for c in cand if "lat" not in c}}]
        mostrar(cand[0], ahora, horarios, base)
        if a.mapa:
            webbrowser.open(Path(crear_mapa(base, osm, horarios, cand[0], ahora)).resolve().as_uri())

    if a.parada:
        consulta(a.parada)
    else:
        if a.mapa:
            webbrowser.open(Path(crear_mapa(base, osm, horarios)).resolve().as_uri())
        while True:
            t = input("\n¿En qué parada estás? (Enter para salir): ").strip()
            if not t:
                break
            consulta(t)


if __name__ == "__main__":
    main()