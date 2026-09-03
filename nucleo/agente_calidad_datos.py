#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGENTE DE CALIDAD DE DATOS — orquestador y CLI.

Revisa una tabla de SQL Server (SQL2025 u otra instancia) contra un fichero de
configuracion de REGLAS DE CALIDAD (ver contrato_reglas_calidad_schema.json /
reglas_calidad_ejemplo.json) y detecta ademas registros ANOMALOS por estadistica,
sin necesidad de que una regla explicita los marque.

Como en consulta_semantica.py: el motor deterministico (motor_reglas_calidad.py,
motor_anomalias.py) hace TODO el trabajo pesado en SQL Server; el LLM local
(phi4 via Ollama) SOLO REDACTA una explicacion en lenguaje natural sobre los
hallazgos ya confirmados, apoyandose en el contexto de negocio del diccionario
(herramientas_diccionario.py) cuando la columna ya esta documentada. Si Ollama no
esta disponible, el informe sale igual, solo que sin el parrafo explicado (la
evidencia estructurada — regla, total, muestra — no depende del LLM).

Salida: un informe JSON con resumen por severidad, reglas evaluadas (incluidas las
que no fallaron) y anomalias detectadas. No hay cola de revision humana: es un
informe/log, no una propuesta a aprobar.

Uso CLI:
  python -m nucleo.agente_calidad_datos --config reglas_calidad_ejemplo.json
  python -m nucleo.agente_calidad_datos --config mi_tabla.json --muestra 10 --sin-llm --out informe.json
"""

import argparse
import json
import os
import sys

# ----------------------------- CONFIGURACION ------------------------------- #
# Este agente corre como SERVICIO PROPIO (ver api_calidad.py, puerto 5001) en
# la MISMA maquina que Ollama (10.0.0.10) -- independiente de la app de
# produccion del diccionario (api_revision.py, puerto 5000). Por eso se llega
# a Ollama por localhost directamente, igual que consulta_semantica.py.
OLLAMA_CHAT = "http://localhost:11434/v1/chat/completions"
# Mismo modelo derivado que usa el traductor (ver traductor_reglas_calidad.py
# para el porque). Aqui los prompts son pequenos y no se truncaban, pero se
# apunta al mismo para que este proyecto cargue UNA sola variante en memoria:
# el servidor ya tiene varias etiquetas de phi4 y solo 3,1 GB de RAM libre.
MODELO_LLM = "phi4-calidad"
RUTA_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "contrato_reglas_calidad_schema.json")
# --------------------------------------------------------------------------- #

_schema_cache = None


def ruta_informe_defecto(config_path: str) -> str:
    """Ruta donde se cachea el informe de una config, junto al propio fichero
    de config: 'reglas_calidad_ejemplo.json' -> 'informe_reglas_calidad_ejemplo.json'.
    Usada como valor por defecto de --out y por calidad_web.py para saber donde
    leer/escribir el informe de cada tabla sin que el usuario gestione rutas."""
    carpeta = os.path.dirname(os.path.abspath(config_path))
    base = os.path.splitext(os.path.basename(config_path))[0]
    return os.path.join(carpeta, f"informe_{base}.json")


def _cargar_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        with open(RUTA_SCHEMA, "r", encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _schema_cache


def _validar_config(cfg: dict) -> dict:
    """Valida cfg contra el contrato JSON Schema. Devuelve {"ok", "errores"}."""
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(_cargar_schema())
    errores = []
    for e in sorted(validator.iter_errors(cfg), key=lambda e: list(e.path)):
        ruta = ".".join(str(p) for p in e.path) or "(raiz)"
        errores.append(f"[contrato] {ruta}: {e.message}")
    return {"ok": len(errores) == 0, "errores": errores}


def _conectar(conexion: dict):
    """Conecta a SQL Server con la configuracion del bloque 'conexion' del JSON."""
    import pyodbc
    driver = conexion.get("driver", "ODBC Driver 17 for SQL Server")
    base_datos = f"{conexion['base_datos']}"
    partes = [f"DRIVER={{{driver}}}", f"SERVER={conexion['servidor']}", f"DATABASE={base_datos}"]
    if conexion.get("auth_windows", True):
        partes.append("Trusted_Connection=yes")
    else:
        partes += [f"UID={conexion['usuario']}", f"PWD={conexion['password']}"]
    return pyodbc.connect(";".join(partes) + ";", timeout=15)


def _contexto_negocio(clave_diccionario) -> str:
    """Contexto de negocio best-effort desde el diccionario ya existente. Si la
    columna no esta documentada (tabla nueva de SQL2025) o el diccionario no
    responde, se devuelve vacio: el LLM razona solo con la regla y la muestra."""
    if not clave_diccionario:
        return ""
    try:
        from diccionario import herramientas_diccionario as hd
        bloques = [hd.descripciones_texto(clave_diccionario), hd.linaje_texto(clave_diccionario)]
        return "\n\n".join(b for b in bloques if b)
    except Exception:
        return ""


def _prompt_hallazgo(hallazgo: dict, es_anomalia: bool, contexto: str) -> tuple:
    if es_anomalia:
        cuerpo = (
            f"Columna: {hallazgo['columna']}\n"
            f"Tipo de anomalia: {hallazgo['tipo_anomalia']}\n"
            f"Detalle estadistico: {json.dumps(hallazgo['detalle_estadistico'], ensure_ascii=False, default=str)}\n"
            f"Filas atipicas encontradas: {hallazgo['total_filas_atipicas']}\n"
            f"Muestra de filas (hasta 10): {json.dumps(hallazgo['muestra'][:10], ensure_ascii=False, default=str)}"
        )
    else:
        cuerpo = (
            f"Regla incumplida: {hallazgo['regla']} sobre {hallazgo['columna']}\n"
            f"Descripcion de la regla: {hallazgo['descripcion']}\n"
            f"Severidad configurada: {hallazgo['severidad']}\n"
            f"Filas que incumplen: {hallazgo['total_incumplen']}\n"
            f"Muestra de filas (hasta 10): {json.dumps(hallazgo['muestra'][:10], ensure_ascii=False, default=str)}"
        )
    if contexto:
        cuerpo += f"\n\nCONTEXTO DE NEGOCIO DE LA COLUMNA:\n{contexto}"
    sistema = (
        "Eres un analista de calidad de datos de una entidad hipotecaria (contexto "
        "regulatorio IFRS9). Se te da un hallazgo YA CONFIRMADO por un motor "
        "deterministico (una regla incumplida o una anomalia estadistica) sobre una "
        "tabla real. Tu trabajo es SOLO explicar el hallazgo en 2-4 frases en espanol: "
        "que significa en terminos de negocio, una posible causa razonable (sin "
        "inventar hechos que no esten en los datos dados) y si merece atencion "
        "prioritaria. No repitas los numeros tal cual, interpreta. No hables de nada "
        "fuera de este hallazgo."
    )
    return sistema, cuerpo


def explicar_hallazgo(hallazgo: dict, es_anomalia: bool = False) -> str:
    """Llama a Ollama/phi4 para redactar una explicacion en lenguaje natural del
    hallazgo. Si Ollama no responde, devuelve cadena vacia (el hallazgo se reporta
    igual, sin parrafo explicado)."""
    contexto = _contexto_negocio(hallazgo.get("clave_diccionario"))
    sistema, usuario = _prompt_hallazgo(hallazgo, es_anomalia, contexto)
    try:
        import requests
        r = requests.post(OLLAMA_CHAT, timeout=120, json={
            "model": MODELO_LLM, "temperature": 0.1,
            "messages": [{"role": "system", "content": sistema},
                         {"role": "user", "content": usuario}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


def _resumen(reglas_out: list, anomalias_out: list) -> dict:
    incumplidas = [r for r in reglas_out if r["total_incumplen"] > 0]
    por_severidad = {"alta": 0, "media": 0, "baja": 0}
    for r in incumplidas:
        por_severidad[r.get("severidad", "media")] = por_severidad.get(r.get("severidad", "media"), 0) + 1
    return {
        "total_reglas": len(reglas_out),
        "reglas_incumplidas": len(incumplidas),
        "por_severidad": por_severidad,
        "anomalias_detectadas": sum(1 for a in anomalias_out if a["total_filas_atipicas"] > 0),
    }


def generar_informe(cfg: dict, reglas_out: list, anomalias_out: list, tabla_nombre: str) -> dict:
    return {
        "tabla": tabla_nombre,
        "resumen": _resumen(reglas_out, anomalias_out),
        "incidencias_reglas": reglas_out,
        "anomalias": anomalias_out,
    }


def _imprimir_resumen(informe: dict):
    r = informe["resumen"]
    print("=" * 70)
    print(f"INFORME DE CALIDAD — {informe['tabla']}")
    print("=" * 70)
    print(f"Reglas evaluadas: {r['total_reglas']}  |  Incumplidas: {r['reglas_incumplidas']}  "
          f"(alta={r['por_severidad'].get('alta', 0)}, media={r['por_severidad'].get('media', 0)}, "
          f"baja={r['por_severidad'].get('baja', 0)})")
    print(f"Anomalias con filas atipicas: {r['anomalias_detectadas']}")
    print("-" * 70)
    for h in informe["incidencias_reglas"]:
        estado = "FALLA" if h["total_incumplen"] > 0 else "OK"
        print(f"[{estado:5}] ({h['severidad']:5}) {h['regla']:16} {h['columna']:24} "
              f"incumplen={h['total_incumplen']}")
        if h.get("explicacion"):
            print(f"          -> {h['explicacion']}")
    for a in informe["anomalias"]:
        if a["total_filas_atipicas"] > 0:
            print(f"[ANOM ]         {a['tipo_anomalia']:16} {a['columna']:24} "
                  f"atipicas={a['total_filas_atipicas']}")
            if a.get("explicacion"):
                print(f"          -> {a['explicacion']}")
    print("=" * 70)


def ejecutar(config_path: str, limite_muestra: int = 20, redactar: bool = True,
             out_path: str = None) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    validacion = _validar_config(cfg)
    if not validacion["ok"]:
        raise ValueError("Configuracion invalida:\n" + "\n".join(validacion["errores"]))

    from nucleo import motor_reglas_calidad as mrc
    from nucleo import motor_anomalias as ma

    conn = _conectar(cfg["conexion"])
    try:
        reglas_out = mrc.evaluar_reglas(conn, cfg, limite_muestra=limite_muestra)
        anomalias_out = ma.detectar_anomalias(conn, cfg, limite_muestra=limite_muestra)
    finally:
        conn.close()

    if redactar:
        for h in reglas_out:
            if h["total_incumplen"] > 0:
                h["explicacion"] = explicar_hallazgo(h, es_anomalia=False)
        for a in anomalias_out:
            if a["total_filas_atipicas"] > 0:
                a["explicacion"] = explicar_hallazgo(a, es_anomalia=True)

    con = cfg["conexion"]
    tabla_nombre = f"{con['servidor']}.{con['base_datos']}.{con.get('esquema', 'dbo')}.{con['tabla']}"
    informe = generar_informe(cfg, reglas_out, anomalias_out, tabla_nombre)

    out_path = out_path or ruta_informe_defecto(config_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(informe, f, ensure_ascii=False, indent=2, default=str)

    return informe


_CAMPOS_POR_TIPO = {
    "no_nulo": ("columna",),
    "rango": ("columna", "min", "max"),
    "valores_permitidos": ("columna", "valores"),
    "patron_like": ("columna", "patron"),
    "unico": ("columnas",),
    "condicional": ("condicion_si", "condicion_entonces"),
    "sql_personalizada": ("condicion",),
}


def _regla_desde_propuesta(regla_json: dict) -> dict:
    """Proyecta el JSON que devuelve traductor_reglas_calidad.py (que trae
    ademas 'confianza'/'columna_elegida'/'error', ajenos al contrato) a un
    objeto que SI valida contra contrato_reglas_calidad_schema.json."""
    tipo = regla_json.get("tipo")
    campos = _CAMPOS_POR_TIPO.get(tipo, ())
    regla = {"tipo": tipo}
    for campo in campos:
        if campo in regla_json and regla_json[campo] is not None:
            regla[campo] = regla_json[campo]
    if regla_json.get("descripcion"):
        regla["descripcion"] = regla_json["descripcion"]
    if regla_json.get("columna_elegida"):
        regla["clave_diccionario"] = regla_json["columna_elegida"]
    regla["severidad"] = regla_json.get("severidad") or "media"
    return regla


# El diccionario nombra los servidores por un ALIAS interno en las CLAVE
# (ej. 'SRV1.DW_PRINCIPAL.dbo.Tabla.Columna'), no por el nombre de host/instancia
# real conectable -- 'SRV1' NO es un servidor que exista en DNS. Traduce el
# alias al servidor real antes de escribirlo en una config ejecutable. Se
# amplia segun se vayan confirmando mas alias con acceso real.
ALIAS_SERVIDOR = {
    "SRV1": r"SERVIDOR_DATOS\INSTANCIA",
}


def _config_desde_tabla_destino(tabla_destino: str) -> dict:
    """Tabla_Destino = 'Servidor.BD.Esquema.Tabla' -> bloque 'conexion' base.
    columnas_clave se deja vacio: el revisor humano lo rellena si hace falta
    para identificar filas en la muestra del informe."""
    partes = tabla_destino.split(".")
    if len(partes) != 4:
        raise ValueError(f"Tabla_Destino inesperada (se espera Servidor.BD.Esquema.Tabla): {tabla_destino!r}")
    servidor, base_datos, esquema, tabla = partes
    servidor = ALIAS_SERVIDOR.get(servidor, servidor)
    return {
        # 'columnas_clave' se omite a proposito: el schema exige minItems=1 si
        # esta presente, y el revisor humano es quien sabe cual es la PK real;
        # sin ella el informe simplemente no identifica filas en la muestra.
        "conexion": {"servidor": servidor, "base_datos": base_datos,
                     "esquema": esquema, "tabla": tabla},
        "reglas": [],
    }


def activar_regla_aprobada(propuesta: dict) -> str:
    """Fusiona una propuesta APROBADA (ver registro_propuestas_reglas.ver) en el
    reglas_calidad_<tabla>.json de su tabla destino, creandolo si no existe.
    Valida contra el contrato antes de guardar. Devuelve la ruta del fichero.

    Requiere que la propuesta tenga Tabla_Destino resuelta (si no, el revisor
    debe rellenarla a mano antes de aprobar: sin tabla no hay donde activarla)."""
    tabla_destino = propuesta.get("Tabla_Destino")
    if not tabla_destino:
        raise ValueError("La propuesta no tiene Tabla_Destino resuelta; "
                          "no se puede activar sin saber que tabla auditar.")

    regla_json = propuesta.get("Regla_Final_JSON") or propuesta.get("Regla_Propuesta_JSON")
    regla = _regla_desde_propuesta(regla_json)

    nombre_tabla = tabla_destino.split(".")[-1]
    ruta_cfg = os.path.join(os.path.dirname(os.path.abspath(RUTA_SCHEMA)),
                             f"reglas_calidad_{nombre_tabla}.json")
    if os.path.isfile(ruta_cfg):
        with open(ruta_cfg, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = _config_desde_tabla_destino(tabla_destino)

    ya_existe = any(r == regla for r in cfg["reglas"])
    if not ya_existe:
        cfg["reglas"].append(regla)

    validacion = _validar_config(cfg)
    if not validacion["ok"]:
        raise ValueError("La regla aprobada produce una config invalida:\n" +
                          "\n".join(validacion["errores"]))

    with open(ruta_cfg, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, default=str)
    return ruta_cfg


def main():
    ap = argparse.ArgumentParser(description="Agente de calidad de datos (reglas + anomalias)")
    ap.add_argument("--config", required=True, help="Ruta al JSON de configuracion (reglas + conexion)")
    ap.add_argument("--muestra", type=int, default=20, help="Filas de muestra por hallazgo")
    ap.add_argument("--sin-llm", action="store_true", help="No redactar explicaciones con el LLM")
    ap.add_argument("--out", help="Ruta donde escribir el informe JSON "
                                   "(por defecto: informe_<config>.json junto a --config)")
    ap.add_argument("--json", action="store_true", help="Imprimir el informe completo en JSON")
    args = ap.parse_args()

    try:
        informe = ejecutar(args.config, limite_muestra=args.muestra,
                            redactar=not args.sin_llm, out_path=args.out)
    except ValueError as e:
        print(f"ERROR: {e}"); sys.exit(1)
    except ImportError as e:
        print("Falta dependencia:  pip install pyodbc jsonschema requests")
        print("Detalle:", e); sys.exit(1)
    except Exception as e:
        print("Error ejecutando el agente de calidad:", e); sys.exit(1)

    if args.json:
        print(json.dumps(informe, ensure_ascii=False, indent=2, default=str))
    else:
        _imprimir_resumen(informe)
    if informe["resumen"]["por_severidad"].get("alta", 0) > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
