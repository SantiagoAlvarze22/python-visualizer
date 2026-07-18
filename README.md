# Python Execution Visualizer

Herramienta educativa estilo Python Tutor que traza y visualiza la
ejecucion paso a paso de codigo Python, corriendo 100 % local.

## Demo

Desplegado en Streamlit Community Cloud (gratis).

## Funcionalidades

- Resaltado de codigo con Pygments (tema Monokai) y linea actual marcada con colores por tipo de evento
- Navegacion manual (Inicio/Atras/Siguiente/Final + slider) y reproduccion automatica (play/pausa con velocidad configurable)
- Soporte para `input()` via campo stdin
- Call stack con profundidad de frames de usuario
- Variables locales, globales y definiciones del usuario
- Heap navegable con expansion bajo demanda
- Deteccion y senalizacion visual de aliasing (dos nombres -> mismo objeto)
- Stdout acumulado por paso
- 4 ejemplos precargados: bucle, factorial recursivo, aliasing de listas, excepcion

## Ejecutar localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Arquitectura

| Capa | Archivos | Responsabilidad |
|---|---|---|
| Motor de trazado | `tracer/engine.py`, `tracer/serializer.py`, `tracer/sandbox.py` | `sys.settrace` -> snapshots inmutables |
| Modelo de datos | `tracer/snapshot.py` | Dataclasses frozen: `Snapshot`, `Frame`, `Variable`, `HeapEntry`, `SerializedValue` |
| UI | `app.py` | Streamlit: editor, navegacion, visualizacion |

## Limitaciones conocidas

1. **No es un sandbox de seguridad.** El codigo se ejecuta con `exec()` sin restricciones.
2. **`repr()`/`__len__()` se ejecutan durante el trazado.** Efectos secundarios incluidos.
3. **CPython recicla `id()`.** Posibles falsos positivos de aliasing entre snapshots no consecutivos.
4. **`depth` cuenta frames de usuario, no de CPython.**
5. **Latencia de pausa en autoplay.** Peor caso = intervalo configurado (sub-segundo).
