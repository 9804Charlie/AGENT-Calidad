#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae un "dataset de prueba" de METADATOS de un servidor SQL Server completo
(pensado para SQL2025): estructura de todas las bases de datos/tablas visibles
con el login usado, para poder disenar y afinar reglas de calidad SIN tocar el
servidor real en cada iteracion.

SOLO LECTURA, SOLO METADATOS: no se descarga ninguna fila real de ninguna
tabla. Por cada columna se guardan como mucho AGREGADOS (min/max/media,
nulos, distintos, valores mas frecuentes) -- nunca un valor individual de una
fila que pueda identificar a una persona. Aun asi, para columnas cuyo nombre
sugiere dato personal (NIF, nombre, direccion, telefono, cliente, titular...)
se omite el listado de "valores mas frecuentes" (solo se guarda el conteo de
distintos), por si el propio VALOR ya fuese sensible.

Que se extrae por tabla:
  - Columnas: nombre, tipo, longitud/precision/escala, nullable, identidad, calculada.
  - Claves primarias / unicas.
  - Claves foraneas (tabla/columna referenciada).
  - Indices.
  - Numero de filas (metadato de particion, NO hace COUNT(*) sobre la tabla).
  - (si no se pide --sin-estadisticas y la tabla no supera --max-filas-stats):
    por columna, min/max/media (numericas y fechas) o distintos+nulos+top10
    valores mas frecuentes (resto de tipos, salvo columnas tipo PII).

Salida: un JSON por base de datos (dataset_prueba_<basededatos>.json) + un
resumen (dataset_prueba_resumen.json) con el listado de bases/tablas/filas.

Requisitos:
  pip install pyodbc

Uso:
  python -m exploracion.extraer_metadatos_sql --servidor SQL2025SERVER
  python -m exploracion.extraer_metadatos_sql --servidor SQL2025SERVER --usuario U --password P
  python -m exploracion.extraer_metadatos_sql --servidor SQL2025SERVER --sin-estadisticas
  python -m exploracion.extraer_metadatos_sql --servidor SQL2025SERVER --out carpeta_salida
"""

import argparse
import json
import os
import sys
import time

DRIVER_DEFECTO = "ODBC Driver 17 for SQL Server"
MAX_FILAS_STATS_DEFECTO = 2_000_000

PII_PISTAS = (
    "nif", "dni", "cif", "nombre", "apellido", "direccion", "domicilio",
    "telefono", "movil", "email", "correo", "cliente", "titular",
    "interviniente", "iban", "tarjeta", "pasaporte", "nacimiento",
)


def _es_pii(nombre_columna: str) -> bool:
    n = nombre_columna.lower()
    return any(p in n for p in PII_PISTAS)


def _conectar(servidor: str, base_datos: str, driver: str, auth_windows: bool,
              usuario: str = None, password: str = None):
    import pyodbc
    partes = [f"DRIVER={{{driver}}}", f"SERVER={servidor}", f"DATABASE={base_datos}"]
    if auth_windows:
        partes.append("Trusted_Connection=yes")
    else:
        partes += [f"UID={usuario}", f"PWD={password}"]
    # TrustServerCertificate: instancias internas suelen usar cert autofirmado
    partes.append("TrustServerCertificate=yes")
    return pyodbc.connect(";".join(partes) + ";", timeout=15)


def _bases_datos(conn, incluir_sistema: bool) -> list:
    cur = conn.cursor()
    if incluir_sistema:
        cur.execute("SELECT name FROM sys.databases WHERE state = 0 ORDER BY name")
    else:
        cur.execute("SELECT name FROM sys.databases WHERE state = 0 AND database_id > 4 ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def _tablas(conn) -> list:
    cur = conn.cursor()
    cur.execute("""
        SELECT s.name AS esquema, t.name AS tabla, t.object_id
        FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
        ORDER BY s.name, t.name
    """)
    return [{"esquema": r[0], "tabla": r[1], "object_id": r[2]} for r in cur.fetchall()]


def _columnas(conn, object_id: int) -> list:
    cur = conn.cursor()
    cur.execute("""
        SELECT c.name, ty.name AS tipo, c.max_length, c.precision, c.scale,
               c.is_nullable, c.is_identity, c.is_computed,
               dc.definition AS default_def
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
        WHERE c.object_id = ?
        ORDER BY c.column_id
    """, object_id)
    return [{
        "nombre": r[0], "tipo": r[1], "longitud": r[2], "precision": r[3], "escala": r[4],
        "nullable": bool(r[5]), "identidad": bool(r[6]), "calculada": bool(r[7]),
        "default": r[8],
    } for r in cur.fetchall()]


def _claves(conn, object_id: int) -> list:
    cur = conn.cursor()
    cur.execute("""
        SELECT kc.type_desc, kc.name, c.name AS columna, ic.key_ordinal
        FROM sys.key_constraints kc
        JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE kc.parent_object_id = ?
        ORDER BY kc.name, ic.key_ordinal
    """, object_id)
    out = {}
    for tipo, nombre, columna, orden in cur.fetchall():
        out.setdefault(nombre, {"tipo": tipo, "columnas": []})
        out[nombre]["columnas"].append(columna)
    return [{"nombre": k, **v} for k, v in out.items()]


def _claves_foraneas(conn, object_id: int) -> list:
    cur = conn.cursor()
    cur.execute("""
        SELECT fk.name,
               c1.name AS columna_local,
               s2.name AS esquema_ref, t2.name AS tabla_ref, c2.name AS columna_ref
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.columns c1 ON c1.object_id = fkc.parent_object_id AND c1.column_id = fkc.parent_column_id
        JOIN sys.columns c2 ON c2.object_id = fkc.referenced_object_id AND c2.column_id = fkc.referenced_column_id
        JOIN sys.tables t2 ON t2.object_id = fkc.referenced_object_id
        JOIN sys.schemas s2 ON s2.schema_id = t2.schema_id
        WHERE fk.parent_object_id = ?
    """, object_id)
    return [{"nombre": r[0], "columna_local": r[1],
             "tabla_referenciada": f"{r[2]}.{r[3]}", "columna_referenciada": r[4]}
            for r in cur.fetchall()]


def _indices(conn, object_id: int) -> list:
    cur = conn.cursor()
    cur.execute("""
        SELECT i.name, i.type_desc, i.is_unique, i.is_primary_key, c.name AS columna, ic.key_ordinal
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE i.object_id = ? AND i.name IS NOT NULL AND ic.is_included_column = 0
        ORDER BY i.name, ic.key_ordinal
    """, object_id)
    out = {}
    for nombre, tipo, unico, es_pk, columna, _orden in cur.fetchall():
        out.setdefault(nombre, {"tipo": tipo, "unico": bool(unico), "es_pk": bool(es_pk), "columnas": []})
        out[nombre]["columnas"].append(columna)
    return [{"nombre": k, **v} for k, v in out.items()]


def _filas(conn, object_id: int) -> int:
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(row_count) FROM sys.dm_db_partition_stats
        WHERE object_id = ? AND index_id IN (0, 1)
    """, object_id)
    r = cur.fetchone()
    return int(r[0] or 0)


NUMERICOS = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money", "smallmoney"}
FECHAS = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}


def _stats_columna(conn, esquema: str, tabla: str, col: dict) -> dict:
    nombre, tipo = col["nombre"], col["tipo"]
    tabla_q = f"[{esquema}].[{tabla}]"
    col_q = f"[{nombre}]"
    cur = conn.cursor()
    try:
        if tipo in NUMERICOS or tipo in FECHAS:
            cur.execute(f"SELECT MIN({col_q}), MAX({col_q}), "
                        f"SUM(CASE WHEN {col_q} IS NULL THEN 1 ELSE 0 END) FROM {tabla_q}")
            mn, mx, nulos = cur.fetchone()
            stats = {"min": mn, "max": mx, "nulos": nulos}
            if tipo in NUMERICOS:
                cur.execute(f"SELECT AVG(CAST({col_q} AS FLOAT)) FROM {tabla_q} WHERE {col_q} IS NOT NULL")
                (media,) = cur.fetchone()
                stats["media"] = media
            return stats
        else:
            cur.execute(f"SELECT COUNT(DISTINCT {col_q}), "
                        f"SUM(CASE WHEN {col_q} IS NULL THEN 1 ELSE 0 END) FROM {tabla_q}")
            distintos, nulos = cur.fetchone()
            stats = {"distintos": distintos, "nulos": nulos}
            if not _es_pii(nombre):
                cur.execute(f"SELECT TOP 10 {col_q}, COUNT(*) AS n FROM {tabla_q} "
                            f"GROUP BY {col_q} ORDER BY COUNT(*) DESC")
                stats["top_valores"] = [{"valor": v, "n": n} for v, n in cur.fetchall()]
            else:
                stats["aviso"] = "columna tipo PII: se omiten los valores, solo conteos"
            return stats
    except Exception as e:
        return {"error": str(e)[:200]}


def extraer_tabla(conn, esquema: str, tabla: str, object_id: int,
                   con_stats: bool, max_filas_stats: int) -> dict:
    columnas = _columnas(conn, object_id)
    filas = _filas(conn, object_id)
    resultado = {
        "esquema": esquema, "tabla": tabla, "filas": filas,
        "columnas": columnas,
        "claves": _claves(conn, object_id),
        "claves_foraneas": _claves_foraneas(conn, object_id),
        "indices": _indices(conn, object_id),
    }
    if con_stats and filas <= max_filas_stats:
        for col in columnas:
            col["stats"] = _stats_columna(conn, esquema, tabla, col)
    elif con_stats:
        resultado["aviso_stats"] = (f"tabla con {filas} filas > limite ({max_filas_stats}); "
                                     f"se omiten estadisticas por columna (usa --max-filas-stats para subir el limite)")
    return resultado


def extraer_base_datos(servidor: str, base_datos: str, driver: str, auth_windows: bool,
                        usuario: str, password: str, con_stats: bool, max_filas_stats: int) -> dict:
    conn = _conectar(servidor, base_datos, driver, auth_windows, usuario, password)
    try:
        tablas = _tablas(conn)
        salida = {"servidor": servidor, "base_datos": base_datos, "tablas": []}
        for i, t in enumerate(tablas, 1):
            t0 = time.time()
            try:
                info = extraer_tabla(conn, t["esquema"], t["tabla"], t["object_id"],
                                      con_stats, max_filas_stats)
                salida["tablas"].append(info)
                print(f"  [{i}/{len(tablas)}] {t['esquema']}.{t['tabla']} "
                      f"({info['filas']} filas, {len(info['columnas'])} columnas) "
                      f"({int((time.time()-t0)*1000)} ms)")
            except Exception as e:
                print(f"  [{i}/{len(tablas)}] {t['esquema']}.{t['tabla']} -> ERROR: {e}")
                salida["tablas"].append({"esquema": t["esquema"], "tabla": t["tabla"], "error": str(e)})
        return salida
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="Extrae metadatos (estructura + estadisticas agregadas, sin filas reales) de un servidor SQL Server")
    ap.add_argument("--servidor", required=True, help="Instancia SQL Server (host o host\\instancia)")
    ap.add_argument("--driver", default=DRIVER_DEFECTO)
    ap.add_argument("--usuario", help="Si se omite, se usa autenticacion de Windows")
    ap.add_argument("--password")
    ap.add_argument("--incluir-sistema", action="store_true", help="Incluir master/tempdb/model/msdb")
    ap.add_argument("--bases", help="Coma-separado: limitar el barrido a estas bases de datos "
                                     "(por defecto, todas las que el login pueda ver)")
    ap.add_argument("--sin-estadisticas", action="store_true",
                     help="Solo estructura (columnas/claves/indices/filas); no calcula min/max/top valores")
    ap.add_argument("--max-filas-stats", type=int, default=MAX_FILAS_STATS_DEFECTO,
                     help="No calcular estadisticas por columna en tablas con mas filas que esto")
    ap.add_argument("--out", default=".", help="Carpeta donde escribir los JSON de salida")
    args = ap.parse_args()

    auth_windows = not args.usuario
    con_stats = not args.sin_estadisticas
    os.makedirs(args.out, exist_ok=True)

    if args.bases:
        bases = [b.strip() for b in args.bases.split(",") if b.strip()]
    else:
        try:
            conn_master = _conectar(args.servidor, "master", args.driver, auth_windows, args.usuario, args.password)
            try:
                bases = _bases_datos(conn_master, args.incluir_sistema)
            finally:
                conn_master.close()
        except ImportError as e:
            print("Falta dependencia:  pip install pyodbc"); print("Detalle:", e); sys.exit(1)
        except Exception as e:
            print(f"No se pudo conectar a {args.servidor}: {e}"); sys.exit(1)

    print(f"Servidor {args.servidor}: {len(bases)} base(s) de datos a explorar.")
    resumen = {"servidor": args.servidor, "bases_datos": []}
    for bd in bases:
        print(f"\n=== {bd} ===")
        try:
            datos = extraer_base_datos(args.servidor, bd, args.driver, auth_windows,
                                        args.usuario, args.password, con_stats, args.max_filas_stats)
        except Exception as e:
            print(f"  ERROR conectando a {bd}: {e}")
            resumen["bases_datos"].append({"base_datos": bd, "error": str(e)})
            continue
        ruta = os.path.join(args.out, f"dataset_prueba_{bd}.json")
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2, default=str)
        resumen["bases_datos"].append({
            "base_datos": bd, "tablas": len(datos["tablas"]),
            "filas_totales": sum(t.get("filas", 0) for t in datos["tablas"]),
            "fichero": os.path.basename(ruta),
        })
        print(f"  -> {ruta}")

    ruta_resumen = os.path.join(args.out, "dataset_prueba_resumen.json")
    with open(ruta_resumen, "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nResumen: {ruta_resumen}")


if __name__ == "__main__":
    main()
