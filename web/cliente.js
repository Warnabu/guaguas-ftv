// Identidad del dispositivo (WebCrypto) y comunicación firmada con la comunidad.
// La clave privada se genera en el dispositivo, no es extraíble y nunca sale de él;
// el servidor solo conoce la clave pública. No hay cuentas, ni contraseñas, ni datos personales.
const Guaguas = (() => {
  // OK para firmas ECDSA (64 B) y claves públicas (65 B). No usar con blobs grandes: el spread revienta el stack.
  const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
  const unb64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
  const rand = n => hex(crypto.getRandomValues(new Uint8Array(n)));

  function abrirDB() {
    return new Promise((res, rej) => {
      const r = indexedDB.open("guaguas-identidad", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("claves");
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  async function guardar(db, k, v) {
    return new Promise((res, rej) => {
      const tx = db.transaction("claves", "readwrite");
      tx.objectStore("claves").put(v, k);
      tx.oncomplete = res; tx.onerror = () => rej(tx.error);
    });
  }
  async function leer(db, k) {
    return new Promise((res, rej) => {
      const tx = db.transaction("claves", "readonly");
      const q = tx.objectStore("claves").get(k);
      q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error);
    });
  }

  let db, priv, pub, dev, perfil = null, cambioPerfil = () => {};

  async function id_de(pubRaw) {
    return hex(await crypto.subtle.digest("SHA-256", pubRaw)).slice(0, 32);
  }

  async function firmar(metodo, ruta, cuerpo) {
    const ts = Math.floor(Date.now() / 1000).toString(), nonce = rand(12);
    const texto = cuerpo === undefined ? "" : JSON.stringify(cuerpo);
    const msg = new TextEncoder().encode([metodo, ruta, ts, nonce, texto].join("\n"));
    const firma = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, priv, msg);
    return { headers: { "X-Dispositivo": dev, "X-Ts": ts, "X-Nonce": nonce, "X-Firma": b64(firma) }, texto };
  }

  async function pedir(metodo, ruta, cuerpo) {
    const { headers, texto } = await firmar(metodo, ruta, cuerpo);
    if (texto) headers["Content-Type"] = "application/json";
    const r = await fetch(ruta, { method: metodo, headers, body: texto || undefined });
    const j = await r.json().catch(() => null);
    if (!r.ok) throw new Error((j && j.error) || `error ${r.status}`);
    return j;
  }

  async function iniciar() {
    db = await abrirDB();
    const guardada = await leer(db, "par");
    if (guardada) {
      ({ priv, pub, dev } = guardada);
    } else {
      const par = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
      priv = par.privateKey;
      pub = new Uint8Array(await crypto.subtle.exportKey("raw", par.publicKey));
      dev = await id_de(pub);
      await guardar(db, "par", { priv, pub, dev });
    }
    try {
      const j = await pedir("POST", "/api/registro", { pub: b64(pub) });
      perfil = j.perfil; cambioPerfil(perfil);
    } catch (e) {
      console.warn("No se pudo registrar el dispositivo:", e.message);
    }
    return dev;
  }

  function ubicacion() {
    return new Promise(res => {
      if (!navigator.geolocation) return res(null);
      navigator.geolocation.getCurrentPosition(
        p => res({ lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy }),
        () => res(null), { enableHighAccuracy: true, timeout: 6000, maximumAge: 30000 });
    });
  }

  async function marcarSubida(paradaId, viaje) {
    const loc = await ubicacion();
    const j = await pedir("POST", "/api/subida", { parada: paradaId, viaje, ...(loc || {}) });
    if (j.perfil) { perfil = j.perfil; cambioPerfil(perfil); }
    return j;
  }

  return {
    iniciar, marcarSubida,
    onPerfil: f => { cambioPerfil = f; if (perfil) f(perfil); },
    perfil: () => perfil,
  };
})();