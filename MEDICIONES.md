# Mediciones de referencia

Lotes de evaluación que sirven como **línea base** para comparar cambios futuros.
Los datos completos siguen en `WORKSPACE_AGENTE.dbo.CAL_Pruebas_Traduccion`,
buscando por el identificador de lote; aquí solo queda anotado qué mide cada uno
y por qué importa, que es lo que no se puede deducir mirando la tabla.

---

## `lote_20260901_120113` — efecto de ampliar la ventana de contexto

**Fecha:** 1 de septiembre de 2026
**Reglas:** 49, 52, 53, 1081, 1083, 2134, 2150, 2156, 2224 (1 intento cada una)
**Se compara con:** `lote_20260826_161609` — mismas 9 reglas, mismo número de
intentos, código idéntico salvo la ventana.

### Por qué este lote es la referencia

Es la **única medición limpia del efecto de la ventana de contexto por sí sola**.
Se lanzó justo después de cambiar el modelo a `phi4-calidad` (num_ctx 8192) y
**antes** de desplegar el filtrado de esquema con umbral 20 y el aviso de
truncamiento por tokens. Cualquier lote posterior mezcla ya varias variables.

### Lo que demuestra: los prompts se truncaban

|                                | Ventana 4096            | Ventana 8192      |
|--------------------------------|-------------------------|-------------------|
| Tokens de entrada              | tope **4095**           | **4.787 – 5.352** |
| Intentos clavados en el tope   | 64 de 113               | 0                 |
| Intentos por encima de 4095    | 0                       | 9 de 9            |

Los prompts necesitan de verdad entre 4.800 y 5.400 tokens. Con la ventana en
4096 se perdía **entre un 13% y un 24% del enunciado en cada llamada**, sin que
nada avisara: el motor no da error, simplemente corta.

### Lo que cambió en las traducciones

Las tres reglas de validación contra la dimensión de tiempo, que en el lote
anterior ignoraron por completo una instrucción explícita del prompt, pasan
todas al mecanismo nativo:

| Regla | Antes               | Después                                                        |
|-------|---------------------|----------------------------------------------------------------|
| 49    | `sql_personalizada` | `FECHA Nulos:N EnDimTiempo:S Clave:Id_Expediente,Id_Pais`       |
| 2134  | `sql_personalizada` | `FECHA Nulos:N EnDimTiempo:S Clave:Id_Objeto,Id_Pais,Id_Fecha_Fin_Mes` |
| 2156  | `sql_personalizada` | `FECHA Nulos:N EnDimTiempo:S Clave:Id_Expediente,Id_Pais`       |

Las de precisión decimal incorporan la etiqueta que faltaba: 1081 → `Dec:0`,
1083 → `Dec:0`, 2224 → `Dec:3`. Y 52/53 pasan de `Nulos:S` (que no comprobaba
nada) a `Nulos:N`.

**La conclusión que importa:** el refuerzo del prompt escrito el 26 de agosto
era correcto. No cambió nada porque **no le llegaba al modelo**. Lo que parecía
un fallo del enunciado era un síntoma del truncamiento.

### Lo que NO mejoró

- **2150** sigue en `sql_personalizada`. Es el caso del maestro de ratings que
  vive en el AS/400 y se resuelve con `openquery`: el agente no tiene forma de
  saberlo, y no es corregible por prompt.
- **1083** sigue inventándose `Max:1000000`, un límite que no aparece en el
  texto de la regla.
- **Desviación nueva a vigilar:** el modelo emite ahora `Nulos:N` donde el
  revisor había escrito `Nulos:S`, de forma consistente en 6 de las 9 reglas.
  No está claro si es mejor o peor; es un cambio sistemático de comportamiento
  que conviene contrastar.

### Salvedades

- Es **1 intento por regla** y el modelo es estocástico. La señal de los tokens
  es objetiva y sólida; la del comportamiento del modelo es fuerte pero no
  concluyente sobre una sola pasada.
- La comparación de acierto se hace contra las **notas previas del revisor**,
  no contra un etiquetado nuevo de estas filas. Para cerrarlo del todo habría
  que etiquetarlas en `/calidad/evaluacion`.

---

## `lote_20260818_160125` — línea base del catálogo completo

**Fecha:** 18 de agosto de 2026
**Alcance:** las 63 reglas activas, hasta 2 intentos cada una (108 filas)
**Estado:** revisado al 100% por un humano.

Es el lote de referencia para **contrastar guardas nuevas**: al tener todas las
filas etiquetadas, permite comprobar que una comprobación nueva no bloquea
ninguna traducción que el revisor haya dado por correcta. Ese contraste ya sirvió
para descartar una guarda que parecía evidente y era falsa (dar por hecho que
"el campo no puede estar vacío" obliga a `Nulos:N`; en las reglas 2089 y 2090 la
respuesta correcta del propio revisor lleva `Nulos:S`).

También es de donde salieron los tokens que destaparon el truncamiento.
