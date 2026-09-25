#!/usr/bin/env python3
"""Pruebas de la comunidad: firmas, consenso, plausibilidad, reputación y aprendizaje.  python test.py"""
import base64, os, sys, uuid
os.environ["GUAGUAS_DEBUG"] = "1"
from datetime import datetime
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Importante: borrar la BD ANTES de importar servidor, porque servidor.Comunidad() la abre al cargar.
for f in ("comunidad.db", "comunidad.db-wal", "comunidad.db-shm"):
    p = os.path.join(os.path.dirname(__file__) or ".", "datos", f)
    try:
        if os.path.exists(p):
            os.remove(p)
    except PermissionError:
        pass  # en Windows puede estar bloqueado por otra ejecución en curso
import servidor as s
from comunidad import Comunidad, mensaje, Error
c = s.app.test_client()
ok = fallos = 0
def check(cond, texto):
    global ok, fallos
    ok += bool(cond); fallos += not cond
    print(("✔ " if cond else "✘ ") + texto)

DIA = "2026-09-24T"
def epoch(h): return int(datetime.fromisoformat(DIA + h).replace(tzinfo=s.TZ).timestamp())

class Disp:
    def __init__(d, n):
        d.k = ec.generate_private_key(ec.SECP256R1()); d.ip = f"10.0.0.{n}"
        d.raw = d.k.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        d.b64, d.id = base64.b64encode(d.raw).decode(), Comunidad.id_de(d.raw)
    def firma(d, m, r, ts, nonce, cuerpo):
        r_, s_ = decode_dss_signature(d.k.sign(mensaje(m, r, ts, nonce, cuerpo), ec.ECDSA(hashes.SHA256())))
        return base64.b64encode(r_.to_bytes(32, "big") + s_.to_bytes(32, "big")).decode()
    def pedir(d, metodo, ruta, cuerpo=None, h="10:15:00", nonce=None, ts=None, firmar_cuerpo=None):
        import json
        ts = str(ts if ts is not None else epoch(h)); nonce = nonce or uuid.uuid4().hex
        texto = json.dumps(cuerpo) if cuerpo is not None else ""
        hd = {"X-Dispositivo": d.id, "X-Ts": ts, "X-Nonce": nonce, "X-Firma": d.firma(metodo, ruta, ts, nonce, firmar_cuerpo if firmar_cuerpo is not None else texto),
              "Content-Type": "application/json"}
        r = c.open(f"{ruta}?hora={DIA}{h}", method=metodo, data=texto or None, headers=hd, environ_base={"REMOTE_ADDR": d.ip})
        return r.status_code, r.get_json()

A, B, C, D, E, F = (Disp(i) for i in range(1, 7))
for d in (A, B, C, D, E, F):
    st, j = d.pedir("POST", "/api/registro", {"pub": d.b64}); assert st == 200, j
st, j = A.pedir("GET", "/api/yo"); check(st == 200 and j["perfil"]["nivel"] == "Nuevo", f"registro y perfil inicial: {j['perfil']}")

# --- localizar una guagua de la línea 1 de forma independiente de los nombres de las paradas
num, sent = "01", "ida"
fecha = datetime.strptime(DIA[:-1], "%Y-%m-%d").date()
sal = next(x for x in s.HORARIOS[num]["salidas"] if x["s"] == sent and s.g.tipo_dia(fecha) in x["d"].split(","))
seq = s.g.recorrido(num, sent, sal, s.BASE)
check(len(seq) >= 6, f"la línea 01 tiene recorrido suficiente para la prueba ({len(seq)} paradas)")
i1, i2 = len(seq) // 4, min(len(seq) - 1, len(seq) // 4 + 3)
p1, p2 = seq[i1], seq[i2]

def cluster_de(stop_id):
    return next(cid for cid, gr in s.PARADAS.items() if stop_id in gr["ids"])
cid1, cid2 = cluster_de(p1["id"]), cluster_de(p2["id"])
an, tu = s.PARADAS[cid1] | {"id": cid1}, s.PARADAS[cid2] | {"id": cid2}
board = lambda pid, h: c.get(f"/api/parada/{pid}?hora={DIA}{h}").json
def hms(seg): return f"{seg // 3600:02d}:{seg % 3600 // 60:02d}:{seg % 60:02d}"

inicio_h, inicio_m = map(int, sal["h"].split(":"))
base = inicio_h * 3600 + inicio_m * 60 + round(s.g.minutos(p1, s.BASE[num]) * 60)  # "ahora" = llegando a p1
fila = next((x for g_ in board(cid1, hms(base))["grupos"] for x in g_["salidas"] if x["linea"] == num and x["subir"]), None)
check(fila is not None, f"se encuentra una salida 'subir=True' de la línea 01 en {an['nombre']!r} a las {hms(base)}")
if fila is None:
    print("Salidas de la línea 01 en esa parada a esa hora (para depurar):")
    for g_ in board(cid1, hms(base))["grupos"]:
        for x in g_["salidas"]:
            if x["linea"] == num:
                print("   ", g_["destino"], x)
    sys.exit(1)
viaje = fila["viaje"]; print("  viaje:", viaje, "| parada:", an["nombre"], "→", tu["nombre"], "| ahora", hms(base), "| fila:", fila["texto"])
T = lambda s_: hms(base + s_)
gps = {"lat": an["lat"], "lon": an["lon"], "acc": 15, "viaje": viaje, "parada": an["id"]}

# --- seguridad
st, j = A.pedir("POST", "/api/subida", gps, h=T(20), nonce="n1"); check(st == 200 and j["estado"] == "pendiente", "1 aviso solo → pendiente (no cambia nada)")
st, _ = A.pedir("POST", "/api/subida", gps, h=T(21), nonce="n1"); check(st == 401, "repetir el mismo nonce → rechazado (replay)")
st, _ = B.pedir("POST", "/api/subida", gps, h=T(30), firmar_cuerpo='{"viaje":"otro"}'); check(st == 401, "cuerpo manipulado tras firmar → firma no válida")
st, _ = B.pedir("POST", "/api/subida", gps, h=T(30), ts=epoch(hms(max(0, base - 600)))); check(st == 401, "petición con hora antigua → rechazada")
st, j = A.pedir("POST", "/api/subida", gps, h=T(40)); check(st == 409, f"mismo dispositivo dos veces en un viaje → {j['error']}")

# --- plausibilidad
lejos_h = min(base + 5 * 3600, 23 * 3600 + 3000)
lejos = next((x for g_ in board(an["id"], hms(lejos_h))["grupos"] for x in g_["salidas"] if x["linea"] == num), None)
if lejos:
    st, j = E.pedir("POST", "/api/subida", {**gps, "viaje": lejos["viaje"]}, h=T(60)); check(st == 400, f"'subirse' a una guagua que pasa varias horas después → {j['error']}")
st, j = E.pedir("POST", "/api/subida", {**gps, "lat": an["lat"] + 0.05}, h=T(70)); check(st == 400, f"aviso desde 5 km de la parada → {j['error']}")
st, j = E.pedir("GET", "/api/yo", h=T(75)); check(j["perfil"]["puntos"] < 20, f"los avisos imposibles restan reputación (puntos: {j['perfil']['puntos']})")

# --- consenso
st, j = B.pedir("POST", "/api/subida", gps, h=T(50)); check(j["estado"] == "pendiente", "2 avisos nuevos con GPS (peso < 1.0) → aún pendiente")
antes = next(x for g_ in board(tu["id"], T(80))["grupos"] for x in g_["salidas"] if x["viaje"] == viaje)
st, j = C.pedir("POST", "/api/subida", gps, h=T(90)); check(j["estado"] == "confirmada", "3 dispositivos coinciden → observación confirmada")
despues = next(x for g_ in board(tu["id"], T(80))["grupos"] for x in g_["salidas"] if x["viaje"] == viaje)
check(despues["prec"] == "vivo" and despues["n"] >= 3, f"la parada siguiente pasa a 'en directo': {antes['texto']} [{antes['prec']}] → {despues['texto']} [{despues['prec']}] ({despues['n']} personas)")
aqui = next(x for g_ in board(an["id"], T(300))["grupos"] for x in g_["salidas"] if x["viaje"] == viaje)
check(aqui["prec"] == "confirmada", f"en la propia parada: '{aqui['texto']}' [{aqui['prec']}]")
st, j = A.pedir("GET", "/api/yo", h=T(100)); pa = j["perfil"]
check(pa["puntos"] > 20 and pa["confirmadas"] == 1, f"quien coincide gana reputación: {pa}")
st, j = D.pedir("POST", "/api/subida", gps, h=T(600))
st, j = D.pedir("GET", "/api/yo", h=T(610)); check(j["perfil"]["discrepantes"] == 1 and j["perfil"]["puntos"] < 20, f"quien discrepa (10 min después) pierde reputación: {j['perfil']}")

# --- imposibles entre avisos del mismo dispositivo (dos extremos de la línea 01, a ~80 km)
def cluster_cercano(lat, lon):
    return min(s.PARADAS.items(), key=lambda kv: s.g.km({"lat": lat, "lon": lon}, kv[1]))[0]
esq01 = s.BASE["01"]["esqueleto"]
cid_a, cid_b = cluster_cercano(esq01[0]["lat"], esq01[0]["lon"]), cluster_cercano(esq01[-1]["lat"], esq01[-1]["lon"])
ga, gb = s.PARADAS[cid_a], s.PARADAS[cid_b]
dist_ab = s.g.km({"lat": ga["lat"], "lon": ga["lon"]}, {"lat": gb["lat"], "lon": gb["lon"]})
check(dist_ab > 30, f"los dos extremos elegidos están lejos entre sí ({dist_ab:.0f} km)")
COM = s.COM
COM.reloj = lambda: epoch("11:00:00")
try:
    COM.enviar_subida(F.id, "zzim1", cid_a, dict(linea="01", sentido="ida", variante="", llegada_ts=epoch("11:00:00"), inicio_ts=epoch("11:00:00"), off=0), 5, epoch("11:00:00"))
    COM.enviar_subida(F.id, "zzim2", cid_b, dict(linea="01", sentido="ida", variante="", llegada_ts=epoch("11:20:00"), inicio_ts=epoch("11:20:00"), off=0), 5, epoch("11:20:00"))
    check(False, "desplazamiento imposible aceptado")
except Error as e:
    check("imposible" in str(e), f"desplazamiento imposible entre dos avisos ({dist_ab:.0f} km en 20 min) → {e}")

# --- aprendizaje: 3 observaciones de otros días cambian la estimación (para la línea y parada ya usadas)
var = s.g.variante_de(sal)
off1 = s.g.minutos(p1, s.BASE[num])
for i in range(3):
    COM.db.execute("INSERT OR REPLACE INTO observaciones VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (f"2026091{i}|{num}|{sent}|{sal['h']}|{var}", cid1, num, sent, var,
                    epoch("10:00:00") + round((off1 + 20) * 60), epoch("10:00:00"), epoch("09:59:00"), 3, 1.2, epoch("10:30:00")))
COM.db.commit(); COM._snap_ts = -1e9
aprend = COM.aprendido(num, sent, var, p1["id"], off1)
check(aprend is not None and off1 < aprend < off1 + 20, f"3 observaciones ({off1 + 20:.0f} min) + modelo ({off1:.0f} min) → estimación mezclada {aprend:.1f} min")
print(f"\n{ok} correctas, {fallos} fallos")
s.COM.db.close()
for f in ("comunidad.db", "comunidad.db-wal", "comunidad.db-shm"):
    p = os.path.join(os.path.dirname(__file__) or ".", "datos", f)
    try:
        if os.path.exists(p): os.remove(p)
    except PermissionError:
        pass
sys.exit(1 if fallos else 0)