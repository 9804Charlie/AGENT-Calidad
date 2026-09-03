#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Registro y revision de PROPUESTAS DE REGLAS DE CALIDAD (HITL / Comite del dato).

Mismo principio que registro_propuestas.py (fichas de metadato): el agente NUNCA
activa una regla traducida directamente. Cada regla de calidad activa en
Tb_Calidad_Regla (ver sincronizar_reglas_bd.py) se REGISTRA aqui como
PENDIENTE, con la regla estructurada que propuso el LLM
(traductor_reglas_calidad.py). El Comite del dato la aprueba o rechaza; solo
al aprobar se activa de verdad -- via el bloque T-SQL de
activacion/generador_sp_agente_ia.py (mecanismo actual), o vía
activar_regla_aprobada/reglas_calidad_<tabla>.json para las propuestas
historicas del extinto pipeline de SharePoint (deprecado, ver _archivo/).

Tabla destino: dbo.CAL_Propuestas en la base 'WORKSPACE_AGENTE' (SERVIDOR_WORKSPACE)
-- el workspace de este proyecto, distinto de 'Agente' (RAG) y de 'APP_CATALOGO'
(diccionario, que solo se LEE). La tabla se crea sola en el primer registrar()
si no existe (ver _asegurar_tabla).

Requisitos:
  pip install pyodbc

Uso por linea de comandos:
  python -m origen_reglas.registro_propuestas_reglas pendientes
  python -m origen_reglas.registro_propuestas_reglas nuevas
  python -m origen_reglas.registro_propuestas_reglas ver 3
  python -m origen_reglas.registro_propuestas_reglas revisar 3 --decision APROBADA --revisor revisor.ejemplo --comentario "OK"
  python -m origen_reglas.registro_propuestas_reglas metricas

Uso como modulo (lo que llama sincronizar_reglas_calidad.py):
  from origen_reglas.registro_propuestas_reglas import registrar, buscar_por_id_sharepoint
"""

import argparse
import json
import sys

# ----------------------------- CONFIGURACION ------------------------------- #
SERVIDOR = r"SERVIDOR_WORKSPACE"
BASE_DATOS = "WORKSPACE_AGENTE"
DRIVER = "ODBC Driver 17 for SQL Server"

USAR_WINDOWS_AUTH = True
USUARIO = None
PASSWORD = None   # rellena solo si USAR_WINDOWS_AUTH = False
# --------------------------------------------------------------------------- #

ESTADOS_VALIDOS = ("APROBADA", "RECHAZADA")

_DDL_TABLA = """
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'CAL_Propuestas' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.CAL_Propuestas (
        Id                          INT IDENTITY PRIMARY KEY,
        Atributo                    NVARCHAR(256),
        Id_Calidad_Tabla_Atributo   NVARCHAR(64),
        Desc_Funcional_Original     NVARCHAR(MAX),
        Regla_Propuesta_JSON        NVARCHAR(MAX),
        Tabla_Destino               NVARCHAR(512),
        Confianza                   FLOAT,
        Estado                      VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE',
        Fecha_Propuesta             DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        Revisor                     NVARCHAR(128),
        Fecha_Revision              DATETIME2,
        Comentario_Revisor          NVARCHAR(MAX),
        Regla_Final_JSON            NVARCHAR(MAX)
    )
END
"""


def _conn_str() -> str:
    partes = [f"DRIVER={{{DRIVER}}}", f"SERVER={SERVIDOR}", f"DATABASE={BASE_DATOS}"]
    if USAR_WINDOWS_AUTH:
        partes.append("Trusted_Connection=yes")
    else:
        partes += [f"UID={USUARIO}", f"PWD={PASSWORD}"]
    if "18" in DRIVER:
        partes += ["Encrypt=yes", "TrustServerCertificate=yes"]
    return ";".join(partes) + ";"


def _conectar():
    import pyodbc
    return pyodbc.connect(_conn_str(), timeout=10)


def _asegurar_tabla(cn):
    """Crea dbo.CAL_Propuestas si no existe. DDL idempotente (IF NOT EXISTS)."""
    cur = cn.cursor()
    cur.execute(_DDL_TABLA)
    cn.commit()


def registrar(atributo: str, id_sharepoint: str, desc_funcional: str,
              regla_propuesta: dict, tabla_destino: str = None,
              confianza: float = None, estado: str = "PENDIENTE") -> int:
    """Inserta una propuesta de regla. Devuelve el Id generado.
    estado='NUEVA' (regla_propuesta=None) es una fila ligera: solo se detecto
    en Tb_Calidad_Regla, todavia no se ha llamado al LLM -- ver
    actualizar_procesada() para el paso que la completa a PENDIENTE."""
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute(
            """
            INSERT INTO dbo.CAL_Propuestas
                (Atributo, Id_Calidad_Tabla_Atributo, Desc_Funcional_Original,
                 Regla_Propuesta_JSON, Tabla_Destino, Confianza, Estado)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, CAST(? AS NVARCHAR(MAX)), ?, ?, ?)
            """,
            atributo, id_sharepoint, desc_funcional,
            json.dumps(regla_propuesta, ensure_ascii=False) if regla_propuesta is not None else None,
            tabla_destino, confianza, estado,
        )
        nuevo_id = cur.fetchone()[0]
        cn.commit()
        return int(nuevo_id)
    finally:
        cn.close()


def buscar_por_id_sharepoint(id_sharepoint: str) -> dict:
    """Ultima propuesta registrada para esta fila de SharePoint (cualquier
    estado), para que sincronizar_reglas_calidad.py decida si re-procesar."""
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT TOP 1 Id, Desc_Funcional_Original, Estado
            FROM dbo.CAL_Propuestas
            WHERE Id_Calidad_Tabla_Atributo = ?
            ORDER BY Fecha_Propuesta DESC
        """, id_sharepoint)
        fila = cur.fetchone()
        if not fila:
            return {}
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, fila))
    finally:
        cn.close()


def pendientes() -> list:
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT Id, Atributo, Tabla_Destino, Confianza, Fecha_Propuesta
            FROM dbo.CAL_Propuestas
            WHERE Estado = 'PENDIENTE'
            ORDER BY Fecha_Propuesta
        """)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cn.close()


def eliminar_por_estado(estado: str) -> int:
    """Borra TODAS las propuestas en un estado dado -- limpieza puntual (p.ej.
    vaciar PENDIENTE para repoblar desde cero via deteccion), no una operacion
    habitual. Sin deshacer. Devuelve cuantas filas borro."""
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("DELETE FROM dbo.CAL_Propuestas WHERE Estado = ?", estado)
        n = cur.rowcount
        cn.commit()
        return n
    finally:
        cn.close()


def nuevas() -> list:
    """Propuestas detectadas por el poller/deteccion manual, todavia SIN
    traducir (Estado='NUEVA') -- lo que alimenta la pestaña 'Reglas nuevas'."""
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT Id, Atributo, Id_Calidad_Tabla_Atributo, Desc_Funcional_Original,
                   Tabla_Destino, Fecha_Propuesta
            FROM dbo.CAL_Propuestas
            WHERE Estado = 'NUEVA'
            ORDER BY Fecha_Propuesta
        """)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cn.close()


def actualizar_procesada(id_propuesta: int, regla_propuesta: dict, confianza: float = None) -> bool:
    """Pasa una propuesta de NUEVA a PENDIENTE, con la traduccion del LLM ya
    hecha (boton 'Aprobar' de la pestaña 'Reglas nuevas'). Devuelve True si
    actualizo (seguia en NUEVA)."""
    cn = _conectar()
    try:
        cur = cn.cursor()
        cur.execute("""
            UPDATE dbo.CAL_Propuestas
            SET Regla_Propuesta_JSON = CAST(? AS NVARCHAR(MAX)), Confianza = ?, Estado = 'PENDIENTE'
            WHERE Id = ? AND Estado = 'NUEVA'
        """, json.dumps(regla_propuesta, ensure_ascii=False), confianza, id_propuesta)
        cn.commit()
        return cur.rowcount > 0
    finally:
        cn.close()


def actualizar_regla_propuesta(id_propuesta: int, regla_propuesta: dict, confianza: float = None) -> bool:
    """Sobrescribe el JSON de una propuesta YA registrada, EN CUALQUIER
    estado, sin tocar Estado -- para correcciones puntuales (p.ej. clave_cols
    manual tras un 'no se encontro clave primaria') sin tener que volver a
    llamar al LLM. A diferencia de actualizar_procesada(), no exige NUEVA."""
    cn = _conectar()
    try:
        cur = cn.cursor()
        if confianza is None:
            cur.execute("""
                UPDATE dbo.CAL_Propuestas SET Regla_Propuesta_JSON = CAST(? AS NVARCHAR(MAX))
                WHERE Id = ?
            """, json.dumps(regla_propuesta, ensure_ascii=False), id_propuesta)
        else:
            cur.execute("""
                UPDATE dbo.CAL_Propuestas
                SET Regla_Propuesta_JSON = CAST(? AS NVARCHAR(MAX)), Confianza = ?
                WHERE Id = ?
            """, json.dumps(regla_propuesta, ensure_ascii=False), confianza, id_propuesta)
        cn.commit()
        return cur.rowcount > 0
    finally:
        cn.close()


def ver(id_propuesta: int) -> dict:
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT Id, Atributo, Id_Calidad_Tabla_Atributo, Desc_Funcional_Original,
                   Regla_Propuesta_JSON, Tabla_Destino, Confianza, Estado,
                   Revisor, Fecha_Revision, Comentario_Revisor, Regla_Final_JSON
            FROM dbo.CAL_Propuestas WHERE Id = ?
        """, id_propuesta)
        fila = cur.fetchone()
        if not fila:
            return {}
        cols = [c[0] for c in cur.description]
        d = dict(zip(cols, fila))
        if d.get("Regla_Propuesta_JSON"):
            d["Regla_Propuesta_JSON"] = json.loads(d["Regla_Propuesta_JSON"])
        if d.get("Regla_Final_JSON"):
            d["Regla_Final_JSON"] = json.loads(d["Regla_Final_JSON"])
        return d
    finally:
        cn.close()


def revisar(id_propuesta: int, decision: str, revisor: str,
            comentario: str = None, regla_final: dict = None) -> bool:
    """Aprueba o rechaza. Devuelve True si actualizo (estaba PENDIENTE).
    NO activa la regla (ver activar_regla_aprobada en agente_calidad_datos.py):
    esto solo registra la decision del comite."""
    decision = decision.upper()
    if decision not in ESTADOS_VALIDOS:
        raise ValueError(f"decision debe ser uno de {ESTADOS_VALIDOS}")

    cn = _conectar()
    try:
        cur = cn.cursor()
        cur.execute("""
            UPDATE dbo.CAL_Propuestas
            SET Estado = ?, Revisor = ?, Fecha_Revision = SYSDATETIME(),
                Comentario_Revisor = ?, Regla_Final_JSON = CAST(? AS NVARCHAR(MAX))
            WHERE Id = ? AND Estado = 'PENDIENTE'
        """,
            decision, revisor, comentario,
            json.dumps(regla_final, ensure_ascii=False) if regla_final else None,
            id_propuesta,
        )
        cn.commit()
        cur.execute("SELECT Estado FROM dbo.CAL_Propuestas WHERE Id = ?", id_propuesta)
        fila = cur.fetchone()
        return bool(fila and fila[0] == decision)
    finally:
        cn.close()


def metricas() -> dict:
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("SELECT Estado, COUNT(*) FROM dbo.CAL_Propuestas GROUP BY Estado")
        return {"global": {r[0]: r[1] for r in cur.fetchall()}}
    finally:
        cn.close()


# --------------------------------- CLI ------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Registro y revision de propuestas de reglas de calidad")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pendientes", help="Listar propuestas pendientes")
    sub.add_parser("nuevas", help="Listar reglas detectadas sin traducir todavia")

    p_ver = sub.add_parser("ver", help="Ver una propuesta completa")
    p_ver.add_argument("id", type=int)

    p_rev = sub.add_parser("revisar", help="Aprobar o rechazar una propuesta")
    p_rev.add_argument("id", type=int)
    p_rev.add_argument("--decision", required=True, choices=["APROBADA", "RECHAZADA"])
    p_rev.add_argument("--revisor", required=True)
    p_rev.add_argument("--comentario")

    sub.add_parser("metricas", help="Conteo por estado")

    p_lim = sub.add_parser("limpiar", help="Borrar TODAS las propuestas de un estado (sin deshacer)")
    p_lim.add_argument("--estado", required=True, choices=["NUEVA", "PENDIENTE", "APROBADA", "RECHAZADA"])

    args = ap.parse_args()

    try:
        if args.cmd == "pendientes":
            filas = pendientes()
            if not filas:
                print("No hay propuestas de reglas pendientes.")
            for r in filas:
                conf = r.get("Confianza")
                conf_txt = f"{conf:.2f}" if conf is not None else "-"
                print(f"  [#{r['Id']}] {r['Atributo']}  | tabla={r.get('Tabla_Destino') or '(sin resolver)'} "
                      f"| confianza={conf_txt}")

        elif args.cmd == "nuevas":
            filas = nuevas()
            if not filas:
                print("No hay reglas nuevas detectadas.")
            for r in filas:
                print(f"  [#{r['Id']}] {r['Atributo']}  | tabla={r.get('Tabla_Destino') or '(sin resolver)'}")

        elif args.cmd == "ver":
            d = ver(args.id)
            print("No existe esa propuesta." if not d else
                  json.dumps(d, ensure_ascii=False, indent=2, default=str))

        elif args.cmd == "revisar":
            ok = revisar(args.id, args.decision, args.revisor, args.comentario)
            print("Revision aplicada." if ok else
                  "No se aplico (¿no existe o ya estaba revisada?).")

        elif args.cmd == "limpiar":
            n = eliminar_por_estado(args.estado)
            print(f"Borradas {n} propuesta(s) en estado {args.estado}.")

        elif args.cmd == "metricas":
            m = metricas()
            for est, n in m["global"].items():
                print(f"  {est}: {n}")
    except ImportError as e:
        print("Falta pyodbc. Instala con:  pip install pyodbc")
        print("Detalle:", e); sys.exit(1)
    except Exception as e:
        print("Error:", e); sys.exit(1)


if __name__ == "__main__":
    main()
