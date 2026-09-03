#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lanzador del agente de calidad de datos con WAITRESS (servidor WSGI de
produccion), en sustitucion del servidor de desarrollo de Flask.

Sirve la app Flask definida en api_calidad.py (objeto 'app') -- servicio
PROPIO, independiente de servir_web.py/api_revision.py (puerto 5000, la app
de produccion del diccionario). Pensado para ejecutarse como su propio
servicio de Windows (NSSM o similar) y arrancar solo tras cada reinicio del
servidor donde corre Ollama.

Uso directo (para probar):
    python -m web.servir_calidad
"""

from waitress import serve
from web import api_calidad   # importa la app Flask ya definida (no lanza el server de desarrollo)

HOST = "0.0.0.0"      # accesible desde la red interna
PORT = 5001           # puerto propio, distinto del 5000 de produccion
THREADS = 4

if __name__ == "__main__":
    from web.poller_reglas_nuevas import iniciar_poller
    iniciar_poller()
    print(f"[calidad] Sirviendo api_calidad con waitress en http://{HOST}:{PORT}  (threads={THREADS})")
    serve(api_calidad.app, host=HOST, port=PORT, threads=THREADS)
