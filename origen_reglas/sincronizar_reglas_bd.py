#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orquestador del NUEVO origen de reglas activas: Tb_Calidad_Regla (Es_Activa=1)
cruzada con Tb_Calidad_Tabla_Atributo -- el sistema de calidad NATIVO ya en
produccion, no la lista de SharePoint. Ver plan de migracion (2026-08).

A diferencia de sincronizar_reglas_calidad.py (que se conserva intacto, por si
se retoma el pipeline de SharePoint), aqui:
  - NO hace falta resolver_atributo.py: Clave_Fisico ya viene resuelta.
  - El LLM elige entre TRES mecanismos, en este orden de preferencia (ver
    traductor_reglas_calidad._SISTEMA_CONDICION): JQL (activacion/jql.py,
    probado inline contra datos reales sin publicar nada -- ver
    _procesar_propuesta), validador generico de APP_VALIDADORES, o condicion SQL
    a mano (generador_sp_agente_ia.generar_bloque).
  - Antes de traducir, se le pasa al LLM el ESQUEMA REAL de la tabla destino
    y su CLAVE PRIMARIA real (generador_sp_agente_ia.columnas_de /
    columnas_clave_de) para que no invente nombres de columnas hermanas ni
    la clave. Degrada con gracia (None) si DW_PRINCIPAL no es alcanzable.

Reutiliza la MISMA cola de revision (registro_propuestas_reglas.py,
CAL_Propuestas) -- el "id" de deduplicacion es agnostico a su origen.

Ademas de sincronizar() (detecta + traduce TODO de golpe), expone el mismo
trabajo partido en dos pasos para la pestaña "Reglas nuevas" del frontend:
  - detectar_nuevas(): solo detecta, sin LLM, barato (poller en segundo
    plano o boton "Buscar ahora"). Registra cada hallazgo como Estado='NUEVA'.
  - procesar_nueva(id): boton "Aprobar" de esa pestaña -- traduce UNA sola
    propuesta NUEVA y la deja en PENDIENTE (cola de revision de siempre).

Uso como modulo (lo llama calidad_web.py):
  from origen_reglas.sincronizar_reglas_bd import sincronizar, detectar_nuevas, procesar_nueva
  resumen = sincronizar()

Uso CLI:
  python -m origen_reglas.sincronizar_reglas_bd
  python -m origen_reglas.sincronizar_reglas_bd --solo-detectar
"""

import argparse
import re
import sys
import unicodedata

# "comprobando si existe en la dimension tiempo" -- el catalogo repite esta
# frase casi palabra por palabra en muchas reglas de fecha.
_RE_DIM_TIEMPO = re.compile(r"dimension\s+tiempo")


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


def _mecanismo_incoherente(desc_funcional: str, propuesta: dict) -> str:
    """Devuelve por que el mecanismo elegido no encaja con la regla, o None.

    Hoy solo cubre un caso, pero es el mas repetido del catalogo: "la fecha
    tiene que ser valida, comprobando si existe en la dimension tiempo". Para
    eso hay soporte nativo -- JQL FECHA con EnDimTiempo:S, o el validador
    generico Fecha con validar_dim -- y escribir SQL a mano es siempre la
    respuesta equivocada: obliga a nombrar la tabla de la dimension, que el
    LLM no tiene en el esquema y termina inventandose (en la regla 49 escribio
    "Dim_Tiempo", que no existe).

    Medido sobre el lote del 2026-08-18: de las 16 traducciones de reglas que
    mencionan la dimension tiempo, NINGUNA eligio JQL; las 14 que fueron por
    sql_personalizada estan marcadas incorrectas por el revisor, y la unica
    aceptada uso el mecanismo generico. Por eso aqui solo se rechaza
    sql_personalizada, no generico."""
    if not desc_funcional or propuesta.get("mecanismo") != "sql_personalizada":
        return None
    if not _RE_DIM_TIEMPO.search(_sin_tildes(desc_funcional).lower()):
        return None
    return ("La regla pide comprobar la fecha contra la dimension tiempo, y "
            "para eso hay soporte nativo: JQL 'FECHA Nulos:<S/N> EnDimTiempo:S "
            "Clave:<clave>', o el mecanismo generico (validador Fecha con "
            "validar_dim). Escribir SQL a mano obliga a nombrar la tabla de la "
            "dimension, que no esta en el esquema que se te da -- de hecho el "
            "LLM se la inventa. Vuelve a traducirla con uno de esos dos.")


def _columnas_para(clave_fisico: str) -> list:
    """Esquema real de la tabla destino, para dar contexto al LLM y que no
    invente columnas hermanas (ver traductor_reglas_calidad.traducir_condicion_sql).
    Busca en DW_PRINCIPAL/DW_CRUDO/DW_LAGO (generador_sp_agente_ia.resolver_base_datos):
    algunas reglas del catalogo declaran DW_PRINCIPAL en Clave_Fisico pero la tabla
    real vive en DW_CRUDO. Degrada a None si no aparece en ninguna -- la
    traduccion sigue funcionando igual que antes, solo sin ese contexto extra."""
    from activacion import generador_sp_agente_ia as gen
    try:
        partes = gen.parsear_clave(clave_fisico)
        return gen.columnas_de(partes["esquema"], partes["tabla"], base_datos=partes["base_datos"])
    except Exception:
        return None


def _clave_cols_para(clave_fisico: str) -> list:
    """PK real de la tabla destino (busca en DW_PRINCIPAL/DW_CRUDO/DW_LAGO, con
    fallback a indice unico sin constraint), para la etiqueta Clave:
    obligatoria en JQL. Degrada a None si no se puede determinar (p.ej.
    vista sin PK ni indice unico)."""
    from activacion import generador_sp_agente_ia as gen
    try:
        partes = gen.parsear_clave(clave_fisico)
        return gen.columnas_clave_de(partes["esquema"], partes["tabla"], base_datos=partes["base_datos"]) or None
    except Exception:
        return None


def _procesar_propuesta(id_tab, clave_fisico: str, propuesta: dict, clave_cols: list = None,
                         desc_funcional: str = None) -> tuple:
    """Ejecuta el mecanismo elegido por el LLM (jql / generico / sql_personalizada)
    y devuelve (propuesta_actualizada, exito: bool). Para JQL: valida y PRUEBA
    de verdad contra datos reales (activacion.jql.probar_jql_inline), sin
    escribir en ningun sitio. Para generico/sql_personalizada: ensambla el
    bloque T-SQL de siempre (generador_sp_agente_ia.generar_bloque).

    clave_cols: fuerza la clave (en vez de auto-detectarla) para
    generico/sql_personalizada -- usado por regenerar_bloque() cuando la
    deteccion automatica no encontro PK ni indice unico.

    desc_funcional: texto original de la regla; se lo pasa a la prueba JQL
    para cazar reglas de decimales traducidas sin Dec (jql.verificar_dec_necesario)."""
    from activacion import generador_sp_agente_ia as gen
    from activacion import jql as jqlmod

    mecanismo = propuesta.get("mecanismo")

    incoherente = _mecanismo_incoherente(desc_funcional, propuesta)
    if incoherente:
        propuesta["aviso_bloque"] = incoherente
        return propuesta, False

    if mecanismo == "jql":
        cadena = propuesta.get("jql") or ""
        try:
            resultado = jqlmod.probar_jql_inline(clave_fisico, cadena, pruebas=True,
                                                  desc_funcional=desc_funcional)
            propuesta["jql"] = resultado["jql"]  # la version realmente ejecutada (limpia)
            propuesta["jql_prueba"] = {
                "ok": resultado["ok"],
                "total_filas_ko": len(resultado["filas"]) if resultado["ok"] else None,
                "muestra_filas_ko": resultado["filas"][:20] if resultado["ok"] else [],
                "error": resultado["error"],
            }
            if resultado["ok"]:
                propuesta.pop("aviso_bloque", None)
            return propuesta, resultado["ok"]
        except Exception as e:
            propuesta["aviso_bloque"] = f"JQL invalido: {e}"
            return propuesta, False

    if mecanismo == "generico" or propuesta.get("condicion_violada"):
        try:
            bloque = gen.generar_bloque(id_tab, clave_fisico, propuesta, clave_cols=clave_cols)
            propuesta["bloque_sql_preview"] = bloque
            propuesta.pop("aviso_bloque", None)
            return propuesta, True
        except Exception as e:
            propuesta["aviso_bloque"] = f"No se pudo generar el bloque automaticamente: {e}"
            return propuesta, False

    return propuesta, False


def sincronizar() -> dict:
    from origen_reglas import registro_propuestas_reglas as rpr
    from origen_reglas import traductor_reglas_calidad as trad
    from origen_reglas import extraer_reglas_activas_bd as ext
    from activacion import generador_sp_agente_ia as gen

    activas = ext.extraer_reglas_activas()
    resumen = {"total_activas": len(activas), "nuevas": 0, "actualizadas": 0,
               "sin_cambios": 0, "sin_bloque_automatico": 0, "detalle": []}

    for regla in activas:
        id_tab = regla["id_calidad_tabla_atributo"]
        clave_fisico = regla["clave_fisico"]
        desc = regla.get("desc_funcional") or ""

        previa = rpr.buscar_por_id_sharepoint(id_tab)
        es_nueva = not previa
        cambio = bool(previa) and (previa.get("Desc_Funcional_Original") != desc)

        if previa and not cambio:
            resumen["sin_cambios"] += 1
            continue

        propuesta = trad.traducir_condicion_sql(
            desc, clave_fisico, columnas_tabla=_columnas_para(clave_fisico),
            clave_cols=_clave_cols_para(clave_fisico))

        propuesta, exito = _procesar_propuesta(id_tab, clave_fisico, propuesta,
                                                desc_funcional=desc)
        if not exito:
            resumen["sin_bloque_automatico"] += 1

        try:
            partes = gen.parsear_clave(clave_fisico)
            tabla_destino = f"{partes['esquema']}.{partes['tabla']}"
        except Exception:
            tabla_destino = None

        nuevo_id = rpr.registrar(
            atributo=clave_fisico, id_sharepoint=id_tab, desc_funcional=desc,
            regla_propuesta=propuesta, tabla_destino=tabla_destino,
            confianza=propuesta.get("confianza"),
        )
        resumen["nuevas" if es_nueva else "actualizadas"] += 1
        resumen["detalle"].append({
            "id_propuesta": nuevo_id, "clave_fisico": clave_fisico,
            "tabla_destino": tabla_destino, "confianza": propuesta.get("confianza"),
            "mecanismo": propuesta.get("mecanismo"), "bloque_generado": exito, "nueva": es_nueva,
        })

    return resumen


def detectar_nuevas() -> dict:
    """Solo DETECTA reglas activas que el sistema no ha visto nunca (ninguna
    fila en CAL_Propuestas para su Id_Calidad_Tabla_Atributo, en ningun
    estado) y las registra como Estado='NUEVA' -- NO llama al LLM, es barato
    y pensado para ejecutarse a menudo (poller en segundo plano o al abrir
    la pestaña 'Reglas nuevas'). Ver procesar_nueva() para el paso que las
    traduce, un boton 'Aprobar' a la vez."""
    from origen_reglas import registro_propuestas_reglas as rpr
    from origen_reglas import extraer_reglas_activas_bd as ext
    from activacion import generador_sp_agente_ia as gen

    activas = ext.extraer_reglas_activas()
    detectadas = []
    for regla in activas:
        id_tab = regla["id_calidad_tabla_atributo"]
        if rpr.buscar_por_id_sharepoint(id_tab):
            continue  # ya hay una propuesta (en cualquier estado) para esta

        clave_fisico = regla["clave_fisico"]
        desc = regla.get("desc_funcional") or ""
        try:
            partes = gen.parsear_clave(clave_fisico)
            tabla_destino = f"{partes['esquema']}.{partes['tabla']}"
        except Exception:
            tabla_destino = None

        nuevo_id = rpr.registrar(
            atributo=clave_fisico, id_sharepoint=id_tab, desc_funcional=desc,
            regla_propuesta=None, tabla_destino=tabla_destino, confianza=None,
            estado="NUEVA",
        )
        detectadas.append({"id_propuesta": nuevo_id, "clave_fisico": clave_fisico,
                            "id_calidad_regla": regla.get("id_calidad_regla")})

    return {"total_activas": len(activas), "nuevas_detectadas": len(detectadas), "detalle": detectadas}


def procesar_nueva(id_propuesta: int) -> dict:
    """Boton 'Aprobar' de la pestaña 'Reglas nuevas': toma UNA propuesta en
    estado NUEVA y ejecuta la traduccion LLM + vista previa del bloque SQL
    (lo que sincronizar()/detectar_nuevas()+esto hacen juntos para las 39 de
    golpe), dejandola en PENDIENTE -- de ahi en adelante es la misma cola de
    revision humana de siempre (/calidad/reglas)."""
    from origen_reglas import registro_propuestas_reglas as rpr
    from origen_reglas import traductor_reglas_calidad as trad

    actual = rpr.ver(id_propuesta)
    if not actual:
        raise ValueError(f"No existe la propuesta #{id_propuesta}")
    if actual["Estado"] != "NUEVA":
        raise ValueError(f"La propuesta #{id_propuesta} no esta en estado NUEVA (esta en {actual['Estado']})")

    clave_fisico = actual["Atributo"]
    desc = actual["Desc_Funcional_Original"] or ""
    id_tab = actual["Id_Calidad_Tabla_Atributo"]

    propuesta = trad.traducir_condicion_sql(
        desc, clave_fisico, columnas_tabla=_columnas_para(clave_fisico),
        clave_cols=_clave_cols_para(clave_fisico))
    propuesta, exito = _procesar_propuesta(id_tab, clave_fisico, propuesta,
                                            desc_funcional=desc)

    rpr.actualizar_procesada(id_propuesta, propuesta, confianza=propuesta.get("confianza"))
    return {"id_propuesta": id_propuesta, "confianza": propuesta.get("confianza"),
            "mecanismo": propuesta.get("mecanismo"), "bloque_generado": exito}


def regenerar_bloque(id_propuesta: int, clave_cols: list = None, jql: str = None) -> dict:
    """Reintenta SOLO la generacion/prueba del bloque de una propuesta YA
    TRADUCIDA (cualquier estado), SIN volver a llamar al LLM -- para corregir
    casos donde la deteccion automatica de clave primaria fallo (vista sin
    PK ni indice unico: generador_sp_agente_ia.resolver_base_datos ya prueba
    DW_PRINCIPAL/DW_CRUDO/DW_LAGO y un indice unico como respaldo, pero algunas
    vistas de verdad no tienen ninguno).

    clave_cols: columnas clave a forzar (generico/sql_personalizada).
    jql: texto JQL editado a mano, reemplaza el que habia (mecanismo jql;
    su 'Clave:' va dentro de la cadena, no como parametro aparte)."""
    from origen_reglas import registro_propuestas_reglas as rpr

    actual = rpr.ver(id_propuesta)
    if not actual:
        raise ValueError(f"No existe la propuesta #{id_propuesta}")

    clave_fisico = actual["Atributo"]
    id_tab = actual["Id_Calidad_Tabla_Atributo"]
    propuesta = actual.get("Regla_Propuesta_JSON") or {}
    if not propuesta.get("mecanismo"):
        raise ValueError("Esta propuesta no tiene un mecanismo elegido todavia "
                         "(sigue en NUEVA); usa 'Aprobar' en Reglas nuevas primero.")

    if jql is not None:
        propuesta["jql"] = jql

    propuesta, exito = _procesar_propuesta(id_tab, clave_fisico, propuesta, clave_cols=clave_cols,
                                            desc_funcional=actual.get("Desc_Funcional_Original"))

    rpr.actualizar_regla_propuesta(id_propuesta, propuesta)
    return {"id_propuesta": id_propuesta, "mecanismo": propuesta.get("mecanismo"), "exito": exito}


def main():
    ap = argparse.ArgumentParser(description="Sincroniza las reglas activas de Tb_Calidad_Regla/Tb_Calidad_Tabla_Atributo a la cola de revision")
    ap.add_argument("--solo-detectar", action="store_true",
                     help="Solo detecta reglas nuevas (Estado=NUEVA), sin traducir con el LLM")
    args = ap.parse_args()

    if args.solo_detectar:
        try:
            resumen = detectar_nuevas()
        except Exception as e:
            print("Error detectando:", e); sys.exit(1)
        print(f"Activas en Tb_Calidad_Regla: {resumen['total_activas']}")
        print(f"  Nuevas detectadas: {resumen['nuevas_detectadas']}")
        return

    try:
        resumen = sincronizar()
    except Exception as e:
        print("Error sincronizando:", e); sys.exit(1)

    print(f"Activas en Tb_Calidad_Regla: {resumen['total_activas']}")
    print(f"  Nuevas:               {resumen['nuevas']}")
    print(f"  Actualizadas:         {resumen['actualizadas']}")
    print(f"  Sin cambios:          {resumen['sin_cambios']}")
    print(f"  Sin bloque automatico:{resumen['sin_bloque_automatico']}  (necesitan clave_cols/condicion a mano)")


if __name__ == "__main__":
    main()
