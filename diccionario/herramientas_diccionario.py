#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Herramienta del agente: CAPA SEMANTICA DEL DICCIONARIO.

  1) linaje_completo(clave) / linaje_texto(clave)
     Linaje MULTINIVEL de CACHE_LinajeCompleto (DW -> RAW -> AS400 ...). Ademas,
     cuando una HOJA es una columna RAW cuya formula referencia campos AS400 y la
     tabla codifica un fichero AS400 (p. ej. Tb_Oper_E31TYT -> fichero E31TYT),
     COMPLETA el ultimo eslabon consultando el catalogo AS400 del diccionario
     (AS400_Columnas), que muchas veces la cache deja fuera.

  2) taxonomia_conceptual(...) / areas_entidades()
     Vocabulario conceptual de negocio (CONCEP_Nombre): lista controlada para que
     el agente elija Area/Entidad/Nombre.

  3) ficha_documentada(clave)
     La ficha de DIC_Columnas si la columna ya esta documentada.

Reutiliza la conexion de herramientas_esquema.py (solo lectura). Debe estar junto
a ese modulo.

Uso (prueba):
  python -m diccionario.herramientas_diccionario linaje "SRV1.DW_PRINCIPAL.dbo.Tb_Fact_Expediente.Id_Operacion"
  python -m diccionario.herramientas_diccionario taxonomia --area "Operaciones Clientes"
  python -m diccionario.herramientas_diccionario ficha "SRV1.DW_PRINCIPAL.dbo.Tb_Fact_Operacion_Historia.CRN"
  python -m diccionario.herramientas_diccionario areas
"""

import argparse
import json
import re
import sys

from diccionario import herramientas_esquema as he

DIC = "APP_CATALOGO.dbo"

# Conexion a la base 'Agente' (donde vive DOC_Campos, catalogos Excel AS400).
AG_SERVIDOR = r"SERVIDOR_WORKSPACE"
AG_BASE = "Agente"
AG_DRIVER = "ODBC Driver 17 for SQL Server"
AG_WINDOWS_AUTH = True
AG_USUARIO = "Agente"
AG_PASSWORD = ""


def _conectar_agente():
    import pyodbc
    p = [f"DRIVER={{{AG_DRIVER}}}", f"SERVER={AG_SERVIDOR}", f"DATABASE={AG_BASE}"]
    if AG_WINDOWS_AUTH:
        p.append("Trusted_Connection=yes")
    else:
        p += [f"UID={AG_USUARIO}", f"PWD={AG_PASSWORD}"]
    return pyodbc.connect(";".join(p) + ";", timeout=10)


def descripcion_documento(fichero: str, campo: str) -> dict:
    """Descripcion de negocio de un campo AS400 desde los catalogos Excel
    (DOC_Campos, clave exacta fichero+campo). Devuelve {descripcion, comentarios}
    o None si no existe."""
    if not fichero or not campo:
        return None
    try:
        cn = _conectar_agente()
    except Exception:
        return None
    try:
        cur = cn.cursor()
        cur.execute(
            "SELECT Descripcion, Comentarios FROM dbo.DOC_Campos "
            "WHERE Fichero = ? AND Campo = ?", fichero.upper(), campo.upper())
        row = cur.fetchone()
        if not row:
            return None
        return {"descripcion": row[0] or "", "comentarios": row[1] or ""}
    except Exception:
        return None
    finally:
        cn.close()


def _pares_as400(clave: str) -> list:
    """(fichero, campo) candidatos para enlazar con DOC_Campos:
      - SIEMPRE (tabla, columna) de la clave: cubre AS400 (SD.LIB.FICHERO.CAMPO) y
        los aterrizajes SQL del fichero (p. ej. ...dbo.PNINI.NUCONT -> PNINI.NUCONT).
        Si la tabla no es un fichero conocido, el lookup no devuelve nada (inocuo).
      - los origenes AS400 del linaje de un nivel.
    """
    pares, vistos = [], set()

    def add(fic, cam):
        k = (fic.upper(), cam.upper())
        if fic and cam and k not in vistos:
            vistos.add(k); pares.append((fic, cam))

    p = clave.split(".")
    if len(p) >= 2:
        add(p[-2], p[-1])
    # origenes AS400 del linaje inmediato
    try:
        L = lineaje_canonico(clave)
        if L:
            for e in L.get("estructurado", []):
                if e.get("plataforma") == "AS400_Columna":
                    rp = (e.get("ruta") or "").split(".")
                    if len(rp) >= 2:
                        add(rp[-2], rp[-1])
    except Exception:
        pass
    return pares


# --------------------------------------------------------------------------- #
#  Completar el eslabon AS400 a partir de una hoja RAW
# --------------------------------------------------------------------------- #
def _as400_desde_hoja(leaf: str, cur) -> list:
    """
    Dada una hoja del linaje tipo:
      "[SQL_Columna] SRV1.DW_CRUDO.dbo.Tb_Oper_E31TYT. P5PVA||DIGITS(JPN1) (OPERACION)"
    intenta localizar el fichero AS400 (sufijo de la tabla: E31TYT) y los campos
    AS400 referenciados en la formula (P5PVA, JPN1) en el catalogo AS400_Columnas.
    Devuelve [{biblioteca, fichero, col, descripcion}, ...] (vacio si no aplica).
    """
    if "DW_CRUDO" not in leaf:
        return []
    m = re.search(r'(Tb_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*)', leaf)
    if not m:
        return []
    tabla = m.group(1)
    fichero = tabla.split("_")[-1]           # E31TYT
    if not fichero or len(fichero) < 3:
        return []
    resto = leaf.split(tabla, 1)[1]          # parte tras la tabla (formula/columna)
    tokens = set(re.findall(r'[A-Z][A-Z0-9]{2,}', resto))   # P5PVA, DIGITS, JPN1, OPERACION...
    if not tokens:
        return []
    placeholders = ",".join("?" * len(tokens))
    cur.execute(f"""
        SELECT DISTINCT BIBLIOTECA, FICHERO, COL_NAME,
               CAST(COL_DESCRIPTION AS NVARCHAR(255)) AS d
        FROM {DIC}.AS400_Columnas
        WHERE FICHERO = ? AND COL_NAME IN ({placeholders})
    """, fichero, *tokens)
    return [{"biblioteca": r[0], "fichero": r[1], "col": r[2], "descripcion": r[3]}
            for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
#  1) LINAJE COMPLETO
# --------------------------------------------------------------------------- #
def linaje_completo(clave: str) -> dict:
    cn = he._conectar()
    try:
        cur = cn.cursor()
        cur.execute(f"""
            SELECT h.Descripcion AS nodo, p.Descripcion AS procede_de, h.EsHoja, h.Raiz
            FROM {DIC}.CACHE_LinajeCompleto h
            LEFT JOIN {DIC}.CACHE_LinajeCompleto p
                   ON p.Hijo = h.Padre AND p.CLAVE = h.CLAVE
            WHERE h.CLAVE = ?
        """, clave)
        filas = cur.fetchall()

        if not filas:
            return {"clave": clave, "encontrado": False, "origenes_directos": [],
                    "hojas": [], "aristas": [], "origenes_as400": []}

        raiz_desc = next((n for n, p, eh, r in filas if r), None)
        origenes_directos, hojas, aristas = [], [], []
        for nodo, procede, es_hoja, raiz in filas:
            if raiz:
                continue
            if procede is not None:
                aristas.append({"de": procede, "a": nodo})
                if procede == raiz_desc:
                    origenes_directos.append(nodo)
            if es_hoja:
                hojas.append(nodo)

        # completar eslabon AS400 desde las hojas RAW
        as400 = []
        vistos = set()
        for h in hojas:
            for a in _as400_desde_hoja(h, cur):
                etiqueta = f"[AS400_Columna] {a['biblioteca']}.{a['fichero']}.{a['col']}"
                if etiqueta not in vistos:
                    vistos.add(etiqueta)
                    a["etiqueta"] = etiqueta
                    as400.append(a)
    finally:
        cn.close()

    return {"clave": clave, "encontrado": True,
            "origenes_directos": sorted(set(origenes_directos)),
            "hojas": sorted(set(hojas)),
            "aristas": aristas,
            "origenes_as400": as400}


def _token_tabla(tabla: str) -> str:
    """Parte distintiva del nombre de tabla (sin prefijos Tb_Fact_/Tb_Dim_/...)."""
    for p in ("Tb_Fact_", "Tb_Dim_", "Tb_Stg_", "Tb_Aux_", "Tb_"):
        if tabla.startswith(p):
            return tabla[len(p):]
    return tabla


def _columna_escrita(definicion: str, tabla: str, columna: str) -> bool:
    """True si la columna se ESCRIBE en la tabla en este SP (lista de INSERT o SET de UPDATE)."""
    low = (definicion or "").lower()
    tl, cl = tabla.lower(), columna.lower()
    tpat = r'(?:\[?\w+\]?\s*\.\s*)*\[?' + re.escape(tl) + r'\b\]?'
    # INSERT INTO <tabla> ( ... [columna] ... )
    for m in re.finditer(r'insert\s+into\s+' + tpat + r'\s*\(', low):
        i = low.find('(', m.end() - 1)
        depth, j = 0, i
        while j < len(low):
            if low[j] == '(':
                depth += 1
            elif low[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if re.search(r'[\[\s,(]' + re.escape(cl) + r'[\]\s,)]', low[i:j + 1]):
            return True
    # UPDATE <tabla> ... SET ... columna =
    for m in re.finditer(r'update\s+' + tpat, low):
        seg = low[m.end(): m.end() + 8000]
        if re.search(r'[,\s\[]' + re.escape(cl) + r'\s*\]?\s*=', seg):
            return True
    return False


def cargador_de_columna(bd: str, tabla: str, columna: str):
    """
    Procedimiento que CARGA/CALCULA la columna. Dos pasos:
      1) candidatos baratos: SP que mencionan la columna Y la tabla (texto plano,
         sin corchetes -> sin el problema de comodines de LIKE), con insert/update.
         Se ordenan por prioridad (Sp_Fact_Carga, nombre que contiene la tabla, tamano).
      2) sobre los mejores, deteccion PRECISA en Python: el que realmente ESCRIBE la
         columna (lista de INSERT o lado izquierdo de un SET de UPDATE de ESA tabla).
    Devuelve (objeto, bd, esquema, definicion) o None.
    """
    token = _token_tabla(tabla)
    cn = he._conectar()
    try:
        cur = cn.cursor()
        # paso 1: ranking sin traer DEFINITION (barato aunque haya muchos)
        cur.execute(f"""
            SELECT OBJ_NAME, BD, OBJ_SCHEMA, LEN(DEFINITION) AS largo
            FROM {DIC}.SQL_Objetos
            WHERE TYPE_DESC='SQL_STORED_PROCEDURE'
              AND DEFINITION LIKE ? AND DEFINITION LIKE ?
              AND (DEFINITION LIKE '%insert%' OR DEFINITION LIKE '%update%')
        """, f"%{columna}%", f"%{tabla}%")
        meta = cur.fetchall()
        if not meta:
            return None

        def prio(nombre):
            n = nombre.lower()
            p = 0 if n.startswith("sp_fact_carga") else (1 if "carga" in n else 2)
            return (p, 0 if token.lower() in n else 1)

        meta.sort(key=lambda r: (prio(r[0]), r[3]))
        mejores = meta[:12]

        # paso 2: traer DEFINITION solo de los mejores y detectar escritura precisa
        primero = None
        for obj, obd, oesq, _ in mejores:
            cur.execute(f"""SELECT CAST(DEFINITION AS NVARCHAR(MAX))
                            FROM {DIC}.SQL_Objetos WHERE OBJ_NAME = ?""", obj)
            row = cur.fetchone()
            deff = row[0] if row else ""
            if primero is None:
                primero = (obj, obd, oesq, deff)
            if _columna_escrita(deff, tabla, columna):
                return (obj, obd, oesq, deff)
        # ninguno escribe la columna exacta: el mejor candidato (mejor que nada)
        return primero
    finally:
        cn.close()


def _fragmento(definicion: str, columna: str, ventana: int = 280) -> str:
    if not definicion:
        return ""
    i = definicion.lower().find(columna.lower())
    if i < 0:
        return ""
    return definicion[max(0, i - 70):i + ventana].strip()


def _fuentes_alias(definicion: str) -> list:
    """Pares (tabla, alias) del FROM/JOIN, para que el modelo resuelva los alias."""
    if not definicion:
        return []
    pat = re.compile(r'\b(?:from|join)\s+(\[?[\w\.]+\]?)(?:\s+as)?\s+(\[?\w+\]?)', re.IGNORECASE)
    fuera = {"on", "where", "inner", "left", "right", "outer", "group", "order",
             "select", "join", "cross", "full"}
    out, vistos = [], set()
    for tbl, ali in pat.findall(definicion):
        t = tbl.replace('[', '').replace(']', '').strip()
        a = ali.replace('[', '').replace(']', '').strip()
        if not t or t.startswith('(') or a.lower() in fuera or a.lower() in vistos:
            continue
        vistos.add(a.lower()); out.append((t, a))
    return out[:20]


def _expr_productora(definicion: str, columna: str) -> str:
    """Expresion del codigo que PRODUCE la columna (ancla 'AS <col>' de un SELECT,
    o lado derecho de 'col =' en un UPDATE). Es el salto de UN nivel."""
    if not definicion:
        return ""
    low = definicion.lower(); cl = columna.lower()
    # INSERT ... SELECT: '... AS <col>'
    m = re.search(r'\bas\s+\[?' + re.escape(cl) + r'\]?(?=[\s,\r\n]|$)', low)
    if m:
        end, depth, i = m.start(), 0, m.start() - 1
        while i >= 0:
            c = low[i]
            if c == ')':
                depth += 1
            elif c == '(':
                if depth == 0:
                    break
                depth -= 1
            elif c == ',' and depth == 0:
                break
            i -= 1
        return definicion[i + 1:end].strip()
    # UPDATE ... SET ... col = <expr>
    m = re.search(r'[,\s\[]' + re.escape(cl) + r'\s*\]?\s*=', low)
    if m:
        start, depth, j = m.end(), 0, m.end()
        while j < len(low):
            c = low[j]
            if c == '(':
                depth += 1
            elif c == ')':
                if depth == 0:
                    break
                depth -= 1
            elif c == ',' and depth == 0:
                break
            j += 1
        return definicion[start:j].strip()
    return ""


def linaje_desde_codigo(clave: str) -> dict:
    """Padre inmediato cuando la cache no cubre la columna: el SP que la calcula,
    la expresion que la produce (un nivel) y los alias del FROM para resolverlos."""
    p = clave.split(".")
    if len(p) < 5:
        return {"encontrado": False}
    bd, tabla, columna = p[1], p[3], p[4]
    c = cargador_de_columna(bd, tabla, columna)
    if not c:
        return {"encontrado": False}
    obj, obd, oesq, deff = c
    return {"encontrado": True, "objeto": obj, "bd": obd or bd, "esquema": oesq or "dbo",
            "expresion": _expr_productora(deff, columna),
            "fragmento": _fragmento(deff, columna),
            "alias": _fuentes_alias(deff)}


def linaje_texto(clave: str) -> str:
    L = linaje_completo(clave)
    if not (L["encontrado"] and L["origenes_directos"]):
        # sin cache: padre inmediato derivado del codigo del cargador
        C = linaje_desde_codigo(clave)
        if not C["encontrado"]:
            return ("=== LINAJE (un nivel) ===\n(no hay padre en la cache ni se "
                    "encontro el procedimiento que calcula esta columna)")
        pp = clave.split(".")
        tabla_doc = f"{pp[0]}.{pp[1]}.{pp[2]}.{pp[3]}" if len(pp) >= 5 else clave
        out = ["=== LINAJE (UN NIVEL: identifica el PADRE INMEDIATO) ===",
               f"La columna se genera en el procedimiento: {C['objeto']} (BD {C['bd']}).",
               "",
               "REGLA: ObjetoOrigen debe contener SOLO la(s) COLUMNA(S) ORIGEN INMEDIATAS",
               "(un unico nivel), nunca el procedimiento ni tablas sin columna. Formato:",
               "[SQL_Columna] servidor.bd.esquema.tabla.columna  (o [AS400_Columna] ...).",
               "- Resuelve los alias con la lista del FROM de abajo.",
               f"- Una columna SIN alias en la expresion es de la PROPIA tabla: {tabla_doc}.",
               "- Si el valor es una constante, indica en la descripcion que no tiene origen."]
        if C["expresion"]:
            out += ["", "Expresion que PRODUCE la columna (de aqui salen sus padres):",
                    f"  {C['expresion']}"]
        if C["alias"]:
            out += ["", "Tablas y alias del FROM (para resolver los alias de arriba):"]
            out += [f"  - alias '{a}' = {t}" for (t, a) in C["alias"]]
        return "\n".join(out)
    # con cache: el PADRE INMEDIATO es el origen directo (un nivel). El arbol
    # completo se reconstruye porque cada padre es a su vez una ficha con su propio
    # ObjetoOrigen (modelo recursivo del diccionario).
    out = ["=== LINAJE (UN NIVEL: padre inmediato, ya en el diccionario) ===",
           "ObjetoOrigen debe ser EXACTAMENTE el/los origen(es) directo(s) de abajo",
           "(un solo nivel). NO encadenes niveles ni anadas las hojas AS400: el arbol",
           "completo se arma solo porque cada padre tiene su propia ficha.",
           "", "Origen(es) DIRECTO(s) (padre inmediato):"]
    out += [f"  - {o}" for o in L["origenes_directos"]] or ["  (ninguno)"]
    return "\n".join(out)


def _parse_nodo(desc: str):
    m = re.match(r'^\[([^\]]+)\]\s*(.+)$', (desc or "").strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, (desc or "").strip()


def _capa(plataforma: str, ruta: str) -> str:
    r = (ruta or "")
    if plataforma == "AS400_Columna" or r.startswith("SD."):
        return "AS400"
    up = r.upper()
    if "DW_CRUDO" in up: return "RAW"
    if "_STG" in up or ".STG" in up or "TB_STG" in up: return "STG"
    if "DW_PRINCIPAL" in up: return "DW"
    if "REPORTING" in up: return "REPORTING"
    return "OTRA"


_cols_cache = {}


def _columnas_de_tabla(prefijo_tabla: str) -> dict:
    """{nombre_columna_en_minuscula: nombre_real} de la tabla, desde SQL_Columnas."""
    if prefijo_tabla in _cols_cache:
        return _cols_cache[prefijo_tabla]
    cn = he._conectar()
    try:
        cur = cn.cursor()
        cur.execute(f"SELECT CLAVE FROM {DIC}.SQL_Columnas WHERE CLAVE LIKE ?",
                    prefijo_tabla + ".%")
        d = {}
        for (cl,) in cur.fetchall():
            col = cl.split(".")[-1]
            d[col.lower()] = col
        _cols_cache[prefijo_tabla] = d
        return d
    finally:
        cn.close()


def padres_propia_tabla(clave: str, expresion: str):
    """
    Padres deterministas cuando la expresion usa SOLO columnas de la propia tabla.
    Devuelve (columnas_reales, hay_alias_externo). Si hay refs con alias (alias.col),
    hay_alias=True y NO se debe fijar por codigo (es mixto: lo resuelve el modelo).
    """
    pp = clave.split(".")
    if len(pp) < 5 or not expresion:
        return [], False
    prefijo = ".".join(pp[:4]); col_obj = pp[4].lower()
    hay_alias = bool(re.search(r'[A-Za-z_]\w*\s*\.\s*[A-Za-z_\[]', expresion))
    cols = _columnas_de_tabla(prefijo)
    encontrados, vistos = [], set()
    for m in re.finditer(r'\b[A-Za-z_]\w*\b', expresion):
        tok = m.group(0).lower()
        if tok in cols and tok != col_obj and tok not in vistos:
            vistos.add(tok); encontrados.append(cols[tok])
    return encontrados, hay_alias


def lineaje_canonico(clave: str) -> dict:
    """
    Padre INMEDIATO (un solo nivel). Determinista cuando se puede:
      - con cache: los origenes directos (lo que guarda el diccionario).
      - sin cache pero con expresion que usa SOLO columnas de la propia tabla:
        esas columnas (cruzadas con el catalogo). Asi no se pierde ningun sumando.
    Si la expresion mezcla alias externos o subconsultas, devolvemos None y lo
    resuelve el modelo (no se puede fijar con fiabilidad por codigo).
    Devuelve {'objeto_origen': str, 'estructurado': [...]} o None.
    """
    L = linaje_completo(clave)
    if not (L["encontrado"] and L["origenes_directos"]):
        # sin cache: intentar padres deterministas de la PROPIA tabla
        C = linaje_desde_codigo(clave)
        if C.get("encontrado") and C.get("expresion"):
            cols, hay_alias = padres_propia_tabla(clave, C["expresion"])
            if cols and not hay_alias:
                prefijo = ".".join(clave.split(".")[:4])
                nodos = [f"[SQL_Columna] {prefijo}.{c}" for c in cols]
                estr = [{"plataforma": "SQL_Columna", "ruta": f"{prefijo}.{c}",
                         "capa": _capa("SQL_Columna", prefijo), "tipo": "determinista"}
                        for c in cols]
                return {"objeto_origen": ", ".join(nodos), "estructurado": estr}
        return None
    nodos = list(L["origenes_directos"])   # SOLO el padre inmediato (un nivel)
    estr = []
    for d in nodos:
        plat, ruta = _parse_nodo(d)
        estr.append({"plataforma": plat, "ruta": ruta,
                     "capa": _capa(plat, ruta), "tipo": "determinista"})
    return {"objeto_origen": ", ".join(nodos), "estructurado": estr}


def descripcion_ssas(clave: str) -> list:
    """
    Descripciones de NEGOCIO del modelo OLAP (SSAS) que apuntan a esta columna.
    Cruza por Tabla.Columna contra KeyColumns/NameColumn (atributos) y ValueColumn
    (medidas), en formato 'esquema_Tabla.Columna'. Devuelve [{origen, name, description}].
    """
    p = clave.split(".")
    if len(p) < 5:
        return []
    tabla, columna = p[3], p[4]
    patron = f"%{tabla}.{columna}%"
    cn = he._conectar()
    try:
        cur = cn.cursor()
        vistos, out = set(), []

        def add(origen, name, desc):
            nd = " ".join((desc or "").split())
            k = ((name or "").strip().lower(), nd.lower())
            if not nd or k in vistos:
                return
            vistos.add(k)
            out.append({"origen": origen, "name": (name or "").strip(), "description": nd})

        cur.execute(f"""
            SELECT DISTINCT Name, CAST(Description AS NVARCHAR(MAX))
            FROM {DIC}.SSAS_Atributos
            WHERE Description IS NOT NULL AND LTRIM(RTRIM(CAST(Description AS NVARCHAR(MAX))))<>''
              AND (KeyColumns LIKE ? OR NameColumn LIKE ?)
        """, patron, patron)
        for name, desc in cur.fetchall():
            add("SSAS atributo", name, desc)
        cur.execute(f"""
            SELECT DISTINCT Name, CAST(Description AS NVARCHAR(MAX))
            FROM {DIC}.SSAS_Medidas
            WHERE Description IS NOT NULL AND LTRIM(RTRIM(CAST(Description AS NVARCHAR(MAX))))<>''
              AND ValueColumn LIKE ?
        """, patron)
        for name, desc in cur.fetchall():
            add("SSAS medida", name, desc)
        return out[:4]
    finally:
        cn.close()


def descripciones_nativas(clave: str) -> dict:
    """
    Recoge las DESCRIPCIONES nativas del catalogo (escritas por personas), que son
    la fuente con mas autoridad para la DescripcionFuncional:
      - columna SQL: SQL_Columnas.COL_DESCRIPTION (+ FORMULA si es calculada)
      - tabla  SQL: SQL_Objetos.OBJ_DESCRIPTION
    Devuelve {col_desc, formula, es_calculada, tabla_desc} (campos vacios si no hay).
    """
    p = clave.split(".")
    if len(p) < 5:
        return {}
    srv, bd, esq, tabla, _ = p[0], p[1], p[2], p[3], p[4]
    cn = he._conectar()
    try:
        cur = cn.cursor()
        cur.execute(f"""
            SELECT CAST(COL_DESCRIPTION AS NVARCHAR(MAX)),
                   CAST(FORMULA AS NVARCHAR(MAX)), IS_COMPUTED
            FROM {DIC}.SQL_Columnas WHERE CLAVE = ?
        """, clave)
        r = cur.fetchone()
        col_desc = (r[0] or "").strip() if r else ""
        formula = (r[1] or "").strip() if r else ""
        es_calc = bool(r[2]) if r else False

        # descripcion de la tabla (mismo servidor/bd/esquema/objeto)
        cur.execute(f"""
            SELECT TOP 1 CAST(OBJ_DESCRIPTION AS NVARCHAR(MAX))
            FROM {DIC}.SQL_Objetos
            WHERE SERVER = ? AND BD = ? AND OBJ_SCHEMA = ? AND OBJ_NAME = ?
              AND OBJ_DESCRIPTION IS NOT NULL
        """, srv, bd, esq, tabla)
        rt = cur.fetchone()
        tabla_desc = (rt[0] or "").strip() if rt else ""
    finally:
        cn.close()
    return {"col_desc": col_desc, "formula": formula,
            "es_calculada": es_calc, "tabla_desc": tabla_desc}


def descripciones_texto(clave: str) -> str:
    """Bloque para el prompt con las descripciones nativas (maxima prioridad)."""
    d = descripciones_nativas(clave)
    try:
        ssas = descripcion_ssas(clave)
    except Exception:
        ssas = []
    # descripciones de negocio de los campos AS400 origen (catalogos Excel)
    docs = []
    try:
        for fic, cam in _pares_as400(clave):
            dd = descripcion_documento(fic, cam)
            if dd and dd.get("descripcion"):
                docs.append((f"{fic}.{cam}", dd))
    except Exception:
        docs = []
    if not d and not ssas and not docs:
        return ""
    lineas = []
    if d.get("col_desc"):
        lineas.append(f"Descripcion oficial de la COLUMNA (catalogo, usala como base): "
                      f"{d['col_desc']}")
    if d.get("formula"):
        lineas.append(f"Formula (columna calculada): {d['formula']}")
    if d.get("tabla_desc"):
        lineas.append(f"Descripcion de la TABLA: {d['tabla_desc']}")
    for s in ssas:
        lineas.append(f"Descripcion de NEGOCIO ({s['origen']} '{s['name']}'): {s['description']}")
    for ref, dd in docs:
        extra = f" (nota: {dd['comentarios']})" if dd.get("comentarios") else ""
        lineas.append(f"Descripcion del campo AS400 origen {ref}: {dd['descripcion']}{extra}")
    if not lineas:
        return ""
    return ("=== DESCRIPCIONES OFICIALES DEL CATALOGO (FUENTE PRIORITARIA) ===\n"
            "Si hay descripcion oficial de la columna, de negocio (SSAS) o del campo\n"
            "AS400 origen, basa la DescripcionFuncional EN ELLA (mejorandola), no la\n"
            "ignores.\n"
            + "\n".join(lineas))


# --------------------------------------------------------------------------- #
#  2) TAXONOMIA CONCEPTUAL
# --------------------------------------------------------------------------- #
def taxonomia_conceptual(area: str = None, texto: str = None, limite: int = 200) -> list:
    cond, params = ["1=1"], []
    if area:
        cond.append("Area = ?"); params.append(area)
    if texto:
        cond.append("(Nombre LIKE ? OR Entidad LIKE ? OR DescripcionConceptual LIKE ?)")
        like = f"%{texto}%"; params += [like, like, like]
    cn = he._conectar()
    try:
        cur = cn.cursor()
        cur.execute(f"""
            SELECT TOP {int(limite)} Area, Entidad, Nombre,
                   CAST(DescripcionConceptual AS NVARCHAR(MAX)) AS DescripcionConceptual, Caso_De_Uso
            FROM {DIC}.CONCEP_Nombre
            WHERE {' AND '.join(cond)}
            ORDER BY Area, Entidad, Orden
        """, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cn.close()


def areas_entidades() -> dict:
    cn = he._conectar()
    try:
        cur = cn.cursor()
        cur.execute(f"SELECT DISTINCT Area FROM {DIC}.CONCEP_Nombre WHERE Area IS NOT NULL ORDER BY Area")
        areas = [r[0] for r in cur.fetchall() if r[0]]
        cur.execute(f"SELECT DISTINCT Area, Entidad FROM {DIC}.CONCEP_Nombre WHERE Area IS NOT NULL ORDER BY Area, Entidad")
        ent = {}
        for a, e in cur.fetchall():
            if a and e:
                ent.setdefault(a, []).append(e)
        return {"areas": areas, "entidades_por_area": ent}
    finally:
        cn.close()


# --------------------------------------------------------------------------- #
#  3) FICHA YA DOCUMENTADA
# --------------------------------------------------------------------------- #
def ficha_documentada(clave: str) -> dict:
    cn = he._conectar()
    try:
        cur = cn.cursor()
        cur.execute(f"""
            SELECT ORIGEN, CLAVE, NombreConceptual, EntidadConceptual, Area,
                   CAST(DescripcionFuncional AS NVARCHAR(MAX)) AS DescripcionFuncional,
                   ObjetoOrigen, CAST(Linaje AS NVARCHAR(MAX)) AS Linaje,
                   Visibilidad, ClaseDato, GDRP, Completado
            FROM {DIC}.DIC_Columnas WHERE CLAVE = ?
        """, clave)
        fila = cur.fetchone()
        if not fila:
            return {}
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, fila))
    finally:
        cn.close()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Acceso a la capa semantica del diccionario")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("linaje"); p1.add_argument("clave")
    p2 = sub.add_parser("taxonomia"); p2.add_argument("--area"); p2.add_argument("--texto")
    p3 = sub.add_parser("ficha"); p3.add_argument("clave")
    sub.add_parser("areas")
    args = ap.parse_args()

    try:
        if args.cmd == "linaje":
            print(linaje_texto(args.clave))
        elif args.cmd == "taxonomia":
            filas = taxonomia_conceptual(area=args.area, texto=args.texto)
            print(f"{len(filas)} conceptos:")
            for r in filas:
                d = (r["DescripcionConceptual"] or "")[:90].replace("\n", " ")
                print(f"  [{r['Area']} > {r['Entidad']}] {r['Nombre']}  ({r['Caso_De_Uso']}) — {d}")
        elif args.cmd == "ficha":
            d = ficha_documentada(args.clave)
            print(json.dumps(d, ensure_ascii=False, indent=2, default=str) if d
                  else "(no esta documentada en DIC_Columnas)")
        elif args.cmd == "areas":
            print(json.dumps(areas_entidades(), ensure_ascii=False, indent=2))
    except Exception as e:
        print("Error:", e); sys.exit(1)


if __name__ == "__main__":
    main()
