#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Evaluacion/benchmark de traducir_condicion_sql: mide que tan bien reproduce
el LLM la logica de reglas YA implementadas a mano en Sp_Calidad_RevisorA/Sonia
(comparacion contra el bloque real), y ademas traduce reglas activas SIN
implementar todavia (sin bloque real con el que comparar -- solo para
revision humana posterior).

Cada llamada al LLM tarda 1-4 minutos (mas con el esquema real incluido, ver
traductor_reglas_calidad.py), y un lote entero (varias reglas x hasta 20
intentos cada una) puede tardar horas -- por eso SIEMPRE corre en un hilo en
segundo plano (ver ejecutar_lote_en_segundo_plano) y escribe cada resultado
en dbo.CAL_Pruebas_Traduccion (workspace WORKSPACE_AGENTE) segun va
terminando, para que el frontend pueda ir viendo resultados parciales sin
esperar a que termine todo el lote.

La comparacion automatica ("coincide") es una heuristica, NO equivalencia
semantica SQL: para mecanismo 'generico' compara el validador elegido + sus
parametros clave contra lo que aparece en el bloque real (regex); para
'sql_personalizada' es best-effort (texto normalizado) y a menudo no puede
decidir (None) -- el bloque real y el generado se guardan siempre completos
para que la revision humana sea la que de verdad importa.

Por eso, ADEMAS de 'Coincide' (heuristico), cada fila se puede etiquetar con
un juicio HUMANO independiente (Correcta_Humano, ver etiquetar_resultado()):
confirma si la traduccion es REALMENTE correcta, este o no de acuerdo con la
heuristica. Comparar ambos campos expone donde falla el arnes (falsos
positivos/negativos de 'Coincide'), y los casos confirmados correctos quedan
disponibles como futura base de ejemplos para el prompt (ejemplos_confirmados()).

Uso como modulo (lo llama calidad_web.py):
  from origen_reglas.evaluar_traduccion import ejecutar_lote_en_segundo_plano, resultados_lote, estado_lote
  lote = ejecutar_lote_en_segundo_plano(ids_implementadas=[44,61,67,1078,31], ids_no_implementadas=[8,9,6,7,51])
"""

import json
import re
import threading
import time

SERVIDOR = r"SERVIDOR_WORKSPACE"
BASE_DATOS = "WORKSPACE_AGENTE"
DRIVER = "ODBC Driver 17 for SQL Server"

MAX_INTENTOS = 20
INTENTOS_ANTES_DE_PARAR = 5  # si ya acerto dentro de estos, no sigue hasta 20

_DDL_TABLA = """
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'CAL_Pruebas_Traduccion' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.CAL_Pruebas_Traduccion (
        Id                          INT IDENTITY PRIMARY KEY,
        Lote                        VARCHAR(64) NOT NULL,
        Id_Calidad_Tabla_Atributo   INT NOT NULL,
        Clave_Fisico                NVARCHAR(512),
        Desc_Funcional              NVARCHAR(MAX),
        Categoria                   VARCHAR(20) NOT NULL,   -- 'implementada' | 'no_implementada'
        Procedimiento_Real          VARCHAR(128),
        Bloque_Real                 NVARCHAR(MAX),
        Intento                     INT NOT NULL,
        Propuesta_JSON              NVARCHAR(MAX),
        Bloque_Generado             NVARCHAR(MAX),
        Coincide                    BIT,                     -- NULL = no se pudo juzgar automaticamente
        Tiempo_Segundos             FLOAT,
        Fecha                       DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    )
END
"""

# Migracion para tablas ya creadas antes de anadir el etiquetado humano --
# ALTER TABLE aparte de la CREATE TABLE de arriba porque esta ya se desplego
# en produccion (10.0.0.10) con datos reales dentro.
_DDL_COLUMNAS_HUMANO = """
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.CAL_Pruebas_Traduccion') AND name = 'Correcta_Humano')
BEGIN
    ALTER TABLE dbo.CAL_Pruebas_Traduccion ADD
        Correcta_Humano        BIT NULL,           -- NULL=sin revisar, 1=confirmada correcta, 0=confirmada incorrecta
        Revisor_Humano         NVARCHAR(100) NULL,
        Nota_Humano            NVARCHAR(500) NULL,
        Fecha_Etiqueta_Humano  DATETIME2 NULL
END
"""

# Migracion para el contador de tokens (Ollama expone "usage" al hablar por
# /v1/chat/completions, ver traductor_reglas_calidad.traducir_condicion_sql).
_DDL_COLUMNAS_TOKENS = """
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.CAL_Pruebas_Traduccion') AND name = 'Tokens_Entrada')
BEGIN
    ALTER TABLE dbo.CAL_Pruebas_Traduccion ADD
        Tokens_Entrada  INT NULL,   -- prompt_tokens
        Tokens_Salida   INT NULL    -- completion_tokens
END
"""

# ------------------------------- estado en memoria -------------------------- #
_estado_actual = {"lote": None, "corriendo": False, "progreso": "", "omitidas": []}
_lock_estado = threading.Lock()


def _conectar():
    import pyodbc
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SERVIDOR};DATABASE={BASE_DATOS};Trusted_Connection=yes;",
        timeout=10)


def _asegurar_tabla(cn):
    cur = cn.cursor()
    cur.execute(_DDL_TABLA)
    cur.execute(_DDL_COLUMNAS_HUMANO)
    cur.execute(_DDL_COLUMNAS_TOKENS)
    cn.commit()


def _guardar_resultado(lote, id_tab, clave_fisico, desc, categoria, procedimiento_real,
                        bloque_real, intento, propuesta, bloque_generado, coincide, tiempo_seg):
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            INSERT INTO dbo.CAL_Pruebas_Traduccion
                (Lote, Id_Calidad_Tabla_Atributo, Clave_Fisico, Desc_Funcional, Categoria,
                 Procedimiento_Real, Bloque_Real, Intento, Propuesta_JSON, Bloque_Generado,
                 Coincide, Tiempo_Segundos, Tokens_Entrada, Tokens_Salida)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS NVARCHAR(MAX)), ?, ?, ?, ?, ?)
        """, lote, id_tab, clave_fisico, desc, categoria, procedimiento_real, bloque_real,
             intento, json.dumps(propuesta, ensure_ascii=False), bloque_generado, coincide, tiempo_seg,
             propuesta.get("_tokens_entrada"), propuesta.get("_tokens_salida"))
        cn.commit()
    finally:
        cn.close()


def resultados_lote(lote: str = None) -> list:
    """Resultados de un lote (o del ultimo si no se indica), mas recientes
    primero dentro de cada regla."""
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        if lote:
            cur.execute("""
                SELECT Id, Lote, Id_Calidad_Tabla_Atributo, Clave_Fisico, Desc_Funcional, Categoria,
                       Procedimiento_Real, Bloque_Real, Intento, Propuesta_JSON, Bloque_Generado,
                       Coincide, Tiempo_Segundos, Fecha,
                       Correcta_Humano, Revisor_Humano, Nota_Humano, Fecha_Etiqueta_Humano,
                       Tokens_Entrada, Tokens_Salida
                FROM dbo.CAL_Pruebas_Traduccion WHERE Lote = ?
                ORDER BY Id_Calidad_Tabla_Atributo, Intento
            """, lote)
        else:
            cur.execute("""
                SELECT Id, Lote, Id_Calidad_Tabla_Atributo, Clave_Fisico, Desc_Funcional, Categoria,
                       Procedimiento_Real, Bloque_Real, Intento, Propuesta_JSON, Bloque_Generado,
                       Coincide, Tiempo_Segundos, Fecha,
                       Correcta_Humano, Revisor_Humano, Nota_Humano, Fecha_Etiqueta_Humano,
                       Tokens_Entrada, Tokens_Salida
                FROM dbo.CAL_Pruebas_Traduccion
                WHERE Lote = (SELECT TOP 1 Lote FROM dbo.CAL_Pruebas_Traduccion ORDER BY Id DESC)
                ORDER BY Id_Calidad_Tabla_Atributo, Intento
            """)
        cols = [c[0] for c in cur.description]
        filas = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            if d.get("Propuesta_JSON"):
                try:
                    d["Propuesta_JSON"] = json.loads(d["Propuesta_JSON"])
                except Exception:
                    pass
            filas.append(d)
        return filas
    finally:
        cn.close()


def lotes_disponibles() -> list:
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT Lote, MIN(Fecha) AS Inicio, COUNT(*) AS Filas
            FROM dbo.CAL_Pruebas_Traduccion GROUP BY Lote ORDER BY Inicio DESC
        """)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cn.close()


def estado_lote() -> dict:
    with _lock_estado:
        return dict(_estado_actual)


def reprocesar_lote(lote: str) -> int:
    """Recalcula 'Coincide' para todas las filas ya guardadas de un lote,
    SIN volver a llamar al LLM -- para cuando se corrige un bug en
    _coincide_heuristico() y no hace falta repetir horas de traduccion para
    tener el veredicto correcto. Devuelve cuantas filas cambiaron de valor."""
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT Id, Propuesta_JSON, Bloque_Real, Categoria, Coincide
            FROM dbo.CAL_Pruebas_Traduccion WHERE Lote = ?
        """, lote)
        filas = cur.fetchall()
        cambios = 0
        for id_fila, propuesta_json, bloque_real, categoria, coincide_previo in filas:
            if categoria != "implementada":
                continue
            try:
                propuesta = json.loads(propuesta_json) if propuesta_json else {}
            except Exception:
                continue
            nuevo = _coincide_heuristico(propuesta, bloque_real)
            if nuevo != coincide_previo:
                cur.execute("UPDATE dbo.CAL_Pruebas_Traduccion SET Coincide = ? WHERE Id = ?",
                            nuevo, id_fila)
                cambios += 1
        cn.commit()
        return cambios
    finally:
        cn.close()


def etiquetar_resultado(id_fila: int, correcta, revisor: str, nota: str = None) -> bool:
    """Guarda el juicio HUMANO sobre una fila ya evaluada, independiente del
    veredicto heuristico ('Coincide'). 'correcta' es True/False, o None para
    quitar la etiqueta (volver a "sin revisar").

    Comparando Correcta_Humano contra Coincide se ve exactamente donde falla
    el arnes automatico:
      Coincide=True  + Correcta_Humano=False -> falso positivo del arnes
      Coincide in (False, None) + Correcta_Humano=True -> falso negativo del arnes
    Las filas confirmadas correctas (Correcta_Humano=1) son la base de datos
    de ejemplos reales para dar mejor contexto al LLM -- ver
    ejemplos_confirmados()."""
    if correcta is not None and not revisor:
        raise ValueError("falta el revisor")
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        if correcta is None:
            cur.execute("""
                UPDATE dbo.CAL_Pruebas_Traduccion
                SET Correcta_Humano = NULL, Revisor_Humano = NULL, Nota_Humano = NULL,
                    Fecha_Etiqueta_Humano = NULL
                WHERE Id = ?
            """, id_fila)
        else:
            cur.execute("""
                UPDATE dbo.CAL_Pruebas_Traduccion
                SET Correcta_Humano = ?, Revisor_Humano = ?, Nota_Humano = ?,
                    Fecha_Etiqueta_Humano = SYSDATETIME()
                WHERE Id = ?
            """, bool(correcta), revisor, nota, id_fila)
        cn.commit()
        return cur.rowcount > 0
    finally:
        cn.close()


def ejemplos_confirmados(limite: int = 20) -> list:
    """Casos con Correcta_Humano=1 -- traducciones que un revisor confirmo
    como realmente correctas, no solo "coincide" segun la heuristica. Pensado
    para usarse mas adelante como ejemplos few-shot en el prompt del
    traductor (traductor_reglas_calidad.py todavia no los consume; esto solo
    los deja disponibles)."""
    cn = _conectar()
    try:
        _asegurar_tabla(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT TOP (?) Id, Clave_Fisico, Desc_Funcional, Propuesta_JSON, Bloque_Generado, Fecha_Etiqueta_Humano
            FROM dbo.CAL_Pruebas_Traduccion
            WHERE Correcta_Humano = 1
            ORDER BY Fecha_Etiqueta_Humano DESC
        """, limite)
        cols = [c[0] for c in cur.description]
        filas = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            if d.get("Propuesta_JSON"):
                try:
                    d["Propuesta_JSON"] = json.loads(d["Propuesta_JSON"])
                except Exception:
                    pass
            filas.append(d)
        return filas
    finally:
        cn.close()


# --------------------------- comparacion heuristica -------------------------- #
def _normalizar(texto):
    """Normaliza para comparar condiciones SQL como texto: colapsa espacios y
    quita los que rodean operadores de comparacion -- "a = 1" y "a=1" son la
    MISMA condicion, pero un bloque escrito a mano y una respuesta del LLM
    casi nunca coinciden en el estilo de espaciado (visto en la regla 44:
    "Es_Novacion_Renego = 1" vs "Es_Novacion_Renego=1" del bloque real,
    misma logica, marcada 'no coincide' solo por esto)."""
    t = (texto or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*(<>|<=|>=|=|<|>)\s*", r"\1", t)
    return t.rstrip(";")


def _quitar_comentarios_sql(texto):
    """Quita las lineas comentadas con -- (frecuentes en los bloques reales:
    parametros de ejemplo desactivados, código viejo dejado como referencia)
    antes de extraer valores -- si no, un @ValidarMaxValue comentado (NO
    vigente de verdad) se compara como si estuviera activo."""
    lineas = (texto or "").split("\n")
    return "\n".join(l for l in lineas if not l.strip().startswith("--"))


_RE_PARAM_NUM = {
    # se detiene en coma, salto de linea O el inicio de un comentario SQL
    # inline ('--') -- si no, "0   --Limite inferior..." se capturaba entero
    # y el valor "0" nunca se podia comparar como numero.
    "min_value": re.compile(r"@ValidarMinValue\s*=\s*(.+?)(?:--|,|\r|\n)", re.IGNORECASE),
    "max_value": re.compile(r"@ValidarMaxValue\s*=\s*(.+?)(?:--|,|\r|\n)", re.IGNORECASE),
    "num_decimales": re.compile(r"@NumDecimales\s*=\s*(.+?)(?:--|,|\r|\n)", re.IGNORECASE),
}


def _valor_o_none(txt):
    txt = (txt or "").strip().rstrip(",")
    if not txt or txt.upper() == "NULL":
        return None
    try:
        return float(txt)
    except ValueError:
        return txt.strip("'\" ")


_JQL_TIPO_A_VALIDADOR = {"NUMERO": "numero", "TEXTO": "texto", "FECHA": "fecha",
                         "LISTA": "lista", "UNICO": "unico", "BOOLEANO": "booleano"}
_JQL_ETIQUETA_A_CANON = {"Min": "min_value", "Max": "max_value",
                         "MinLen": "min_len", "MaxLen": "max_len",
                         "MinDate": "min_date", "MaxDate": "max_date"}


def _extraer_jql_params(jql_texto: str) -> dict:
    """Extrae Min/Max/MinLen/MaxLen/MinDate/MaxDate de una cadena JQL a un
    dict con las MISMAS claves canonicas que normalizar_parametros(), para
    poder comparar un JQL propuesto contra un validador generico REAL con el
    mismo codigo de comparacion (misma semantica, dos motores distintos)."""
    valores = dict(re.findall(r"(?:^|\s)([A-Za-z]+):(\S*)", jql_texto or ""))
    return {canon: _valor_o_none(valores[etq]) for etq, canon in _JQL_ETIQUETA_A_CANON.items() if etq in valores}


def _coincide_heuristico(propuesta: dict, bloque_real: str) -> "bool | None":
    """Best-effort, NO es equivalencia semantica SQL -- ver docstring del
    modulo. Devuelve None si no se puede juzgar automaticamente."""
    if not bloque_real or not propuesta:
        return None

    bloque_real = _quitar_comentarios_sql(bloque_real)
    m_validador_real = re.search(r"Sp_Calidad_(Numero|Texto|Fecha|Lista|Booleano|Unico)",
                                  bloque_real, re.IGNORECASE)
    mecanismo_generado = propuesta.get("mecanismo")

    if m_validador_real:
        # el bloque real usa un validador generico (via APP_VALIDADORES directo)
        # -- el LLM puede haber propuesto lo mismo por dos vias equivalentes:
        # "generico" (mismo mecanismo, EXEC directo) o "jql" (mismo validador,
        # pero interpretado por el motor parametrico). Ambas son correctas si
        # el TIPO y los parametros coinciden.
        validador_real = m_validador_real.group(1).lower()
        if mecanismo_generado == "generico":
            if (propuesta.get("validador") or "").lower() != validador_real:
                return False
            from activacion.generador_sp_agente_ia import normalizar_parametros
            generados = normalizar_parametros(propuesta.get("parametros") or {})
        elif mecanismo_generado == "jql":
            tipo_jql = (propuesta.get("jql") or "").strip().split(" ", 1)[0].upper()
            if _JQL_TIPO_A_VALIDADOR.get(tipo_jql) != validador_real:
                return False
            generados = _extraer_jql_params(propuesta.get("jql") or "")
        else:
            return False

        if validador_real == "numero":
            reales = {k: _valor_o_none(p.search(bloque_real).group(1)) if p.search(bloque_real) else None
                      for k, p in _RE_PARAM_NUM.items()}
            for clave in ("min_value", "max_value", "num_decimales"):
                if reales.get(clave) != generados.get(clave):
                    return False
            return True
        # otros validadores genericos: solo comparamos que se eligio el mismo tipo
        return None

    # el bloque real es SQL a mano (sql_personalizada) -- comparacion de texto
    # normalizado del WHERE, best-effort. Un JQL aqui no es comparable por
    # texto (motor distinto, sintaxis distinta): se deja sin juzgar.
    if mecanismo_generado in ("generico", "jql"):
        return False if mecanismo_generado == "generico" else None
    condicion = propuesta.get("condicion_violada")
    if not condicion:
        return False
    m_where = re.search(r"\bWHERE\b(.*?)(?:\bSELECT\b|\bEXEC\b|$)", bloque_real,
                         re.IGNORECASE | re.DOTALL)
    if not m_where:
        return None
    # generador_sp_agente_ia.bloque_sql() junta condicion_violada Y
    # filtro_poblacion con AND en el WHERE real (desde 2026-08-11, antes
    # filtro_poblacion se perdia silenciosamente) -- hay que comparar la
    # MISMA combinacion, si no una regla con poblacion acotada nunca
    # coincide con el bloque real aunque este bien traducida.
    filtro = propuesta.get("filtro_poblacion")
    condicion_completa = f"({condicion}) AND ({filtro})" if filtro else condicion
    return _normalizar(condicion_completa) == _normalizar(m_where.group(1))


# ------------------------------- ejecucion del lote -------------------------- #
def _evaluar_una_regla(lote, id_tab, clave_fisico, desc, categoria, procedimiento_real, bloque_real,
                        max_intentos_impl=MAX_INTENTOS):
    from origen_reglas import traductor_reglas_calidad as trad
    from origen_reglas import sincronizar_reglas_bd as srb

    # Reutiliza EXACTAMENTE el mismo contexto y despachador que el flujo real
    # de produccion (procesar_nueva/sincronizar) -- antes este modulo tenia su
    # propia copia vieja (solo generar_bloque(), de antes de anadir JQL) que
    # fallaba con "falta condicion_violada" para cualquier propuesta jql y
    # nunca le daba la clave primaria real al LLM. Bug del arnes de pruebas,
    # no del flujo real: aprobar_nueva() ya usaba _procesar_propuesta.
    columnas = srb._columnas_para(clave_fisico)
    clave_cols_ctx = srb._clave_cols_para(clave_fisico)
    max_intentos = max_intentos_impl if categoria == "implementada" else 1
    aciertos = 0

    for intento in range(1, max_intentos + 1):
        with _lock_estado:
            _estado_actual["progreso"] = f"regla {id_tab} ({categoria}), intento {intento}/{max_intentos}"

        t0 = time.time()
        propuesta = trad.traducir_condicion_sql(desc, clave_fisico, columnas_tabla=columnas,
                                                  clave_cols=clave_cols_ctx)
        tiempo_seg = time.time() - t0

        propuesta, _exito = srb._procesar_propuesta(id_tab, clave_fisico, propuesta,
                                                     desc_funcional=desc)
        bloque_generado = propuesta.get("jql") if propuesta.get("mecanismo") == "jql" \
            else propuesta.get("bloque_sql_preview")

        coincide = _coincide_heuristico(propuesta, bloque_real) if categoria == "implementada" else None

        _guardar_resultado(lote, id_tab, clave_fisico, desc, categoria, procedimiento_real,
                            bloque_real, intento, propuesta, bloque_generado, coincide, tiempo_seg)

        if coincide:
            aciertos += 1
        if categoria == "implementada" and intento >= INTENTOS_ANTES_DE_PARAR and aciertos > 0:
            break


def _bucle_lote(lote, ids_implementadas, ids_no_implementadas, max_intentos_impl=MAX_INTENTOS):
    from origen_reglas import extraer_reglas_activas_bd as ext
    from activacion import generador_sp_agente_ia as gen

    activas = {r["id_calidad_tabla_atributo"]: r for r in ext.extraer_reglas_activas()}

    # Los ids que se piden pero ya no estan activos se saltan, y antes se
    # saltaban EN SILENCIO: el lote salia con menos filas de las pedidas y no
    # habia forma de saber cuales ni por que. Paso de verdad el 2026-09-01
    # (reglas 2109 y 2187, borradas del catalogo entre un lote y el
    # siguiente) y costo una investigacion averiguarlo. Ahora se anotan en el
    # estado, que es lo que consulta el frontend.
    omitidas = [i for i in list(ids_implementadas) + list(ids_no_implementadas)
                if i not in activas]
    with _lock_estado:
        _estado_actual["omitidas"] = omitidas

    try:
        for id_tab in ids_implementadas:
            regla = activas.get(id_tab)
            if not regla:
                continue
            info = gen.buscar_bloque_real(id_tab)
            _evaluar_una_regla(lote, id_tab, regla["clave_fisico"], regla.get("desc_funcional") or "",
                                "implementada", info.get("procedimiento"), info.get("bloque"),
                                max_intentos_impl=max_intentos_impl)

        for id_tab in ids_no_implementadas:
            regla = activas.get(id_tab)
            if not regla:
                continue
            _evaluar_una_regla(lote, id_tab, regla["clave_fisico"], regla.get("desc_funcional") or "",
                                "no_implementada", None, None)
    finally:
        with _lock_estado:
            _estado_actual["corriendo"] = False
            _estado_actual["progreso"] = "terminado"


def ejecutar_lote_en_segundo_plano(ids_implementadas: list, ids_no_implementadas: list,
                                    max_intentos_impl: int = MAX_INTENTOS) -> str:
    """Lanza el lote completo en un hilo daemon y devuelve el identificador
    del lote inmediatamente -- ver estado_lote()/resultados_lote() para ir
    consultando el progreso. max_intentos_impl: techo de reintentos para las
    reglas 'implementada' (no_implementada siempre es 1, no tiene bloque
    real con el que parar antes) -- por defecto MAX_INTENTOS (20), pasa 1
    para una pasada rapida de una sola prueba por regla."""
    with _lock_estado:
        if _estado_actual["corriendo"]:
            raise ValueError(f"Ya hay un lote corriendo ({_estado_actual['lote']})")
        lote = time.strftime("lote_%Y%m%d_%H%M%S")
        _estado_actual["lote"] = lote
        _estado_actual["corriendo"] = True
        _estado_actual["progreso"] = "arrancando"

    hilo = threading.Thread(target=_bucle_lote, args=(lote, ids_implementadas, ids_no_implementadas, max_intentos_impl),
                             name=f"evaluar_traduccion_{lote}", daemon=True)
    hilo.start()
    return lote
