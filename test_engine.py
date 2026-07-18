from __future__ import annotations

from tracer.engine import run_trace
from tracer.snapshot import Snapshot


def _get_global(snapshot: Snapshot, name: str) -> str | None:
    for v in snapshot.globals:
        if v.name == name:
            return v.value.display
    return None


def test_simple_loop_step_count_and_values() -> None:
    src = "total = 0\nfor i in range(3):\n    total += i\n"
    result = run_trace(src)

    assert result.termination_reason == "completed"

    body_snapshots = [s for s in result.snapshots if s.event == "line" and s.line_no == 3]
    assert len(body_snapshots) == 3

    assert _get_global(body_snapshots[0], "total") == "0"
    assert _get_global(body_snapshots[0], "i") == "0"

    assert _get_global(body_snapshots[1], "total") == "0"
    assert _get_global(body_snapshots[1], "i") == "1"

    assert _get_global(body_snapshots[2], "total") == "1"
    assert _get_global(body_snapshots[2], "i") == "2"

    last = result.snapshots[-1]
    assert last.event == "return"
    assert _get_global(last, "total") == "3"


def test_snapshots_not_mutated_retroactively() -> None:
    src = "lst = [1, 2]\nlst.append(3)\n"
    result = run_trace(src)

    before_append = [s for s in result.snapshots if s.event == "line" and s.line_no == 2]
    assert len(before_append) == 1
    snap = before_append[0]

    lst_var = next(v for v in snap.globals if v.name == "lst")
    lst_entry = snap.heap[lst_var.value.obj_id]
    assert [c[1].display for c in lst_entry.children] == ["1", "2"]

    last = result.snapshots[-1]
    lst_final = next(v for v in last.globals if v.name == "lst")
    lst_entry_final = last.heap[lst_final.value.obj_id]
    assert [c[1].display for c in lst_entry_final.children] == ["1", "2", "3"]


def test_recursion_stack_depth_and_return_values() -> None:
    src = (
        "def fact(n):\n"
        "    if n <= 1:\n"
        "        return 1\n"
        "    return n * fact(n - 1)\n"
        "\n"
        "result = fact(4)\n"
    )
    result = run_trace(src)

    assert result.termination_reason == "completed"

    max_depth = max(len(s.call_stack) for s in result.snapshots)
    assert max_depth == 5

    fact_returns = [
        s for s in result.snapshots
        if s.event == "return" and s.call_stack[-1].function_name == "fact"
    ]
    return_values = [s.return_value.display for s in fact_returns]
    assert return_values == ["1", "2", "6", "24"]

    module_return = result.snapshots[-1]
    assert module_return.event == "return"
    assert module_return.return_value.type_name == "NoneType"


def test_while_true_hits_max_steps() -> None:
    result = run_trace("while True:\n    x = 1\n", max_steps=50)

    assert result.termination_reason == "max_steps"
    line_snapshots = [s for s in result.snapshots if s.event == "line"]
    assert len(line_snapshots) == 50


def test_uncaught_exception_clean_termination() -> None:
    result = run_trace("x = 1\ny = 0\nz = x / y\n")

    assert result.termination_reason == "exception"

    last = result.snapshots[-1]
    assert last.event == "exception"
    assert last.line_no == 3
    assert "ZeroDivisionError" in last.exception_repr


def test_syntax_error() -> None:
    result = run_trace("def broken(:\n")

    assert result.termination_reason == "exception"
    assert "SyntaxError" in result.error_message
    assert len(result.snapshots) == 0


def test_final_stdout_len_matches_full_stdout() -> None:
    result = run_trace("for i in range(5):\n    print(i)\n")

    assert result.termination_reason == "completed"
    last = result.snapshots[-1]
    assert last.stdout_len == len(result.stdout)
    assert result.stdout == "0\n1\n2\n3\n4\n"


def test_user_code_called_from_stdlib_lambda_in_sorted() -> None:
    src = "result = sorted([3, 1, 2], key=lambda v: -v)\n"
    result = run_trace(src)

    assert result.termination_reason == "completed"

    lambda_returns = [
        s for s in result.snapshots
        if s.event == "return" and s.call_stack[-1].function_name == "<lambda>"
    ]
    assert len(lambda_returns) == 3
    return_values = sorted(int(s.return_value.display) for s in lambda_returns)
    assert return_values == [-3, -2, -1]

    for s in lambda_returns:
        lambda_frame = s.call_stack[-1]
        assert lambda_frame.depth == 1
        assert s.call_stack[0].function_name == "<module>"
        assert s.call_stack[0].depth == 0


def test_aliasing_end_to_end() -> None:
    src = "shared = [10, 20]\na = [shared, 1]\nb = [shared, 2]\n"
    result = run_trace(src)

    last = result.snapshots[-1]
    shared_var = next(v for v in last.globals if v.name == "shared")
    a_var = next(v for v in last.globals if v.name == "a")
    b_var = next(v for v in last.globals if v.name == "b")

    a_entry = last.heap[a_var.value.obj_id]
    b_entry = last.heap[b_var.value.obj_id]

    a_first_child = a_entry.children[0][1]
    b_first_child = b_entry.children[0][1]

    assert a_first_child.obj_id == b_first_child.obj_id
    assert a_first_child.obj_id == shared_var.value.obj_id

    shared_entry = last.heap[shared_var.value.obj_id]
    assert shared_entry.type_name == "list"
    assert [c[1].display for c in shared_entry.children] == ["10", "20"]


def test_stable_loop_shares_heap_and_mapping_instances() -> None:
    src = "lst = [1, 2, 3]\nfor i in range(1000):\n    x = i\n"
    result = run_trace(src, max_steps=2500)

    assert result.termination_reason == "completed"

    body_snapshots = [s for s in result.snapshots if s.event == "line" and s.line_no == 3]
    assert len(body_snapshots) == 1000

    lst_entries = []
    for s in body_snapshots:
        lst_var = next(v for v in s.globals if v.name == "lst")
        lst_entries.append(s.heap[lst_var.value.obj_id])

    first_entry = lst_entries[0]
    for entry in lst_entries[1:]:
        assert entry is first_entry

    heaps = [s.heap for s in body_snapshots]
    first_heap = heaps[0]
    for heap in heaps[1:]:
        assert heap is first_heap


def test_mutation_generates_new_interned_entry() -> None:
    src = "lst = [1, 2]\nlst.append(3)\n"
    result = run_trace(src)

    before = next(s for s in result.snapshots if s.event == "line" and s.line_no == 2)
    after = result.snapshots[-1]

    before_entry = before.heap[next(v for v in before.globals if v.name == "lst").value.obj_id]
    after_entry = after.heap[next(v for v in after.globals if v.name == "lst").value.obj_id]

    assert before_entry is not after_entry
    assert [c[1].display for c in before_entry.children] == ["1", "2"]
    assert [c[1].display for c in after_entry.children] == ["1", "2", "3"]


def test_memory_limit_cutoff() -> None:
    src = "items = []\nfor i in range(10000):\n    items.append([i])\n"
    result = run_trace(src, max_steps=20_000, max_heap_entries=100)

    assert result.termination_reason == "memory_limit"
    assert len(result.snapshots) > 0
