#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Soporte para el mecanismo JQL (reglas parametricas) del motor de calidad
nativo -- un DSL propio (acuñado "JQL" en el código, obra de el responsable de datos),
interpretado por APP_CATALOGO.dbo.Sp_Calidad_Ejecuta_Regla_Parametrica[_Base].

ORIGEN: la gramatica, las constantes y validar_jql() son una COPIA -- no un
import en vivo -- de activacion/jql.py del proyecto hermano ProyectoHermano
(C:\\ProyectoHermano en 10.0.0.10, colega del equipo), concretamente de su
src/generador_jql.py y docs/HALLAZGOS_MANUALES.md (leidos el 2026-08-07,
contrastados contra los manuales oficiales: Documentacion_Reglas_Parametricas.docx
y Documentacion_Validacion_Datos_Ampliada_v2_1.docx). Se copia a proposito en
vez de importar para no acoplar este proyecto al suyo -- si su codigo cambia,
esta copia NO cambia sola, hay que releerla a mano.

DOS FORMAS DE USAR JQL EN EL MOTOR:
  1. Sp_Calidad_Ejecuta_Regla_Parametrica(@Fecha, @Id_Calidad_Tabla_Atributo,
     @Usuario, @Origen, @Pruebas) -- lee el JQL de
     Tb_Calidad_Tabla_Atributo.Consulta_SQL_Base. EXIGE publicar el JQL ahi
     primero (escritura en tabla compartida de produccion). NO SE USA AQUI
     TODAVIA -- publicar es una decision aparte, deliberada, de una sesion
     futura.
  2. Sp_Calidad_Ejecuta_Regla_Parametrica_Base(@Fecha,
     @Id_Calidad_Tabla_Atributo, @CLAVE, @JQL, @Usuario, @Origen, @Pruebas)
     -- recibe el JQL INLINE, NO requiere publicacion. Con
     @Id_Calidad_Tabla_Atributo = NULL no escribe en el log. Es la via
     "medir primero, publicar despues": lo que usa probar_jql_inline() de
     este modulo.

Hallazgos criticos de los manuales (docs/HALLAZGOS_MANUALES.md del proyecto
hermano) que hacen que validar_jql() sea imprescindible:
  - BOOLEANO no marca como invalidos los valores fuera de lista (los
    normaliza a 0/1 sin avisar) -- una regla de "valores validos" con
    BOOLEANO mediria 100% de cumplimiento SIEMPRE, falso. Usar LISTA con
    ErrorNotIn:S en su lugar.
  - MaxDate:99991231 es un GETDATE() disfrazado: el motor lo traduce a
    31/12 del año siguiente, asi que un artefacto con ese literal mide un
    rango distinto cada año.
  - Los valores por defecto del motor (Nulos:S, EnDimTiempo:S, Recorte:S,
    MinDate:19890101...) NO son neutros -- hay que emitirlos siempre
    explicitos.
  - Blanqueo solo se admite como "[original]" (marca la fila como invalida
    sin tocar el valor); cualquier otro valor esta prohibido.
  - NUMERO SI admite precision decimal via la etiqueta `Dec:<n>` (mapea a
    @NumDecimales de Sp_Calidad_Numero, igual que num_decimales en el
    mecanismo generico) -- confirmado releyendo
    Sp_Calidad_Ejecuta_Regla_Parametrica_Base tras el hallazgo del usuario:
    ni el bloque de comentarios "Formato esperado" de ese mismo
    procedimiento ni los manuales oficiales la mencionan, pero SI esta
    implementada y conectada (EXEC ... @NumDecimales = @DecN). Omitir Dec
    es neutro (NULL = sin comprobacion), a diferencia de Nulos/MinDate/etc.
    Este hallazgo probablemente NO estaba cuando se audito este modulo por
    primera vez -- el motor cambia sin avisar, por eso esta copia puede
    quedarse corta en cualquier momento; si algo no cuadra con lo que
    devuelve el motor de verdad, releer el procedimiento antes de asumir
    que el LLM se equivoco.
  - NUEVO (encontrado leyendo Sp_Calidad_Ejecuta_Regla_Parametrica_Base en
    APP_CATALOGO el 2026-08-11, NO documentado en HALLAZGOS_MANUALES.md
    del proyecto hermano -- bug real del motor, no de este repo): en las
    ramas TEXTO y LISTA, el @AdmiteNull que se pasa al validador real es el
    de OTRA rama (NUMERO para TEXTO, BOOLEANO para LISTA) por un
    copy-paste, nunca el que la propia rama calcula a partir de "Nulos:".
    Esa variable nunca se asigna, asi que llega NULL, y "@AdmiteNull = 0"
    con NULL nunca es verdadero. Efecto:
      * TEXTO: "Nulos:N" NO FUNCIONA -- un NULL nunca se marca invalido,
        pase lo que pase. validar_jql() bloquea esta combinacion.
      * LISTA: parcialmente enmascarado (ErrorNotIn:S por defecto ya trata
        un NULL como "no esta en la lista"), pero si de verdad hace falta
        "Nulos:S" (nulos permitidos) hay que añadir tambien "[NULO]" a
        Valores -- el flag Nulos por si solo no lo garantiza en LISTA.
    Mitigacion: el validador GENERICO (Sp_Calidad_Texto/Sp_Calidad_Lista)
    NO pasa por este procedimiento -- generador_sp_agente_ia.py lo llama
    directo con el @AdmiteNull correcto. Para TEXTO con Nulos:N, usar
    "generico" en vez de JQL.

Uso como modulo:
  from activacion import jql
  jql.validar_jql("NUMERO Nulos:S Min:0 Clave:Id_Operacion,Id_Pais")
  resultado = jql.probar_jql_inline(clave_fisico, "NUMERO Nulos:S Min:0 Clave:...")
"""

import re

# ----------------------- gramatica (copia, ver docstring) ------------------ #
ETIQUETAS_VALIDAS = {
    "Nulos", "Blanqueo", "Min", "Max", "Dec", "MinLen", "MaxLen", "Recorte",
    "Valores", "Separador", "ErrorNotIn", "FalseNotIn", "DefaultTrue",
    "DefaultFalse", "EnDimTiempo", "MinDate", "MaxDate", "Duplicados",
    "Clave", "Filtro",
}
TIPOS_VALIDOS = {"NUMERO", "TEXTO", "FECHA", "BOOLEANO", "LISTA", "UNICO"}

# Ninguno es neutro: omitir el parametro no desactiva la comprobacion, la
# deja en este valor. Nulos:S = no comprueba completitud; EnDimTiempo:S =
# ADEMAS valida contra Tb_Dim_Tiempo; Recorte:S = MinLen/MaxLen se miden ya
# recortado; MinDate:19890101 = inicio de actividad de la entidad, no "sin limite".
DEFECTOS_DEL_MOTOR = {
    "Nulos": "S", "EnDimTiempo": "S", "Recorte": "S", "Duplicados": "S",
    "Separador": ",", "ErrorNotIn": "S", "FalseNotIn": "S",
    "MinDate": "19890101", "MaxDate": "99991231",
}

BLANQUEO_SIN_TOCAR = "[original]"
MAXDATE_DINAMICO = "99991231"
ETIQUETAS_VALOR_BOOLEANO = {"Valores", "Separador", "FalseNotIn",
                            "DefaultTrue", "DefaultFalse"}


def limpiar_jql(jql: str) -> str:
    """Quita etiquetas cuyo valor es el literal 'null'/'none' (en cualquier
    combinacion de mayusculas). La gramatica no tiene ese valor: "sin limite"
    se expresa OMITIENDO la etiqueta, no escribiendo Max:null. El LLM a veces
    lo confunde con JSON -- normalizar aqui evita depender de que el motor
    tolere un valor que no documenta ningun manual."""
    if not jql:
        return jql
    partes = jql.strip().split(" ", 1)
    if len(partes) < 2:
        return jql
    tipo, resto = partes
    # Filtro: va siempre al final y su valor SI puede llevar espacios (es una
    # condicion SQL), asi que se aparta antes de tokenizar -- igual que hace
    # validar_jql(). El resto del cuerpo no lleva espacios dentro de un valor.
    cuerpo, hay_filtro, filtro = resto.partition("Filtro:")
    limpios = []
    for tok in cuerpo.split():
        m = re.match(r"^([A-Za-z]+):(.*)$", tok)
        if m and m.group(2).strip().lower() in ("null", "none", ""):
            continue
        limpios.append(tok)
    # Se rearma con un solo espacio: quitar un token dejaba antes los DOS
    # separadores que lo rodeaban ("Min:0  Clave:X"), y ese espacio doble
    # llegaba tal cual al motor.
    salida = (tipo + " " + " ".join(limpios)).strip()
    if hay_filtro and filtro.strip().lower() not in ("null", "none", ""):
        salida += " Filtro:" + filtro.strip()
    return salida


def validar_jql(jql: str) -> None:
    """Comprueba la cadena contra la gramatica ANTES de guardarla o
    ejecutarla. El motor lanza THROW 51000 ante una etiqueta desconocida --
    validar aqui convierte un fallo de ejecucion en un fallo de generacion.
    Lanza ValueError si algo no cumple; no devuelve nada si es valida."""
    partes = jql.strip().split(" ", 1)
    tipo = partes[0].upper()
    if tipo not in TIPOS_VALIDOS:
        raise ValueError(f"Tipo JQL no valido: {tipo!r}")
    resto = partes[1] if len(partes) > 1 else ""

    cuerpo, _, _filtro = resto.partition("Filtro:")

    etiquetas = re.findall(r"(?:^|\s)([A-Za-z]+):", resto)
    for etiqueta in etiquetas:
        if etiqueta not in ETIQUETAS_VALIDAS:
            raise ValueError(
                f"Etiqueta JQL no reconocida por el motor: {etiqueta!r}. "
                f"Provocaria THROW 51000 en ejecucion.")

    valores = dict(re.findall(r"(?:^|\s)([A-Za-z]+):(\S*)", resto))

    if "Blanqueo" in valores and valores["Blanqueo"] != BLANQUEO_SIN_TOCAR:
        raise ValueError(
            f"Blanqueo:{valores['Blanqueo']!r} no esta permitido. El agente "
            f"detecta, no corrige: la unica forma admitida es "
            f"Blanqueo:{BLANQUEO_SIN_TOCAR}, que marca la fila como invalida "
            f"dejando el valor original.")

    if valores.get("MaxDate") == MAXDATE_DINAMICO:
        raise ValueError(
            f"MaxDate:{MAXDATE_DINAMICO} esta prohibido: el motor lo convierte a "
            f"31/12 del año siguiente, asi que el artefacto mediria un rango "
            f"distinto cada año. Para no acotar por arriba, omite MaxDate.")

    if tipo == "TEXTO" and valores.get("Nulos") == "N":
        raise ValueError(
            "TEXTO con Nulos:N: bug confirmado en "
            "Sp_Calidad_Ejecuta_Regla_Parametrica_Base (le pasa al validador el "
            "@AdmiteNull de la rama NUMERO, nunca el que calcula la propia rama "
            "TEXTO -- llega NULL, y un NULL en el dato nunca se marca invalido "
            "por mucho que Nulos:N lo pida). Usa el mecanismo 'generico' "
            "(validador Texto) en su lugar: llama a Sp_Calidad_Texto directo, "
            "sin pasar por este bug.")

    if tipo == "BOOLEANO":
        usadas = ETIQUETAS_VALOR_BOOLEANO & set(etiquetas)
        if usadas:
            raise ValueError(
                f"BOOLEANO con {sorted(usadas)}: el motor NO marca como invalidos "
                f"los valores fuera de lista en BOOLEANO, los normaliza a 0/1 y no "
                f"aparecen en #VALIDACIONES. La regla devolveria 0 KO siempre, o "
                f"sea un 100% de cumplimiento FALSO. Para validar valores usa "
                f"LISTA con ErrorNotIn:S. BOOLEANO solo vale para completitud.")

    for token in re.sub(r"\{\{[^}]*\}\}", "", cuerpo).split():
        if token.count(":") > 1:
            raise ValueError(
                f"El token {token!r} lleva mas de un ':'. El parser del motor "
                f"interpretaria la segunda parte como nombre de parametro y "
                f"fallaria con 'Parametros no reconocidos'.")

    if "Clave:" not in resto:
        raise ValueError("Falta la etiqueta Clave, obligatoria para el agente.")
    if re.search(r"[A-Za-z]+\s+:", resto):
        raise ValueError("Hay una etiqueta con espacio antes de ':'; el motor no "
                         "la reconoceria.")


# ------------------------ prueba inline (sin publicar) --------------------- #
SERVIDOR = r"SERVIDOR_DATOS\INSTANCIA"
BASE_DATOS = "APP_CATALOGO"
DRIVER = "ODBC Driver 17 for SQL Server"
USUARIO_EJECUCION = "AgenteIA"
ORIGEN_EJECUCION = "Prueba inline desde el agente de calidad (JQL, sin publicar)"
# Origen para las ejecuciones que SI se registran (registrar_ejecucion). Lleva
# el puntero a nuestra tabla porque Tb_Calidad_Resultado_Validacion_Pruebas
# guarda el resultado pero NO la sentencia ejecutada ni la poblacion: sin esta
# pista, desde una fila de resultado no hay forma de llegar al artefacto que
# la produjo. Se cierra el circulo por Id_Calidad_Tabla_Atributo.
ORIGEN_REGISTRO = ("Agente de calidad (JQL) -- sentencia y poblacion en "
                   "WORKSPACE_AGENTE.dbo.CAL_Bloques_JQL por Id_Calidad_Tabla_Atributo")


def _conectar():
    import pyodbc
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SERVIDOR};DATABASE={BASE_DATOS};Trusted_Connection=yes;",
        timeout=30)


# Etiquetas que de verdad pueden marcar una fila como invalida, por tipo. Solo
# NUMERO y TEXTO: en los demas hay defectos del motor que YA comprueban algo
# (FECHA trae EnDimTiempo:S y MinDate:19890101 de serie, ver DEFECTOS_DEL_MOTOR),
# asi que un JQL "pelado" de esos tipos no es necesariamente un no-op.
ETIQUETAS_QUE_COMPRUEBAN = {
    "NUMERO": {"Min", "Max", "Dec"},
    "TEXTO": {"MinLen", "MaxLen"},
}

# OJO, no añadir aqui una guarda del tipo "la regla dice 'no puede estar
# vacio' => el JQL tiene que llevar Nulos:N": se probo contra el lote del
# 2026-08-18 y bloqueaba traducciones que el revisor habia dado por BUENAS
# (reglas 2089 y 2090, ambas "no puede estar vacio", donde su respuesta es
# "NUMERO Nulos:S Min:0"). En estas tablas "vacio" no siempre significa NULL,
# depende de la columna, y el texto de la regla por si solo no lo decide.
# verificar_jql_mide_algo() si aguanta ese contraste: distingue esos mismos
# casos por si hay o no una comprobacion real dentro.

_RE_DEC = re.compile(r"(?:^|\s)Dec:")
# "entero" / "decimal(es)" / "sin decimales" / "precision de N decimales" (con
# y sin tilde, y con el "presición" mal escrito que aparece en el catalogo real).
_RE_REGLA_DECIMALES = re.compile(r"\b(entero|entera|decimal|decimales)\b")


def _sin_tildes(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


def verificar_jql_mide_algo(jql: str) -> None:
    """Bloquea un JQL que no puede marcar ni una fila, mire los datos que
    mire. Es un chequeo ESTRUCTURAL, sin mirar el texto de la regla: si el
    tipo es NUMERO o TEXTO, `Nulos` es S (no comprueba completitud) y no hay
    ninguna etiqueta de las que de verdad validan algo
    (ETIQUETAS_QUE_COMPRUEBAN), la regla dara 0 KO SIEMPRE -- un 100% de
    cumplimiento que no significa nada.

    Visto en el lote del 2026-08-18: las reglas 52 y 53 ("el campo no debe
    estar vacio") acabaron en "NUMERO Nulos:S Clave:..." -- literalmente sin
    ninguna comprobacion dentro -- y pasaron la prueba inline con 0 filas KO
    sin que saltara nada.

    Solo cubre NUMERO y TEXTO a proposito: FECHA arrastra EnDimTiempo:S y
    MinDate:19890101 por defecto (ver DEFECTOS_DEL_MOTOR), asi que aunque
    parezca pelado si comprueba cosas; LISTA y UNICO dependen de defectos
    parecidos y no se tocan aqui para no bloquear traducciones validas."""
    if not jql:
        return
    partes = jql.strip().split(" ", 1)
    tipo = partes[0].upper()
    if tipo not in ETIQUETAS_QUE_COMPRUEBAN:
        return
    resto = partes[1] if len(partes) > 1 else ""
    cuerpo, _, _filtro = resto.partition("Filtro:")
    presentes = set(re.findall(r"(?:^|\s)([A-Za-z]+):", cuerpo))
    valores = dict(re.findall(r"(?:^|\s)([A-Za-z]+):(\S*)", cuerpo))

    if valores.get("Nulos", DEFECTOS_DEL_MOTOR["Nulos"]).upper() == "N":
        return  # comprueba completitud, ya puede marcar filas
    if presentes & ETIQUETAS_QUE_COMPRUEBAN[tipo]:
        return

    raise ValueError(
        f"Este JQL no comprueba nada: {tipo} con Nulos:S y sin ninguna de "
        f"{sorted(ETIQUETAS_QUE_COMPRUEBAN[tipo])}. No puede marcar ni una "
        f"fila, mire los datos que mire, asi que daria un 100% de "
        f"cumplimiento FALSO pase lo que pase. Si la regla exige que el campo "
        f"tenga valor usa Nulos:N; si exige un rango/formato, añade la "
        f"etiqueta que lo mida.")


def verificar_dec_necesario(desc_funcional: str, jql: str) -> None:
    """Bloquea el caso "la regla habla de decimales pero el JQL no los mide".

    Omitir `Dec` es NEUTRO en el motor (NULL = no comprueba nada), a
    diferencia de Nulos/MinDate. Asi que una regla como "tiene que ser un
    numero entero" traducida a "NUMERO Nulos:S Min:0 Clave:..." (sin Dec:0)
    se ejecuta sin error, no marca ni una fila, y reporta un 100% de
    cumplimiento FALSO -- la regla no comprueba lo unico que tenia que
    comprobar. Es el mismo fallo silencioso que ya se bloquea para BOOLEANO
    con valores y para MaxDate:99991231, y no lo detecta validar_jql()
    porque la cadena es gramaticalmente correcta: solo se ve comparandola
    con el texto de la regla.

    Visto de verdad en el lote del 2026-08-18: las reglas 1081 ("tiene que
    ser un numero entero"), 1083 ("no puede tener decimales") y 2224
    ("precision de 3 decimales") pasaron la prueba inline con 0 filas KO y
    confianza alta, todas sin Dec.

    No hace nada si no hay descripcion, si el tipo no es NUMERO, o si el JQL
    ya trae Dec."""
    if not desc_funcional or not jql:
        return
    if jql.strip().split(" ", 1)[0].upper() != "NUMERO":
        return
    if _RE_DEC.search(jql):
        return
    if not _RE_REGLA_DECIMALES.search(_sin_tildes(desc_funcional).lower()):
        return
    raise ValueError(
        f"La regla habla de decimales/enteros ({desc_funcional.strip()[:80]!r}) "
        f"pero el JQL no lleva la etiqueta Dec. Omitir Dec es NEUTRO en el "
        f"motor: la regla se ejecutaria sin error, no marcaria ninguna fila y "
        f"daria un 100% de cumplimiento FALSO sin comprobar lo unico que tenia "
        f"que comprobar. Añade Dec:0 para 'numero entero' o Dec:<n> para 'n "
        f"decimales'. (La etiqueta es 'Dec', no 'Decimales'.)")


def _verificar_clave_real(clave_fisico: str, jql: str) -> None:
    """Comprueba que el valor de la etiqueta Clave: sean nombres de columna
    que existen de verdad en la tabla destino -- validar_jql() NO puede
    hacer esto (es una copia de la gramatica del motor, sin acceso a
    esquema, ver docstring del modulo). Sin este chequeo, un LLM sin
    clave_cols real (tabla sin PK/indice unico, ver
    generador_sp_agente_ia.columnas_clave_de) puede copiar la etiqueta
    legible de clave_fisico como si fuera un nombre de columna (visto en la
    regla 1082: 'Clave:MESES Nº Impagos Expte (SREJPA)', el texto entre
    comillas de Clave_Fisico, no una columna), y el motor lo acepta sin
    fallar -- devuelve 'ok' con 0 filas KO porque @CLAVE solo se usa para
    identificar filas en el resultado, no para decidir si la regla se
    cumple. Una clave incompleta pero real (p.ej. 'Id_Pais' solo) no la
    detecta esto -- eso requeriria conocer la clave de negocio completa, que
    no esta disponible en ningun sitio para estas tablas."""
    from activacion import generador_sp_agente_ia as gen
    valores = dict(re.findall(r"(?:^|\s)([A-Za-z]+):(\S*)", jql))
    clave = valores.get("Clave", "")
    if not clave:
        return
    try:
        partes = gen.parsear_clave(clave_fisico)
        columnas_reales = {c["nombre"].lower() for c in gen.columnas_de(
            partes["esquema"], partes["tabla"], base_datos=partes["base_datos"]) or []}
    except Exception:
        return  # sin esquema alcanzable, no se puede verificar -- no bloquea
    if not columnas_reales:
        return
    for nombre in clave.split(","):
        if nombre.strip().lower() not in columnas_reales:
            raise ValueError(
                f"Clave:{clave!r} incluye {nombre.strip()!r}, que no es una "
                f"columna real de {partes['tabla']}. El motor lo aceptaria "
                f"sin fallar (@CLAVE solo identifica filas en el resultado, "
                f"no afecta si la regla se cumple), asi que un nombre "
                f"inventado pasaria como 'correcto' sin que nadie lo note.")


def probar_jql_inline(clave_fisico: str, jql: str, pruebas: bool = True,
                       desc_funcional: str = None,
                       id_calidad_tabla_atributo: int = None) -> dict:
    """Ejecuta el JQL de verdad contra los datos reales via
    Sp_Calidad_Ejecuta_Regla_Parametrica_Base, SIN escribir en
    Tb_Calidad_Tabla_Atributo.Consulta_SQL_Base -- la via "medir primero,
    publicar despues" del proyecto hermano. @Id_Calidad_Tabla_Atributo=NULL
    para no escribir en el log; @Pruebas=True por si acaso.

    Normaliza con limpiar_jql() y valida con validar_jql() ANTES de mandarlo
    al motor -- un JQL mal formado fallaria en ejecucion con un THROW poco
    claro; aqui se detecta antes y con un mensaje explicable. Tambien
    verifica con _verificar_clave_real() que Clave: sean columnas reales,
    no solo que la etiqueta este presente (ver su docstring).

    Ademas de la gramatica, pasa dos chequeos contra el "100% de cumplimiento
    falso": verificar_jql_mide_algo() (estructural: el JQL no puede marcar ni
    una fila) y verificar_dec_necesario() (compara la cadena con lo que pedia
    la regla).

    desc_funcional: texto original de la regla, si se tiene. Lo necesita
    verificar_dec_necesario(); sin el se salta (el estructural sigue
    corriendo).

    id_calidad_tabla_atributo: por DEFECTO None, y eso es deliberado. Leido
    en el motor real el 2026-09-01 (Sp_Calidad_Numero y los demas
    validadores):

        IF NOT @Id_Calidad_Tabla_Atributo IS NULL
            EXEC APP_CATALOGO..Sp_Calidad_Ejecuta_Regla ...   -- registra
        ELSE
            SELECT @Recuento, * FROM #VALIDACIONES               -- solo devuelve

    O sea que con None la ejecucion NO deja rastro, que es lo que interesa
    para las pruebas automaticas de cada propuesta (un lote son cientos de
    ejecuciones y no tiene sentido llenar una tabla compartida con ellas).
    Pasando el Id real, el motor SI registra el resultado; y como aqui
    siempre se manda @Pruebas=1, va a Tb_Calidad_Resultado_Validacion_Pruebas
    y NUNCA al log real de produccion (Sp_Calidad_Ejecuta_Regla:
    "IF ISNULL(@Pruebas,1) = 0" elige entre una tabla y otra).
    Ese camino es el de registrar_ejecucion(), que se lanza a mano.

    Devuelve {"ok": bool, "jql": <cadena realmente ejecutada>, "filas": [...],
    "error": str|None}."""
    jql = limpiar_jql(jql)
    validar_jql(jql)
    verificar_jql_mide_algo(jql)
    verificar_dec_necesario(desc_funcional, jql)
    _verificar_clave_real(clave_fisico, jql)

    cn = _conectar()
    try:
        cur = cn.cursor()
        origen = (ORIGEN_REGISTRO if id_calidad_tabla_atributo is not None
                  else ORIGEN_EJECUCION)
        cur.execute(
            "EXEC dbo.Sp_Calidad_Ejecuta_Regla_Parametrica_Base "
            "@Fecha = ?, @Id_Calidad_Tabla_Atributo = ?, @CLAVE = ?, @JQL = ?, "
            "@Usuario = ?, @Origen = ?, @Pruebas = ?",
            __import__("datetime").datetime.now(), id_calidad_tabla_atributo,
            clave_fisico, jql, USUARIO_EJECUCION, origen, 1 if pruebas else 0)
        filas = []
        if cur.description:
            cols = [c[0] for c in cur.description]
            filas = [dict(zip(cols, r)) for r in cur.fetchall()]
        cn.commit()
        return {"ok": True, "jql": jql, "filas": filas, "error": None}
    except Exception as e:
        return {"ok": False, "jql": jql, "filas": [], "error": str(e)}
    finally:
        cn.close()


def filtro_de(jql: str) -> str:
    """La poblacion acotada de una sentencia JQL: el valor de la etiqueta
    Filtro:, que va siempre al final y puede llevar espacios (es una condicion
    SQL). None si la regla aplica a toda la tabla."""
    if not jql:
        return None
    _cuerpo, hay, filtro = jql.partition("Filtro:")
    if not hay:
        return None
    return filtro.strip() or None


def registrar_ejecucion(id_calidad_tabla_atributo: int, clave_fisico: str, jql: str,
                         desc_funcional: str = None) -> dict:
    """Ejecuta un JQL DEJANDO CONSTANCIA en el log de pruebas del motor
    (APP_CATALOGO.dbo.Tb_Calidad_Resultado_Validacion_Pruebas), para poder
    replicar y auditar la ejecucion despues -- lo que pidio el responsable de datos el
    2026-08-03 en la historia #42164.

    Se lanza A MANO, nunca desde la traduccion automatica: un lote de
    evaluacion son cientos de ejecuciones y no tiene sentido volcarlas todas
    a una tabla compartida con el resto del equipo. Por eso probar_jql_inline()
    sigue sin registrar por defecto.

    Escribe SOLO en la tabla de PRUEBAS, nunca en el log real: se manda
    siempre @Pruebas=1, y es ese flag el que el motor usa para elegir tabla
    (Sp_Calidad_Ejecuta_Regla, "IF ISNULL(@Pruebas,1) = 0").

    No hay riesgo de doble ejecucion: Sp_Calidad_Ejecuta_Regla solo re-lanza
    la regla desde el catalogo cuando le llega la temporal vacia, y por esta
    via siempre llega con '#VALIDACIONES'.

    Devuelve lo mismo que probar_jql_inline mas 'registrado'."""
    resultado = probar_jql_inline(clave_fisico, jql, pruebas=True,
                                   desc_funcional=desc_funcional,
                                   id_calidad_tabla_atributo=id_calidad_tabla_atributo)
    resultado["registrado"] = resultado.get("ok", False)
    resultado["tabla_log"] = ("APP_CATALOGO.dbo.Tb_Calidad_Resultado_Validacion_Pruebas"
                              if resultado["registrado"] else None)
    resultado["filtro_poblacion"] = filtro_de(resultado.get("jql") or jql)
    return resultado


# --------------------- persistencia de JQL aprobado ------------------------ #
# Deliberadamente SEPARADA de CAL_Bloques_Sp_AgenteIA (generador_sp_agente_ia.py):
# esa tabla es solo para mecanismos que SI generan SQL a mano (generico/
# sql_personalizada); JQL no genera ningun bloque T-SQL, solo la sentencia
# JQL en si, y vive en su propia tabla para no forzar columnas que no le
# aplican (Bloque_SQL no tiene sentido para una regla JQL).
WS_SERVIDOR = r"SERVIDOR_WORKSPACE"
WS_BASE_DATOS = "WORKSPACE_AGENTE"
WS_DRIVER = "ODBC Driver 17 for SQL Server"

_DDL_TABLA_JQL = """
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'CAL_Bloques_JQL' AND schema_id = SCHEMA_ID('dbo'))
BEGIN
    CREATE TABLE dbo.CAL_Bloques_JQL (
        Id_Calidad_Regla            INT,
        Desc_Funcional              NVARCHAR(MAX),
        Id_Calidad_Tabla_Atributo   INT NOT NULL PRIMARY KEY,
        JQL                         NVARCHAR(MAX) NOT NULL,
        Filtro_Poblacion            NVARCHAR(MAX),
        Clave_Fisico                NVARCHAR(512),
        Aprobado_Por                NVARCHAR(128),
        Fecha_Aprobacion            DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    )
END
"""

# Migracion para la tabla YA desplegada con datos dentro: ALTER aparte del
# CREATE de arriba, mismo patron que las columnas de etiquetado humano en
# evaluar_traduccion.py. Sin Desc_Funcional la tabla guardaba solo
# identificadores y la sentencia JQL, asi que no se podia leer QUE dice la
# regla sin volver al catalogo -- justo lo que se necesita para auditarla.
_DDL_COLUMNA_DESC = """
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.CAL_Bloques_JQL') AND name = 'Desc_Funcional')
BEGIN
    ALTER TABLE dbo.CAL_Bloques_JQL ADD Desc_Funcional NVARCHAR(MAX) NULL
END
"""

# Filtro_Poblacion sale de la etiqueta Filtro: del propio JQL, asi que es
# informacion redundante... pero solo LEGIBLE, no consultable: dentro de la
# cadena no se puede preguntar "que reglas miden sobre poblacion acotada", y
# esa es justo la mitad de la trazabilidad que pidio el responsable de datos. Se extrae a su
# propia columna para poder filtrar y auditar por ella.
_DDL_COLUMNA_FILTRO = """
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.CAL_Bloques_JQL') AND name = 'Filtro_Poblacion')
BEGIN
    ALTER TABLE dbo.CAL_Bloques_JQL ADD Filtro_Poblacion NVARCHAR(MAX) NULL
END
"""


def _conectar_workspace():
    import pyodbc
    return pyodbc.connect(
        f"DRIVER={{{WS_DRIVER}}};SERVER={WS_SERVIDOR};DATABASE={WS_BASE_DATOS};Trusted_Connection=yes;",
        timeout=10)


def _id_regla_de(id_calidad_tabla_atributo: int):
    """Id_Calidad_Regla real (Tb_Calidad_Regla), via el FK en
    Tb_Calidad_Tabla_Atributo -- las propuestas solo guardan
    Id_Calidad_Tabla_Atributo, hay que resolverlo aparte. None si no se
    encuentra (no bloquea el guardado del JQL por esto)."""
    cn = _conectar()
    try:
        cur = cn.cursor()
        cur.execute("SELECT Id_Calidad_Regla FROM dbo.Tb_Calidad_Tabla_Atributo "
                    "WHERE Id_Calidad_Tabla_Atributo = ?", id_calidad_tabla_atributo)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cn.close()


def guardar_jql(id_calidad_tabla_atributo: int, jql_texto: str, clave_fisico: str,
                 aprobado_por: str, desc_funcional: str = None) -> None:
    """Persiste una regla JQL ya aprobada en dbo.CAL_Bloques_JQL (workspace
    propio, NO Tb_Calidad_Tabla_Atributo.Consulta_SQL_Base -- eso sigue
    siendo 'publicar', decision aparte todavia pendiente). Un INSERT/UPDATE
    por Id_Calidad_Tabla_Atributo: aprobar de nuevo la misma regla
    sobrescribe la version anterior, igual que guardar_bloque() en
    generador_sp_agente_ia.py.

    La fila queda con las cuatro cosas que hacen falta para auditar una regla
    sin salir de la tabla: el id de la regla de negocio, SU TEXTO, el id del
    atributo sobre el que aplica, y el JQL con el que se implemento.

    desc_funcional: texto original de la regla. Es opcional para no romper
    llamadas antiguas, pero conviene pasarlo siempre: sin el la fila guarda
    solo identificadores y hay que ir al catalogo para saber que valida."""
    id_regla = _id_regla_de(id_calidad_tabla_atributo)
    cn = _conectar_workspace()
    try:
        cur = cn.cursor()
        cur.execute(_DDL_TABLA_JQL)
        cur.execute(_DDL_COLUMNA_DESC)
        cur.execute(_DDL_COLUMNA_FILTRO)
        filtro = filtro_de(jql_texto)
        cur.execute("""
            MERGE dbo.CAL_Bloques_JQL AS destino
            USING (SELECT ? AS Id_Calidad_Tabla_Atributo) AS origen
                ON destino.Id_Calidad_Tabla_Atributo = origen.Id_Calidad_Tabla_Atributo
            WHEN MATCHED THEN UPDATE SET
                Id_Calidad_Regla = ?, Desc_Funcional = ?, Clave_Fisico = ?, JQL = ?,
                Filtro_Poblacion = ?, Aprobado_Por = ?, Fecha_Aprobacion = SYSDATETIME()
            WHEN NOT MATCHED THEN INSERT
                (Id_Calidad_Tabla_Atributo, Id_Calidad_Regla, Desc_Funcional, Clave_Fisico,
                 JQL, Filtro_Poblacion, Aprobado_Por)
                VALUES (?, ?, ?, ?, ?, ?, ?);
        """, id_calidad_tabla_atributo,
             id_regla, desc_funcional, clave_fisico, jql_texto, filtro, aprobado_por,
             id_calidad_tabla_atributo, id_regla, desc_funcional, clave_fisico,
             jql_texto, filtro, aprobado_por)
        cn.commit()
    finally:
        cn.close()
