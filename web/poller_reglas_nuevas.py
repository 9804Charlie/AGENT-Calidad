#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Poller en segundo plano: cada INTERVALO_SEGUNDOS revisa Tb_Calidad_Regla en
busca de reglas activas que el sistema no haya visto nunca (ninguna fila en
CAL_Propuestas para su Id_Calidad_Tabla_Atributo) y las registra como
Estado='NUEVA' -- solo detecta, NO llama al LLM. Verlas y arrancar su
traduccion es un paso humano aparte: boton "Aprobar" en la pestaña
"Reglas nuevas" de revision_reglas_calidad.html, que llama a
origen_reglas.sincronizar_reglas_bd.procesar_nueva(id).

IMPORTANTE -- doble salto Kerberos: la consulta de deteccion necesita llegar
a SERVIDOR_DATOS\\INSTANCIA (APP_CATALOGO.Tb_Calidad_Regla). Si este proceso
Flask se ha lanzado via WinRM/Tarea Programada con logon S4U (en vez de una
sesion interactiva real), esa conexion falla con 'ANONYMOUS LOGON' -- Windows
no delega las credenciales a un tercer servidor en ese tipo de sesion. El
poller capa el error y solo lo deja en el log de cada vuelta, nunca tira el
proceso. Se resuelve lanzando el servicio con un logon que soporte
delegacion (sesion interactiva, o logon type Password en la tarea
programada) o configurando delegacion restringida en el dominio para la
cuenta de servicio -- pendiente de decidir, ver CONTEXTO/memoria del
despliegue en 10.0.0.10.

Uso como modulo (lo llaman api_calidad.py / servir_calidad.py al arrancar):
  from web.poller_reglas_nuevas import iniciar_poller
  iniciar_poller()
"""

import threading
import time
import traceback

INTERVALO_SEGUNDOS = 300  # 5 minutos


def _bucle():
    from origen_reglas import sincronizar_reglas_bd as sync
    while True:
        try:
            resumen = sync.detectar_nuevas()
            n = resumen.get("nuevas_detectadas", 0)
            if n:
                print(f"[poller_reglas_nuevas] {n} regla(s) nueva(s) detectada(s) en Tb_Calidad_Regla")
        except Exception as e:
            print(f"[poller_reglas_nuevas] error en la deteccion: {e}")
            traceback.print_exc()
        time.sleep(INTERVALO_SEGUNDOS)


def iniciar_poller():
    """Lanza el bucle de deteccion en un hilo daemon. Llamar UNA sola vez
    (desde el bloque __main__ de api_calidad.py/servir_calidad.py) -- no es
    idempotente, dos llamadas arrancan dos hilos duplicados."""
    hilo = threading.Thread(target=_bucle, name="poller_reglas_nuevas", daemon=True)
    hilo.start()
    print(f"[poller_reglas_nuevas] iniciado (cada {INTERVALO_SEGUNDOS}s)")
