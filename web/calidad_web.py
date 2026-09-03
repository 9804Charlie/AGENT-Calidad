#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rutas web del AGENTE DE CALIDAD DE DATOS (la pieza /calidad del diccionario).

Se registra en web/api_calidad.py:

    from web.calidad_web import bp_calidad
    app.register_blueprint(bp_calidad)

Sirve:
    GET  /calidad                    -> la pagina (calidad.html)
    GET  /api/calidad/configs        -> lista de configs reglas_calidad_*.json disponibles
    GET  /api/calidad/informe?config=<archivo>
                                      -> ultimo informe CACHEADO de esa config (lectura
                                         instantanea; no ejecuta nada contra SQL/Ollama)
    POST /api/calidad/ejecutar       {config, muestra?, sin_llm?}
                                      -> ejecuta el agente EN VIVO (puede tardar: SQL +
                                         LLM por hallazgo), cachea el informe y lo devuelve

Ademas, la COLA DE REVISION de reglas (registro_propuestas_reglas.py). El
origen SharePoint (Excel/Graph) se deprecio -- ver _archivo/ -- el unico
origen activo es el nativo (Tb_Calidad_Regla/Tb_Calidad_Tabla_Atributo).
api_reglas_revisar todavia distingue DOS mecanismos de activacion por la
forma del JSON guardado, porque quedan propuestas historicas del pipeline
viejo en CAL_Propuestas:
    GET  /calidad/reglas                    -> revision_reglas_calidad.html
    GET  /api/calidad/reglas/pendientes
    GET  /api/calidad/reglas/<id>
    GET  /api/calidad/reglas/nuevas             -> reglas detectadas SIN traducir (Estado='NUEVA')
    POST /api/calidad/reglas/detectar           -> dispara a mano la misma deteccion del poller
    POST /api/calidad/reglas/<id>/aprobar_nueva -> traduce ESA regla (NUEVA -> PENDIENTE)
    POST /api/calidad/reglas/<id>/regenerar     -> reintenta el bloque/JQL con clave manual,
                                                 SIN llamar al LLM otra vez
    POST /api/calidad/reglas/<id>/revisar   {decision, revisor, comentario, reglaFinal?}
                                             -> si APROBADA, segun 'mecanismo': jql (exige
                                                jql_prueba.ok, guarda la sentencia en
                                                CAL_Bloques_JQL -- NO publica en
                                                Consulta_SQL_Base, eso sigue pendiente);
                                                generico/sql_personalizada (guarda el bloque
                                                T-SQL en CAL_Bloques_Sp_AgenteIA, NO despliega
                                                solo); pipeline viejo SharePoint (activa en
                                                reglas_calidad_<tabla>.json)
    GET  /api/calidad/sp_agente_ia/bloque_real/<id_tab> -> bloque YA escrito a mano
                                                  (Sp_Calidad_<revisor>) para esta
                                                  regla, si existe, para comparar
    GET  /api/calidad/sp_agente_ia/previsualizar -> SQL completo que se desplegaria
    POST /api/calidad/sp_agente_ia/desplegar     {confirmar:true} -> CREATE OR ALTER
                                                  PROCEDURE real en DW_PRINCIPAL

IMPORTANTE: todo va envuelto en try/except y devuelve SIEMPRE JSON, con codigo
HTTP correcto, igual que modelo_web.py: el front nunca se queda colgado en
'Cargando...'.
"""

import glob
import json
import os
import traceback

from flask import Blueprint, jsonify, request, send_from_directory

bp_calidad = Blueprint("calidad", __name__)
RAIZ = os.path.dirname(os.path.abspath(__file__))          # web/ (aqui viven las paginas/CSS)
RAIZ_REPO = os.path.dirname(RAIZ)                           # raiz del repo
CARPETA_NUCLEO = os.path.join(RAIZ_REPO, "nucleo")          # reglas_calidad_*.json / informe_*.json viven aqui


def _ruta_config(nombre: str) -> str:
    """Config de reglas (reglas_calidad_*.json), que vive en nucleo/. Evita
    path traversal: solo se sirven ficheros por su nombre base."""
    ruta = os.path.join(CARPETA_NUCLEO, os.path.basename(nombre))
    if not os.path.isfile(ruta):
        raise FileNotFoundError(f"No existe la config '{nombre}'.")
    return ruta


@bp_calidad.route("/calidad")
def pagina_calidad():
    return send_from_directory(RAIZ, "calidad.html")


@bp_calidad.route("/api/calidad/configs")
def api_configs():
    try:
        rutas = sorted(glob.glob(os.path.join(CARPETA_NUCLEO, "reglas_calidad_*.json")))
        return jsonify([os.path.basename(r) for r in rutas])
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudieron listar las configs: {e}"}), 500


@bp_calidad.route("/api/calidad/informe")
def api_informe():
    config = request.args.get("config")
    if not config:
        return jsonify({"error": "falta el parametro 'config'"}), 400
    try:
        from nucleo import agente_calidad_datos as acd
        ruta_cfg = _ruta_config(config)
        ruta_informe = acd.ruta_informe_defecto(ruta_cfg)
        if not os.path.isfile(ruta_informe):
            return jsonify({"error": "Aun no hay informe para esta config. "
                                      "Pulsa 'Ejecutar ahora'."}), 404
        with open(ruta_informe, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo leer el informe: {e}"}), 500


@bp_calidad.route("/api/calidad/ejecutar", methods=["POST"])
def api_ejecutar():
    d = request.get_json(silent=True) or {}
    config = d.get("config")
    if not config:
        return jsonify({"error": "falta 'config'"}), 400
    muestra = int(d.get("muestra") or 20)
    sin_llm = bool(d.get("sin_llm"))
    try:
        from nucleo import agente_calidad_datos as acd
        ruta_cfg = _ruta_config(config)
        informe = acd.ejecutar(ruta_cfg, limite_muestra=muestra, redactar=not sin_llm)
        return jsonify(informe)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error ejecutando el agente de calidad: {e}"}), 500


# --------------------- COLA DE REVISION DE REGLAS (HITL) -------------------- #
@bp_calidad.route("/calidad/reglas")
def pagina_reglas():
    return send_from_directory(RAIZ, "revision_reglas_calidad.html")


@bp_calidad.route("/api/calidad/reglas/pendientes")
def api_reglas_pendientes():
    try:
        from origen_reglas import registro_propuestas_reglas as rpr
        return jsonify(rpr.pendientes())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudieron listar las propuestas: {e}"}), 500


@bp_calidad.route("/api/calidad/reglas/<int:id_propuesta>")
def api_reglas_ver(id_propuesta):
    try:
        from origen_reglas import registro_propuestas_reglas as rpr
        d = rpr.ver(id_propuesta)
        if not d:
            return jsonify({"error": "no existe esa propuesta"}), 404
        return jsonify(d)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo leer la propuesta: {e}"}), 500


@bp_calidad.route("/api/calidad/reglas/nuevas")
def api_reglas_nuevas():
    """Reglas detectadas en Tb_Calidad_Regla que el sistema no habia visto
    nunca, todavia SIN traducir (Estado='NUEVA') -- pestaña 'Reglas nuevas'."""
    try:
        from origen_reglas import registro_propuestas_reglas as rpr
        return jsonify(rpr.nuevas())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudieron listar las reglas nuevas: {e}"}), 500


@bp_calidad.route("/api/calidad/reglas/detectar", methods=["POST"])
def api_reglas_detectar():
    """Disparo manual de la misma deteccion que hace el poller en segundo
    plano (web/poller_reglas_nuevas.py) cada 5 minutos -- util para refrescar
    la pestaña 'Reglas nuevas' sin esperar a la siguiente vuelta."""
    try:
        from origen_reglas import sincronizar_reglas_bd as srb
        return jsonify(srb.detectar_nuevas())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error detectando reglas nuevas: {e}"}), 500


@bp_calidad.route("/api/calidad/reglas/<int:id_propuesta>/aprobar_nueva", methods=["POST"])
def api_reglas_aprobar_nueva(id_propuesta):
    """Boton 'Aprobar' de la pestaña 'Reglas nuevas': traduce esta UNA regla
    con el LLM y genera la vista previa del bloque SQL, dejandola en
    PENDIENTE -- de ahi en adelante es la cola de revision de siempre
    (/calidad/reglas), esto NO activa ni despliega nada."""
    try:
        from origen_reglas import sincronizar_reglas_bd as srb
        return jsonify(srb.procesar_nueva(id_propuesta))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error procesando la regla nueva: {e}"}), 500


@bp_calidad.route("/calidad/evaluacion")
def pagina_evaluacion():
    return send_from_directory(RAIZ, "evaluacion_traduccion.html")


@bp_calidad.route("/api/calidad/evaluacion/lanzar", methods=["POST"])
def api_evaluacion_lanzar():
    """Lanza en segundo plano el lote de comparacion LLM vs bloques reales.
    {"implementadas": [44,61,...], "no_implementadas": [8,9,...], "max_intentos"?: 20}
    max_intentos es el techo de reintentos SOLO para 'implementada' (por
    defecto 20); pasa 1 para una pasada rapida de una sola prueba por regla."""
    d = request.get_json(silent=True) or {}
    ids_impl = d.get("implementadas") or []
    ids_no_impl = d.get("no_implementadas") or []
    max_intentos = d.get("max_intentos")
    if not ids_impl and not ids_no_impl:
        return jsonify({"error": "indica al menos un Id_Calidad_Tabla_Atributo"}), 400
    try:
        from origen_reglas import evaluar_traduccion as ev
        kwargs = {"max_intentos_impl": int(max_intentos)} if max_intentos else {}
        lote = ev.ejecutar_lote_en_segundo_plano(ids_impl, ids_no_impl, **kwargs)
        return jsonify({"lote": lote})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo lanzar el lote: {e}"}), 500


@bp_calidad.route("/api/calidad/evaluacion/estado")
def api_evaluacion_estado():
    try:
        from origen_reglas import evaluar_traduccion as ev
        return jsonify(ev.estado_lote())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@bp_calidad.route("/api/calidad/evaluacion/reprocesar", methods=["POST"])
def api_evaluacion_reprocesar():
    """Recalcula 'Coincide' de un lote ya corrido, sin volver a llamar al
    LLM -- util tras corregir un bug en la heuristica de comparacion."""
    d = request.get_json(silent=True) or {}
    lote = d.get("lote")
    if not lote:
        return jsonify({"error": "falta 'lote'"}), 400
    try:
        from origen_reglas import evaluar_traduccion as ev
        cambios = ev.reprocesar_lote(lote)
        return jsonify({"lote": lote, "filas_actualizadas": cambios})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@bp_calidad.route("/api/calidad/evaluacion/lotes")
def api_evaluacion_lotes():
    try:
        from origen_reglas import evaluar_traduccion as ev
        return jsonify(ev.lotes_disponibles())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@bp_calidad.route("/api/calidad/evaluacion/resultados")
def api_evaluacion_resultados():
    lote = request.args.get("lote")
    try:
        from origen_reglas import evaluar_traduccion as ev
        return jsonify(ev.resultados_lote(lote))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@bp_calidad.route("/api/calidad/evaluacion/<int:id_fila>/etiquetar", methods=["POST"])
def api_evaluacion_etiquetar(id_fila):
    """Guarda el juicio HUMANO sobre un resultado ya evaluado, independiente
    del veredicto heuristico 'Coincide' -- para detectar falsos positivos/
    negativos del arnes y acumular ejemplos confirmados.
    {"correcta": true|false|null, "revisor": "...", "nota": "..."}
    correcta=null quita la etiqueta (vuelve a "sin revisar")."""
    d = request.get_json(silent=True) or {}
    revisor = (d.get("revisor") or "").strip()
    correcta = d.get("correcta")
    if correcta is not None and not revisor:
        return jsonify({"error": "falta el revisor"}), 400
    try:
        from origen_reglas import evaluar_traduccion as ev
        ok = ev.etiquetar_resultado(id_fila, correcta, revisor, d.get("nota"))
        if not ok:
            return jsonify({"error": "no se encontro esa fila"}), 404
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@bp_calidad.route("/api/calidad/reglas/<int:id_propuesta>/regenerar", methods=["POST"])
def api_reglas_regenerar(id_propuesta):
    """Reintenta SOLO la generacion/prueba del bloque de una propuesta ya
    traducida, SIN llamar al LLM -- para corregir 'no se encontro clave
    primaria' con clave_cols manual, o un JQL editado a mano.
    {"clave_cols": ["Col1","Col2"]} y/o {"jql": "NUMERO ... Clave:..."}"""
    d = request.get_json(silent=True) or {}
    clave_cols = d.get("clave_cols") or None
    jql_editado = d.get("jql") or None
    try:
        from origen_reglas import sincronizar_reglas_bd as srb
        return jsonify(srb.regenerar_bloque(id_propuesta, clave_cols=clave_cols, jql=jql_editado))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error regenerando: {e}"}), 500


@bp_calidad.route("/api/calidad/reglas/<int:id_propuesta>/registrar_ejecucion", methods=["POST"])
def api_reglas_registrar_ejecucion(id_propuesta):
    """Ejecuta el JQL de una propuesta DEJANDO CONSTANCIA en el log de pruebas
    del motor (Tb_Calidad_Resultado_Validacion_Pruebas), para poder replicar y
    auditar esa ejecucion despues.

    Es una accion MANUAL y a proposito: la prueba automatica de cada propuesta
    (probar_jql_inline) sigue sin registrar nada, porque un lote de evaluacion
    son cientos de ejecuciones y llenarian una tabla compartida con el resto
    del equipo.

    Escribe SOLO en la tabla de pruebas, nunca en el log real de produccion
    (@Pruebas=1 siempre). Tampoco publica el JQL en
    Tb_Calidad_Tabla_Atributo.Consulta_SQL_Base: eso sigue siendo otra cosa."""
    try:
        from origen_reglas import registro_propuestas_reglas as rpr
        from activacion import jql as jqlmod
        actual = rpr.ver(id_propuesta)
        if not actual:
            return jsonify({"error": f"No existe la propuesta #{id_propuesta}"}), 404
        regla = actual.get("Regla_Final_JSON") or actual.get("Regla_Propuesta_JSON") or {}
        if regla.get("mecanismo") != "jql":
            return jsonify({"error": "Solo las reglas con mecanismo 'jql' se pueden ejecutar "
                                      "y registrar por esta via; los otros mecanismos generan "
                                      "un bloque que se ejecuta al desplegar el procedimiento."}), 400
        id_tab = actual.get("Id_Calidad_Tabla_Atributo")
        if not id_tab:
            return jsonify({"error": "La propuesta no tiene Id_Calidad_Tabla_Atributo; sin el "
                                      "el motor no puede registrar el resultado."}), 400
        resultado = jqlmod.registrar_ejecucion(
            int(id_tab), actual.get("Atributo"), regla.get("jql"),
            desc_funcional=actual.get("Desc_Funcional_Original"))
        if not resultado.get("ok"):
            return jsonify({"error": resultado.get("error") or "No se pudo ejecutar el JQL",
                            "jql": resultado.get("jql")}), 400
        return jsonify({"id_propuesta": id_propuesta,
                        "id_calidad_tabla_atributo": id_tab,
                        "jql": resultado.get("jql"),
                        "filtro_poblacion": resultado.get("filtro_poblacion"),
                        "filas_ko": len(resultado.get("filas") or []),
                        "registrado_en": resultado.get("tabla_log")})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error registrando la ejecucion: {e}"}), 500


@bp_calidad.route("/api/calidad/sp_agente_ia/columnas")
def api_sp_agente_ia_columnas():
    """Esquema real (columnas) de una tabla/vista de DW_PRINCIPAL, dado su Clave_Fisico
    completo (?clave=SRV1.DW_PRINCIPAL.dbo.Tabla.Columna) -- lo mismo que se le da
    al LLM al traducir, util para depurar/inspeccionar a mano."""
    clave = request.args.get("clave") or ""
    try:
        from activacion import generador_sp_agente_ia as gen
        partes = gen.parsear_clave(clave)
        return jsonify(gen.columnas_de(partes["esquema"], partes["tabla"]))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo obtener el esquema: {e}"}), 500


@bp_calidad.route("/api/calidad/sp_agente_ia/procedimientos")
def api_sp_agente_ia_procedimientos():
    """Nombres de los Sp_Calidad_<revisor> YA en produccion en DW_PRINCIPAL (sin sus
    definiciones completas, para no sobrecargar la respuesta)."""
    try:
        from activacion import generador_sp_agente_ia as gen
        return jsonify([p["nombre"] for p in gen.listar_procedimientos_calidad()])
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo listar los procedimientos: {e}"}), 500


@bp_calidad.route("/api/calidad/sp_agente_ia/procedimientos_completo")
def api_sp_agente_ia_procedimientos_completo():
    """Como el anterior pero con la definicion completa de cada uno -- para
    analisis puntual (comparar muchos IDs de golpe sin releer DW_PRINCIPAL en cada
    llamada). Payload grande, uso ocasional."""
    try:
        from activacion import generador_sp_agente_ia as gen
        return jsonify(gen.listar_procedimientos_calidad())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo listar los procedimientos: {e}"}), 500


@bp_calidad.route("/api/calidad/sp_agente_ia/bloque_real/<int:id_calidad_tabla_atributo>")
def api_sp_agente_ia_bloque_real(id_calidad_tabla_atributo):
    """Busca si algun Sp_Calidad_<revisor> YA en produccion (escrito a mano,
    anterior a Sp_Calidad_AgenteIA) tiene un bloque para esta regla -- para
    comparar contra lo que propone el LLM antes de aprobar."""
    try:
        from activacion import generador_sp_agente_ia as gen
        return jsonify(gen.buscar_bloque_real(id_calidad_tabla_atributo))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo buscar en los procedimientos existentes: {e}"}), 500


@bp_calidad.route("/api/calidad/sp_agente_ia/previsualizar")
def api_sp_agente_ia_previsualizar():
    """Devuelve el SQL completo que se desplegaria (CREATE OR ALTER PROCEDURE),
    SIN ejecutar nada -- junta todos los bloques ya aprobados."""
    try:
        from activacion import generador_sp_agente_ia as gen
        return jsonify({"sql": gen.previsualizar_despliegue(), "bloques": gen.listar_bloques()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"No se pudo generar la vista previa: {e}"}), 500


@bp_calidad.route("/api/calidad/sp_agente_ia/desplegar", methods=["POST"])
def api_sp_agente_ia_desplegar():
    """Ejecuta de verdad el CREATE OR ALTER PROCEDURE dbo.Sp_Calidad_AgenteIA en
    DW_PRINCIPAL. Requiere {"confirmar": true} explicito -- no hay despliegue por
    accidente."""
    d = request.get_json(silent=True) or {}
    if not d.get("confirmar"):
        return jsonify({"error": "falta 'confirmar: true' -- no se despliega sin confirmacion explicita"}), 400
    try:
        from activacion import generador_sp_agente_ia as gen
        resultado = gen.desplegar(ejecutar=True)
        return jsonify(resultado)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error desplegando Sp_Calidad_AgenteIA: {e}"}), 500


@bp_calidad.route("/api/calidad/reglas/<int:id_propuesta>/revisar", methods=["POST"])
def api_reglas_revisar(id_propuesta):
    d = request.get_json(silent=True) or {}
    decision = (d.get("decision") or "").upper()
    revisor = d.get("revisor")
    comentario = d.get("comentario")
    regla_final = d.get("reglaFinal")

    if decision not in ("APROBADA", "RECHAZADA"):
        return jsonify({"error": "decision debe ser APROBADA o RECHAZADA"}), 400
    if not revisor:
        return jsonify({"error": "falta el revisor"}), 400
    if decision == "RECHAZADA" and not (comentario and comentario.strip()):
        return jsonify({"error": "un rechazo necesita comentario"}), 400

    try:
        from origen_reglas import registro_propuestas_reglas as rpr
        actual = rpr.ver(id_propuesta)
        if not actual:
            return jsonify({"error": "no existe esa propuesta"}), 404
        if actual.get("Estado") != "PENDIENTE":
            return jsonify({"error": "la propuesta ya estaba revisada"}), 409

        activada_en = None
        if decision == "APROBADA":
            # Se intenta ACTIVAR ANTES de persistir la decision: si falla, la
            # propuesta sigue PENDIENTE y el revisor puede corregirla y
            # reintentar -- nunca queda "aprobada" sin activar.
            #
            # TRES mecanismos posibles segun la forma del JSON:
            #   - jql: ya se prueba inline al traducir/regenerar (ver
            #     sincronizar_reglas_bd.py); aqui solo se exige que esa
            #     prueba haya sido un exito. Publicar en Consulta_SQL_Base
            #     (tabla compartida de produccion) NO esta implementado
            #     todavia -- es una decision aparte, deliberada, pendiente.
            #   - generico / sql_personalizada (Tb_Calidad_Regla ->
            #     Sp_Calidad_AgenteIA): generador_sp_agente_ia.generar_bloque()
            #     ya sabe distinguir los dos.
            #   - ninguno de los anteriores (pipeline viejo, SharePoint +
            #     reglas_calidad_<tabla>.json): activar_regla_aprobada().
            regla_original = actual.get("Regla_Propuesta_JSON") or {}
            regla_efectiva = regla_final or regla_original
            mecanismo = regla_efectiva.get("mecanismo") or regla_original.get("mecanismo")
            es_mecanismo_nuevo = bool(mecanismo) or "condicion_violada" in regla_original or "condicion_violada" in regla_efectiva

            try:
                if mecanismo == "jql":
                    prueba = regla_efectiva.get("jql_prueba") or {}
                    if not prueba.get("ok"):
                        return jsonify({"error": "El JQL no se ha probado con exito todavia "
                                                  "(jql_prueba.ok debe ser true) -- usa 'Reintentar con esto' "
                                                  "para probarlo antes de aprobar."}), 400
                    from activacion import jql as jqlmod
                    clave_fisico = actual.get("Atributo")  # en este pipeline, Atributo = Clave_Fisico
                    id_tab = actual.get("Id_Calidad_Tabla_Atributo")
                    jqlmod.guardar_jql(id_tab, regla_efectiva.get("jql"), clave_fisico, revisor,
                                        desc_funcional=actual.get("Desc_Funcional_Original"))
                    activada_en = (f"CAL_Bloques_JQL (Id_Calidad_Tabla_Atributo={id_tab}) -- probado inline "
                                   "contra datos reales. Publicar en Tb_Calidad_Tabla_Atributo.Consulta_SQL_Base "
                                   "todavia no esta implementado -- decision aparte pendiente.")
                elif es_mecanismo_nuevo:
                    from activacion import generador_sp_agente_ia as gen
                    clave_fisico = actual.get("Atributo")  # en este pipeline, Atributo = Clave_Fisico
                    id_tab = actual.get("Id_Calidad_Tabla_Atributo")
                    clave_cols = regla_efectiva.get("clave_cols") or None
                    bloque = gen.generar_bloque(id_tab, clave_fisico, regla_efectiva, clave_cols=clave_cols)
                    gen.guardar_bloque(id_tab, bloque, clave_fisico, revisor)
                    activada_en = f"CAL_Bloques_Sp_AgenteIA (Id_Calidad_Tabla_Atributo={id_tab}) -- pendiente de 'Desplegar'"
                else:
                    tabla_destino_efectiva = actual.get("Tabla_Destino")
                    if regla_efectiva.get("columna_elegida"):
                        from origen_reglas import resolver_atributo as ra
                        tabla_destino_efectiva = ra._tabla_de_clave(regla_efectiva["columna_elegida"])
                    from nucleo import agente_calidad_datos as acd
                    propuesta_para_activar = {
                        "Tabla_Destino": tabla_destino_efectiva,
                        "Regla_Final_JSON": regla_final,
                        "Regla_Propuesta_JSON": regla_original,
                    }
                    activada_en = acd.activar_regla_aprobada(propuesta_para_activar)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

        ok = rpr.revisar(id_propuesta, decision, revisor, comentario, regla_final)
        if not ok:
            return jsonify({"error": "la propuesta no existe o ya estaba revisada"}), 409
        return jsonify({"ok": True, "activada_en": activada_en})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error al revisar: {e}"}), 500
