# Agente de Calidad de Datos

> **Copia pública y saneada.** Este repositorio es una copia de un proyecto
> interno, publicada como muestra de trabajo. Los nombres de servidores, bases
> de datos, procedimientos almacenados, rutas de despliegue y personas se han
> sustituido por equivalentes genéricos (`SERVIDOR_DATOS`, `DW_PRINCIPAL`,
> `Sp_Calidad_RevisorA`...). **No se incluye el historial de commits**, ni
> datos, ni credenciales.
>
> Por tanto el código **no es ejecutable tal cual**: apunta a una
> infraestructura que no existe. Se publica para mostrar la arquitectura, las
> decisiones de diseño y el razonamiento documentado en el código, no para
> ejecutarse.


Agente que evalúa reglas de calidad de datos sobre las tablas del DWH de una
entidad hipotecaria (contexto regulatorio IFRS9), y ayuda a traducir reglas de
negocio escritas en texto libre a reglas ejecutables, con un LLM local (phi4
vía Ollama) que **propone** y un Comité de dato que **revisa y aprueba** antes
de que nada corra contra datos reales.

Vive en la rama `agent-dev`. Es un proyecto hermano, independiente, del agente
de documentación del diccionario (rama `main`, puerto 5000) — corre en su
propio servicio Flask (puerto 5001), en la misma máquina que Ollama, y no
comparte ciclo de despliegue con la app de producción del diccionario.

## Qué hace, en una frase

Detecta reglas activas en `Tb_Calidad_Regla`/`Tb_Calidad_Tabla_Atributo`, las
traduce con el LLM a un mecanismo ejecutable (JQL, validador genérico o SQL a
mano), las deja en una cola de revisión humana, y solo tras aprobación genera
el bloque T-SQL real (`Sp_Calidad_AgenteIA`) — nunca escribe en producción sin
que alguien lo confirme explícitamente.

## Estructura del repo

El código vive en paquetes Python reales (con `__init__.py` e imports
absolutos) organizados por capa de arquitectura:

| Paquete | Rol |
|---|---|
| `nucleo/` | Motor determinista de reglas/anomalías + orquestador (`agente_calidad_datos.py`) — evalúa una tabla contra un fichero de configuración de reglas y genera un informe. |
| `web/` | Servicio Flask propio (puerto 5001): `api_calidad.py`/`servir_calidad.py` (arranque), `calidad_web.py` (todas las rutas `/api/calidad/...`), páginas HTML (`calidad.html`, `revision_reglas_calidad.html`, `evaluacion_traduccion.html`). |
| `origen_reglas/` | De texto libre (`Tb_Calidad_Regla`) a propuesta en la cola de revisión: detección, traducción vía LLM (`traductor_reglas_calidad.py`), registro de propuestas, y el arnés de evaluación LLM-vs-bloques-reales (`evaluar_traduccion.py`). |
| `activacion/` | De propuesta aprobada a algo que corre en producción: genera el bloque `Sp_Calidad_AgenteIA` (`generador_sp_agente_ia.py`) o el JQL inline (`jql.py`). |
| `diccionario/` | Dependencias de solo lectura sobre el sistema RAG/diccionario ya existente (contexto de negocio para el LLM). |
| `exploracion/` | Herramientas sueltas de investigación/pruebas contra el servidor real, no parte del flujo de producción. |
| `_archivo/` | Pipeline viejo (SharePoint/Excel) archivado. No se toca ni forma parte de la arquitectura activa. |

Cada script se invoca como módulo desde la raíz del repo, por ejemplo:

```
python -m web.servir_calidad
python -m nucleo.agente_calidad_datos --config reglas_calidad_ejemplo.json
python -m origen_reglas.sincronizar_reglas_bd
```

## Flujo end-to-end

1. **Detección** (`origen_reglas/sincronizar_reglas_bd.py`): lee reglas
   activas de `Tb_Calidad_Regla` (`Es_Activa=1`) cruzadas con
   `Tb_Calidad_Tabla_Atributo`, donde `Clave_Fisico` ya viene resuelto.
2. **Traducción** (`traductor_reglas_calidad.py`): el LLM elige, en este
   orden de preferencia, entre **JQL** (mini-lenguaje de parámetros que
   interpreta el motor nativo sin SQL a mano), un **validador genérico**
   (`APP_VALIDADORES.dbo.Sp_Calidad_*`), o una **condición SQL personalizada**.
   Se le da el esquema real de la tabla (con precisión/escala/longitud, no
   solo el nombre del tipo) y su clave primaria real (buscada en
   `DW_PRINCIPAL`/`DW_CRUDO`/`DW_LAGO`, la que corresponda) para que no invente
   columnas ni claves. Para tablas ANCHAS (> 50 columnas), un paso previo
   más simple reduce el esquema a las columnas relevantes antes de traducir
   (por LLM, con un heurístico de coincidencia de palabras como red de
   seguridad si esa llamada también falla) — mandar el esquema entero hacía
   que el LLM perdiera el formato de salida. Si aun así el prompt sigue
   siendo grande, la propuesta lleva un `aviso_contexto` para que el
   revisor la mire con más atención.
3. **Cola de revisión** (`registro_propuestas_reglas.py`, tabla
   `CAL_Propuestas`): cada propuesta queda `PENDIENTE` hasta que un revisor
   humano la aprueba, corrige o rechaza desde `/calidad/reglas`.
4. **Activación** (`activacion/generador_sp_agente_ia.py`,
   `activacion/jql.py`): al aprobar, se persiste según el mecanismo —
   `generico`/`sql_personalizada` genera el bloque real (`CAL_Bloques_Sp_AgenteIA`,
   workspace); `jql` guarda la sentencia (`CAL_Bloques_JQL`, workspace,
   separada porque no genera SQL a mano). **Desplegarlo de verdad en
   `DW_PRINCIPAL`** (`CREATE OR ALTER PROCEDURE`, o publicar el JQL en
   `Tb_Calidad_Tabla_Atributo.Consulta_SQL_Base`) es un paso aparte,
   explícito y confirmado, nunca automático.
5. **Evaluación/benchmark** (`origen_reglas/evaluar_traduccion.py`, página
   `/calidad/evaluacion`): compara en segundo plano cómo traduce el LLM
   reglas ya implementadas a mano, contra el bloque real escrito por el
   equipo (número de intentos configurable por lote). Cada resultado se
   puede etiquetar con un juicio humano (correcta/incorrecta) independiente
   del veredicto heurístico automático; las confirmadas correctas se usan
   como ejemplos few-shot para mejorar futuras traducciones. También
   registra tokens de entrada/salida por intento, para diagnosticar el
   coste y el riesgo de tablas anchas con datos, no solo intuición.

## Arrancar en local

```
pip install -r requirements.txt
python -m web.servir_calidad
```

Sirve en `http://localhost:5001`:
- `/calidad` — informe del motor determinista
- `/calidad/reglas` — cola de revisión de reglas traducidas
- `/calidad/evaluacion` — benchmark LLM vs bloques reales, con etiquetado

Requiere alcanzar por red: SQL Server (`SERVIDOR_DATOS\INSTANCIA` para
`DW_PRINCIPAL`/`DW_CRUDO`/`APP_CATALOGO`; `SERVIDOR_WORKSPACE` para el workspace
`WORKSPACE_AGENTE`) y Ollama en `localhost:11434` (phi4).

## Despliegue

Corre como servicio de Windows (`ServicioAgenteCalidad`, vía NSSM) en
`servidor-workspace.interno`, bajo una cuenta de dominio con acceso real a los
tres saltos de SQL Server necesarios. El árbol desplegado vive en
`C:\ruta\despliegue` en ese servidor.

## Estado / pendiente

- Publicar JQL en `Tb_Calidad_Tabla_Atributo.Consulta_SQL_Base` (para que lo
  ejecute el motor de producción, no solo la prueba inline / `CAL_Bloques_JQL`)
  queda pendiente, decisión deliberada de una sesión futura.
- El pipeline viejo de SharePoint/Excel (`_archivo/`) sigue archivado, sin
  planes de retomarlo salvo que cambie la decisión.

## Una nota sobre el motor real (`APP_VALIDADORES`/`APP_CATALOGO`)

Los validadores y el intérprete de JQL no son de este repo — son
procedimientos SQL de producción, ajenos, que pueden cambiar sin avisar. Ya
se han encontrado varias veces comportamientos reales que ni los manuales
oficiales ni los comentarios del propio procedimiento documentaban (p.ej. un
bug real de `@AdmiteNull` en las ramas TEXTO/LISTA de
`Sp_Calidad_Ejecuta_Regla_Parametrica_Base`, o la etiqueta `Dec:` de JQL para
decimales, no documentada en ningún sitio pero sí implementada). El prompt
del traductor (`traductor_reglas_calidad.py`) y `activacion/jql.py` reflejan
lo auditado en su momento, con la fecha de la auditoría anotada — no es un
enlace vivo. Si algo no cuadra entre lo que dice el prompt y lo que hace el
motor de verdad, releer el procedimiento real antes de asumir que el LLM se
equivocó.
