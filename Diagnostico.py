#!/usr/bin/env python3
"""Resumen del estado de los datos. Ejecuta:  python Diagnostico.py   y pega la salida."""
import json
from pathlib import Path
D = Path(__file__).parent / "datos"
base = json.loads((D / "lineas_base.json").read_text(encoding="utf-8"))["lineas"]
hor = json.loads((D / "horarios.json").read_text(encoding="utf-8"))
real = json.loads((D / "paradas_reales.json").read_text(encoding="utf-8")) if (D / "paradas_reales.json").exists() else {}
print(f"{'Lín':>4} {'estado':<11} {'paradas':>7} {'km':>6} {'min':>4} {'km/h':>5}  avisos")
for n in sorted(hor, key=int):
    r = real.get(n)
    dur = base.get(n, {}).get("duracion_min", {}).get("ida")
    if not r:
        print(f"{n:>4} {'sin datos':<11} {'-':>7} {'-':>6} {(dur or '-'):>4}")
        continue
    kmt = r.get("km_total") or (r["paradas"][-1]["k"] if r["paradas"] else 0)
    v = f"{kmt / dur * 60:.0f}" if dur and kmt else "-"
    estado = "OK" if r.get("valida", True) else "DESCARTADA"
    print(f"{n:>4} {estado:<11} {len(r['paradas']):>7} {kmt:>6} {(dur or '-'):>4} {v:>5}  {'; '.join(r.get('avisos', []))[:150]}")
    ks = [p["k"] for p in r["paradas"]]
    rep = max((ks.count(k) for k in set(ks)), default=0)
    if rep >= 5:
        print(f"       ! {rep} paradas comparten el mismo km (proyección sospechosa)")
    if dur and kmt and not 20 <= kmt / dur * 60 <= 85:
        print(f"       ! velocidad media {kmt / dur * 60:.0f} km/h con la duración de Moovit: revisa el trazado o la duración")