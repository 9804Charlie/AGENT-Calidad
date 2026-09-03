#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Herramienta del agente (F2): LECTURA DE ESQUEMA REAL.

Que hace:
  Dada la CLAVE de una columna (convencion del diccionario:
  Servidor.BD.Esquema.Tabla.Columna), devuelve sus METADATOS FISICOS REALES
  leidos del catalogo cosechado APP_CATALOGO.dbo.SQL_Columnas:
  tipo, longitud, precision, escala, nulabilidad, si es clave/calculada, formula...

Para que sirve en el agente:
  - Antes de proponer una ficha, el agente lee aqui el tipo/longitud REALES en vez
    de inventarlos. Los metadatos fisicos NO van en la ficha (viven en el catalogo),
    pero el agente los necesita para razonar y para verificar.
  - existe_columna()/existen_columnas() permiten COMPROBAR que una columna existe
    de verdad: la base de la verificacion de linaje (que cada eslabon de ObjetoOrigen
    apunte a una columna real).

Donde se ejecuta:
  En el cliente del agente. Se conecta SOLO LECTURA a la instancia SRV1, base
  APP_CATALOGO (la del diccionario), que es distinta de la base 'Agente' del RAG.

Requisitos:
  pip install pyodbc   (+ ODBC Driver 17/18 for SQL Server)

Uso por linea de comandos (prueba):
  python -m diccionario.herramientas_esquema "SRV1.DW_PRINCIPAL.dbo.Tb_Fact_Operacion_Historia.CRN"

Uso como modulo (lo que llamara el agente):
  from diccionario.herramientas_esquema import leer_esquema, existe_columna, existen_columnas
  meta = leer_esquema("SRV1.DW_PRINCIPAL.dbo.Tb_Fact_Operacion_Historia.CRN")
"""

import argparse
import json
import sys

# ----------------------------- CONFIGURACION ------------------------------- #
# OJO: esto apunta al diccionario (SRV1 / APP_CATALOGO), NO a la base 'Agente'.
SERVIDOR = r"ReportSQL2019\SERVIDOR_DATOS"
BASE_DATOS = "APP_CATALOGO"
DRIVER = "ODBC Driver 18 for SQL Server"

USAR_WINDOWS_AUTH = True
USUARIO = "Documentador"   # login de solo lectura del diccionario
PASSWORD = "Agente_User_1"              # rellena solo si USAR_WINDOWS_AUTH = False
# --------------------------------------------------------------------------- #

# Columnas que devolvemos, con nombre 'humano' -> columna real de SQL_Columnas
_CAMPOS = {
    "tipo": "DATA_TYPE",
    "longitud": "MAX_LENGTH",
    "precision": "NUMERIC_PRECISION",
    "escala": "NUMERIC_SCALE",
    "nullable": "IS_NULLABLE",
    "es_clave_primaria": "IS_PRIMARYKEY",
    "es_identity": "IS_IDENTITY",
    "es_calculado": "IS_COMPUTED",
    "formula": "FORMULA",
    "valor_defecto": "DEFAULT_VALUE",
    "check": "CHECK_VALUE",
}
_SELECT_COLS = ", ".join(_CAMPOS.values())


def _conn_str() -> str:
    partes = [f"DRIVER={{{DRIVER}}}", f"SERVER={SERVIDOR}", f"DATABASE={BASE_DATOS}"]
    if USAR_WINDOWS_AUTH:
        partes.append("Trusted_Connection=yes")
    else:
        partes.append(f"UID={USUARIO}")
        partes.append(f"PWD={PASSWORD}")
    if "18" in DRIVER:
        partes.append("Encrypt=yes")
        partes.append("TrustServerCertificate=yes")
    return ";".join(partes) + ";"


def _conectar():
    import pyodbc
    return pyodbc.connect(_conn_str(), timeout=10, readonly=True)


def leer_esquema(clave: str, _cn=None) -> dict:
    """
    Devuelve los metadatos fisicos de la columna identificada por CLAVE.
    Si no existe, devuelve {'clave': clave, 'encontrado': False}.
    El parametro _cn permite reutilizar una conexion abierta (uso interno).
    """
    cerrar = False
    cn = _cn
    if cn is None:
        cn = _conectar()
        cerrar = True
    try:
        cur = cn.cursor()
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM dbo.SQL_Columnas WHERE CLAVE = ?",
            clave,
        )
        fila = cur.fetchone()
        if fila is None:
            return {"clave": clave, "encontrado": False}
        nombres = list(_CAMPOS.keys())
        res = {"clave": clave, "encontrado": True}
        res.update({nombres[i]: fila[i] for i in range(len(nombres))})
        return res
    finally:
        if cerrar:
            cn.close()


def existe_columna(clave: str, _cn=None) -> bool:
    """True si la CLAVE existe en el catalogo de columnas SQL."""
    return leer_esquema(clave, _cn=_cn).get("encontrado", False)


def existen_columnas(claves) -> dict:
    """
    Comprueba en bloque una lista de CLAVE. Devuelve {clave: True/False}.
    Util para verificar de una vez todos los eslabones de ObjetoOrigen / linaje.
    """
    claves = list(claves)
    if not claves:
        return {}
    cn = _conectar()
    try:
        cur = cn.cursor()
        marcas = ",".join("?" for _ in claves)
        cur.execute(
            f"SELECT CLAVE FROM dbo.SQL_Columnas WHERE CLAVE IN ({marcas})",
            *claves,
        )
        existentes = {r[0] for r in cur.fetchall()}
    finally:
        cn.close()
    return {c: (c in existentes) for c in claves}


def main():
    ap = argparse.ArgumentParser(description="Lectura de esquema real desde el diccionario")
    ap.add_argument("clave", nargs="?", help="CLAVE: Servidor.BD.Esquema.Tabla.Columna")
    ap.add_argument("--json", action="store_true", help="Salida JSON")
    args = ap.parse_args()

    clave = args.clave or input("CLAVE: ").strip()
    if not clave:
        print("CLAVE vacia.")
        sys.exit(1)

    try:
        meta = leer_esquema(clave)
    except ImportError as e:
        print("Falta pyodbc. Instala con:  pip install pyodbc")
        print("Detalle:", e)
        sys.exit(1)
    except Exception as e:
        print("Error de conexion/consulta. Revisa SERVIDOR/BASE_DATOS/autenticacion.")
        print("Detalle:", e)
        sys.exit(1)

    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=2, default=str))
    elif not meta["encontrado"]:
        print(f"NO encontrada en el catalogo: {clave}")
    else:
        print(f"\nColumna: {clave}")
        for k in _CAMPOS:
            print(f"  {k:18}: {meta[k]}")
        print()


if __name__ == "__main__":
    main()
