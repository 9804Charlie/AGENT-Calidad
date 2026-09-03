#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor de deteccion ESTADISTICA de anomalias (F1 del agente de calidad de datos).

Complementa a motor_reglas_calidad.py: no verifica reglas explicitas, sino que
busca registros que se salen del patron normal de una columna sin que ninguna
regla los marque. Los calculos (media, desviacion, percentiles, frecuencias) se
hacen en SQL Server (AVG/STDEV/PERCENTILE_CONT/GROUP BY), no en Python: sobre una
tabla grande, traer todas las filas para calcular esto con numpy seria mas lento y
no aporta nada que SQL no de ya.

Dos familias, ver contrato_reglas_calidad_schema.json -> "anomalias":
  - numericas  : outliers por z-score o rango intercuartilico (IQR).
  - categoricas: valores que aparecen en un porcentaje de filas anormalmente bajo.

Uso como modulo:
  from nucleo.motor_anomalias import detectar_anomalias
  hallazgos = detectar_anomalias(conn, cfg, limite_muestra=20)
"""

import json
import sys

from nucleo.motor_reglas_calidad import _ident, _tabla_qualificada, _fetch_muestra


def _total_filas(cur, tabla: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {tabla}")
    return cur.fetchone()[0]


def _anomalia_numerica_zscore(cur, tabla: str, columna: str, umbral: float,
                               columnas_clave: list, limite_muestra: int) -> dict:
    col = _ident(columna)
    cur.execute(f"SELECT AVG(CAST({col} AS FLOAT)), STDEV(CAST({col} AS FLOAT)) "
                f"FROM {tabla} WHERE {col} IS NOT NULL")
    media, desviacion = cur.fetchone()
    if media is None or not desviacion:
        return {"columna": columna, "tipo_anomalia": "numerica_zscore",
                "detalle_estadistico": {"media": media, "desviacion": desviacion},
                "total_filas_atipicas": 0, "muestra": [],
                "aviso": "Sin desviacion (columna constante, vacia o toda NULL); no se puede calcular z-score."}

    condicion = (f"{col} IS NOT NULL AND ABS(CAST({col} AS FLOAT) - {media}) / {desviacion} > {umbral}")
    cur.execute(f"SELECT COUNT(*) FROM {tabla} WHERE {condicion}")
    total = cur.fetchone()[0]
    muestra = []
    if total:
        cols = list(columnas_clave) + ([columna] if columna not in columnas_clave else [])
        muestra = _fetch_muestra(cur, tabla, cols, condicion, limite_muestra)
    return {"columna": columna, "tipo_anomalia": "numerica_zscore",
            "detalle_estadistico": {"media": media, "desviacion": desviacion, "umbral": umbral},
            "total_filas_atipicas": total, "muestra": muestra}


def _anomalia_numerica_iqr(cur, tabla: str, columna: str, factor: float,
                            columnas_clave: list, limite_muestra: int) -> dict:
    col = _ident(columna)
    cur.execute(
        f"SELECT DISTINCT "
        f"PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY CAST({col} AS FLOAT)) OVER (), "
        f"PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY CAST({col} AS FLOAT)) OVER () "
        f"FROM {tabla} WHERE {col} IS NOT NULL"
    )
    fila = cur.fetchone()
    if not fila or fila[0] is None:
        return {"columna": columna, "tipo_anomalia": "numerica_iqr",
                "detalle_estadistico": {}, "total_filas_atipicas": 0, "muestra": [],
                "aviso": "Sin datos suficientes para calcular percentiles."}
    q1, q3 = fila
    iqr = q3 - q1
    limite_inf, limite_sup = q1 - factor * iqr, q3 + factor * iqr
    condicion = (f"{col} IS NOT NULL AND (CAST({col} AS FLOAT) < {limite_inf} "
                 f"OR CAST({col} AS FLOAT) > {limite_sup})")
    cur.execute(f"SELECT COUNT(*) FROM {tabla} WHERE {condicion}")
    total = cur.fetchone()[0]
    muestra = []
    if total:
        cols = list(columnas_clave) + ([columna] if columna not in columnas_clave else [])
        muestra = _fetch_muestra(cur, tabla, cols, condicion, limite_muestra)
    return {"columna": columna, "tipo_anomalia": "numerica_iqr",
            "detalle_estadistico": {"q1": q1, "q3": q3, "iqr": iqr,
                                     "limite_inferior": limite_inf, "limite_superior": limite_sup},
            "total_filas_atipicas": total, "muestra": muestra}


def _anomalia_categorica(cur, tabla: str, columna: str, umbral_pct: float,
                          columnas_clave: list, limite_muestra: int) -> list:
    col = _ident(columna)
    total = _total_filas(cur, tabla)
    if not total:
        return []
    cur.execute(f"SELECT {col}, COUNT(*) AS n FROM {tabla} GROUP BY {col}")
    conteos = cur.fetchall()
    hallazgos = []
    cols_muestra = list(columnas_clave) + ([columna] if columna not in columnas_clave else [])
    for valor, n in conteos:
        pct = n * 100.0 / total
        if pct >= umbral_pct:
            continue
        if valor is None:
            condicion = f"{col} IS NULL"
        elif isinstance(valor, str):
            condicion = f"{col} = '{valor.replace(chr(39), chr(39) * 2)}'"
        else:
            condicion = f"{col} = {valor}"
        muestra = _fetch_muestra(cur, tabla, cols_muestra, condicion, limite_muestra)
        hallazgos.append({
            "columna": columna, "tipo_anomalia": "categorica_rara",
            "detalle_estadistico": {"valor": valor, "ocurrencias": n, "porcentaje": round(pct, 4),
                                     "umbral_pct": umbral_pct},
            "total_filas_atipicas": n, "muestra": muestra,
        })
    return hallazgos


def detectar_anomalias(conn, cfg: dict, limite_muestra: int = 20) -> list:
    """Evalua cfg['anomalias'] (numericas + categoricas) contra cfg['conexion'].
    Devuelve lista de hallazgos; vacia si la seccion 'anomalias' no esta configurada."""
    anomalias_cfg = cfg.get("anomalias") or {}
    if not anomalias_cfg:
        return []
    tabla = _tabla_qualificada(cfg["conexion"])
    columnas_clave = cfg["conexion"].get("columnas_clave", [])
    cur = conn.cursor()
    out = []

    num_cfg = anomalias_cfg.get("numericas")
    if num_cfg:
        metodo = num_cfg.get("metodo", "zscore")
        for columna in num_cfg["columnas"]:
            if metodo == "iqr":
                out.append(_anomalia_numerica_iqr(cur, tabla, columna, num_cfg.get("factor_iqr", 1.5),
                                                   columnas_clave, limite_muestra))
            else:
                out.append(_anomalia_numerica_zscore(cur, tabla, columna, num_cfg.get("umbral", 3.0),
                                                       columnas_clave, limite_muestra))

    cat_cfg = anomalias_cfg.get("categoricas")
    if cat_cfg:
        umbral_pct = cat_cfg.get("umbral_pct", 0.5)
        for columna in cat_cfg["columnas"]:
            out.extend(_anomalia_categorica(cur, tabla, columna, umbral_pct, columnas_clave, limite_muestra))

    return out


def main():
    if len(sys.argv) < 2:
        print("Uso: python -m nucleo.motor_anomalias <config.json> [--muestra N]")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cfg = json.load(f)
    limite = 20
    if "--muestra" in sys.argv:
        limite = int(sys.argv[sys.argv.index("--muestra") + 1])

    from nucleo import agente_calidad_datos as acd
    conn = acd._conectar(cfg["conexion"])
    try:
        hallazgos = detectar_anomalias(conn, cfg, limite_muestra=limite)
    finally:
        conn.close()
    print(json.dumps(hallazgos, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
