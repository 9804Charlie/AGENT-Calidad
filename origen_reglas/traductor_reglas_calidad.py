#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Traduce una regla de calidad en TEXTO LIBRE (Desc_Funcional, tal como la
escribe el equipo de negocio en la lista de SharePoint) a una regla
ESTRUCTURADA que entiende motor_reglas_calidad.py (ver
contrato_reglas_calidad_schema.json).

El LLM local (phi4 via Ollama, mismo patron que agente_calidad_datos.py) SOLO
PROPONE: el resultado se registra SIEMPRE como propuesta pendiente
(registro_propuestas_reglas.py) para que el Comite del dato la revise antes de
activarla. Una mala traduccion aqui significaria SQL incorrecto ejecutandose
contra datos reales -- por eso no hay atajo que la salte.

Uso como modulo:
  from origen_reglas.traductor_reglas_calidad import traducir
  propuesta = traducir("Capital Riesgo neto (CRN)", "El valor tiene que ser mayor de 0",
                        candidatas=[{"clave": "...", "tabla": "...", "confianza": 0.9}])
"""

import json
import re

# Este agente corre como SERVICIO PROPIO (ver api_calidad.py, puerto 5001) en
# la MISMA maquina que Ollama (10.0.0.10) -- independiente de la app de
# produccion del diccionario. Por eso se llega a Ollama por localhost directo.
OLLAMA_CHAT = "http://localhost:11434/v1/chat/completions"
# Modelo derivado de phi4:14b-q8_0 -- MISMOS pesos, solo cambia num_ctx a 8192.
# El defecto de Ollama son 4096, y el prompt de traduccion ya son ~4200 tokens
# (13.477 caracteres solo de instrucciones), asi que se estaba TRUNCANDO: en el
# lote del 2026-08-18, 64 de 113 intentos reportaron exactamente 4095 tokens de
# entrada, o sea el techo. Parte del prompt no le llegaba al modelo, lo que
# explica que "pierda el formato de salida" en tablas anchas y que reforzar el
# prompt no cambiara nada (el texto anadido caia fuera de la ventana).
# phi4 soporta 16384 de forma nativa; se queda en 8192 porque el servidor tenia
# 3,1 GB de RAM libre y la cache KV se reserva entera al cargar.
# NO se toca la etiqueta phi4:14b-q8_0 original: la usa tambien el agente del
# diccionario (rama main), que comparte esta maquina.
MODELO_LLM = "phi4-calidad"

# Umbral a partir del cual un esquema es "ancho": calibrado el 2026-08-18
# contra Tb_Fact_Expediente_Historia (86 columnas), donde el LLM devolvia un
# JSON valido pero con un formato INVENTADO (sin "mecanismo") en la llamada
# de traduccion completa -- sin llegar a tocar el timeout (160s/293s de 600s
# disponibles), asi que no era un corte a mitad de respuesta, era perder de
# vista el formato de salida con un prompt demasiado grande. 32 columnas
# (Tb_Fact_Expediente_Actual) siempre funciono bien. Se usa TANTO para
# decidir si merece la pena el paso previo de _seleccionar_columnas_relevantes
# como para el aviso_contexto de traducir_condicion_sql (que ahora es sobre
# todo una red de seguridad para cuando ese paso previo no reduce lo bastante).
# Bajado de 50 a 20 el 2026-09-01: el 50 se calibro cuando el filtrado era
# solo una RED DE SEGURIDAD para que el LLM no perdiera el formato en tablas
# enormes. Ahora se busca ademas dejarle margen de ventana, asi que conviene
# recortar el esquema mucho antes. Medido sobre las 12 tablas del catalogo de
# pruebas (153, 114, 98, 93, 86, 60, 46, 41, 32, 22, 17 y 9 columnas): con 50
# filtraban 6 de 12; con 20 filtran 10 de 12. Las dos que quedan fuera (17 y 9
# columnas) aportan menos de 250 tokens de esquema, que no compensan una
# llamada extra al LLM de 1-4 minutos.
UMBRAL_COLUMNAS_CONTEXTO = 20

# Ventana de contexto REAL con la que corre el modelo: el num_ctx del modelo
# derivado phi4-calidad. Si se cambia alla (o se cambia MODELO_LLM), hay que
# cambiarlo aqui -- es lo que decide cuando avisar de truncamiento.
VENTANA_CONTEXTO = 8192
# El motor reporta como maximo la ventana menos uno (medido: 4095 con una
# ventana de 4096), y ademas la respuesta necesita sitio, asi que no hace
# falta clavar el numero exacto para dar por seguro que se corto el prompt.
MARGEN_VENTANA = 32

_SISTEMA = """Eres un experto en calidad de datos de una entidad hipotecaria (contexto
regulatorio IFRS9). Se te da una regla de calidad escrita en TEXTO LIBRE por el
equipo de negocio (en una lista de SharePoint), y una lista de columnas FISICAS
candidatas a las que podria referirse. Tu trabajo es traducirla a una regla
ESTRUCTURADA ejecutable.

Devuelve UNICAMENTE un objeto JSON valido, sin markdown ni texto alrededor:
{
  "tipo": "no_nulo" | "rango" | "valores_permitidos" | "patron_like" | "unico" | "condicional" | "sql_personalizada",
  "columna": "<nombre de columna simple, para tipos de una sola columna>",
  "columnas": ["<col1>", "<col2>"],
  "min": <numero o ausente>,
  "max": <numero o ausente>,
  "valores": ["<valor1>", "..."],
  "patron": "<patron T-SQL LIKE, ej. '%@%' o '________' para longitud exacta>",
  "condicion_si": "<fragmento SQL booleano>",
  "condicion_entonces": "<fragmento SQL booleano>",
  "condicion": "<fragmento SQL booleano arbitrario>",
  "descripcion": "<explicacion breve en negocio de la regla>",
  "severidad": "alta" | "media" | "baja",
  "confianza": <0.0 a 1.0>,
  "columna_elegida": "<CLAVE EXACTA elegida de las candidatas dadas, o null>"
}

REGLAS DE TRADUCCION (criticas, aprendidas de errores reales):
- "rango" usa limites INCLUSIVOS (>=, <=). Si el texto dice "mayor que" o "menor
  que" en sentido ESTRICTO (sin incluir el limite), NO uses "rango": usa
  "sql_personalizada" con la condicion exacta (ej. "Columna > 0").
- Comprobaciones de existencia contra una tabla/dimension (ej. "que exista en
  la dimension tiempo/provincias"), formulas que combinan varias columnas (ej.
  "A + B - C"), precision decimal, o "debe ser numerico"/formato: SIEMPRE
  "sql_personalizada" con la condicion SQL mas fiel posible y una descripcion
  clara. NO inventes nombres de tablas de dimension que no te den: si no
  puedes escribir la condicion completa, bajA la confianza y dejalo lo mas
  fiel posible en la descripcion.
- "columna_elegida" DEBE ser una de las CLAVE dadas en las candidatas, copiada
  EXACTA. Si ninguna encaja con seguridad, o el texto es ambiguo sobre a que
  columna se refiere, deja columna_elegida en null y baja la confianza.
- "columna"/"columnas" en la regla estructurada son el nombre SIMPLE de la
  columna (ultimo segmento de la CLAVE elegida), no la CLAVE completa.
- Si el texto menciona que esta "en pruebas" o parece provisional, baja la
  confianza aunque la traduccion en si sea clara.
- confianza baja (< 0.5) si no estas seguro del tipo, de los limites exactos,
  o de la columna. Es preferible una traduccion honesta de baja confianza que
  una inventada: el Comite del dato revisa TODO antes de activarla.
"""


def _prompt_usuario(atributo: str, desc_funcional: str, candidatas: list) -> str:
    cand_txt = "\n".join(
        f"- {c['clave']} (tabla: {c['tabla']}, confianza de resolucion: {c['confianza']})"
        for c in candidatas
    ) or "(ninguna candidata encontrada; si no puedes determinar la columna, deja columna_elegida en null)"
    return (
        f"ATRIBUTO (nombre de negocio): {atributo}\n"
        f"REGLA (texto libre del equipo de negocio): {desc_funcional}\n\n"
        f"COLUMNAS FISICAS CANDIDATAS:\n{cand_txt}"
    )


def _extraer_json(texto: str) -> dict:
    texto = (texto or "").strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(json)?", "", texto).rstrip("`").strip()
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fin == -1:
        raise ValueError("la respuesta del LLM no contiene un objeto JSON")
    return json.loads(texto[inicio:fin + 1])


def traducir(atributo: str, desc_funcional: str, candidatas: list = None) -> dict:
    """Devuelve la regla estructurada propuesta por el LLM. En caso de fallo
    (Ollama caido, JSON invalido...) devuelve un dict con 'error' y
    confianza 0 -- NUNCA lanza excepcion: sincronizar_reglas_calidad.py debe
    poder seguir registrando la propuesta (aunque sea de baja confianza) para
    que el Comite decida, en vez de perder la regla por completo."""
    candidatas = candidatas or []
    usuario = _prompt_usuario(atributo, desc_funcional, candidatas)
    try:
        import requests
        r = requests.post(OLLAMA_CHAT, timeout=120, json={
            "model": MODELO_LLM, "temperature": 0.1,
            "messages": [{"role": "system", "content": _SISTEMA},
                         {"role": "user", "content": usuario}]})
        r.raise_for_status()
        contenido = r.json()["choices"][0]["message"]["content"]
        propuesta = _extraer_json(contenido)
    except Exception as e:
        return {"tipo": "sql_personalizada", "descripcion": desc_funcional,
                "condicion": None, "confianza": 0.0, "columna_elegida": None,
                "error": f"No se pudo traducir automaticamente: {e}"}

    claves_validas = {c["clave"] for c in candidatas}
    if propuesta.get("columna_elegida") not in claves_validas:
        propuesta["columna_elegida"] = None
    return propuesta


# ============================================================================
# Traduccion para el mecanismo nativo (Sp_Calidad_AgenteIA, ver
# generador_sp_agente_ia.py). Aqui la CLAVE ya viene resuelta (de
# Tb_Calidad_Tabla_Atributo.Clave_Fisico, via extraer_reglas_activas_bd.py) --
# no hace falta elegir columna, solo traducir el texto libre a la condicion
# SQL que identifica las filas que INCUMPLEN, al estilo de los bloques que ya
# escriben a mano Sp_Calidad_RevisorA/Sonia/Elena.
# ============================================================================

_SISTEMA_CONDICION = """Eres un experto en calidad de datos de una entidad hipotecaria (contexto
regulatorio IFRS9). Se te da una regla de calidad escrita en TEXTO LIBRE por el
equipo de negocio, la columna FISICA exacta sobre la que aplica (ya resuelta,
no hace falta elegirla), y el ESQUEMA REAL de la tabla (lista de columnas
existentes). Tu trabajo es traducirla eligiendo el mecanismo MAS SIMPLE que la
exprese bien -- cuanto menos SQL a mano, menos hay que revisar -- entre TRES,
en este orden de preferencia:

1. JQL (preferido si encaja): un mini-lenguaje de parametros que YA interpreta
   el motor nativo (Sp_Calidad_Ejecuta_Regla_Parametrica_Base), sin escribir
   SQL en absoluto. Forma: "TIPO Etiqueta:valor Etiqueta:valor ...", con
   TIPO en mayusculas y los dos puntos PEGADOS a la etiqueta (Min:0, nunca
   Min : 0). Tipos y sus etiquetas (usa SOLO estas, cualquier otra hace
   fallar el motor):
   - NUMERO: Min, Max (ambos inclusive). Dec:<n> exige exactamente n
     decimales (0 = numero entero) -- confirmado en el motor real
     (Sp_Calidad_Ejecuta_Regla_Parametrica_Base pasa Dec directo a
     @NumDecimales de Sp_Calidad_Numero), aunque NO aparece en el bloque
     de ejemplos del propio procedimiento ni en los manuales oficiales.
     La etiqueta se llama `Dec`, NUNCA `Decimales` (esa no existe y hace
     fallar el motor con THROW 51000).
     OBLIGATORIO: si la regla habla de "numero entero", "sin decimales" o
     "precision de N decimales", el JQL TIENE que llevar Dec (Dec:0 para
     entero, Dec:N para N decimales). Omitir Dec es neutro (no comprueba
     nada), asi que la regla se ejecutaria sin error, no marcaria ninguna
     fila y daria un 100% de cumplimiento FALSO sin comprobar lo unico que
     tenia que comprobar. Es tan grave como el caso BOOLEANO de mas abajo.
     OJO: igual que num_decimales en el mecanismo generico, si la columna
     en el ESQUEMA REAL ya es decimal/numeric(p,s), Dec con ese mismo
     valor de s nunca podria fallar (la escala ya la garantiza el tipo) --
     dilo en el mensaje y baja la confianza en vez de generar un chequeo
     que nunca detecta nada.
   - TEXTO: MinLen, MaxLen, Recorte (S/N, si se mide ya recortado).
     PROHIBIDO combinar TEXTO con Nulos:N -- bug confirmado en el motor
     (Sp_Calidad_Ejecuta_Regla_Parametrica_Base) hace que el NULL nunca se
     marque invalido pase lo que pase, aunque Nulos:N lo pida. Si la regla
     necesita que el campo de texto no admita NULL, usa el mecanismo
     "generico" (validador Texto) en su lugar, NUNCA JQL.
   - FECHA: EnDimTiempo (S/N, si debe existir en la dimension tiempo),
     MinDate, MaxDate (formato AAAAMMDD)
     CASO MUY FRECUENTE, no lo falles: "la fecha tiene que ser una fecha
     valida, esto se realiza comprobando si existe en la dimension tiempo"
     (y variantes) se traduce con JQL FECHA y EnDimTiempo:S -- NUNCA con
     sql_personalizada. El motor ya sabe cual es la tabla de la dimension;
     tu no la tienes en el ESQUEMA REAL, asi que si escribes el SQL a mano
     acabas inventandote su nombre (paso de verdad: "Dim_Tiempo", que no
     existe). Si por lo que sea FECHA no encaja, la segunda opcion es el
     mecanismo generico (validador Fecha con validar_dim), tampoco SQL.
   - LISTA: Valores (lista separada por Separador), ErrorNotIn (S/N -- S
     si el error es NO estar en la lista, el caso normal). Si la regla
     necesita "Nulos:S" (nulo permitido) de verdad, añade TAMBIEN "[NULO]"
     como miembro de Valores -- por el mismo bug del motor, Nulos por si
     solo no lo garantiza en LISTA (ErrorNotIn:S por defecto ya trata un
     NULL sin [NULO] en la lista como "no esta en la lista").
   - UNICO: Duplicados (S/N -- N es el caso normal, detecta duplicados)
   - NUNCA uses BOOLEANO para validar que un valor sea correcto: el motor
     NO marca como invalidos los valores fuera de lista en BOOLEANO (los
     normaliza a 0/1 sin avisar), asi que mediria 100% de cumplimiento
     SIEMPRE, falso. Para "el campo solo puede ser S o N" usa LISTA con
     Valores:S,N ErrorNotIn:S. BOOLEANO no se ofrece como opcion aqui.
   Etiquetas SIEMPRE obligatorias en cualquier tipo: `Nulos` (S/N -- N =
   el nulo SI es incumplimiento) y `Clave` (columnas clave separadas por
   coma, para que el detalle sea trazable). Emite Nulos siempre explicito:
   su defecto real es S (no comprueba completitud) y NO es neutro.
   PROHIBIDO `MaxDate:99991231` -- el motor lo convierte a 31/12 del año
   siguiente (un GETDATE() disfrazado); para "sin limite superior" omite
   MaxDate directamente, no pongas ese valor.
   PROHIBIDO poner una funcion SQL como valor de MinDate/MaxDate (p.ej.
   `MinDate:GETDATE()`) para expresar "no puede ser fecha futura" o
   cualquier limite RELATIVO/dinamico -- confirmado en el motor real
   (Sp_Calidad_Fecha tipa esos parametros como DATE puro): el texto se
   intenta CONVERTIR a fecha, no se evalua como funcion, asi que
   "GETDATE()" como valor simplemente falla la conversion (en JQL se traga
   el error y no comprueba nada; en el validador generico tira un error de
   SQL real en produccion). JQL y el validador generico SOLO admiten limites
   ESTATICOS (AAAAMMDD fijo). Para "fecha futura", "dentro de los ultimos N
   dias" o cualquier comparacion contra la fecha de ejecucion, usa
   sql_personalizada con GETDATE()/DATEADD() dentro de la condicion SQL de
   verdad (esa SI se evalua en cada ejecucion, a diferencia de un parametro).
   Si necesitas "no corregir el valor pero marcar la fila como invalida":
   Blanqueo:[original] en TEXTO/LISTA, o Duplicados:S en UNICO -- ningun
   otro valor de Blanqueo esta permitido.
   Etiqueta OPCIONAL `Filtro:<condicion SQL>` -- acota la POBLACION sobre la
   que se mide Y se detectan incumplimientos (equivalente a filtro_poblacion
   en sql_personalizada, pero dentro de JQL). Imprescindible para reglas
   CONDICIONALES del tipo "si <otra columna> entonces <esta columna>" (p.ej.
   "solo tiene que tener valor si es una novacion" -> el campo a validar es
   la fecha, pero la poblacion la define OTRA columna: Filtro:
   Es_Novacion_Renego=1). Sin Filtro, JQL comprueba la columna en TODA la
   tabla, lo cual es incorrecto para una regla condicional -- NO downgrades
   a sql_personalizada solo porque la regla sea condicional, primero mira
   si Filtro la resuelve dentro de JQL. Debe ir el ULTIMO parametro del JQL
   (el motor no trocea mas alla de "Filtro:", asi que un espacio con otra
   etiqueta dentro de la condicion del filtro rompe el parseo).

2. VALIDADOR GENERICO (si JQL no encaja, p.ej. precision decimal): estos
   procedimientos YA EXISTEN en produccion (APP_VALIDADORES.dbo.*). Los nombres
   entre parentesis son las claves EXACTAS a usar dentro de "parametros"
   (minusculas, con guion bajo, SIN arroba):
   - Numero: rango numerico (min_value, max_value, ambos inclusive, null =
     sin limite; num_decimales opcional para exigir un numero exacto de
     decimales -- este SI cubre precision decimal, a diferencia de JQL).
     OJO: si la columna en el ESQUEMA REAL ya es decimal/numeric(p,s), el
     tipo de dato YA garantiza exactamente s decimales para cualquier valor
     almacenado -- num_decimales con ese mismo valor de s nunca podria
     fallar (es un no-op, verificado leyendo Fn_Calidad_Numero: compara
     contra la ESCALA DECLARADA de la columna, no contra el valor). Si la
     regla de negocio pide una precision que YA coincide con la escala de
     la columna, dilo en el mensaje y baja la confianza en vez de generar
     una comprobacion que nunca puede detectar nada.
   - Texto: longitud de texto (min_len, max_len, trim: true/false)
   - Fecha: rango de fechas (min_date, max_date, formato 'YYYY-MM-DD';
     validar_dim: true si debe existir en la dimension fecha). min_date/
     max_date son SIEMPRE literales ESTATICOS -- Sp_Calidad_Fecha los tipa
     como DATE puro, poner una funcion como "GETDATE()" falla la conversion
     (error real en produccion, no se evalua como funcion). Para "fecha
     futura" o cualquier limite relativo a la fecha de ejecucion, usa
     sql_personalizada con GETDATE() en la condicion SQL de verdad.
   - Lista: el valor debe estar en una lista cerrada (lista: [valores],
     separador, error_not_in: true -- el caso normal). Un valor de la lista
     puede ser el token especial [vacio] (cadena vacia) o [nulo] (NULL),
     para tratarlos como miembros validos/invalidos de la lista sin
     confundirlo con admite_null (que es una comprobacion aparte).
   - Unico: la columna (o combinacion de columnas clave) no debe repetirse
     (permite_duplicados: false es el caso normal). OJO -- verificado contra
     el codigo real de Sp_Calidad_Unico: permite_duplicados NO desactiva la
     deteccion, solo si el valor se corrige (blanquea) despues. Un duplicado
     SIEMPRE cuenta como incumplimiento en el % de cumplimiento, tenga este
     flag el valor que tenga. Si una regla de verdad necesita "hay
     duplicados pero no cuentan como fallo", este validador no lo puede
     expresar -- usa sql_personalizada.
   - (Booleano existe pero tiene el mismo problema que en JQL -- no lo uses
     para validar valores, usa Lista)

3. CONDICION SQL PERSONALIZADA (solo si NINGUNO de los anteriores encaja):
   formulas que combinan varias columnas, comprobaciones contra otra columna
   de la misma fila/tabla, condicionales tipo "si X entonces Y", o cualquier
   logica que ni JQL ni un validador generico puedan expresar con parametros.

Devuelve UNICAMENTE un objeto JSON valido, sin markdown ni texto alrededor.

Si encaja JQL:
{
  "mecanismo": "jql",
  "jql": "NUMERO Nulos:S Min:0 Max:1000000 Clave:Id_Operacion,Id_Pais",
  "mensaje": "<mensaje breve en negocio a guardar junto a cada fila que incumple>",
  "confianza": <0.0 a 1.0>
}

Si encaja un validador generico (ejemplo real para Numero, usa las mismas
claves con el validador que corresponda):
{
  "mecanismo": "generico",
  "validador": "Numero" | "Texto" | "Fecha" | "Lista" | "Unico",
  "admite_null": <true|false>,
  "parametros": {"min_value": 0, "max_value": null, "num_decimales": null},
  "mensaje": "<mensaje breve en negocio a guardar junto a cada fila que incumple>",
  "confianza": <0.0 a 1.0>
}

Si hace falta condicion personalizada:
{
  "mecanismo": "sql_personalizada",
  "condicion_violada": "<fragmento SQL booleano: TRUE para las filas que INCUMPLEN>",
  "mensaje": "<mensaje breve en negocio a guardar junto a cada fila que incumple>",
  "filtro_poblacion": "<fragmento SQL opcional para acotar el total de filas contra las que se mide el % de cumplimiento, o null si aplica a toda la tabla>",
  "confianza": <0.0 a 1.0>
}

REGLAS DE TRADUCCION (criticas, aprendidas de errores reales -- las notas de
Lista/Unico/Texto de arriba se verificaron leyendo el codigo real de
Fn_Calidad_*/Sp_Calidad_* en APP_VALIDADORES, de Sp_Calidad_Ejecuta_Regla y de
Sp_Calidad_Ejecuta_Regla_Parametrica_Base en APP_CATALOGO, el 2026-08-11;
si ese codigo cambia, esto NO se actualiza solo, hay que releerlo a mano --
ver activacion/jql.py para el mismo patron aplicado a JQL, incluido el bug
de @AdmiteNull en las ramas TEXTO/LISTA de ese procedimiento):
- Orden de preferencia SIEMPRE: JQL > validador generico > sql_personalizada.
  Menos codigo a mano es menos que un humano tiene que revisar. Solo baja de
  nivel cuando el de arriba de verdad no pueda expresar la regla (ver las
  notas de "NO uses JQL" en cada tipo).
- En "sql_personalizada", "condicion_violada" describe el INCUMPLIMIENTO, no
  el cumplimiento. Si el texto dice "el valor tiene que ser mayor que 0", la
  condicion violada es "Columna <= 0" (lo contrario, no "Columna > 0"). Este
  es el error mas comun: revisa dos veces la direccion antes de responder.
- Usa el nombre de columna SIMPLE (el que viene en CAMPO), sin prefijos de
  tabla ni corchetes salvo que el nombre real los necesite (ej. columnas con
  espacios o parentesis).
- SOLO puedes usar columnas que aparezcan literalmente en el ESQUEMA REAL que
  se te da. NUNCA inventes un nombre de columna "hermana" plausible (p.ej. a
  partir del texto de negocio) aunque suene razonable -- si la regla necesita
  una columna que no ves en el esquema, bajA la confianza a menos de 0.4 y
  dejalo dicho en el mensaje en vez de inventarla.
- Comprobaciones de existencia contra OTRA tabla (p.ej. "que exista en el
  maestro de provincias/dimension tiempo"): el ESQUEMA REAL que se te da es
  SOLO de la tabla de la regla, nunca de una tabla de referencia/maestro/
  dimension -- NUNCA inventes el nombre de esa otra tabla (ni columnas
  suyas) por muy plausible que suene (visto en produccion: "Maestro_Provincias"
  inventado con confianza 0.7 para "debe existir en el maestro de provincias
  de España"). Si no te dan el nombre real de esa tabla, usa
  sql_personalizada, deja la condicion lo mas fiel posible en el mensaje
  (sin inventar el JOIN) y baja la confianza a menos de 0.4.
- NULL nunca cuenta como incumplimiento salvo que el texto diga explicitamente
  "no puede estar vacio"/"es obligatorio" (en ese caso admite_null=false, o en
  sql_personalizada la condicion violada es "Columna IS NULL").
- Si el texto menciona que esta "en pruebas" o parece provisional, baja la
  confianza aunque la traduccion en si sea clara.
- confianza baja (< 0.5) si no estas seguro de la condicion/parametros
  exactos. Es preferible una traduccion honesta de baja confianza que una
  inventada: un humano revisa TODO antes de que se active de verdad.
"""


# Lo que el LLM debe devolver, segun _SISTEMA_CONDICION. Todo lo demas que
# acabe en una propuesta guardada (avisos, resultados de prueba, contadores de
# tokens, vistas previas del bloque) lo pone el sistema DESPUES, y no tiene
# ningun sentido mostrarselo como ejemplo de respuesta correcta.
CAMPOS_RESPUESTA = {
    "mecanismo", "jql", "validador", "parametros", "admite_null",
    "condicion_violada", "mensaje", "filtro_poblacion",
}


# Campos que pone el SISTEMA sobre una propuesta despues de traducir. Si el
# LLM los devuelve dentro de su JSON, no son suyos: hay que descartarlos.
CAMPOS_DEL_SISTEMA = {
    "aviso_contexto", "aviso_bloque", "jql_prueba", "bloque_sql_preview",
}


def _quitar_campos_del_sistema(propuesta: dict) -> dict:
    """Descarta de la respuesta del LLM los campos que son del sistema.

    La telemetria ya iba con prefijo "_" precisamente para no chocar con lo
    que devuelva el modelo, pero los avisos no llevaban prefijo. Resultado
    visto el 2026-09-01 en las reglas 53 y 2089: el LLM devolvio un
    "aviso_contexto" inventado, copiando el texto de un aviso del sistema ya
    retirado, y la pantalla de revision lo pinto como si lo hubiera generado
    el agente -- un aviso falso con apariencia de oficial.

    Lo aprendio de los propios ejemplos few-shot, que hasta el mismo dia le
    mostraban estos campos como parte de una "respuesta correcta" (ver
    CAMPOS_RESPUESTA). Aquello ya esta corregido; esto es la otra mitad, para
    que no dependa de que los ejemplos esten limpios.

    Aqui se filtra por lista NEGRA, al reves que en los ejemplos: lo que se
    quiere evitar es que el modelo suplante campos NUESTROS, no acotar lo que
    puede responder. Una lista blanca aqui tiraria en silencio cualquier
    campo legitimo que se anada al prompt mas adelante."""
    if not isinstance(propuesta, dict):
        return propuesta
    return {k: v for k, v in propuesta.items()
            if k not in CAMPOS_DEL_SISTEMA and not k.startswith("_")}


def _bloque_ejemplos_confirmados(limite: int = 3) -> str:
    """Few-shot: traducciones reales que un revisor humano confirmo como
    CORRECTAS (evaluar_traduccion.etiquetar_resultado/Correcta_Humano=1),
    independiente de si la heuristica 'Coincide' las hubiera marcado bien o
    no. Import local para evitar el ciclo evaluar_traduccion<->traductor (ese
    modulo ya importa este mismo por funcion, no a nivel de modulo). Si
    todavia no hay ninguna confirmada, o la BD de pruebas no esta alcanzable
    (esto corre en produccion, no solo en el arnes de evaluacion), no aporta
    nada -- NUNCA lanza, la traduccion tiene que poder seguir sin esto."""
    try:
        from origen_reglas.evaluar_traduccion import ejemplos_confirmados
        ejemplos = ejemplos_confirmados(limite=limite)
    except Exception:
        return ""
    if not ejemplos:
        return ""
    bloques = []
    for e in ejemplos:
        guardada = e.get("Propuesta_JSON") or {}
        # Solo los campos que el LLM debe DEVOLVER (los que describe
        # _SISTEMA_CONDICION). Antes se mandaba la propuesta entera quitando
        # dos campos, y colaban dentro cosas nuestras: aviso_contexto,
        # aviso_bloque, jql_prueba, _tokens_*... El LLM las tomaba por parte
        # de una "respuesta correcta" y las reproducia: el 2026-09-01 se le
        # vio devolver un aviso_contexto inventado, con el texto literal de
        # un aviso viejo del sistema, que la pantalla de revision pintaba
        # como si lo hubiera generado el agente. Ademas ocupaban mas de la
        # mitad del bloque de ejemplos, o sea contexto tirado.
        # Lista blanca, no lista negra: un campo interno nuevo no se cuela
        # solo por olvidarse de excluirlo.
        propuesta = {k: v for k, v in guardada.items() if k in CAMPOS_RESPUESTA}
        bloques.append(f'Regla: "{e.get("Desc_Funcional") or ""}"\n'
                        f"Respuesta correcta: {json.dumps(propuesta, ensure_ascii=False)}")
    return ("EJEMPLOS REALES CONFIRMADOS POR UN REVISOR (traducciones ya verificadas -- "
            "sigue el mismo estilo y nivel de detalle):\n\n" + "\n\n".join(bloques))


_SISTEMA_SELECCION_COLUMNAS = """Eres un asistente que reduce el esquema de una tabla ANCHA a solo las
columnas relevantes, como paso previo a traducir una regla de calidad de
datos -- NO traduzcas la regla todavia, en esta llamada solo eliges columnas.

Se GENEROSO: incluye no solo la columna que la regla valida directamente,
sino cualquier otra que pueda hacer falta para expresarla completa. Las
reglas CONDICIONALES ("solo si es una novacion", "solo aplica al pais X",
"si X entonces Y") casi siempre dependen de OTRA columna distinta a la que
se valida -- si el texto de la regla sugiere una condicion, un filtro, una
comparacion con otro campo, o una excepcion, incluye esa columna tambien
aunque no estes seguro. Es mucho peor descartar una columna que hace falta
que incluir alguna de mas: el siguiente paso ya no vera las que descartes.

Devuelve UNICAMENTE un objeto JSON valido, sin markdown ni texto alrededor:
{"columnas_relevantes": ["NombreExacto1", "NombreExacto2", ...]}
Usa el nombre de columna EXACTO, copiado tal cual del esquema dado -- no lo
traduzcas, no lo abrevies, no inventes uno que no este en la lista."""


def _prompt_usuario_seleccion_columnas(desc_funcional: str, campo: str, columnas_tabla: list) -> str:
    esquema_txt = "\n".join(f"- {c['nombre']} ({c['tipo']})" for c in columnas_tabla)
    return (
        f"REGLA (texto libre del equipo de negocio): {desc_funcional}\n"
        f"CAMPO que la regla valida directamente: {campo}\n\n"
        f"ESQUEMA COMPLETO de la tabla:\n{esquema_txt}"
    )


def _seleccionar_columnas_relevantes(desc_funcional: str, campo: str, columnas_tabla: list,
                                      uso: dict = None):
    """Paso previo, mas simple y barato que traducir_condicion_sql (sin las
    instrucciones de JQL/generico/sql_personalizada), para reducir un
    esquema ANCHO a solo las columnas relevantes ANTES de la traduccion real
    -- pensado para tablas por encima de UMBRAL_COLUMNAS_CONTEXTO, donde
    mandar las 86 columnas de golpe junto con todas las instrucciones hacia
    que el LLM perdiera de vista el formato de salida (ver aviso_contexto).

    Devuelve la lista de nombres elegidos (filtrada contra columnas_tabla,
    por si el LLM inventa alguno), o None si falla o no elige ninguna --
    el llamador (traducir_condicion_sql) degrada a usar el esquema completo,
    exactamente el comportamiento de antes de esto existir.

    uso: dict opcional donde acumular los tokens de ESTA llamada. Sin el, el
    coste del prefiltro no se registraba en ningun sitio y las tablas anchas
    parecian mas baratas de lo que son (solo se contaba la traduccion final)."""
    usuario = _prompt_usuario_seleccion_columnas(desc_funcional, campo, columnas_tabla)
    try:
        import requests
        r = requests.post(OLLAMA_CHAT, timeout=120, json={
            "model": MODELO_LLM, "temperature": 0.1,
            "messages": [{"role": "system", "content": _SISTEMA_SELECCION_COLUMNAS},
                         {"role": "user", "content": usuario}]})
        r.raise_for_status()
        cuerpo = r.json()
        if uso is not None:
            u = cuerpo.get("usage") or {}
            uso["entrada"] = uso.get("entrada", 0) + (u.get("prompt_tokens") or 0)
            uso["salida"] = uso.get("salida", 0) + (u.get("completion_tokens") or 0)
        contenido = cuerpo["choices"][0]["message"]["content"]
        data = _extraer_json(contenido)
        nombres_validos = {c["nombre"] for c in columnas_tabla}
        elegidas = [n for n in (data.get("columnas_relevantes") or []) if n in nombres_validos]
        return elegidas or None
    except Exception:
        return None


_STOPWORDS_ES = {
    "el", "la", "los", "las", "de", "del", "que", "no", "se", "un", "una",
    "unos", "unas", "en", "con", "por", "para", "tiene", "debe", "ser", "es",
    "al", "su", "sus", "si", "esto", "este", "esta", "estos", "estas",
    "valor", "campo", "regla", "cuando", "solo", "sino", "mas", "the",
}


def _tokens(texto: str) -> set:
    """Palabras utiles del texto de una regla, para cruzarlas con nombres de
    columna. Ademas de las palabras largas, rescata las SIGLAS en mayusculas
    de 2-3 letras: este catalogo esta lleno de ellas (DAP, EGI, SP, NI...) y
    el filtro de longitud las tiraba justo en las reglas que hablan de esas
    columnas -- p.ej. "menor o igual a la suma del DAP + EGI + ENGI" solo
    conservaba ENGI, por tener 4 letras, y perdia las otras dos."""
    texto = texto or ""
    largas = {t for t in re.findall(r"[a-zA-ZÀ-ÿ]+", texto.lower())
              if len(t) > 3 and t not in _STOPWORDS_ES}
    siglas = {s.lower() for s in re.findall(r"\b[A-ZÁÉÍÓÚÑ]{2,}\b", texto)}
    return largas | siglas


def _seleccionar_columnas_heuristico(desc_funcional: str, campo: str, columnas_tabla: list,
                                      clave_cols: list = None, limite: int = 30) -> list:
    """Fallback SIN LLM cuando _seleccionar_columnas_relevantes() tambien
    falla -- visto en tablas extremadamente anchas (153 columnas,
    Tb_Fact_Operacion) donde incluso esa llamada mas simple perdia el
    formato de salida. Coincidencia de palabras entre el texto de la regla y
    los nombres de columna (partidos por guion_bajo): best-effort, no
    entiende semantica como el LLM, pero SIEMPRE reduce el esquema en vez de
    rendirse y mandar la tabla entera -- mejor un recorte imperfecto que
    ninguno. Conserva SIEMPRE 'campo' y 'clave_cols' aunque no haya ningun
    solape de palabras."""
    palabras_regla = _tokens(desc_funcional)
    conservar = {campo} | set(clave_cols or [])
    if palabras_regla:
        puntuadas = []
        for c in columnas_tabla:
            nombre = c["nombre"]
            if nombre in conservar:
                continue
            palabras_col = _tokens(nombre.replace("_", " "))
            solape = len(palabras_col & palabras_regla)
            if solape:
                puntuadas.append((solape, nombre))
        puntuadas.sort(reverse=True)
        conservar |= {nombre for _, nombre in puntuadas[:limite]}
    return [c for c in columnas_tabla if c["nombre"] in conservar]


def _prompt_usuario_condicion(desc_funcional: str, clave_fisico: str, columnas_tabla: list = None,
                              clave_cols: list = None, ejemplos_txt: str = "") -> str:
    columna = clave_fisico.rsplit(".", 1)[-1]
    if columnas_tabla:
        esquema_txt = "\n".join(f"- {c['nombre']} ({c['tipo']})" for c in columnas_tabla)
    else:
        esquema_txt = "(no disponible -- no inventes nombres de columnas hermanas, usa solo CAMPO)"
    clave_txt = ",".join(clave_cols) if clave_cols else "(no disponible -- omite 'Clave' si eliges JQL, se completara a mano)"
    prefijo = f"{ejemplos_txt}\n\n" if ejemplos_txt else ""
    return (
        f"{prefijo}"
        f"REGLA (texto libre del equipo de negocio): {desc_funcional}\n"
        f"CLAVE completa (ya resuelta): {clave_fisico}\n"
        f"CAMPO (nombre de columna simple a usar en la condicion): {columna}\n"
        f"CLAVE PRIMARIA real de la tabla (usa EXACTAMENTE esto en la etiqueta "
        f"Clave: de JQL, o en clave_cols si hiciera falta): {clave_txt}\n\n"
        f"ESQUEMA REAL de la tabla (columnas existentes, NO inventes otras):\n{esquema_txt}"
    )


def traducir_condicion_sql(desc_funcional: str, clave_fisico: str, columnas_tabla: list = None,
                            clave_cols: list = None) -> dict:
    """Traduce una regla ya resuelta (Tb_Calidad_Regla + Tb_Calidad_Tabla_Atributo)
    a un mecanismo (jql / generico / sql_personalizada), lista para que
    generador_sp_agente_ia/activacion.jql la ensamble o pruebe. NUNCA lanza
    excepcion: si Ollama falla, devuelve confianza 0 con el error, para que la
    propuesta se registre igual y un humano la complete a mano.

    columnas_tabla: lista real de columnas de la tabla (de
    generador_sp_agente_ia.columnas_de), para que el LLM no invente nombres de
    columnas hermanas a partir del texto de negocio.
    clave_cols: PK real (de generador_sp_agente_ia.columnas_clave_de), para la
    etiqueta Clave: obligatoria en JQL. Ambos opcionales -- si no se dan
    (p.ej. DW_PRINCIPAL no alcanzable), degrada al comportamiento anterior.

    Ademas antepone al prompt hasta 3 ejemplos reales confirmados por un
    revisor humano (ver _bloque_ejemplos_confirmados) -- crece solo segun se
    va usando el boton de etiquetado en /calidad/evaluacion.

    Si el esquema es ANCHO (> UMBRAL_COLUMNAS_CONTEXTO), primero se llama a
    _seleccionar_columnas_relevantes() -- una pasada mas simple y barata que
    reduce el esquema antes de la traduccion real, en vez de mandar la tabla
    entera junto con todas las instrucciones de JQL/generico/sql_personalizada
    (ver ese umbral, calibrado el 2026-08-18 contra un caso real de 86
    columnas). Si ESE paso tambien falla (visto con 153 columnas: hasta la
    llamada simple pierde el formato), cae a _seleccionar_columnas_heuristico
    -- sin LLM, coincidencia de palabras, siempre reduce algo en vez de
    rendirse y mandar la tabla entera. La clave primaria y el propio campo de
    la regla se conservan SIEMPRE, aunque ningun paso de seleccion los
    mencione."""
    columna_regla = clave_fisico.rsplit(".", 1)[-1]
    columnas_para_prompt = columnas_tabla
    filtrado_metodo = None
    uso_prefiltro = {}
    if columnas_tabla and len(columnas_tabla) > UMBRAL_COLUMNAS_CONTEXTO:
        elegidas = _seleccionar_columnas_relevantes(desc_funcional, columna_regla, columnas_tabla,
                                                     uso=uso_prefiltro)
        if elegidas:
            conservar = set(elegidas) | {columna_regla} | set(clave_cols or [])
            columnas_para_prompt = [c for c in columnas_tabla if c["nombre"] in conservar]
            filtrado_metodo = "llm"
        else:
            columnas_para_prompt = _seleccionar_columnas_heuristico(
                desc_funcional, columna_regla, columnas_tabla, clave_cols)
            if len(columnas_para_prompt) < len(columnas_tabla):
                filtrado_metodo = "heuristico"

    ejemplos_txt = _bloque_ejemplos_confirmados()
    usuario = _prompt_usuario_condicion(desc_funcional, clave_fisico, columnas_para_prompt, clave_cols, ejemplos_txt)

    n_cols = len(columnas_para_prompt) if columnas_para_prompt else 0

    try:
        import requests
        # 600s: incluir el esquema real + las instrucciones de validadores
        # genericos alarga el prompt bastante -- medido: 98.9s con 32
        # columnas antes de anadir esas instrucciones, >240s despues. Subido
        # de 300 a 600 el 2026-08-18 tras ver timeouts reales Y respuestas
        # JSON incompletas (truncadas justo antes del limite, sin la clave
        # "mecanismo") en una tabla de 86 columnas -- con el prompt de hoy
        # (Filtro, Dec, avisos de Lista/Unico/Texto) unas cuantas tablas
        # anchas ya rozaban o pasaban los 300s.
        r = requests.post(OLLAMA_CHAT, timeout=600, json={
            "model": MODELO_LLM, "temperature": 0.1,
            "messages": [{"role": "system", "content": _SISTEMA_CONDICION},
                         {"role": "user", "content": usuario}]})
        r.raise_for_status()
        respuesta = r.json()
        contenido = respuesta["choices"][0]["message"]["content"]
        propuesta = _extraer_json(contenido)
        propuesta = _quitar_campos_del_sistema(propuesta)
        # Ollama expone "usage" con la misma forma que la API de OpenAI
        # (prompt_tokens/completion_tokens) al hablar por /v1/chat/completions
        # -- se guarda con prefijo "_" para que no se confunda con un campo
        # que el propio LLM pueda devolver dentro del JSON de la regla.
        usage = respuesta.get("usage") or {}
        # Se suma el prefiltro de columnas si hubo: son DOS llamadas al LLM y
        # antes solo se contaba esta, con lo que las tablas anchas figuraban
        # mas baratas de lo que realmente costaban.
        tokens_llamada = usage.get("prompt_tokens") or 0
        propuesta["_tokens_entrada"] = tokens_llamada + uso_prefiltro.get("entrada", 0)
        propuesta["_tokens_salida"] = (usage.get("completion_tokens") or 0) + uso_prefiltro.get("salida", 0)
        if uso_prefiltro:
            propuesta["_tokens_prefiltro"] = dict(uso_prefiltro)

        # Aviso de perdida de contexto, medido sobre los tokens REALES que
        # reporta el motor para ESTA llamada (no sobre el prefiltro, que va en
        # su propia ventana). Antes se estimaba por numero de columnas y
        # longitud del prompt en caracteres: dos proxies que se quedaban
        # cortos. El 2026-09-01 se descubrio que el prompt llevaba tiempo
        # TRUNCANDOSE de verdad y esos proxies no lo cazaban -- lo delato
        # justamente este numero, clavado en el tope en 64 de 113 intentos.
        # Si el motor reporta una entrada pegada a la ventana, no es sospecha:
        # es que no cabia y se corto.
        if tokens_llamada >= VENTANA_CONTEXTO - MARGEN_VENTANA:
            if filtrado_metodo == "llm":
                origen = f"el filtrado por LLM dejo {n_cols} columnas y aun asi no cabe"
            elif filtrado_metodo == "heuristico":
                origen = (f"el filtrado por LLM fallo y el heuristico (coincidencia de "
                          f"palabras) dejo {n_cols} columnas, que siguen sin caber")
            elif columnas_para_prompt:
                origen = (f"{n_cols} columnas sin filtrar (por debajo de "
                          f"{UMBRAL_COLUMNAS_CONTEXTO}, no se intento reducir)")
            else:
                origen = "sin esquema de tabla en el prompt"
            propuesta["aviso_contexto"] = (
                f"PROMPT TRUNCADO: la llamada reporta {tokens_llamada} tokens de entrada "
                f"con una ventana de {VENTANA_CONTEXTO}, o sea que parte del enunciado NO "
                f"le ha llegado al modelo ({origen}). La respuesta puede ignorar "
                f"instrucciones que nunca leyo. Revisar esta propuesta con mucha atencion, "
                f"y plantearse subir num_ctx del modelo o recortar el prompt.")
        return propuesta
    except Exception as e:
        # Sin respuesta no hay tokens que mirar, asi que aqui no se puede
        # afirmar que hubiera truncamiento. Se deja constancia del tamaño del
        # prompt por si el fallo fue justamente por pasarse: un prompt que no
        # cabe puede acabar en timeout o en una respuesta ilegible.
        resultado = {"mecanismo": "sql_personalizada", "condicion_violada": None,
                     "mensaje": desc_funcional, "filtro_poblacion": None, "confianza": 0.0,
                     "error": f"No se pudo traducir automaticamente: {e}"}
        resultado["aviso_contexto"] = (
            f"La llamada al modelo fallo, asi que no se sabe cuantos tokens ocupaba. "
            f"Como referencia, el prompt enviado eran {len(usuario)} caracteres con "
            f"{n_cols} columnas de esquema, contra una ventana de {VENTANA_CONTEXTO} "
            f"tokens.")
        return resultado
