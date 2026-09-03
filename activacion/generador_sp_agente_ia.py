#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Genera y despliega dbo.Sp_Calidad_AgenteIA en DW_PRINCIPAL -- el mecanismo A del
sistema de calidad nativo (mismo patron que Sp_Calidad_RevisorA/Sonia/Elena): un
procedimiento con un bloque IF (@Id_Calidad_Tabla_Atributo = <id> OR ... IS
NULL) BEGIN ... END por regla, que mete las filas que incumplen en una
temporal y llama a EXEC APP_CATALOGO..Sp_Calidad_Ejecuta_Regla.

DOS PASOS, DOS NIVELES DE RIESGO:
  1. APROBAR una regla (registro_propuestas_reglas.py) -> guarda su bloque en
     dbo.CAL_Bloques_Sp_AgenteIA, en NUESTRO workspace (WORKSPACE_AGENTE,
     SERVIDOR_WORKSPACE). No toca DW_PRINCIPAL en absoluto.
  2. DESPLEGAR (accion separada y explicita) -> junta TODOS los bloques
     guardados, genera el CREATE OR ALTER PROCEDURE completo, y SOLO si se
     pide con ejecutar=True lo lanza de verdad contra DW_PRINCIPAL (una base de
     produccion compartida). Por defecto siempre devuelve el SQL para
     previsualizar, sin ejecutar nada.

ALTER PROCEDURE exige el cuerpo COMPLETO cada vez -- no se puede parchear una
sola regla del procedimiento ya desplegado. Por eso dbo.CAL_Bloques_Sp_AgenteIA
es la fuente de verdad de que bloques estan aprobados; el procedimiento real
en DW_PRINCIPAL se regenera entero a partir de ella en cada despliegue.

Uso como modulo:
  from activacion.generador_sp_agente_ia import bloque_sql, guardar_bloque, desplegar
  bloque = bloque_sql(2105, clave_fisico, condicion_violada, mensaje)
  guardar_bloque(2105, bloque, aprobado_por="revisor.ejemplo")
  resultado = desplegar(ejecutar=False)   # previsualizar
  resultado = desplegar(ejecutar=True)    # CREATE OR ALTER PROCEDURE real en DW_PRINCIPAL

Tambien expone columnas_de() (esquema real de una tabla, para dar contexto al
LLM al traducir) y buscar_bloque_real() (busca si algun Sp_Calidad_<revisor>
YA en produccion tiene un bloque escrito a mano para un Id_Calidad_Tabla_Atributo
dado -- util para comparar contra lo que propone el LLM).
"""

import re

from nucleo.agente_calidad_datos import ALIAS_SERVIDOR  # reutiliza el mapeo alias->servidor real

# ----------------------------- CONFIGURACION ------------------------------- #
# Workspace propio: donde se guardan los bloques aprobados (no es DW_PRINCIPAL).
WS_SERVIDOR = r"SERVIDOR_WORKSPACE"
WS_BASE_DATOS = "WORKSPACE_AGENTE"
WS_DRIVER = "ODBC Driver 17 for SQL Server"

# Donde vive el procedimiento real (produccion, escritura sensible).
DW_SERVIDOR_ALIAS = "SRV1"
DW_BASE_DATOS = "DW_PRINCIPAL"
DW_DRIVER = "ODBC Driver 17 for SQL Server"

# Las tres bases de datos de solo lectura para ANALIZAR datos (no confundir
# con APP_CATALOGO/APP_VALIDADORES, que son metadatos/motor). Ver CONTEXTO.md
# del proyecto hermano ProyectoHermano, tabla de topologia: "Datos a analizar |
# DW_PRINCIPAL, DW_CRUDO, DW_LAGO | SOLO LECTURA". Algunas reglas del catalogo
# declaran DW_PRINCIPAL en Clave_Fisico pero la tabla real (a menudo de origen
# AS400/mainframe, como Tb_Historico_Impagos_SREJMT) vive en DW_CRUDO --
# resolver_base_datos() prueba las tres antes de rendirse.
BASES_DATOS_CANDIDATAS = ("DW_PRINCIPAL", "DW_CRUDO", "DW_LAGO")

NOMBRE_PROCEDIMIENTO = "Sp_Calidad_AgenteIA"
USUARIO_EJECUCION = "AgenteIA"
ORIGEN_EJECUCION = f"Desde el DW_PRINCIPAL.[dbo].[{NOMBRE_PROCEDIMIENTO}]"
# --------------------------------------------------------------------------- #

_RE_CLAVE = re.compile(r"^(?P<servidor>[^.]+)\.(?P<base_datos>[^.]+)\.(?P<esquema>[^.]+)\.(?P<tabla>[^.]+)\.(?P<columna>.+)$")

_DDL_TABLA_BLOQUES = """
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'CAL_Bloques_Sp_AgenteIA' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.CAL_Bloques_Sp_AgenteIA (
        Id_Calidad_Tabla_Atributo   INT NOT NULL PRIMARY KEY,
        Clave_Fisico                NVARCHAR(512),
        Bloque_SQL                  NVARCHAR(MAX) NOT NULL,
        Aprobado_Por                NVARCHAR(128),
        Fecha_Aprobacion            DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    )
END
"""


def _conectar_workspace():
    import pyodbc
    return pyodbc.connect(
        f"DRIVER={{{WS_DRIVER}}};SERVER={WS_SERVIDOR};DATABASE={WS_BASE_DATOS};Trusted_Connection=yes;",
        timeout=10)


def _conectar_dw_uci():
    import pyodbc
    servidor_real = ALIAS_SERVIDOR.get(DW_SERVIDOR_ALIAS, DW_SERVIDOR_ALIAS)
    return pyodbc.connect(
        f"DRIVER={{{DW_DRIVER}}};SERVER={servidor_real};DATABASE={DW_BASE_DATOS};Trusted_Connection=yes;",
        timeout=15)


def parsear_clave(clave_fisico: str) -> dict:
    """'SRV1.DW_PRINCIPAL.dbo.Tabla.Columna' -> {servidor, base_datos, esquema, tabla, columna}.
    Lanza ValueError si la CLAVE no tiene la forma esperada (p. ej. columnas
    AS400 con expresiones entre comillas) -- esos casos necesitan que el
    revisor humano rellene el bloque a mano, no se adivinan."""
    m = _RE_CLAVE.match(clave_fisico)
    if not m:
        raise ValueError(f"Clave_Fisico con formato inesperado (no es Servidor.BD.Esquema.Tabla.Columna): {clave_fisico!r}")
    return m.groupdict()


def resolver_base_datos(esquema: str, tabla: str, preferida: str = None) -> str:
    """En cual de DW_PRINCIPAL/DW_CRUDO/DW_LAGO vive de verdad esquema.tabla. Prueba
    primero 'preferida' (normalmente la que dice Clave_Fisico), luego el
    resto de BASES_DATOS_CANDIDATAS. Una sola conexion vale para las tres:
    estan en la misma instancia (SERVIDOR_DATOS\\INSTANCIA), consulta cruzada
    con nombre de tres partes. Lanza ValueError si no aparece en ninguna."""
    orden = list(BASES_DATOS_CANDIDATAS)
    if preferida and preferida in orden:
        orden.remove(preferida)
        orden.insert(0, preferida)
    elif preferida:
        orden.insert(0, preferida)  # base no listada (rara), se prueba igual la primera

    cn = _conectar_dw_uci()
    try:
        cur = cn.cursor()
        for bd in orden:
            cur.execute("SELECT OBJECT_ID(?)", f"{bd}.{esquema}.{tabla}")
            if cur.fetchone()[0] is not None:
                return bd
        raise ValueError(
            f"No existe la tabla/vista {esquema}.{tabla} en ninguna de "
            f"{', '.join(BASES_DATOS_CANDIDATAS)}")
    finally:
        cn.close()


def columnas_clave_de(esquema: str, tabla: str, base_datos: str = None) -> list:
    """PK real de la tabla (busca en DW_PRINCIPAL/DW_CRUDO/DW_LAGO, ver
    resolver_base_datos). Si no hay PK/UNIQUE constraint formal, cae a un
    indice unico sin constraint como segunda opcion -- las vistas normales
    no suelen tener ninguno de los dos, eso no se puede adivinar."""
    bd = resolver_base_datos(esquema, tabla, preferida=base_datos)
    cn = _conectar_dw_uci()
    try:
        cur = cn.cursor()
        cur.execute("SELECT OBJECT_ID(?)", f"{bd}.{esquema}.{tabla}")
        object_id = cur.fetchone()[0]
        cur.execute(f"""
            SELECT c.name FROM {bd}.sys.key_constraints kc
            JOIN {bd}.sys.index_columns ic ON ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
            JOIN {bd}.sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE kc.parent_object_id = ?
            ORDER BY ic.key_ordinal
        """, object_id)
        cols = [r[0] for r in cur.fetchall()]
        if cols:
            return cols
        cur.execute(f"""
            SELECT c.name FROM {bd}.sys.indexes i
            JOIN {bd}.sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN {bd}.sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.object_id = ? AND i.is_unique = 1 AND i.is_primary_key = 0
            ORDER BY ic.key_ordinal
        """, object_id)
        return [r[0] for r in cur.fetchall()]
    finally:
        cn.close()


_TIPOS_DECIMALES = {"decimal", "numeric"}
_TIPOS_TEXTO_WIDE = {"nvarchar", "nchar"}  # max_length en bytes (UTF-16), hay que /2
_TIPOS_TEXTO_NARROW = {"varchar", "char", "varbinary", "binary"}  # max_length en bytes = caracteres


def _tipo_con_tamano(tipo: str, precision: int, scale: int, max_length: int) -> str:
    """'numeric' -> 'numeric(13,2)', 'varchar' -> 'varchar(50)'/'varchar(MAX)' --
    sin esto el LLM solo ve el nombre pelado del tipo y no puede saber, por
    ejemplo, si una columna numeric(13,2) YA garantiza 2 decimales a nivel de
    tipo (con lo que "num_decimales" via el validador generico nunca podria
    fallar, es un no-op) -- visto en la regla 1085, donde el LLM alternaba
    sin criterio entre sql_personalizada y generico para la misma regla
    precisamente por no tener este dato."""
    if tipo in _TIPOS_DECIMALES:
        return f"{tipo}({precision},{scale})"
    if tipo in _TIPOS_TEXTO_WIDE:
        return f"{tipo}(MAX)" if max_length == -1 else f"{tipo}({max_length // 2})"
    if tipo in _TIPOS_TEXTO_NARROW:
        return f"{tipo}(MAX)" if max_length == -1 else f"{tipo}({max_length})"
    return tipo


def columnas_de(esquema: str, tabla: str, base_datos: str = None) -> list:
    """TODAS las columnas (nombre + tipo, con precision/escala/longitud real)
    de la tabla/vista (busca en DW_PRINCIPAL/DW_CRUDO/DW_LAGO, ver
    resolver_base_datos) -- para darle al LLM el esquema real al traducir una
    regla, en vez de que se invente nombres de columnas 'hermanas' plausibles
    a partir del texto de negocio."""
    bd = resolver_base_datos(esquema, tabla, preferida=base_datos)
    cn = _conectar_dw_uci()
    try:
        cur = cn.cursor()
        cur.execute("SELECT OBJECT_ID(?)", f"{bd}.{esquema}.{tabla}")
        object_id = cur.fetchone()[0]
        cur.execute(f"""
            SELECT c.name, t.name AS tipo, c.precision, c.scale, c.max_length
            FROM {bd}.sys.columns c
            JOIN {bd}.sys.types t ON t.user_type_id = c.user_type_id
            WHERE c.object_id = ?
            ORDER BY c.column_id
        """, object_id)
        return [{"nombre": r[0], "tipo": _tipo_con_tamano(r[1], r[2], r[3], r[4])} for r in cur.fetchall()]
    finally:
        cn.close()


_RE_TABLA_REFERENCIADA = re.compile(
    r"\b(?:FROM|JOIN)\s+(\[?[A-Za-z_][\w$#]*\]?(?:\s*\.\s*\[?[\w$#]*\]?)*)", re.IGNORECASE)
# Funciones con forma de tabla: 'FROM openquery(...)' no es un nombre de tabla.
_TVF_CONOCIDAS = {"openquery", "opendatasource", "openrowset", "openxml", "string_split"}


def _verificar_tablas_referenciadas(condicion_violada: str) -> None:
    """Comprueba que las tablas que la condicion referencia con FROM/JOIN
    existan de verdad. El prompt del traductor ya PROHIBE inventar tablas de
    maestro/dimension, pero es solo un aviso y el LLM lo cumple a medias:
    en la regla 2150 ("el valor tiene que estar entre los recogidos en el
    maestro de ratings de riesgo") hizo lo que se le pedia -- mecanismo
    sql_personalizada y confianza 0.3 -- pero aun asi escribio
    "Rating NOT IN (SELECT Rating FROM Maestro_Ratings_De_Riesgo)", con esa
    tabla inventada. Igual en la 49, contra "Dim_Tiempo".

    Como condicion_violada se incrusta tal cual en el WHERE del bloque
    (ver bloque_sql), una tabla inexistente NO da la cara al generar ni al
    desplegar -- SQL Server resuelve los nombres en diferido -- sino en la
    primera ejecucion real, con 'Invalid object name'. Mejor detenerlo aqui.

    No bloquea si no se puede consultar el esquema (mismo criterio que el
    resto del modulo: degradar, no romper)."""
    if not condicion_violada:
        return
    # Vacia los literales de cadena antes de buscar: dentro de un
    # openquery(sd,'select x from YODIFIC.EE68mT') hay un FROM que apunta a
    # una tabla del AS400, invisible para OBJECT_ID -- mirarlo daria un falso
    # positivo y bloquearia una condicion correcta (asi resuelve la 2150 el
    # bloque escrito a mano).
    sin_literales = re.sub(r"'[^']*'", "''", condicion_violada)
    candidatas = []
    for m in _RE_TABLA_REFERENCIADA.finditer(sin_literales):
        # 'FROM openquery(...)' y demas funciones con forma de tabla: el
        # nombre va seguido de '(' y no es un objeto que consultar.
        if sin_literales[m.end():m.end() + 1].lstrip()[:1] == "(":
            continue
        nombre = re.sub(r"[\[\]\s]", "", m.group(1)).strip(".")
        if not nombre or nombre.split(".")[-1].lower() in _TVF_CONOCIDAS:
            continue
        candidatas.append(nombre)
    if not candidatas:
        return

    try:
        cn = _conectar_dw_uci()
    except Exception:
        return  # sin conexion no se puede verificar -- no bloquea
    try:
        cur = cn.cursor()
        inexistentes = []
        for nombre in candidatas:
            partes = nombre.split(".")
            if len(partes) >= 3:
                pruebas = [nombre]
            elif len(partes) == 2:
                pruebas = [f"{bd}.{nombre}" for bd in BASES_DATOS_CANDIDATAS]
            else:
                pruebas = [f"{bd}.dbo.{nombre}" for bd in BASES_DATOS_CANDIDATAS]
            encontrada = False
            for candidata in pruebas:
                cur.execute("SELECT OBJECT_ID(?)", candidata)
                if cur.fetchone()[0] is not None:
                    encontrada = True
                    break
            if not encontrada:
                inexistentes.append(nombre)
        if inexistentes:
            raise ValueError(
                f"La condicion referencia tablas que no existen en "
                f"{'/'.join(BASES_DATOS_CANDIDATAS)}: {', '.join(inexistentes)}. "
                f"Casi seguro son nombres inventados por el LLM (tipico en "
                f"reglas del estilo 'debe existir en el maestro de X'). El "
                f"bloque se generaria y hasta se desplegaria sin queja -- SQL "
                f"Server resuelve los nombres en diferido -- y fallaria con "
                f"'Invalid object name' en la primera ejecucion real. Pon el "
                f"nombre real de la tabla a mano antes de aprobar.")
    finally:
        cn.close()


def bloque_sql(id_calidad_tabla_atributo: int, clave_fisico: str, condicion_violada: str,
                mensaje: str, clave_cols: list = None, filtro_poblacion: str = None) -> str:
    """Construye el bloque IF/BEGIN/END al estilo Sp_Calidad_RevisorA para una
    regla ya aprobada. 'condicion_violada' es un fragmento SQL (propuesto por
    el LLM, revisado por un humano) que describe las filas que INCUMPLEN.

    'filtro_poblacion' acota TANTO el recuento total (@Recuento, el
    denominador del % de cumplimiento) COMO las filas de las que se buscan
    incumplimientos -- si una regla solo aplica a un pais, por ejemplo, ni
    las filas de otros paises deben contar en el denominador ni pueden
    aparecer como incumplimiento. Antes de 2026-08-11 este campo lo
    proponia el LLM (ver traductor_reglas_calidad) pero NUNCA se usaba aqui
    -- toda regla con poblacion acotada media el % de cumplimiento contra
    la tabla ENTERA, denominador incorrecto. Bug real de este repo, no del
    motor -- corregido.

    Antes de construir nada comprueba con _verificar_tablas_referenciadas()
    que las tablas que menciona la condicion existan: una inventada por el
    LLM no fallaria hasta la primera ejecucion en produccion."""
    _verificar_tablas_referenciadas(condicion_violada)
    partes = parsear_clave(clave_fisico)
    esquema, tabla = partes["esquema"], partes["tabla"]
    bd_real = resolver_base_datos(esquema, tabla, preferida=partes["base_datos"])
    tabla_calificada = f"{bd_real}.{esquema}.{tabla}"

    if clave_cols is None:
        clave_cols = columnas_clave_de(esquema, tabla, base_datos=bd_real)
    if not clave_cols:
        raise ValueError(f"No se encontro clave primaria para {esquema}.{tabla}; "
                          f"indicala a mano (clave_cols) antes de generar el bloque.")
    clave_cols_sql = ", ".join(clave_cols)
    mensaje_sql = mensaje.replace("'", "''")
    id_ = int(id_calidad_tabla_atributo)

    filtro_where = f" WHERE ({filtro_poblacion})" if filtro_poblacion else ""
    filtro_and = f" AND ({filtro_poblacion})" if filtro_poblacion else ""

    return f"""IF (@Id_Calidad_Tabla_Atributo = {id_} OR @Id_Calidad_Tabla_Atributo IS NULL)
BEGIN
    DROP TABLE IF EXISTS #resultado{id_};

    SELECT {clave_cols_sql}, '{mensaje_sql}' AS Resultado
    INTO #resultado{id_}
    FROM {tabla_calificada}
    WHERE ({condicion_violada}){filtro_and}

    SELECT @Recuento = COUNT(*) FROM {tabla_calificada}{filtro_where}

    EXEC APP_CATALOGO..Sp_Calidad_Ejecuta_Regla
        @Fecha = @Fecha, @Id_Calidad_Tabla_Atributo = {id_}, @Temporal = '#resultado{id_}',
        @Registros = @Recuento, @Tipo = 'PROCESO', @Usuario = '{USUARIO_EJECUCION}',
        @Origen = '{ORIGEN_EJECUCION}', @Clave = '{clave_cols_sql}', @Pruebas = @Pruebas
END
"""


# --------------------- VALIDADORES GENERICOS (APP_VALIDADORES) ----------------- #
# Ya existen en produccion (Sp_Calidad_Numero/Texto/Fecha/Lista/Booleano/Unico
# en APP_VALIDADORES, cifrados con WITH ENCRYPTION -- firma real obtenida via
# sys.parameters, no via OBJECT_DEFINITION). Es el estilo que de verdad usan
# Sp_Calidad_RevisorA/Sonia para las comprobaciones simples (rango, longitud,
# lista cerrada, unicidad...), en vez de escribir la condicion SQL a mano.
_VALIDADORES_GENERICOS = {"Numero", "Texto", "Fecha", "Lista", "Booleano", "Unico"}

# El LLM devuelve las claves de "parametros" con variacion natural (a veces
# con '@' y CamelCase como el parametro SQL real -- "@ValidarMinValue" --,
# a veces en snake_case como se documenta en el prompt -- "min_value").
# Normalizamos TODAS las variantes razonables a la clave canonica snake_case
# antes de leerlas -- si no, un min_value=0 real se pierde silenciosamente
# (queda NULL en el bloque generado) solo por una diferencia de formato.
_ALIAS_PARAMETROS = {
    "validarminvalue": "min_value", "minvalue": "min_value",
    "validarmaxvalue": "max_value", "maxvalue": "max_value",
    "numdecimales": "num_decimales",
    "validarminlen": "min_len", "minlen": "min_len",
    "validarmaxlen": "max_len", "maxlen": "max_len",
    "trim": "trim",
    "validarmindate": "min_date", "mindate": "min_date",
    "validarmaxdate": "max_date", "maxdate": "max_date",
    "validardim": "validar_dim",
    "validarlista": "lista", "lista": "lista",
    "validarseparador": "separador", "separador": "separador",
    "errornotin": "error_not_in",
    "validarnotin": "not_in",
    "defaulttruevalue": "default_true",
    "defaultfalsevalue": "default_false",
    "permiteduplicados": "permite_duplicados",
}


def normalizar_parametros(parametros: dict) -> dict:
    """Aplica _ALIAS_PARAMETROS a todas las claves de un dict de parametros
    del LLM. Claves no reconocidas se dejan tal cual (por si acaso)."""
    resultado = {}
    for k, v in (parametros or {}).items():
        clave_norm = k.lstrip("@").lower().replace("_", "")
        resultado[_ALIAS_PARAMETROS.get(clave_norm, k)] = v
    return resultado


def _num_o_null(v):
    """Numero suelto (sin comillas) para el EXEC generado. Se castea de
    verdad: al ir SIN comillas, cualquier texto que se colara aqui entraria
    como SQL crudo en el procedimiento que luego se despliega en DW_PRINCIPAL, y no
    habria comillas que escapar. Estos valores vienen del LLM o de un revisor
    editando el JSON de la propuesta a mano, asi que no se dan por buenos."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):  # bool es int en Python; aqui no tiene sentido
        raise ValueError(f"Valor numerico no valido: {v!r}")
    if isinstance(v, (int, float)):
        return str(v)
    try:
        texto = str(v).strip()
        return str(int(texto)) if texto.lstrip("+-").isdigit() else str(float(texto))
    except (TypeError, ValueError):
        raise ValueError(
            f"Se esperaba un numero y llego {v!r}. Iria sin comillas al EXEC "
            f"del bloque generado, o sea como SQL crudo en un procedimiento de "
            f"produccion. Corrige el parametro antes de aprobar la regla.")


def _fecha_o_null(v):
    """Fecha entre comillas para el EXEC generado. Escapa la comilla simple
    igual que _lista_sql(): sin eso, un valor con comilla rompe el bloque (o
    algo peor) cuando se despliega en DW_PRINCIPAL."""
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def _bit(v, defecto=0):
    return "1" if (v if v is not None else defecto) else "0"


def _lista_sql(v):
    if not v:
        return "NULL"
    if isinstance(v, list):
        v = ",".join(str(x) for x in v)
    return "N'" + str(v).replace("'", "''") + "'"


def _parametros_validador(validador: str, parametros: dict) -> str:
    """Porcion de parametros especificos del validador elegido (los comunes
    -- Temporal/Clave/ValidarCampo/AdmiteNull/DefaultInvalid/... -- los pone
    bloque_sql_generico)."""
    p = normalizar_parametros(parametros)
    if validador == "Numero":
        partes = [f"@ValidarMinValue = {_num_o_null(p.get('min_value'))}",
                  f"@ValidarMaxValue = {_num_o_null(p.get('max_value'))}"]
        if p.get("num_decimales") is not None:
            partes.append(f"@NumDecimales = {int(p['num_decimales'])}")
        return ",\n        ".join(partes)
    if validador == "Texto":
        return (f"@Trim = {_bit(p.get('trim'), 1)},\n"
                f"        @ValidarMinLen = {_num_o_null(p.get('min_len'))},\n"
                f"        @ValidarMaxLen = {_num_o_null(p.get('max_len'))}")
    if validador == "Fecha":
        return (f"@ValidarDim = {_bit(p.get('validar_dim'), 0)},\n"
                f"        @ValidarMinDate = {_fecha_o_null(p.get('min_date'))},\n"
                f"        @ValidarMaxDate = {_fecha_o_null(p.get('max_date'))}")
    if validador == "Lista":
        return (f"@ValidarLista = {_lista_sql(p.get('lista'))},\n"
                f"        @ValidarSeparador = N'{p.get('separador') or ','}',\n"
                f"        @ErrorNotIn = {_bit(p.get('error_not_in'), 1)}")
    if validador == "Booleano":
        return (f"@ValidarLista = {_lista_sql(p.get('lista'))},\n"
                f"        @ValidarSeparador = N'{p.get('separador') or ','}',\n"
                f"        @ValidarNotIn = {_bit(p.get('not_in'), 0)},\n"
                f"        @DefaultTrueValue = {_bit(p.get('default_true'), 1)},\n"
                f"        @DefaultFalseValue = {_bit(p.get('default_false'), 0)}")
    if validador == "Unico":
        return f"@PermiteDuplicados = {_bit(p.get('permite_duplicados'), 0)}"
    raise ValueError(f"Validador generico desconocido: {validador!r}")


def bloque_sql_generico(id_calidad_tabla_atributo: int, clave_fisico: str, validador: str,
                         parametros: dict, admite_null: bool = True, clave_cols: list = None,
                         filtro_poblacion: str = None) -> str:
    """Bloque IF/BEGIN/END que llama a un validador YA EXISTENTE en
    APP_VALIDADORES (Sp_Calidad_Numero/Texto/Fecha/Lista/Booleano/Unico), al
    estilo real de Sp_Calidad_RevisorA/Sonia -- alternativa a bloque_sql() cuando
    la regla es una comprobacion simple que ese validador ya cubre.

    'filtro_poblacion' acota la tabla temporal que se le pasa al validador,
    igual que hacen los bloques escritos a mano (p.ej. el real de la regla
    2150: "select ... into #Para_Validar2150 from ... where NI<3 AND
    ALQUILER=0"). Aqui basta con filtrar una vez, a diferencia de
    bloque_sql(): el validador saca el denominador del propio temporal, asi
    que acotarlo arregla a la vez la poblacion y los incumplimientos.

    Este parametro NO existia: bloque_sql() se arreglo el 2026-08-11 para
    dejar de medir el % contra la tabla entera, pero la correccion nunca se
    aplico a esta rama y generar_bloque() se limitaba a no pasar el filtro,
    perdiendolo sin avisar."""
    if validador not in _VALIDADORES_GENERICOS:
        raise ValueError(f"Validador generico desconocido: {validador!r}")

    partes = parsear_clave(clave_fisico)
    esquema, tabla, columna = partes["esquema"], partes["tabla"], partes["columna"]
    bd_real = resolver_base_datos(esquema, tabla, preferida=partes["base_datos"])
    tabla_calificada = f"{bd_real}.{esquema}.{tabla}"

    if clave_cols is None:
        clave_cols = columnas_clave_de(esquema, tabla, base_datos=bd_real)
    if not clave_cols:
        raise ValueError(f"No se encontro clave primaria para {esquema}.{tabla}; "
                          f"indicala a mano (clave_cols) antes de generar el bloque.")
    clave_cols_sql = ", ".join(clave_cols)
    id_ = int(id_calidad_tabla_atributo)
    params_especificos = _parametros_validador(validador, parametros)

    filtro_where = f"\n    WHERE ({filtro_poblacion})" if filtro_poblacion else ""

    return f"""IF (@Id_Calidad_Tabla_Atributo = {id_} OR @Id_Calidad_Tabla_Atributo IS NULL)
BEGIN
    DROP TABLE IF EXISTS #Para_Validar{id_};

    SELECT {clave_cols_sql}, {columna}
    INTO #Para_Validar{id_}
    FROM {tabla_calificada}{filtro_where}

    EXEC APP_VALIDADORES.dbo.Sp_Calidad_{validador}
        @Temporal = '#Para_Validar{id_}',
        @Clave = '{clave_cols_sql}',
        @ValidarCampo = '{columna}',
        @AdmiteNull = {_bit(admite_null, 1)},
        {params_especificos},
        @DefaultInvalid = NULL,
        @Id_Calidad_Tabla_Atributo = {id_},
        @Fecha = @Fecha, @Tipo = 'PROCESO', @Usuario = '{USUARIO_EJECUCION}',
        @Origen = '{ORIGEN_EJECUCION}', @Pruebas = @Pruebas
END
"""


def generar_bloque(id_calidad_tabla_atributo: int, clave_fisico: str, propuesta: dict,
                    clave_cols: list = None) -> str:
    """Despacha entre bloque_sql_generico (validador ya existente) y
    bloque_sql (condicion SQL a mano), segun el campo 'mecanismo' que
    devuelve traductor_reglas_calidad.traducir_condicion_sql. Punto unico de
    entrada para el resto del sistema (sincronizar_reglas_bd.py)."""
    mecanismo = propuesta.get("mecanismo") or "sql_personalizada"
    if mecanismo == "generico":
        return bloque_sql_generico(
            id_calidad_tabla_atributo, clave_fisico,
            propuesta["validador"], propuesta.get("parametros") or {},
            admite_null=propuesta.get("admite_null", True), clave_cols=clave_cols,
            filtro_poblacion=propuesta.get("filtro_poblacion"))

    condicion = propuesta.get("condicion_violada")
    if not condicion:
        raise ValueError("falta 'condicion_violada' para el mecanismo sql_personalizada")
    return bloque_sql(id_calidad_tabla_atributo, clave_fisico, condicion,
                       propuesta.get("mensaje") or "", clave_cols=clave_cols,
                       filtro_poblacion=propuesta.get("filtro_poblacion"))


def _asegurar_tabla_bloques(cn):
    cur = cn.cursor()
    cur.execute(_DDL_TABLA_BLOQUES)
    cn.commit()


def guardar_bloque(id_calidad_tabla_atributo: int, bloque: str, clave_fisico: str, aprobado_por: str):
    """UPSERT del bloque aprobado en nuestro workspace. NO toca DW_PRINCIPAL."""
    cn = _conectar_workspace()
    try:
        _asegurar_tabla_bloques(cn)
        cur = cn.cursor()
        cur.execute("""
            UPDATE dbo.CAL_Bloques_Sp_AgenteIA
            SET Bloque_SQL = ?, Clave_Fisico = ?, Aprobado_Por = ?, Fecha_Aprobacion = SYSDATETIME()
            WHERE Id_Calidad_Tabla_Atributo = ?
        """, bloque, clave_fisico, aprobado_por, id_calidad_tabla_atributo)
        if cur.rowcount == 0:
            cur.execute("""
                INSERT INTO dbo.CAL_Bloques_Sp_AgenteIA
                    (Id_Calidad_Tabla_Atributo, Clave_Fisico, Bloque_SQL, Aprobado_Por)
                VALUES (?, ?, ?, ?)
            """, id_calidad_tabla_atributo, clave_fisico, bloque, aprobado_por)
        cn.commit()
    finally:
        cn.close()


def listar_bloques() -> list:
    cn = _conectar_workspace()
    try:
        _asegurar_tabla_bloques(cn)
        cur = cn.cursor()
        cur.execute("""
            SELECT Id_Calidad_Tabla_Atributo, Clave_Fisico, Bloque_SQL, Aprobado_Por, Fecha_Aprobacion
            FROM dbo.CAL_Bloques_Sp_AgenteIA ORDER BY Id_Calidad_Tabla_Atributo
        """)
        cols = ["id_calidad_tabla_atributo", "clave_fisico", "bloque_sql", "aprobado_por", "fecha_aprobacion"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cn.close()


def generar_procedimiento_completo(bloques: list = None) -> str:
    """Ensambla el CREATE OR ALTER PROCEDURE completo a partir de todos los
    bloques aprobados (o la lista dada). @Pruebas por defecto 1 (simulacro),
    igual que el resto del sistema de calidad ya en produccion."""
    if bloques is None:
        bloques = [b["bloque_sql"] for b in listar_bloques()]
    cuerpo_bloques = "\n".join(bloques) if bloques else "    -- (sin reglas aprobadas todavia)\n"
    return f"""CREATE OR ALTER PROCEDURE dbo.{NOMBRE_PROCEDIMIENTO}
    @Id_Calidad_Tabla_Atributo VARCHAR(512) = NULL,
    @Pruebas BIT = 1
AS
SET NOCOUNT ON;

DECLARE @Fecha DATETIME = GETDATE();
DECLARE @Recuento INT = 0;

{cuerpo_bloques}
"""


def previsualizar_despliegue() -> str:
    """Solo genera el texto del procedimiento, NUNCA lo ejecuta."""
    return generar_procedimiento_completo()


def desplegar(ejecutar: bool = False) -> dict:
    """Genera el procedimiento completo. Si ejecutar=True, lo despliega de
    verdad contra DW_PRINCIPAL (CREATE OR ALTER PROCEDURE real). Si no, solo
    devuelve el SQL para revisar antes de confirmar."""
    sql = generar_procedimiento_completo()
    if not ejecutar:
        return {"sql": sql, "ejecutado": False}

    cn = _conectar_dw_uci()
    try:
        cur = cn.cursor()
        cur.execute(sql)
        cn.commit()
        return {"sql": sql, "ejecutado": True}
    finally:
        cn.close()


def listar_procedimientos_calidad() -> list:
    """Todos los Sp_Calidad_<revisor> YA en produccion en DW_PRINCIPAL (el sistema
    nativo, escrito a mano, anterior a Sp_Calidad_AgenteIA -- ej.
    Sp_Calidad_RevisorA/Sonia/Elena/Estape...). Solo lectura."""
    cn = _conectar_dw_uci()
    try:
        cur = cn.cursor()
        cur.execute(r"""
            SELECT p.name, OBJECT_DEFINITION(p.object_id)
            FROM sys.procedures p
            WHERE p.name LIKE 'Sp\_Calidad\_%' ESCAPE '\'
            ORDER BY p.name
        """)
        return [{"nombre": r[0], "definicion": r[1]} for r in cur.fetchall()]
    finally:
        cn.close()


def buscar_bloque_real(id_calidad_tabla_atributo) -> dict:
    """Busca, entre los Sp_Calidad_<revisor> ya en produccion, si alguno ya
    tiene un bloque IF (@Id_Calidad_Tabla_Atributo = <id> ...) para esta
    regla -- para comparar el bloque escrito a mano por un humano contra lo
    que propone/genera el LLM. {"procedimiento": None, "bloque": None} si
    ninguno la tiene todavia."""
    id_ = str(id_calidad_tabla_atributo)
    patron_cabecera = re.compile(r"IF\s*\(\s*@Id_Calidad_Tabla_Atributo\s*=\s*(\d+)", re.IGNORECASE)
    for proc in listar_procedimientos_calidad():
        definicion = proc["definicion"] or ""
        cabeceras = list(patron_cabecera.finditer(definicion))
        for i, m in enumerate(cabeceras):
            if m.group(1) == id_:
                inicio = m.start()
                fin = cabeceras[i + 1].start() if i + 1 < len(cabeceras) else len(definicion)
                return {"procedimiento": proc["nombre"], "bloque": definicion[inicio:fin].strip()}
    return {"procedimiento": None, "bloque": None}
