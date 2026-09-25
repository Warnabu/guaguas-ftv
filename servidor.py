#!/usr/bin/env python3
"""Web de guaguas de Fuerteventura.

    python servidor.py          # http://127.0.0.1:5000
En producción:  gunicorn -w 2 servidor:app   (detrás de nginx con HTTPS)
Pruebas: GUAGUAS_DEBUG=1 permite ?hora=2026-09-24T10:05 en las consultas.
"""
import base64, hashlib, json, os, time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

from comunidad import Comunidad, Error, km_ll, mensaje

import guaguas as g

if not hasattr(g, "nombre_destino"):
    raise SystemExit("guaguas.py es una versión antigua: sustitúyelo por el último y reinicia el servidor. "
                     "Los archivos guaguas.py, servidor.py, importar_mapas.py y web/index.html deben ser de la misma entrega.")

TZ = ZoneInfo("Atlantic/Canary")
DEBUG = bool(os.environ.get("GUAGUAS_DEBUG"))
app = Flask(__name__, static_folder="web", static_url_path="")
HORARIOS, BASE = g.cargar()
_osm = g.DATOS / "paradas_osm.json"
OSM = [p for p in json.loads(_osm.read_text(encoding="utf-8")) if p["nombre"]] if _osm.exists() else []
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
COLOR = {num: g.COLORES[i % len(g.COLORES)] for i, num in enumerate(BASE)}


def agrupar(radio_km=0.06):
    """Une las paradas (de cualquier línea) a menos de ~60 m: una parada física con sus dos sentidos
    es un único punto del mapa. Se conservan los ids de cada parada concreta de cada línea."""
    pts = sorted((p["lat"], p["lon"], p["n"], p["id"], num) for num, l in BASE.items() for p in l["paradas"])
    grupos = []
    for lat, lon, n, pid, num in pts:
        for gr in grupos:
            if g.km({"lat": lat, "lon": lon}, gr) <= radio_km:
                gr["c"] += 1
                gr["lat"] += (lat - gr["lat"]) / gr["c"]
                gr["lon"] += (lon - gr["lon"]) / gr["c"]
                gr["ids"].add(pid); gr["nombres"].append(n); gr["lineas"].add(num)
                break
        else:
            grupos.append(dict(lat=lat, lon=lon, c=1, ids={pid}, nombres=[n], lineas={num}))
    res = {}
    for gr in grupos:
        gr["nombre"] = Counter(gr["nombres"]).most_common(1)[0][0]
    rep = Counter(gr["nombre"] for gr in grupos)
    for gr in grupos:  # nombres repetidos: se distinguen por las líneas que pasan (y por nº si aún coinciden)
        if rep[gr["nombre"]] > 1:
            gr["nombre"] += " · L" + ", L".join(sorted(gr["lineas"]))
    vistos = Counter()
    for gr in grupos:
        vistos[gr["nombre"]] += 1
        if vistos[gr["nombre"]] > 1:
            gr["nombre"] += f" ({vistos[gr['nombre']]})"
        pid = "p" + hashlib.md5(f"{gr['lat']:.4f},{gr['lon']:.4f}".encode()).hexdigest()[:8]  # estable entre arranques
        while pid in res:
            pid += "x"
        gr["id"] = pid
        res[pid] = gr
    return res


PARADAS = agrupar()
if os.environ.get("GUAGUAS_PROXY"):  # detrás de nginx: usa la IP real para limitar registros
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)


def ahora_ts():
    """Segundos epoch 'de ahora' (en pruebas, ?hora=... con GUAGUAS_DEBUG=1)."""
    try:
        if DEBUG and request.args.get("hora"):
            return datetime.fromisoformat(request.args["hora"]).replace(tzinfo=TZ).timestamp()
    except RuntimeError:
        pass
    return time.time()


def a_ts(dt):
    return dt.replace(tzinfo=TZ).timestamp()


from pathlib import Path
DB_DIR = Path(os.environ.get("DATA_DIR", str(g.DATOS)))
DB_DIR.mkdir(parents=True, exist_ok=True)
COM = Comunidad(DB_DIR / "comunidad.db",
                {pid: set(gr["ids"]) for pid, gr in PARADAS.items()},
                {pid: (gr["lat"], gr["lon"]) for pid, gr in PARADAS.items()},
                TZ, reloj=ahora_ts)

@app.errorhandler(Error)
def error_comunidad(e):
    return jsonify(ok=False, error=str(e)), e.codigo


def autenticar():
    h = request.headers
    try:
        dev, ts, nonce, firma = h["X-Dispositivo"], h["X-Ts"], h["X-Nonce"], h["X-Firma"]
    except KeyError:
        raise Error("faltan cabeceras de autenticación", 401)
    return COM.verificar(dev, ts, nonce, firma, mensaje(request.method, request.path, ts, nonce, request.get_data(as_text=True)), ahora_ts())


def lugares():
    """Núcleos (Las Playitas, Antigua…) para el buscador, aunque ninguna parada se llame así."""
    acc = {}
    for l in BASE.values():
        for p in l["esqueleto"]:
            acc.setdefault(p["n"], (p["lat"], p["lon"]))
    return [dict(n=n, lat=la, lon=lo) for n, (la, lo) in sorted(acc.items())]


_cache = {}


def ahora_local():
    if DEBUG and request.args.get("hora"):
        return datetime.fromisoformat(request.args["hora"])
    return datetime.now(TZ).replace(tzinfo=None)


def tablero(clave, objetivo, ahora, por_destino=6):
    k = (clave, ahora.strftime("%Y%m%d%H%M"))  # las respuestas cambian como mucho cada minuto
    if k in _cache:
        return _cache[k]
    if len(_cache) > 800:
        _cache.clear()
    grupos = {}
    for r in g.proximas(objetivo, ahora, HORARIOS, BASE, com=COM, gracia=12):
        lista = grupos.setdefault(r["destino"], [])
        if sum(1 for x in lista if x["linea"] == r["linea"]) < 3 and len(lista) < por_destino:
            dif = (r["llegada"] - ahora).total_seconds() / 60
            lista.append(dict(linea=r["linea"], texto=g.texto_espera(r, ahora), hora=f"{r['llegada']:%H:%M}", minutos=round(dif),
                              prec=r["prec"], cal=r["cal"], nota=r["nota"], viaje=r["viaje"], n=r["n"],
                              subir=-12 <= dif <= min(20, 12 + 0.25 * r["off"])))  # solo se puede avisar cuando está pasando
    orden = sorted(grupos.items(), key=lambda kv: kv[1][0]["minutos"])
    _cache[k] = dict(ahora=f"{ahora:%H:%M}", dia=f"{DIAS[ahora.weekday()]} {ahora:%d/%m}",
                     grupos=[dict(destino=d, salidas=s) for d, s in orden])
    return _cache[k]


@app.post("/api/registro")
def api_registro():
    """Alta de un dispositivo: envía su clave pública firmando la petición con la clave privada (prueba de posesión)."""
    pub = (request.get_json(silent=True) or {}).get("pub", "")
    try:
        raw = base64.b64decode(pub, validate=True)
        dev = Comunidad.id_de(raw)
    except Exception:
        raise Error("clave pública no válida")
    h = request.headers
    try:
        ts, nonce, firma = h["X-Ts"], h["X-Nonce"], h["X-Firma"]
    except KeyError:
        raise Error("faltan cabeceras de autenticación", 401)
    ahora = ahora_ts()
    COM.verificar(dev, ts, nonce, firma, mensaje("POST", request.path, ts, nonce, request.get_data(as_text=True)), ahora, pub_raw=raw)
    COM.registrar(pub, request.remote_addr or "?", ahora)
    return jsonify(ok=True, dispositivo=dev, perfil=COM.perfil(dev, ahora))


@app.get("/api/yo")
def api_yo():
    dev = autenticar()
    return jsonify(ok=True, dispositivo=dev, perfil=COM.perfil(dev, ahora_ts()))


@app.post("/api/subida")
def api_subida():
    """'Me he subido a esta guagua en esta parada'. Se valida, pero solo cuenta cuando coinciden varias personas."""
    dev = autenticar()
    d = request.get_json(silent=True) or {}
    gr = PARADAS.get(d.get("parada", ""))
    if not gr:
        raise Error("parada desconocida", 404)
    ahora = ahora_ts()
    pred = g.prediccion_viaje(str(d.get("viaje", "")), gr["ids"], HORARIOS, BASE, COM)
    if not pred:
        raise Error("esa guagua no existe o no pasa por esta parada")
    if abs(ahora - a_ts(pred["inicio"])) > 30 * 3600:
        raise Error("ese viaje no es de hoy")
    dist = None
    try:  # ubicación opcional: si viene y es precisa, se comprueba que estás en la parada
        lat, lon, acc = float(d["lat"]), float(d["lon"]), float(d.get("acc", 50))
        if acc <= 200:
            dist = max(0.0, km_ll((lat, lon), (gr["lat"], gr["lon"])) * 1000 - min(acc, 100))
    except (KeyError, TypeError, ValueError):
        pass
    res = COM.enviar_subida(dev, d["viaje"], d["parada"], dict(linea=pred["linea"], sentido=pred["sentido"], variante=pred["variante"],
                            llegada_ts=int(a_ts(pred["llegada"])), inicio_ts=int(a_ts(pred["inicio"])), off=pred["off"]), dist, int(ahora))
    _cache.clear()
    msg = ("¡Confirmado! Ya coincidís varias personas y la hora se ha actualizado para los demás." if res["estado"] == "confirmada"
           else "Anotado. Cuando otras personas confirmen esta guagua, la hora se actualizará."
           + ("" if res["gps"] else " (Con la ubicación activada tu aviso pesa más.)"))
    return jsonify(ok=True, estado=res["estado"], mensaje=msg, perfil=COM.perfil(dev, ahora))


@app.get("/")
def inicio():
    return app.send_static_file("index.html")


@app.get("/api/paradas")
def api_paradas():
    return jsonify([dict(id=p["id"], nombre=p["nombre"], lat=round(p["lat"], 5), lon=round(p["lon"], 5), lineas=sorted(p["lineas"]))
                    for p in PARADAS.values()])


@app.get("/api/lineas")
def api_lineas():
    return jsonify([dict(num=n, nombre=HORARIOS[n]["nombre"], color=COLOR[n], calibrada="duracion_min" in l, real=bool(l.get("real")),
                         trazado=l["trazado"] if "trazado" in l else [[p["lat"], p["lon"]] for p in l["paradas"] if "opt" not in p])
                    for n, l in BASE.items()])


@app.get("/api/lugares")
def api_lugares():
    return jsonify(lugares())


@app.get("/api/osm")
def api_osm():
    return jsonify([dict(n=p["nombre"], lat=p["lat"], lon=p["lon"]) for p in OSM])


@app.get("/api/parada/<pid>")
def api_parada(pid):
    gr = PARADAS.get(pid)
    if not gr:
        raise Error("parada desconocida", 404)
    d = dict(tablero(pid, dict(nombre=gr["nombre"], ids=gr["ids"]), ahora_local()))
    d["parada"] = dict(id=pid, nombre=gr["nombre"], lineas=sorted(gr["lineas"]))
    return jsonify(d)


@app.get("/api/cerca")
def api_cerca():
    if "lat" not in request.args or "lon" not in request.args:
        raise Error("faltan lat/lon", 400)
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
    except ValueError:
        raise Error("lat/lon no son números", 400)
    nombre = request.args.get("nombre", "Parada")[:80]
    d = dict(tablero(("osm", round(lat, 5), round(lon, 5)), dict(nombre=nombre, lat=lat, lon=lon), ahora_local()))
    d["parada"] = dict(id="osm", nombre=nombre, lineas=[], aprox=True)
    return jsonify(d)


if __name__ == "__main__":
    print(f"{len(PARADAS)} paradas · {len(OSM)} paradas OSM · http://127.0.0.1:{os.environ.get('PORT', 5000)}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=DEBUG)
