#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API STANDALONE del AGENTE DE CALIDAD DE DATOS -- servicio PROPIO, independiente
de api_revision.py (la app de produccion del diccionario, puerto 5000).

Se ejecuta como su propio servicio en su propio puerto (5001), para no
depender del ciclo de despliegue de la app de produccion ni arriesgar su
disponibilidad -- cambios/errores de este agente no pueden tumbar el comite de
fichas ni el buscador. Pensado para correr en la MISMA maquina que Ollama:
agente_calidad_datos.py / traductor_reglas_calidad.py hablan con Ollama por
localhost:11434 directamente (ver esa configuracion en esos ficheros), sin
proxy ni abrir ese puerto a la red.

Sirve:
  GET  /calidad                    -> informe del agente (ver calidad_web.py)
  GET  /calidad/reglas             -> cola de revision de reglas traducidas
  GET  /api/calidad/...            -> API del agente (ver calidad_web.py)

Requisitos:
  pip install flask pyodbc jsonschema requests

Arranque (desarrollo):
  python -m web.api_calidad            ->  http://localhost:5001

Arranque (produccion, con waitress): ver servir_calidad.py
"""

from flask import Flask, send_from_directory

from web.calidad_web import bp_calidad, RAIZ

# ----------------------------- CONFIGURACION ------------------------------- #
HOST = "0.0.0.0"
PUERTO = 5001
# --------------------------------------------------------------------------- #

app = Flask(__name__)
app.register_blueprint(bp_calidad)


@app.route("/ui_base.css")
def ui_base_css():
    # mismo fichero de estilos compartido que usa api_revision.py; se sirve
    # aqui tambien porque este es un servicio Flask distinto (puerto propio).
    return send_from_directory(RAIZ, "ui_base.css")


if __name__ == "__main__":
    from web.poller_reglas_nuevas import iniciar_poller
    iniciar_poller()
    print(f"Sirviendo agente de calidad en http://{HOST}:{PUERTO}  (Ctrl+C para parar)")
    print(f"  Informes  : http://{HOST}:{PUERTO}/calidad")
    print(f"  Reglas    : http://{HOST}:{PUERTO}/calidad/reglas")
    app.run(host=HOST, port=PUERTO, debug=False)
