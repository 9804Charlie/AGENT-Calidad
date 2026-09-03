#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recuperacion RAG sobre SQL Server.

Dada una consulta en texto:
  1) la embebe con el MISMO modelo con el que se cargaron los vectores
     (paraphrase-multilingual-MiniLM-L12-v2, 384 dim, normalizado),
  2) busca los top-k mas cercanos por VECTOR_DISTANCE('cosine', ...),
  3) devuelve tres grupos:
       - ejemplos : fichas reales ya documentadas (RAG_Ejemplos)
       - glosario : conceptos de negocio para la TAXONOMIA (RAG_Glosario,
                    Tipo='ConceptoNegocio')
       - negocio  : conocimiento de negocio para la DESCRIPCION (RAG_Glosario,
                    otros tipos: 'MedidaMDX' y, en el futuro, documentos)

Se separan glosario y negocio para que las medidas MDX (muchas) no desplacen a
los conceptos de taxonomia en el top-k.

Uso modulo:
  from diccionario.recupera_rag import recuperar
  ctx = recuperar("saldo dispuesto", k=5)
  # ctx -> {"ejemplos": [...], "glosario": [...], "negocio": [...]}
"""

import argparse
import json
import sys

# ----------------------------- CONFIGURACION ------------------------------- #
SERVIDOR = r"SERVIDOR_WORKSPACE"
BASE_DATOS = "Agente"
DRIVER = "ODBC Driver 18 for SQL Server"

USAR_WINDOWS_AUTH = True
USUARIO = "Agente"
PASSWORD = ""

MODELO = "paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384
K_POR_DEFECTO = 5
TIPO_CONCEPTO = "ConceptoNegocio"
# --------------------------------------------------------------------------- #

_modelo = None


def _cargar_modelo():
    global _modelo
    if _modelo is None:
        from sentence_transformers import SentenceTransformer
        _modelo = SentenceTransformer(MODELO)
    return _modelo


def _vector_texto(consulta: str) -> str:
    m = _cargar_modelo()
    vec = m.encode([consulta], normalize_embeddings=True)[0]
    if len(vec) != DIM:
        raise ValueError(f"El modelo devolvio dim={len(vec)}, se esperaba {DIM}")
    return "[" + ",".join("{:.6f}".format(float(x)) for x in vec) + "]"


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


def recuperar(consulta: str, k: int = K_POR_DEFECTO) -> dict:
    """
    Devuelve {'ejemplos': [...], 'glosario': [...], 'negocio': [...]} con los
    top-k de cada grupo, cada item con su 'distancia' (menor = mas cercano).
      - glosario: conceptos de negocio (taxonomia)  -> Tipo='ConceptoNegocio' (o NULL)
      - negocio : conocimiento de negocio (descripcion) -> resto de tipos (MedidaMDX, docs)
    """
    import pyodbc

    vec = _vector_texto(consulta)
    cast = f"CAST(N'{vec}' AS VECTOR(384))"

    sql_ej = f"""
        SELECT TOP ({int(k)})
               Id, CLAVE, Area, Entidad, Texto, Ficha_JSON,
               VECTOR_DISTANCE('cosine', Embedding, {cast}) AS distancia
        FROM dbo.RAG_Ejemplos
        ORDER BY distancia ASC
    """
    sql_gl = f"""
        SELECT TOP ({int(k)})
               Id, Tipo, Area, Entidad, Referencia, Texto,
               VECTOR_DISTANCE('cosine', Embedding, {cast}) AS distancia
        FROM dbo.RAG_Glosario
        WHERE Tipo = '{TIPO_CONCEPTO}' OR Tipo IS NULL
        ORDER BY distancia ASC
    """
    sql_neg = f"""
        SELECT TOP ({int(k)})
               Id, Tipo, Area, Entidad, Referencia, Texto,
               VECTOR_DISTANCE('cosine', Embedding, {cast}) AS distancia
        FROM dbo.RAG_Glosario
        WHERE Tipo IS NOT NULL AND Tipo <> '{TIPO_CONCEPTO}'
        ORDER BY distancia ASC
    """

    cn = pyodbc.connect(_conn_str(), timeout=10)
    try:
        cur = cn.cursor()
        cur.execute(sql_ej)
        cols = [c[0] for c in cur.description]
        ejemplos = [dict(zip(cols, row)) for row in cur.fetchall()]

        cur.execute(sql_gl)
        cols = [c[0] for c in cur.description]
        glosario = [dict(zip(cols, row)) for row in cur.fetchall()]

        cur.execute(sql_neg)
        cols = [c[0] for c in cur.description]
        negocio = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cn.close()

    for grupo in (ejemplos, glosario, negocio):
        for r in grupo:
            r["distancia"] = float(r["distancia"])
    return {"ejemplos": ejemplos, "glosario": glosario, "negocio": negocio}


def _imprimir(consulta: str, res: dict):
    print(f"\nConsulta: {consulta!r}\n")
    print("== Ejemplos (fichas mas cercanas) ==")
    for r in res["ejemplos"]:
        print(f"  [{r['distancia']:.4f}] Id={r['Id']}  {r['Area']} / {r['Entidad']}")
        if r.get("Texto"):
            print(f"           {r['Texto'][:120]}")
    print("\n== Glosario / conceptos (taxonomia) ==")
    for r in res["glosario"]:
        print(f"  [{r['distancia']:.4f}] {r.get('Area')} / {r.get('Entidad')}  {r.get('Referencia')}")
        if r.get("Texto"):
            print(f"           {r['Texto'][:120]}")
    print("\n== Negocio (MDX / docs, para la descripcion) ==")
    for r in res["negocio"]:
        print(f"  [{r['distancia']:.4f}] {r.get('Tipo')}  {r.get('Referencia')}")
        if r.get("Texto"):
            print(f"           {r['Texto'][:120]}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Recuperacion RAG sobre SQL Server")
    ap.add_argument("consulta", nargs="?", help="Texto de la consulta")
    ap.add_argument("--k", type=int, default=K_POR_DEFECTO)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    consulta = args.consulta or input("Consulta: ").strip()
    if not consulta:
        print("Consulta vacia."); sys.exit(1)

    try:
        res = recuperar(consulta, k=args.k)
    except ImportError as e:
        print("Falta una dependencia:  pip install pyodbc sentence-transformers")
        print("Detalle:", e); sys.exit(1)
    except Exception as e:
        print("Error en la recuperacion:", e); sys.exit(1)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _imprimir(consulta, res)


if __name__ == "__main__":
    main()
