"""Dataclasses inmutables que modelan una traza de ejecucion completa.

No importa Streamlit ni nada de UI: son el contrato de datos entre el
motor (engine.py / serializer.py) y la capa de presentacion (app.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping


@dataclass(frozen=True)
class SerializedValue:
    """Referencia segura a un valor de Python en un instante dado.

    Es la forma "ligera": la que cuelga de una Variable o de los
    children de un HeapEntry. Cuando is_mutable es True, obj_id apunta
    a la entrada completa en Snapshot.heap — la estructura (children) no
    se repite aqui, así dos nombres que apuntan al mismo objeto (aliasing)
    referencian la misma clave de heap en vez de duplicar el subarbol.
    """

    type_name: str
    display: str
    obj_id: int | None
    is_mutable: bool


@dataclass(frozen=True)
class HeapEntry:
    """Nodo completo del grafo de memoria: la expansion de un valor mutable.

    children usa como clave el indice (list/tuple), el repr de la key
    (dict) o el nombre de atributo (objetos), y como valor otro
    SerializedValue -- que si a su vez es mutable, se resuelve siguiendo
    su obj_id en Snapshot.heap, nunca embebido inline.

    Un ciclo (un objeto que se referencia a si mismo, directa o
    indirectamente) no es un caso especial: es simplemente una children
    cuyo SerializedValue.obj_id apunta de vuelta a este mismo obj_id.

    omitted_count es cuantos elementos de la coleccion original no estan
    en children porque se paso el limite configurado (children siempre
    tiene como maximo max_elements entradas).
    """

    obj_id: int
    type_name: str
    display: str
    children: tuple[tuple[str, SerializedValue], ...]
    omitted_count: int = 0


@dataclass(frozen=True)
class Variable:
    name: str
    value: SerializedValue


@dataclass(frozen=True)
class Frame:
    """Un frame activo del call stack del usuario.

    call_id es un contador propio asignado en cada evento 'call', no
    id(frame): CPython recicla frame objects al retornar, asi que dos
    invocaciones recursivas distintas podrian compartir id(frame) si la
    primera ya fue liberada. call_id garantiza identidad estable de la
    invocacion a lo largo de toda la traza.

    depth cuenta solo frames de codigo del usuario, no frames reales de
    CPython. Si codigo del usuario es invocado desde C (p.ej. un lambda
    pasado como key a sorted()), los frames de C intermedios no aparecen
    en call_stack: el lambda queda a depth 1 (justo despues del modulo)
    aunque la pila real de CPython tenga sorted() en medio.
    """

    call_id: int
    function_name: str
    line_no: int
    depth: int
    locals: tuple[Variable, ...]


EventType = Literal["call", "line", "return", "exception"]


@dataclass(frozen=True)
class Snapshot:
    """Estado completo del programa del usuario en un paso de ejecucion.

    Es inmutable y autocontenido: no conserva referencias vivas a
    objetos del usuario, por lo que una mutacion posterior de esos
    objetos no puede alterar retroactivamente un snapshot ya emitido.

    line_no depende de `event` -- la UI no debe asumir siempre "esta
    linea ya corrio":
      - "line": la linea A PUNTO de ejecutarse. El estado de esta
        snapshot (locals, globals, heap) es el de ANTES de que esa linea
        corra. Es la semantica de sys.settrace, no una eleccion nuestra.
      - "return": se emite para todo frame de usuario al retornar, no
        solo el modulo. line_no es la linea del return (o la ultima linea
        del modulo). return_value contiene el valor de retorno
        serializado. Los efectos de esa linea ya estan reflejados en el
        estado.
      - "exception": la linea donde se origino la excepcion; sus efectos
        (parciales si la excepcion es a mitad de expresion) estan
        reflejados en el estado.

    heap solo contiene objetos alcanzables desde los locals de frames
    con f_code.co_filename == "<user_code>" y desde los globals de ese
    mismo codigo -- nunca desde el estado interno del motor de trazado
    (el propio call stack de tracer.py, sus buffers, contadores, etc.)
    ni desde frames de libreria/stdlib, que el motor ni siquiera trazea.

    stdout_len es la longitud del buffer de stdout compartido de toda la
    traza en el momento de esta snapshot -- no una copia del string. Con
    eso alcanza para que la UI muestre el prefijo correcto
    (`trace_result.stdout[:snapshot.stdout_len]`) sin que cada snapshot
    guarde su propia copia (lo que seria O(n^2) en memoria con miles de
    pasos que imprimen).
    """

    step_index: int
    event: EventType
    line_no: int
    call_stack: tuple[Frame, ...]
    globals: tuple[Variable, ...]
    user_definitions: tuple[Variable, ...]
    heap: Mapping[int, HeapEntry]
    stdout_len: int
    exception_repr: str | None = None
    return_value: SerializedValue | None = None
