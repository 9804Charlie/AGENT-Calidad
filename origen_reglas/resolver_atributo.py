#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Resuelve un atributo de la lista de SharePoint a la(s) COLUMNA(S) FISICA(S)
reales (CLAVE = Servidor.BD.Esquema.Tabla.Columna), para que
traductor_reglas_calidad.py sepa sobre que tabla escribir la condicion SQL.

DESCUBRIMIENTO CLAVE (sesion de exploracion con acceso real a APP_CATALOGO
en SERVIDOR_DATOS\\INSTANCIA): ya existe un sistema de calidad de datos en
produccion con la resolucion YA HECHA por humanos:

    dbo.Tb_Calidad_Tabla_Atributo.Id_Calidad_Tabla_Atributo  (== el mismo ID
        que trae la columna 'Id_Calidad_Tabla_Atributo' de la lista de
        SharePoint -- confirmado 1:1 contra datos reales)
    dbo.Tb_Calidad_Tabla_Atributo.Clave_Fisico                (la CLAVE exacta,
        FK a DIC_Columnas.CLAVE)

Por eso la resolucion PRIMARIA (paso 0) es una consulta directa por ese ID:
exacta y fiable al 100% cuando la regla ya esta formalizada ahi (206 filas hoy).
Solo se usa la resolucion difusa (vocabulario conceptual + fallback semantico,
pasos 1-4) cuando el Id_Calidad_Tabla_Atributo no aparece en esa tabla -- una
regla nueva que SharePoint conoce pero que APP_CATALOGO aun no formalizo.

IMPORTANTE (decision explicita del usuario): este modulo SOLO LEE de
APP_CATALOGO. No escribe nada ahi -- es una tabla de produccion mantenida
por otro equipo (ejecutan Sp_Calidad_RevisorA y similares sobre ella). Las
propuestas/activaciones de este proyecto viven aparte, en WORKSPACE_AGENTE.

Estrategia completa, de mas a menos fiable (nunca elige a ciegas: si nada
supera el umbral, devuelve lista vacia y el revisor humano decide en la cola):
  0. Id_Calidad_Tabla_Atributo -> Tb_Calidad_Tabla_Atributo.Clave_Fisico (exacta).
  1. Coincidencia EXACTA de 'Atributo' con el vocabulario conceptual controlado
     APP_CATALOGO.dbo.CONCEP_Nombre / DIC_Columnas.NombreConceptual.
  2. Muchos Atributo vienen como "Nombre largo (SIGLA)" -- se extrae la SIGLA
     entre parentesis y se busca como NombreConceptual exacto, o como sufijo
     de columna fisica en DIC_Columnas.CLAVE (p. ej. '...Tb_Fact....CRN').
  3. Coincidencia LIKE mas laxa sobre NombreConceptual.
  4. Fallback SEMANTICO: recupera_rag.recuperar(atributo, k=5) (embeddings ya
     cargados sobre RAG_Glosario/RAG_Ejemplos, base 'Agente' en SERVIDOR_WORKSPACE).

Si APP_CATALOGO no responde, los pasos 0-3 simplemente no aportan
candidatas -- NUNCA lanza excepcion hacia arriba (sincronizar_reglas_calidad.py
debe poder seguir con lo que si haya, aunque sea solo el fallback semantico).

Uso como modulo:
  from origen_reglas.resolver_atributo import resolver
  candidatas = resolver("Capital Riesgo neto (CRN)", id_calidad_tabla_atributo="2105")
  # -> [{"clave": "...", "tabla": "...", "confianza": 1.0, "origen": "tabla_calidad_existente"}, ...]

Uso CLI (prueba manual):
  python -m origen_reglas.resolver_atributo "Capital Riesgo neto (CRN)" --id 2105
"""

import argparse
import json
import re

# ----------------------------- CONFIGURACION ------------------------------- #
# Config propia (no se reutiliza herramientas_esquema.py: su SERVIDOR hardcodeado
# resulto ser incorrecto -- el real, verificado con acceso de verdad, es este).
SERVIDOR = r"SERVIDOR_DATOS\INSTANCIA"
BASE_DATOS = "APP_CATALOGO"
DRIVER = "ODBC Driver 17 for SQL Server"
USAR_WINDOWS_AUTH = True
USUARIO = None
PASSWORD = None
# --------------------------------------------------------------------------- #

_RE_ACRONIMO = re.compile(r"\(([^)]+)\)\s*$")


def _conectar():
    import pyodbc
    partes = [f"DRIVER={{{DRIVER}}}", f"SERVER={SERVIDOR}", f"DATABASE={BASE_DATOS}"]
    if USAR_WINDOWS_AUTH:
        partes.append("Trusted_Connection=yes")
    else:
        partes += [f"UID={USUARIO}", f"PWD={PASSWORD}"]
    return pyodbc.connect(";".join(partes) + ";", timeout=15)


def _sigla_de(atributo: str) -> str:
    m = _RE_ACRONIMO.search(atributo or "")
    return m.group(1).strip() if m else ""


def _nombre_sin_sigla(atributo: str) -> str:
    return _RE_ACRONIMO.sub("", atributo or "").strip()


def _tabla_de_clave(clave: str) -> str:
    """CLAVE = Servidor.BD.Esquema.Tabla.Columna -> 'Servidor.BD.Esquema.Tabla'."""
    partes = clave.split(".")
    return ".".join(partes[:-1]) if len(partes) >= 2 else clave


def _candidata_tabla_calidad_existente(cur, id_calidad_tabla_atributo) -> list:
    """Paso 0: resolucion YA HECHA en el sistema de calidad existente. Exacta."""
    if not id_calidad_tabla_atributo:
        return []
    try:
        cur.execute("""
            SELECT Clave_Fisico FROM dbo.Tb_Calidad_Tabla_Atributo
            WHERE Id_Calidad_Tabla_Atributo = ?
        """, id_calidad_tabla_atributo)
        filas = cur.fetchall()
    except Exception:
        return []
    return [{"clave": r[0], "tabla": _tabla_de_clave(r[0]), "confianza": 1.0,
             "origen": "tabla_calidad_existente"} for r in filas if r[0]]


def _candidatas_vocabulario_conceptual(cur, atributo: str) -> list:
    sigla = _sigla_de(atributo)
    nombre_sin_sigla = _nombre_sin_sigla(atributo)
    out, vistos = [], set()

    # 1) coincidencia EXACTA con el vocabulario conceptual
    cur.execute("SELECT DISTINCT CLAVE FROM dbo.DIC_Columnas WHERE NombreConceptual = ?", atributo)
    for (clave,) in cur.fetchall():
        if clave not in vistos:
            vistos.add(clave)
            out.append({"clave": clave, "tabla": _tabla_de_clave(clave),
                        "confianza": 0.95, "origen": "exacto_concepto"})

    # 2) sigla entre parentesis: como NombreConceptual exacto, o como sufijo
    #    de columna fisica (CLAVE termina en '.SIGLA')
    if sigla:
        cur.execute("SELECT DISTINCT CLAVE FROM dbo.DIC_Columnas WHERE NombreConceptual = ?", sigla)
        for (clave,) in cur.fetchall():
            if clave not in vistos:
                vistos.add(clave)
                out.append({"clave": clave, "tabla": _tabla_de_clave(clave),
                            "confianza": 0.85, "origen": "acronimo_concepto"})
        cur.execute("SELECT DISTINCT CLAVE FROM dbo.DIC_Columnas WHERE CLAVE LIKE ?", f"%.{sigla}")
        for (clave,) in cur.fetchall():
            if clave not in vistos:
                vistos.add(clave)
                out.append({"clave": clave, "tabla": _tabla_de_clave(clave),
                            "confianza": 0.7, "origen": "acronimo_columna_fisica"})

    # 3) LIKE mas laxo sobre el nombre sin la sigla
    if nombre_sin_sigla and len(nombre_sin_sigla) >= 4:
        cur.execute("SELECT DISTINCT CLAVE FROM dbo.DIC_Columnas WHERE NombreConceptual LIKE ?",
                    f"%{nombre_sin_sigla}%")
        for (clave,) in cur.fetchall():
            if clave not in vistos:
                vistos.add(clave)
                out.append({"clave": clave, "tabla": _tabla_de_clave(clave),
                            "confianza": 0.5, "origen": "like_concepto"})
    return out


def _candidatas_semanticas(atributo: str, k: int = 5) -> list:
    try:
        from diccionario import recupera_rag
    except ImportError:
        return []
    try:
        ctx = recupera_rag.recuperar(atributo, k=k)
    except Exception:
        return []
    out = []
    for e in ctx.get("ejemplos", []):
        try:
            ficha = json.loads(e["Ficha_JSON"]) if e.get("Ficha_JSON") else {}
        except Exception:
            ficha = {}
        clave = ficha.get("CLAVE") or e.get("CLAVE")
        dist = e.get("distancia", 1.0)
        if clave and dist <= 0.40:
            out.append({"clave": clave, "tabla": _tabla_de_clave(clave),
                        "confianza": round(max(0.0, 1 - dist), 2), "origen": "semantico"})
    return out


def resolver(atributo: str, id_calidad_tabla_atributo=None, umbral_confianza: float = 0.4) -> list:
    """Candidatas ordenadas por confianza desc, dedupe por CLAVE, filtradas por
    umbral. Si el Id_Calidad_Tabla_Atributo ya esta formalizado en
    Tb_Calidad_Tabla_Atributo, se devuelve DIRECTAMENTE esa resolucion (no hace
    falta gastar el resto de estrategias). Lista vacia si nada supera el
    umbral: el revisor humano decide."""
    exacta = []
    dificionario_ok = True
    try:
        cn = _conectar()
    except Exception:
        dificionario_ok = False
        cn = None

    if cn is not None:
        try:
            cur = cn.cursor()
            exacta = _candidata_tabla_calidad_existente(cur, id_calidad_tabla_atributo)
            if exacta:
                return exacta  # resolucion ya conocida y fiable: no hace falta nada mas
            candidatas_dic = _candidatas_vocabulario_conceptual(cur, atributo)
        except Exception:
            candidatas_dic = []
        finally:
            cn.close()
    else:
        candidatas_dic = []

    candidatas = candidatas_dic + _candidatas_semanticas(atributo)
    mejor = {}
    for c in candidatas:
        k = c["clave"]
        if k not in mejor or c["confianza"] > mejor[k]["confianza"]:
            mejor[k] = c
    out = sorted(mejor.values(), key=lambda c: -c["confianza"])
    return [c for c in out if c["confianza"] >= umbral_confianza]


def main():
    ap = argparse.ArgumentParser(description="Resuelve un Atributo de SharePoint a columna(s) fisica(s)")
    ap.add_argument("atributo")
    ap.add_argument("--id", dest="id_calidad_tabla_atributo", help="Id_Calidad_Tabla_Atributo de SharePoint, si se conoce")
    ap.add_argument("--umbral", type=float, default=0.4)
    args = ap.parse_args()
    candidatas = resolver(args.atributo, id_calidad_tabla_atributo=args.id_calidad_tabla_atributo,
                           umbral_confianza=args.umbral)
    if not candidatas:
        print("Sin candidatas por encima del umbral. Requiere resolucion manual.")
    else:
        for c in candidatas:
            print(f"  [{c['confianza']:.2f}] ({c['origen']}) {c['clave']}")


if __name__ == "__main__":
    main()
