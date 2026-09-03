#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae las reglas de calidad ACTIVAS directamente del sistema nativo ya en
produccion (APP_CATALOGO.dbo.Tb_Calidad_Regla / Tb_Calidad_Tabla_Atributo),
en vez de la lista de SharePoint. Esta es la UNICA fuente autoritativa de
"que reglas hay que activar" -- el pipeline de SharePoint (carga_reglas_calidad_excel.py
/ carga_reglas_sharepoint.py / sincronizar_reglas_calidad.py) se deprecio y
se movio a _archivo/.

Tb_Calidad_Regla.Es_Activa = 1 marca las reglas vigentes; se cruzan con
Tb_Calidad_Tabla_Atributo (FK Id_Calidad_Regla) para obtener, por cada
combinacion regla+atributo fisico, la CLAVE ya resuelta (Clave_Fisico) --
a diferencia del pipeline de SharePoint, aqui NO hace falta
resolver_atributo.py: la resolucion ya viene hecha por el propio sistema.

Uso como modulo:
  from origen_reglas.extraer_reglas_activas_bd import extraer_reglas_activas
  reglas = extraer_reglas_activas()
  # -> [{"id_calidad_regla":.., "id_calidad_tabla_atributo":.., "clave_fisico":..,
  #      "desc_funcional":.., "ambito":..}, ...]

Uso CLI (prueba manual):
  python -m origen_reglas.extraer_reglas_activas_bd
"""

import argparse
import json

# ----------------------------- CONFIGURACION ------------------------------- #
SERVIDOR = r"SERVIDOR_DATOS\INSTANCIA"
BASE_DATOS = "APP_CATALOGO"
DRIVER = "ODBC Driver 17 for SQL Server"
USAR_WINDOWS_AUTH = True
USUARIO = None
PASSWORD = None
# --------------------------------------------------------------------------- #


def _conectar():
    import pyodbc
    partes = [f"DRIVER={{{DRIVER}}}", f"SERVER={SERVIDOR}", f"DATABASE={BASE_DATOS}"]
    if USAR_WINDOWS_AUTH:
        partes.append("Trusted_Connection=yes")
    else:
        partes += [f"UID={USUARIO}", f"PWD={PASSWORD}"]
    return pyodbc.connect(";".join(partes) + ";", timeout=15)


def extraer_reglas_activas() -> list:
    """Reglas con Es_Activa=1, cruzadas con su(s) columna(s) fisica(s) ya
    resuelta(s). Solo lectura: NO escribe nada en APP_CATALOGO."""
    cn = _conectar()
    try:
        cur = cn.cursor()
        cur.execute("""
            SELECT REG.Id_Calidad_Regla, TAB.Id_Calidad_Tabla_Atributo,
                   TAB.Clave_Fisico, REG.Desc_Funcional, REG.Ambito
            FROM dbo.Tb_Calidad_Regla REG
            INNER JOIN dbo.Tb_Calidad_Tabla_Atributo TAB
                    ON TAB.Id_Calidad_Regla = REG.Id_Calidad_Regla
            WHERE REG.Es_Activa = 1
            ORDER BY REG.Id_Calidad_Regla, TAB.Id_Calidad_Tabla_Atributo
        """)
        cols = [c[0].lower() for c in cur.description]
        return [dict(zip(cols, fila)) for fila in cur.fetchall()]
    finally:
        cn.close()


def main():
    ap = argparse.ArgumentParser(description="Lista las reglas activas de Tb_Calidad_Regla/Tb_Calidad_Tabla_Atributo")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    reglas = extraer_reglas_activas()
    if args.json:
        print(json.dumps(reglas, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Reglas activas: {len(reglas)}")
        for r in reglas:
            print(f"  [{r['id_calidad_tabla_atributo']}] {r['clave_fisico']} — {(r['desc_funcional'] or '')[:80]}")


if __name__ == "__main__":
    main()
