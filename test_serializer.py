from __future__ import annotations

from tracer.serializer import SnapshotSerializer


def test_nested_aliasing_shares_obj_id() -> None:
    """Dos contenedores que comparten un mismo objeto interno deben
    reportar el mismo obj_id para ese objeto compartido, y el heap debe
    tener una unica entrada para el -- no una copia por cada referencia."""
    shared = [1, 2, 3]
    outer = {"a": shared, "b": shared}

    serializer = SnapshotSerializer()
    variables = serializer.build_variables([("outer", outer)])

    outer_entry = serializer.heap[variables[0].value.obj_id]
    (key_a, ref_a), (key_b, ref_b) = outer_entry.children
    assert key_a == "'a'"
    assert key_b == "'b'"

    assert ref_a.obj_id == ref_b.obj_id == id(shared)
    assert ref_a.is_mutable is True

    # Una sola entrada de heap para "shared", no dos copias.
    assert ref_a.obj_id in serializer.heap
    shared_entry = serializer.heap[ref_a.obj_id]
    assert shared_entry.type_name == "list"
    assert [c[1].display for c in shared_entry.children] == ["1", "2", "3"]

    # outer + shared: exactamente dos entradas en el heap.
    assert len(serializer.heap) == 2


def test_self_referential_cycle_terminates_and_points_to_itself() -> None:
    cyclic: list = [1, 2]
    cyclic.append(cyclic)

    serializer = SnapshotSerializer()
    value = serializer.serialize(cyclic)

    entry = serializer.heap[value.obj_id]
    self_ref = entry.children[-1][1]
    assert self_ref.obj_id == value.obj_id
    assert self_ref.is_mutable is True
    # El ciclo no es un caso especial: es una HeapEntry normal que se
    # referencia a si misma. No hay ningun flag de "circular" en el dato.
    assert not hasattr(self_ref, "is_circular_ref")


def test_large_collection_is_truncated_without_full_traversal() -> None:
    huge = list(range(10_000_000))

    serializer = SnapshotSerializer(max_elements=100)
    value = serializer.serialize(huge)

    entry = serializer.heap[value.obj_id]
    assert len(entry.children) == 100
    assert entry.omitted_count == 10_000_000 - 100
    assert entry.children[0][1].display == "0"
    assert entry.children[-1][1].display == "99"


def test_immutable_scalars_are_inlined_not_added_to_heap() -> None:
    serializer = SnapshotSerializer()
    value = serializer.serialize(42)

    assert value.is_mutable is False
    assert value.display == "42"
    assert len(serializer.heap) == 0


def test_two_names_for_same_object_share_one_heap_entry() -> None:
    box = {"count": 1}
    serializer = SnapshotSerializer()

    variables = serializer.build_variables([("a", box), ("b", box)])
    value_a, value_b = variables[0].value, variables[1].value

    assert value_a.obj_id == value_b.obj_id == id(box)
    assert len(serializer.heap) == 1
