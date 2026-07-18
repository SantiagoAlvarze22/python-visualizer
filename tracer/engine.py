"""Ejecuta codigo Python arbitrario y produce una traza completa paso a paso.

No importa Streamlit ni nada de UI.

Mecanismo: sys.settrace con eventos call/line/return/exception. El
codigo del usuario se compila con el filename centinela "<user_code>";
el callback de traceo devuelve None para cualquier frame cuyo
f_code.co_filename no sea ese -- asi ni siquiera se entra a trazar
llamadas a libreria/stdlib, y Snapshot.heap queda automaticamente
acotado a objetos alcanzables desde el codigo del usuario (ver el
docstring de Snapshot.heap para el detalle de esa regla).

Cada evento 'line' produce un snapshot (el evento fira ANTES de
ejecutar esa linea: ver Snapshot.line_no). Cada evento 'return' de
todo frame de usuario tambien produce un snapshot con el valor
retornado en return_value -- incluido el del modulo (sin el, el efecto
de la ultima linea del programa nunca quedaria visible). Esto hace que
la traza muestre los valores subiendo por la pila durante el
desenrollado de llamadas recursivas.

Limitacion conocida (no resuelta; documentar tambien en el README del
proyecto): serializer.py llama a repr()/len() de los objetos del
usuario para construir cada snapshot. Si el __repr__ o __len__ de una
clase definida por el usuario tiene efectos secundarios (imprime algo,
muta estado, hace I/O), esos efectos SI se ejecutan durante el
trazado -- potencialmente una vez por cada paso en el que esa variable
siga viva. Python Tutor tiene la misma limitacion; no la resolvemos.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import sys
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

from tracer.sandbox import ExecutionLimiter, MemoryLimitExceeded, StepLimitExceeded, TimeoutExceeded
from tracer.serializer import DEFAULT_MAX_DEPTH, DEFAULT_MAX_ELEMENTS, SnapshotSerializer
from tracer.snapshot import Frame, HeapEntry, Snapshot, Variable

_USER_CODE_FILENAME = "<user_code>"
_SENTINEL = object()

TerminationReason = Literal["completed", "max_steps", "timeout", "exception", "memory_limit"]


@dataclass(frozen=True)
class TraceResult:
    """Resultado de trazar un programa completo.

    stdout es el UNICO buffer con toda la salida impresa durante la
    ejecucion. Cada Snapshot.stdout_len es un indice dentro de este
    string (`stdout[:snapshot.stdout_len]`), no una copia -- asi la
    traza no crece O(n^2) en memoria con muchos prints en muchos pasos.
    """

    snapshots: tuple[Snapshot, ...]
    stdout: str
    termination_reason: TerminationReason
    error_message: str | None = None


class _CountingStringIO(io.StringIO):
    """StringIO que trackea su longitud sin recalcularla con getvalue().

    getvalue() reconstruye el string completo cada vez que se llama; si
    la llamaramos en cada evento 'line' para saber cuanto se lleva
    escrito, volveriamos a pagar el costo O(n) por paso que stdout_len
    existe justamente para evitar.
    """

    def __init__(self) -> None:
        super().__init__()
        self.length = 0

    def write(self, s: str) -> int:
        n = super().write(s)
        self.length += n
        return n


def _safe_exception_repr(exc_type: type, exc_value: BaseException) -> str:
    try:
        return f"{exc_type.__name__}: {exc_value}"
    except Exception:
        return f"{exc_type.__name__}: <no se pudo formatear el mensaje>"


def _split_globals(
    exec_globals: dict[str, Any], serializer: SnapshotSerializer
) -> tuple[tuple[Variable, ...], tuple[Variable, ...]]:
    """Separa los globals del usuario en (variables de datos, funciones/clases).

    Excluye __builtins__, dunders (__name__, __doc__, etc.) y modulos
    importados -- son ruido de ejecucion, no estado del programa que el
    usuario este siguiendo paso a paso.
    """
    data_items: list[tuple[str, Any]] = []
    definition_items: list[tuple[str, Any]] = []
    for name, value in exec_globals.items():
        if name == "__builtins__" or (name.startswith("__") and name.endswith("__")):
            continue
        if inspect.ismodule(value):
            continue
        if inspect.isfunction(value) or inspect.isclass(value) or inspect.ismethod(value):
            definition_items.append((name, value))
        else:
            data_items.append((name, value))
    return serializer.build_variables(data_items), serializer.build_variables(definition_items)


class _Tracer:
    """Callback de sys.settrace mas el estado mutable de una traza en curso.

    El interning de HeapEntry (self._entry_cache) y la reutilizacion del
    heap Mapping entre snapshots consecutivos (self._prev_heap) reducen el
    consumo de memoria de la traza pero no el de CPU: el heap completo se
    serializa desde cero en cada evento porque el serializer necesita
    recorrer los objetos vivos del usuario para capturar su estado actual.
    El costo de CPU es proporcional al numero de objetos alcanzables desde
    locals+globals del usuario POR CADA evento line/return.
    """

    def __init__(
        self,
        exec_globals: dict[str, Any],
        limiter: ExecutionLimiter,
        stdout_buffer: _CountingStringIO,
        max_elements: int,
        max_depth: int,
        max_heap_entries: int,
    ) -> None:
        self.exec_globals = exec_globals
        self.limiter = limiter
        self.stdout_buffer = stdout_buffer
        self.max_elements = max_elements
        self.max_depth = max_depth
        self._max_heap_entries = max_heap_entries
        self.snapshots: list[Snapshot] = []
        self.frame_stack: list[tuple[Any, int]] = []
        self.termination_reason: TerminationReason = "completed"
        self._next_call_id = 0
        self._line_count = 0
        self._entry_cache: dict[HeapEntry, HeapEntry] = {}
        self._prev_heap: Mapping[int, HeapEntry] | None = None

    def trace(self, frame: Any, event: str, arg: Any) -> Any:
        if frame.f_code.co_filename != _USER_CODE_FILENAME:
            return None

        if event == "call":
            self._next_call_id += 1
            self.frame_stack.append((frame, self._next_call_id))
            return self.trace

        if event == "line":
            self.limiter.check(self._line_count)
            self._capture(frame, "line")
            self._line_count += 1
            return self.trace

        if event == "return":
            self._capture(frame, "return", return_value=arg)
            if self.frame_stack and self.frame_stack[-1][0] is frame:
                self.frame_stack.pop()
            return self.trace

        if event == "exception":
            exc_type, exc_value, _tb = arg
            self._capture(frame, "exception", exception=(exc_type, exc_value))
            self.termination_reason = "exception"
            # Una vez que hay una excepcion sin capturar propagandose, ya
            # tenemos la snapshot util (donde se origino); no hace falta
            # seguir trazando el desenrollado del stack.
            sys.settrace(None)
            return None

        return self.trace

    def _heaps_match(self, new_heap: dict[int, HeapEntry]) -> bool:
        prev = self._prev_heap
        if prev is None or len(new_heap) != len(prev):
            return False
        for obj_id, entry in new_heap.items():
            if prev.get(obj_id) is not entry:
                return False
        return True

    def _capture(
        self,
        current_frame: Any,
        event: Literal["line", "return", "exception"],
        exception: tuple[type, BaseException] | None = None,
        return_value: Any = _SENTINEL,
    ) -> None:
        # Una instancia de serializer por snapshot, compartida entre
        # todos los frames + globals de ESE paso: asi el aliasing se
        # detecta tambien entre un local y un global, no solo dentro de
        # un mismo frame.
        serializer = SnapshotSerializer(max_elements=self.max_elements, max_depth=self.max_depth)

        call_stack = tuple(
            Frame(
                call_id=call_id,
                function_name=f.f_code.co_name,
                line_no=f.f_lineno,
                depth=depth,
                # El frame de modulo (depth 0) tiene f_locals is f_globals
                # en CPython: sus "locals" son exactamente lo que ya se
                # reporta por separado en Snapshot.globals/user_definitions
                # (filtrado de __builtins__/dunders/modulos). Serializarlo
                # aqui de nuevo, sin filtrar, arrastraria __builtins__
                # completo (todos los tipos/descriptores del interprete)
                # al heap -- por eso el modulo se muestra con locals vacio.
                locals=() if depth == 0 else serializer.build_variables(f.f_locals.items()),
            )
            for depth, (f, call_id) in enumerate(self.frame_stack)
        )
        globals_vars, user_definitions = _split_globals(self.exec_globals, serializer)

        exception_repr = _safe_exception_repr(*exception) if exception is not None else None
        serialized_return = serializer.serialize(return_value) if return_value is not _SENTINEL else None

        interned_heap: dict[int, HeapEntry] = {}
        for obj_id, entry in serializer.heap.items():
            cached = self._entry_cache.get(entry)
            if cached is None:
                self._entry_cache[entry] = entry
                cached = entry
            interned_heap[obj_id] = cached

        if self._heaps_match(interned_heap):
            final_heap = self._prev_heap
        else:
            final_heap = MappingProxyType(interned_heap)
            self._prev_heap = final_heap

        self.snapshots.append(
            Snapshot(
                step_index=len(self.snapshots),
                event=event,
                line_no=current_frame.f_lineno,
                call_stack=call_stack,
                globals=globals_vars,
                user_definitions=user_definitions,
                heap=final_heap,
                stdout_len=self.stdout_buffer.length,
                exception_repr=exception_repr,
                return_value=serialized_return,
            )
        )

        if len(self._entry_cache) >= self._max_heap_entries:
            raise MemoryLimitExceeded(
                f"Se alcanzo el limite de {self._max_heap_entries} objetos unicos en el heap"
            )


def run_trace(
    source: str,
    *,
    stdin_text: str = "",
    max_steps: int = 10_000,
    timeout_seconds: float = 5.0,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_heap_entries: int = 50_000,
) -> TraceResult:
    """Ejecuta `source` y devuelve la traza completa.

    Nunca lanza: un error de sintaxis, una excepcion en tiempo de
    ejecucion del codigo del usuario, o alcanzar el limite de pasos/de
    tiempo, se reflejan en TraceResult.termination_reason / error_message
    en vez de propagarse -- la app nunca se cae por codigo del usuario.
    """
    try:
        code_obj = compile(source, _USER_CODE_FILENAME, "exec")
    except SyntaxError as exc:
        return TraceResult(
            snapshots=(),
            stdout="",
            termination_reason="exception",
            error_message=f"SyntaxError: {exc}",
        )

    exec_globals: dict[str, Any] = {"__name__": "__main__"}
    stdout_buffer = _CountingStringIO()
    stdin_buffer = io.StringIO(stdin_text)
    limiter = ExecutionLimiter(max_steps=max_steps, timeout_seconds=timeout_seconds)
    tracer = _Tracer(exec_globals, limiter, stdout_buffer, max_elements, max_depth, max_heap_entries)

    sys.settrace(tracer.trace)
    old_stdin = sys.stdin
    try:
        sys.stdin = stdin_buffer
        with contextlib.redirect_stdout(stdout_buffer):
            exec(code_obj, exec_globals)
    except StepLimitExceeded:
        tracer.termination_reason = "max_steps"
    except TimeoutExceeded:
        tracer.termination_reason = "timeout"
    except MemoryLimitExceeded:
        tracer.termination_reason = "memory_limit"
    except BaseException:
        # Limite deliberado del sandbox de ejecucion (no de seguridad):
        # cualquier cosa que el codigo del usuario lance -- incluyendo
        # SystemExit si llama a sys.exit() -- debe terminar la traza
        # limpiamente, nunca tumbar la app. El evento 'exception' ya
        # capturo una snapshot util en el punto exacto donde se origino.
        if tracer.termination_reason == "completed":
            tracer.termination_reason = "exception"
    finally:
        sys.stdin = old_stdin
        sys.settrace(None)

    return TraceResult(
        snapshots=tuple(tracer.snapshots),
        stdout=stdout_buffer.getvalue(),
        termination_reason=tracer.termination_reason,
    )
