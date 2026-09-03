#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRUEBA DE HUMO del agente de calidad de datos.

Verifica las tres patas que agente_calidad_datos.py necesita para funcionar,
igual en espiritu que prueba_humo.py:
  1. Configuracion  -> el JSON de reglas carga y pasa el contrato (jsonschema).
  2. SQL2025        -> conecta a la tabla indicada en la configuracion.
  3. Ollama / phi4  -> la explicacion en lenguaje natural de los hallazgos.

Uso:
  python -m exploracion.prueba_calidad_datos [ruta_config.json]
  (por defecto usa reglas_calidad_ejemplo.json)
"""

import os
import sys
import time

OK = "[ OK ]"
KO = "[ KO ]"

CONFIG_POR_DEFECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "reglas_calidad_ejemplo.json")


def _check(nombre, fn):
    t0 = time.time()
    try:
        detalle = fn()
        ms = int((time.time() - t0) * 1000)
        print(f"{OK} {nombre:<28} {detalle}  ({ms} ms)")
        return True
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        msg = str(e).replace("\n", " ")[:140]
        print(f"{KO} {nombre:<28} {msg}  ({ms} ms)")
        return False


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_POR_DEFECTO
    print("=" * 70)
    print("PRUEBA DE HUMO - agente de calidad de datos")
    print(f"Config: {config_path}")
    print("=" * 70)

    import json
    from nucleo import agente_calidad_datos as acd

    cfg = {}

    def check_config():
        nonlocal cfg
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        validacion = acd._validar_config(cfg)
        if not validacion["ok"]:
            raise RuntimeError("; ".join(validacion["errores"][:3]))
        return f"{len(cfg['reglas'])} reglas, config valida"

    def check_sql2025():
        conn = acd._conectar(cfg["conexion"])
        try:
            cur = conn.cursor()
            con = cfg["conexion"]
            tabla = f"{con.get('esquema', 'dbo')}.{con['tabla']}"
            cur.execute(f"SELECT COUNT(*) FROM {tabla}")
            n = cur.fetchone()[0]
            return f"{tabla}: {n} filas"
        finally:
            conn.close()

    def check_ollama():
        import requests
        base = acd.OLLAMA_CHAT.split("/v1")[0]
        r = requests.get(base.rstrip("/") + "/api/tags", timeout=8)
        r.raise_for_status()
        modelos = [m.get("name", "") for m in r.json().get("models", [])]
        hay_phi4 = any("phi4" in m for m in modelos)
        if not hay_phi4:
            return "Ollama responde, pero NO veo phi4 (ejecuta: ollama run phi4 'ok')"
        return f"Ollama OK, phi4 disponible ({len(modelos)} modelos)"

    resultados = [
        _check("1. Configuracion", check_config),
        _check("2. SQL2025 (tabla objetivo)", check_sql2025),
        _check("3. Ollama / phi4", check_ollama),
    ]
    print("-" * 70)
    if all(resultados):
        print(f"{OK} TODO CORRECTO: el agente de calidad puede ejecutarse.")
        sys.exit(0)
    else:
        fallan = sum(1 for r in resultados if not r)
        print(f"{KO} HAY {fallan} COMPROBACION(ES) FALLIDA(S). Revisa las lineas KO de arriba.")
        sys.exit(1)


if __name__ == "__main__":
    main()
