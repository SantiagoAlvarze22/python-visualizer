"""Convierte objetos Python vivos en SerializedValue/HeapEntry seguros.

Recorre el objeto tal como esta en memoria en el momento de la llamada
(no una copia) para poder capturar el id() real de cada nivel -- asi
el aliasing anidado (dos contenedores que comparten un mismo objeto
interno) queda representado correctamente en el heap del snapshot. Una
vez que una funcion de este modulo retorna, no queda ninguna referencia
viva al objeto del usuario: todo lo que se guarda es texto y datos
planos, asi que una mutacion posterior no puede alterar retroactivamente
lo ya serializado.
"""
from __future__ import annotations

import itertools
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from tracer.snapshot import HeapEntry, SerializedValue, Variable

DEFAULT_MAX_ELEMENTS = 100
DEFAULT_MAX_DEPTH = 10
_MAX_DISPLAY_CHARS = 200

_IMMUTABLE_SCALAR_TYPES = (type(None), bool, int, float, complex, str, bytes, range)


def _is_immutable_scalar(value: Any) -> bool:
    return (
        isinstance(value, _IMMUTABLE_SCALAR_TYPES)
        or value is NotImplemented
        or value is Ellipsis
    )


def _safe_repr(value: Any, *, max_chars: int = _MAX_DISPLAY_CHARS) -> str:
    """repr() protegido: nunca deja que un __repr__ de usuario (que puede
    lanzar excepcion, o ser gigante para un str/bytes enorme) tumbe la
    traza. Para str/bytes largos, trunca ANTES de llamar a repr() para no
    pagar el costo de construir la representacion completa."""
    try:
        source = value
        if isinstance(source, (str, bytes)) and len(source) > max_chars:
            source = source[:max_chars]
        text = repr(source)
    except Exception as exc:
        return f"<{type(value).__name__}: repr() failed: {exc!r}>"
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


def _cheap_len(value: Any) -> int | None:
    try:
        return len(value)
    except Exception:
        return None


def _instance_attrs(value: Any) -> dict[str, Any] | None:
    """Atributos de una instancia "comun" (con __dict__ o __slots__).
    None significa "objeto opaco", no descomponible en hijos."""
    try:
        d = getattr(value, "__dict__", None)
        if d:
            return dict(d)
        slots = getattr(type(value), "__slots__", None)
        if slots:
            if isinstance(slots, str):
                slots = (slots,)
            return {s: getattr(value, s) for s in slots if hasattr(value, s)}
    except Exception:
        return None
    return None


def _decompose(
    value: Any, limit: int
) -> tuple[list[tuple[str, Any]], int] | None:
    """Descompone value en pares (key_label, child_value), sin recorrer
    mas alla de `limit` elementos ni materializar la coleccion completa.
    Devuelve (children, omitted_count), o None si value es opaco.

    Usa len() (O(1) para list/dict/set/tuple) para el total real, y
    slicing/islice para tomar solo los primeros `limit` -- por eso una
    coleccion de millones de elementos no se recorre entera solo por
    haber pasado por una linea de la traza.
    """
    if isinstance(value, (list, tuple)):
        total = len(value)
        shown = value[:limit]
        return [(str(i), item) for i, item in enumerate(shown)], max(0, total - limit)

    if isinstance(value, dict):
        total = len(value)
        shown = list(itertools.islice(value.items(), limit))
        return [(_safe_repr(k), v) for k, v in shown], max(0, total - limit)

    if isinstance(value, (set, frozenset)):
        total = len(value)
        shown = list(itertools.islice(value, limit))
        return [(str(i), item) for i, item in enumerate(shown)], max(0, total - limit)

    attrs = _instance_attrs(value)
    if attrs is None:
        return None
    total = len(attrs)
    shown = list(itertools.islice(attrs.items(), limit))
    return [(str(k), v) for k, v in shown], max(0, total - limit)


class SnapshotSerializer:
    """Recorre los valores vivos de UN paso de ejecucion completo.

    Una instancia se usa para todas las variables de un mismo Snapshot
    (locals de todos los frames + globals + user_definitions), no una
    por variable -- asi el heap se comparte entre ellas y el aliasing
    entre, por ejemplo, un local y un global apuntando al mismo objeto
    tambien se detecta.
    """

    def __init__(
        self,
        max_elements: int = DEFAULT_MAX_ELEMENTS,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self._max_elements = max_elements
        self._max_depth = max_depth
        self._heap: dict[int, HeapEntry] = {}
        # Memo de recorrido en curso: solo sirve para cortar la recursion
        # cuando un objeto se contiene a si mismo (directa o
        # indirectamente); no se refleja como flag en el resultado.
        self._in_progress: set[int] = set()

    @property
    def heap(self) -> Mapping[int, HeapEntry]:
        return MappingProxyType(self._heap)

    def build_variables(self, items: Iterable[tuple[str, Any]]) -> tuple[Variable, ...]:
        return tuple(Variable(name=name, value=self.serialize(value)) for name, value in items)

    def serialize(self, value: Any, *, depth: int = 0) -> SerializedValue:
        if _is_immutable_scalar(value):
            return SerializedValue(
                type_name=type(value).__name__,
                display=_safe_repr(value),
                obj_id=id(value),
                is_mutable=False,
            )

        obj_id = id(value)
        type_name = type(value).__name__

        # Ya resuelto (aliasing: otro nombre ya trajo este mismo objeto) o
        # en construccion ahora mismo (ciclo): en ambos casos, apuntar sin
        # volver a recorrer. self._heap[obj_id] queda valido en cuanto
        # termina el build de nivel superior que lo disparo.
        if obj_id in self._heap or obj_id in self._in_progress:
            return SerializedValue(type_name=type_name, display=type_name, obj_id=obj_id, is_mutable=True)

        self._in_progress.add(obj_id)
        try:
            entry = self._build_heap_entry(value, obj_id, type_name, depth)
        finally:
            self._in_progress.discard(obj_id)
        self._heap[obj_id] = entry

        return SerializedValue(type_name=type_name, display=type_name, obj_id=obj_id, is_mutable=True)

    def _build_heap_entry(self, value: Any, obj_id: int, type_name: str, depth: int) -> HeapEntry:
        if depth >= self._max_depth:
            total = _cheap_len(value)
            return HeapEntry(
                obj_id=obj_id,
                type_name=type_name,
                display=f"{type_name}[{total}]" if total is not None else _safe_repr(value),
                children=(),
                omitted_count=total or 0,
            )

        decomposed = _decompose(value, self._max_elements)
        if decomposed is None:
            return HeapEntry(
                obj_id=obj_id,
                type_name=type_name,
                display=_safe_repr(value),
                children=(),
                omitted_count=0,
            )

        raw_children, omitted = decomposed
        children = tuple(
            (key, self.serialize(child_value, depth=depth + 1)) for key, child_value in raw_children
        )
        total = len(raw_children) + omitted
        return HeapEntry(
            obj_id=obj_id,
            type_name=type_name,
            display=f"{type_name}[{total}]",
            children=children,
            omitted_count=omitted,
        )
