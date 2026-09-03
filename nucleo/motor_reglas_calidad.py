#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor DETERMINISTICO de reglas de calidad (F1 del agente de calidad de datos).

Dada una conexion abierta y el bloque "reglas" de la configuracion (ver
contrato_reglas_calidad_schema.json), traduce cada regla a una condicion SQL y la
EJECUTA EN EL SERVIDOR (COUNT + TOP N de muestra), en vez de traer la tabla entera a
Python: sobre tablas grandes de SQL2025 esto es lo que hace viable el chequeo.

Tipos de regla soportados: no_nulo, rango, valores_permitidos, patron_like, unico,
condicional, sql_personalizada. Los dos ultimos son un escape hatch de SQL crudo para
reglas de negocio arbitrarias; el fichero de reglas es configuracion interna del
equipo de datos, no input de usuario final.

Uso como modulo:
  from nucleo.motor_reglas_calidad import evaluar_reglas
  hallazgos = evaluar_reglas(conn, cfg, limite_muestra=20)
"""

import json
import sys


def _ident(nombre: str) -> str:
    """Escapa un identificador SQL Server entre corchetes: [nombre] -> [na]]me] si trae ']'."""
    return "[" + str(nombre).replace("]", "]]") + "]"


def _tabla_qualificada(conexion: dict) -> str:
    esquema = conexion.get("esquema") or "dbo"
    return f"{_ident(esquema)}.{_ident(conexion['tabla'])}"


def _condicion_sql(regla: dict) -> str:
    """Fragmento SQL booleano que representa 'la regla SE CUMPLE'. La condicion de
    incumplimiento que se ejecuta es WHERE NOT (<esto>), salvo 'unico' (ver evaluar_reglas)."""
    tipo = regla["tipo"]
    if tipo == "no_nulo":
        return f"{_ident(regla['columna'])} IS NOT NULL"
    if tipo == "rango":
        partes = []
        if "min" in regla:
            partes.append(f"{_ident(regla['columna'])} >= {regla['min']}")
        if "max" in regla:
            partes.append(f"{_ident(regla['columna'])} <= {regla['max']}")
        # NULL no es un incumplimiento de rango (para eso esta 'no_nulo'): se acepta explicitamente.
        return f"({_ident(regla['columna'])} IS NULL OR (" + " AND ".join(partes) + "))"
    if tipo == "valores_permitidos":
        # La comilla se escapa igual que en 'patron_like', justo debajo: sin
        # esto un valor legitimo con apostrofe (O'Brien, D'Angelo...) genera
        # IN ('O'Brien', ...) y revienta la evaluacion de la tabla entera.
        vals = ", ".join(
            "'" + v.replace("'", "''") + "'" if isinstance(v, str) else str(v)
            for v in regla["valores"]
        )
        return f"({_ident(regla['columna'])} IS NULL OR {_ident(regla['columna'])} IN ({vals}))"
    if tipo == "patron_like":
        patron = regla["patron"].replace("'", "''")
        return f"({_ident(regla['columna'])} IS NULL OR {_ident(regla['columna'])} LIKE '{patron}')"
    if tipo == "condicional":
        return f"(NOT ({regla['condicion_si']}) OR ({regla['condicion_entonces']}))"
    if tipo == "sql_personalizada":
        return f"({regla['condicion']})"
    raise ValueError(f"Tipo de regla no soportado por _condicion_sql: {tipo!r}")


def _columnas_muestra(regla: dict, columnas_clave: list) -> list:
    """Columnas a proyectar en la muestra: las claves + la(s) columna(s) de la regla."""
    cols = list(columnas_clave)
    if "columna" in regla and regla["columna"] not in cols:
        cols.append(regla["columna"])
    for c in regla.get("columnas", []):
        if c not in cols:
            cols.append(c)
    return cols


def _fetch_muestra(cur, tabla: str, columnas: list, where: str, limite: int) -> list:
    # 'columnas' puede quedar vacia (reglas 'sql_personalizada'/'condicional', que
    # no referencian una sola columna, combinado con conexion sin columnas_clave):
    # sin fallback a '*', esto generaba 'SELECT TOP N  FROM ...' -- SQL invalido.
    select_cols = ", ".join(_ident(c) for c in columnas) if columnas else "*"
    cur.execute(f"SELECT TOP {int(limite)} {select_cols} FROM {tabla} WHERE {where}")
    filas = cur.fetchall()
    nombres = [d[0] for d in cur.description]
    return [dict(zip(nombres, fila)) for fila in filas]


def _evaluar_una(cur, tabla: str, regla: dict, columnas_clave: list, limite_muestra: int) -> dict:
    severidad = regla.get("severidad", "media")
    etiqueta = regla.get("columna") or ", ".join(regla.get("columnas", [])) or "(fila)"
    descripcion = regla.get("descripcion") or f"{regla['tipo']} sobre {etiqueta}"

    if regla["tipo"] == "unico":
        cols = regla["columnas"]
        select_cols = ", ".join(_ident(c) for c in cols)
        cur.execute(
            f"SELECT COUNT(*) FROM (SELECT {select_cols} FROM {tabla} "
            f"GROUP BY {select_cols} HAVING COUNT(*) > 1) t"
        )
        grupos_duplicados = cur.fetchone()[0]
        muestra = []
        if grupos_duplicados:
            cur.execute(
                f"SELECT TOP {int(limite_muestra)} {select_cols}, COUNT(*) AS Repeticiones "
                f"FROM {tabla} GROUP BY {select_cols} HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC"
            )
            filas = cur.fetchall()
            nombres = [d[0] for d in cur.description]
            muestra = [dict(zip(nombres, fila)) for fila in filas]
        return {
            "regla": regla["tipo"], "columna": etiqueta, "severidad": severidad,
            "descripcion": descripcion, "total_incumplen": grupos_duplicados, "muestra": muestra,
            "clave_diccionario": regla.get("clave_diccionario"),
        }

    condicion = _condicion_sql(regla)
    cur.execute(f"SELECT COUNT(*) FROM {tabla} WHERE NOT ({condicion})")
    total = cur.fetchone()[0]
    muestra = []
    if total:
        cols = _columnas_muestra(regla, columnas_clave)
        muestra = _fetch_muestra(cur, tabla, cols, f"NOT ({condicion})", limite_muestra)
    return {
        "regla": regla["tipo"], "columna": etiqueta, "severidad": severidad,
        "descripcion": descripcion, "total_incumplen": total, "muestra": muestra,
        "clave_diccionario": regla.get("clave_diccionario"),
    }


def evaluar_reglas(conn, cfg: dict, limite_muestra: int = 20) -> list:
    """Evalua TODAS las reglas de cfg['reglas'] contra cfg['conexion']. Devuelve una
    lista de hallazgos, INCLUYENDO las reglas que no encontraron incumplimientos
    (total_incumplen == 0), para que el informe sea un chequeo completo."""
    tabla = _tabla_qualificada(cfg["conexion"])
    columnas_clave = cfg["conexion"].get("columnas_clave", [])
    cur = conn.cursor()
    return [_evaluar_una(cur, tabla, regla, columnas_clave, limite_muestra) for regla in cfg["reglas"]]


def main():
    if len(sys.argv) < 2:
        print("Uso: python -m nucleo.motor_reglas_calidad <config.json> [--muestra N]")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cfg = json.load(f)
    limite = 20
    if "--muestra" in sys.argv:
        limite = int(sys.argv[sys.argv.index("--muestra") + 1])

    from nucleo import agente_calidad_datos as acd  # reutiliza _conectar (misma config de conexion)
    conn = acd._conectar(cfg["conexion"])
    try:
        hallazgos = evaluar_reglas(conn, cfg, limite_muestra=limite)
    finally:
        conn.close()
    print(json.dumps(hallazgos, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
