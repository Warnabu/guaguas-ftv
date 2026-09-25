"""Comunidad: subidas colaborativas ("me he subido a la guagua"), confianza y consenso.

Idea: una subida sola NO cambia nada. Solo cuando varios dispositivos distintos, con suficiente
confianza conjunta, coinciden en la hora a la que pasó una guagua concreta por una parada, se crea
una OBSERVACIÓN confirmada. Las observaciones sirven para dos cosas:
  1. EN VIVO: si la guagua X ya pasó por la parada A con N min de retraso, se aplica a las paradas siguientes.
  2. APRENDIZAJE: con muchas observaciones se aprende cuánto tarda de verdad cada línea hasta cada parada.

Autenticación: cada dispositivo firma sus peticiones (ECDSA P-256, WebCrypto); el servidor guarda solo la
clave pública. No hay cuentas, contraseñas ni datos personales.
"""
import base64, hashlib, math, sqlite3, threading, time
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

# ------------------------------------------------ parámetros (ajustables)
REP_INICIAL, PESO_MIN, PESO_MAX = 20.0, 0.35, 0.90   # el peso de un dispositivo nunca pasa de 0.9 (< un voto entero)
FACTOR_SIN_GPS = 0.4                                  # sin ubicación, su aviso pesa 40 %
QUORUM_DISPOSITIVOS, QUORUM_PESO, MAX_CUOTA = 2, 1.0, 0.75   # ≥2 dispositivos, peso ≥1.0, ninguno >75 % del peso
TOLERANCIA = 3 * 60          # avisos a ≤3 min de la mediana coinciden
SUBE, BAJA = 0.06, 10.0      # reputación: +6 % de lo que falta hasta 100 al coincidir; −10 al discrepar
MAX_AL_DIA, MIN_ENTRE_SUBIDAS, VEL_MAX_KMH = 12, 8 * 60, 110
RADIO_GPS_M, MAX_REGISTROS_IP = 200, 8
K_PRIOR, MIN_OBS_APRENDER = 4, 3   # aprendizaje: el modelo cuenta como 4 observaciones; hacen falta ≥3 reales
NIVELES = [(30, "Nuevo"), (55, "Colaborador"), (75, "Fiable"), (101, "Veterano")]


def mediana_pond(pares):
    pares = sorted(pares)
    if not pares:
        return 0.0
    total, acc = sum(w for _, w in pares), 0.0
    for v, w in pares:
        acc += w
        if acc >= total / 2:
            return v
    return pares[-1][0]


def mensaje(metodo, ruta, ts, nonce, cuerpo):
    return "\n".join([metodo, ruta, str(ts), nonce, cuerpo]).encode()


def km_ll(a, b):
    p = math.pi / 180
    h = math.sin((b[0] - a[0]) * p / 2) ** 2 + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin((b[1] - a[1]) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


class Error(Exception):
    def __init__(self, msg, codigo=400):
        super().__init__(msg); self.codigo = codigo


class Comunidad:
    def __init__(self, ruta, ids_cluster, centros, tz, reloj=time.time):
        """ids_cluster: {cluster: {ids de paradas de línea}} · centros: {cluster: (lat, lon)}"""
        self.ids_cluster, self.centros, self.tz, self.reloj = ids_cluster, centros, tz, reloj
        self.cluster_de = {i: c for c, ids in ids_cluster.items() for i in ids}
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(ruta), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS dispositivos(id TEXT PRIMARY KEY, pub BLOB, creado INT, ult INT, rep REAL,
            confirmadas INT DEFAULT 0, discrepantes INT DEFAULT 0, subidas INT DEFAULT 0, strikes INT DEFAULT 0,
            strikes_ts INT DEFAULT 0, bloqueado INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS subidas(id INTEGER PRIMARY KEY AUTOINCREMENT, dev TEXT, viaje TEXT, cluster TEXT,
            linea TEXT, sentido TEXT, variante TEXT, ts INT, pred_ts INT, inicio_ts INT, peso REAL, gps INT,
            estado TEXT DEFAULT 'pendiente', evaluado INT DEFAULT 0, UNIQUE(dev, viaje));
        CREATE TABLE IF NOT EXISTS observaciones(viaje TEXT, cluster TEXT, linea TEXT, sentido TEXT, variante TEXT,
            ts INT, pred_ts INT, inicio_ts INT, n INT, peso REAL, creado INT, PRIMARY KEY(viaje, cluster));
        CREATE TABLE IF NOT EXISTS nonces(n TEXT PRIMARY KEY, ts INT);
        CREATE TABLE IF NOT EXISTS registros(ip TEXT, ts INT);
        """)
        self._snap, self._snap_ts = None, 0

    # ---------------------------------------------------------- dispositivos y firmas
    @staticmethod
    def id_de(pub_raw):
        return hashlib.sha256(pub_raw).hexdigest()[:32]

    def registrar(self, pub_b64, ip, ahora):
        try:
            raw = base64.b64decode(pub_b64)
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        except Exception:
            raise Error("clave pública no válida")
        dev = self.id_de(raw)
        with self.lock:
            if not self.db.execute("SELECT 1 FROM dispositivos WHERE id=?", (dev,)).fetchone():
                n = self.db.execute("SELECT COUNT(*) FROM registros WHERE ip=? AND ts>?", (ip, ahora - 86400)).fetchone()[0]
                if n >= MAX_REGISTROS_IP:
                    raise Error("demasiados dispositivos nuevos desde esta red; inténtalo mañana", 429)
                self.db.execute("INSERT INTO registros VALUES(?,?)", (ip, ahora))
                self.db.execute("INSERT INTO dispositivos(id,pub,creado,ult,rep) VALUES(?,?,?,?,?)", (dev, raw, ahora, ahora, REP_INICIAL))
                self.db.commit()
        return dev, raw

    def verificar(self, dev, ts, nonce, firma_b64, msg, ahora, pub_raw=None):
        """Comprueba firma, frescura y no repetición. Devuelve el id del dispositivo."""
        if abs(ahora - int(ts)) > 120:
            raise Error(f"reloj del dispositivo desajustado o petición antigua (servidor: {int(ahora)})", 401)
        with self.lock:
            if pub_raw is None:
                fila = self.db.execute("SELECT pub FROM dispositivos WHERE id=?", (dev,)).fetchone()
                if not fila:
                    raise Error("dispositivo desconocido", 401)
                pub_raw = fila["pub"]
            try:
                sig = base64.b64decode(firma_b64)
                der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
                ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_raw).verify(der, msg, ec.ECDSA(hashes.SHA256()))
            except (InvalidSignature, ValueError):
                raise Error("firma no válida", 401)
            if self.db.execute("SELECT 1 FROM nonces WHERE n=?", (dev + nonce,)).fetchone():
                raise Error("petición repetida", 401)
            self.db.execute("INSERT INTO nonces VALUES(?,?)", (dev + nonce, ahora))
            self.db.execute("DELETE FROM nonces WHERE ts<?", (ahora - 600,))
            self.db.commit()
        return dev

    # ------------------------------------------------------------ reputación
    @staticmethod
    def rep_efectiva(fila, ahora):
        dias = max(0, (ahora - fila["ult"]) / 86400)
        return fila["rep"] if dias < 30 else REP_INICIAL + (fila["rep"] - REP_INICIAL) * 0.5 ** (dias / 90)

    @staticmethod
    def peso(rep):
        return PESO_MIN + (PESO_MAX - PESO_MIN) * max(0, min(100, rep)) / 100

    @staticmethod
    def nivel(rep):
        return next(n for lim, n in NIVELES if rep < lim)

    def perfil(self, dev, ahora):
        f = self.db.execute("SELECT * FROM dispositivos WHERE id=?", (dev,)).fetchone()
        rep = self.rep_efectiva(f, ahora)
        return dict(nivel=self.nivel(rep), puntos=round(rep), peso=round(self.peso(rep), 2), subidas=f["subidas"],
                    confirmadas=f["confirmadas"], discrepantes=f["discrepantes"])

    # ---------------------------------------------------------------- subidas
    def enviar_subida(self, dev, viaje, cluster, pred, dist_m, ahora):
        """pred: dict(linea, sentido, variante, llegada_ts, inicio_ts, off). Valida y guarda un aviso de subida."""
        with self.lock:
            f = self.db.execute("SELECT * FROM dispositivos WHERE id=?", (dev,)).fetchone()
            if f["bloqueado"] > ahora:
                raise Error("tus avisos están pausados por avisos imposibles repetidos; vuelve mañana", 429)

            def rechazar(msg, castigo=True):
                if castigo:
                    st = f["strikes"] + 1 if ahora - f["strikes_ts"] < 86400 else 1
                    self.db.execute("UPDATE dispositivos SET strikes=?, strikes_ts=?, rep=MAX(0, rep-2), bloqueado=? WHERE id=?",
                                    (st, ahora, ahora + 86400 if st >= 6 else 0, dev))
                    self.db.commit()
                raise Error(msg)

            if self.db.execute("SELECT 1 FROM subidas WHERE dev=? AND viaje=?", (dev, viaje)).fetchone():
                raise Error("ya avisaste de esta guagua", 409)
            if self.db.execute("SELECT COUNT(*) FROM subidas WHERE dev=? AND ts>?", (dev, ahora - 86400)).fetchone()[0] >= MAX_AL_DIA:
                raise Error(f"máximo {MAX_AL_DIA} avisos al día", 429)
            ult = self.db.execute("SELECT cluster, ts FROM subidas WHERE dev=? ORDER BY ts DESC LIMIT 1", (dev,)).fetchone()
            if ult:
                dt = ahora - ult["ts"]
                if dt < MIN_ENTRE_SUBIDAS:
                    rechazar("ya marcaste una subida hace muy poco", castigo=False)
                if ult["cluster"] in self.centros and cluster in self.centros:
                    d = km_ll(self.centros[ult["cluster"]], self.centros[cluster])
                    if d > 1 and d / (dt / 3600) > VEL_MAX_KMH:
                        rechazar(f"imposible: {d:.0f} km en {dt // 60} min desde tu último aviso")
            ventana = 12 * 60 + 0.25 * pred["off"] * 60
            if abs(ahora - pred["llegada_ts"]) > ventana:
                rechazar("esa guagua no debería estar pasando por aquí ahora mismo")
            gps = 0
            if dist_m is not None:
                if dist_m > RADIO_GPS_M:
                    rechazar(f"tu ubicación está a {int(dist_m)} m de la parada")
                gps = 1
            peso = self.peso(self.rep_efectiva(f, ahora)) * (1 if gps else FACTOR_SIN_GPS)
            self.db.execute("INSERT INTO subidas(dev,viaje,cluster,linea,sentido,variante,ts,pred_ts,inicio_ts,peso,gps) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (dev, viaje, cluster, pred["linea"], pred["sentido"], pred["variante"], ahora, pred["llegada_ts"], pred["inicio_ts"], peso, gps))
            self.db.execute("UPDATE dispositivos SET subidas=subidas+1, ult=? WHERE id=?", (ahora, dev))
            self.db.commit()
            estado = self._evaluar(viaje, cluster, ahora)
            self._snap_ts = 0
            return dict(estado=estado, gps=bool(gps))

    def _evaluar(self, viaje, cluster, ahora):
        filas = self.db.execute("SELECT * FROM subidas WHERE viaje=? AND cluster=?", (viaje, cluster)).fetchall()
        m = mediana_pond([(f["ts"], f["peso"]) for f in filas])
        acuerdo = [f for f in filas if abs(f["ts"] - m) <= TOLERANCIA]
        peso = sum(f["peso"] for f in acuerdo)
        quorum = (len({f["dev"] for f in acuerdo}) >= QUORUM_DISPOSITIVOS and peso >= QUORUM_PESO
                  and max(f["peso"] for f in acuerdo) / peso <= MAX_CUOTA)
        if not quorum:
            return "pendiente"
        ts = int(mediana_pond([(f["ts"], f["peso"]) for f in acuerdo]))
        base = acuerdo[0]
        self.db.execute("INSERT OR REPLACE INTO observaciones VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (viaje, cluster, base["linea"], base["sentido"], base["variante"], ts, int(base["pred_ts"]), base["inicio_ts"], len(acuerdo), peso, ahora))
        ids_ok = {f["id"] for f in acuerdo}
        for f in filas:
            if f["evaluado"]:
                continue
            if f["id"] in ids_ok:
                self.db.execute("UPDATE dispositivos SET rep=MIN(100, rep+(100-rep)*?), confirmadas=confirmadas+1 WHERE id=?", (SUBE, f["dev"]))
                est = "confirmada"
            else:
                self.db.execute("UPDATE dispositivos SET rep=MAX(0, rep-?), discrepantes=discrepantes+1 WHERE id=?", (BAJA, f["dev"]))
                est = "discrepante"
            self.db.execute("UPDATE subidas SET estado=?, evaluado=1 WHERE id=?", (est, f["id"]))
        self.db.commit()
        return "confirmada"

    def limpiar(self, ahora):
        with self.lock:
            self.db.execute("DELETE FROM subidas WHERE ts<?", (ahora - 90 * 86400,))
            self.db.execute("UPDATE subidas SET estado='sin_confirmar', evaluado=1 WHERE estado='pendiente' AND ts<?", (ahora - 7200,))
            self.db.commit()

    # ------------------------------------------- enganche con el cálculo de tiempos
    def _local(self, ts):
        return datetime.fromtimestamp(ts, self.tz).replace(tzinfo=None)

    def _snapshot(self):
        ahora = self.reloj()
        if self._snap is None or abs(ahora - self._snap_ts) > 15:
            with self.lock:
                viaje, aprendido = {}, {}
                for o in self.db.execute("SELECT * FROM observaciones WHERE ts>?", (ahora - 36 * 3600,)):
                    viaje.setdefault(o["viaje"], []).append(dict(o))
                for o in self.db.execute("SELECT * FROM observaciones WHERE creado>?", (ahora - 60 * 86400,)):
                    aprendido.setdefault((o["linea"], o["sentido"], o["variante"], o["cluster"]), []).append(
                        ((o["ts"] - o["inicio_ts"]) / 60, o["peso"]))
                self._snap, self._snap_ts = dict(viaje=viaje, aprendido=aprendido), ahora
        return self._snap

    def aprendido(self, linea, sentido, variante, stop_id, off_modelo):
        """Minutos hasta esta parada mezclando lo observado con el modelo (o None si aún no hay datos)."""
        vals = self._snapshot()["aprendido"].get((linea, sentido, variante, self.cluster_de.get(stop_id)))
        if not vals or len(vals) < MIN_OBS_APRENDER:
            return None
        aprendido = mediana_pond([(v, 1.0) for v, _ in vals])
        n = len(vals)
        return (n * aprendido + K_PRIOR * off_modelo) / (n + K_PRIOR)

    def ajuste(self, viaje, orden_ids, hit_id, llegada):
        """Si esta guagua ya fue confirmada en esta parada o en una anterior, corrige la hora."""
        obs = self._snapshot()["viaje"].get(viaje)
        if not obs or hit_id not in orden_ids:
            return None
        i, mejor = orden_ids.index(hit_id), None
        for o in obs:
            ids = self.ids_cluster.get(o["cluster"], ())
            if hit_id in ids:
                return dict(llegada=self._local(o["ts"]), prec="confirmada", n=o["n"])
            j = next((k for k, x in enumerate(orden_ids) if x in ids), None)
            if j is not None and j < i and (mejor is None or j > mejor[0]):
                mejor = (j, o)
        if mejor:
            o = mejor[1]
            retraso = max(-30, min(30, (o["ts"] - o["pred_ts"]) / 60))
            return dict(llegada=llegada + timedelta(minutes=retraso), prec="vivo", n=o["n"], retraso=retraso)
        return None