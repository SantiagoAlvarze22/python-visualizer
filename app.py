"""UI Streamlit del visualizador de ejecucion Python paso a paso.

Ejecutar:  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import html as html_mod
import time

import streamlit as st
import streamlit.components.v1 as components
from pygments import highlight as pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import PythonLexer

from tracer.engine import run_trace
from tracer.snapshot import HeapEntry, SerializedValue, Variable

# ── Ejemplos precargados ────────────────────────────────────────────────

EXAMPLES: dict[str, str] = {
    "Bucle simple": (
        "total = 0\n"
        "for i in range(5):\n"
        "    total += i\n"
        'print(f"Total: {total}")\n'
    ),
    "Factorial recursivo": (
        "def fact(n):\n"
        "    if n <= 1:\n"
        "        return 1\n"
        "    return n * fact(n - 1)\n"
        "\n"
        "result = fact(5)\n"
        'print(f"5! = {result}")\n'
    ),
    "Aliasing de listas": (
        "x = [1, 2]\n"
        "y = x\n"
        "y.append(3)\n"
        'print(f"x = {x}")\n'
        'print(f"y = {y}")\n'
        'print(f"x is y: {x is y}")\n'
    ),
    "Excepcion no capturada": (
        "def dividir(a, b):\n"
        "    return a / b\n"
        "\n"
        "x = dividir(10, 2)\n"
        "y = dividir(10, 0)\n"
    ),
}

# ── Paleta de colores para badges de aliasing ───────────────────────────

_PALETTE = [
    "#4ECDC4", "#FF6B6B", "#45B7D1", "#96CEB4",
    "#FFEAA7", "#DDA0DD", "#98D8C8", "#F7DC6F",
    "#BB8FCE", "#85C1E9",
]

_LEXER = PythonLexer()

_HL_BG = {
    "line": "#3b3520",
    "call": "#1a2e3e",
    "return": "#1a3328",
    "exception": "#3d1a1a",
}
_HL_BORDER = {
    "line": "#e6c430",
    "call": "#4a9eff",
    "return": "#34d87a",
    "exception": "#ff4d4d",
}


# ── Helpers ─────────────────────────────────────────────────────────────


def _badge(obj_id: int) -> str:
    c = _PALETTE[obj_id % len(_PALETTE)]
    tag = f"{obj_id & 0xFFFF:04x}"
    return (
        f'<span style="background:{c};color:#000;padding:1px 6px;'
        f'border-radius:3px;font-size:0.78em;font-family:monospace;'
        f'white-space:nowrap;">●{tag}</span>'
    )


def _sv_html(sv: SerializedValue) -> str:
    d = html_mod.escape(sv.display)
    if not sv.is_mutable:
        return f"<code>{d}</code>"
    return f"{html_mod.escape(sv.type_name)}&nbsp;{_badge(sv.obj_id)}"


def _var_table_html(variables: tuple[Variable, ...]) -> str:
    rows: list[str] = []
    for v in variables:
        n = html_mod.escape(v.name)
        rows.append(
            f'<tr><td style="padding:2px 8px 2px 0;vertical-align:top;">'
            f"<code>{n}</code></td>"
            f'<td style="padding:2px 0;">{_sv_html(v.value)}</td></tr>'
        )
    return f'<table style="border-collapse:collapse;">{"".join(rows)}</table>'


def _heap_children_html(entry: HeapEntry) -> str:
    rows: list[str] = []
    for key, child in entry.children:
        k = html_mod.escape(key)
        rows.append(
            f'<tr><td style="padding:2px 8px 2px 0;"><code>[{k}]</code></td>'
            f'<td style="padding:2px 0;">{_sv_html(child)}</td></tr>'
        )
    html = f'<table style="border-collapse:collapse;font-size:0.9em;">{"".join(rows)}</table>'
    if entry.omitted_count > 0:
        html += (
            f'<p style="color:#888;font-size:0.85em;margin:4px 0 0;">'
            f"… y {entry.omitted_count:,} elementos más</p>"
        )
    return html


def _code_viewer(source: str, line_no: int, event: str) -> None:
    bg = _HL_BG.get(event, _HL_BG["line"])
    border = _HL_BORDER.get(event, _HL_BORDER["line"])
    fmt = HtmlFormatter(nowrap=True, style="monokai", noclasses=True)

    lines = source.rstrip("\n").split("\n")
    rows: list[str] = []
    for i, line in enumerate(lines, 1):
        highlighted = pyg_highlight(line + "\n", _LEXER, fmt).rstrip("\n")
        num = (
            f'<span style="color:#555;user-select:none;display:inline-block;'
            f'width:2.5em;text-align:right;padding-right:12px;flex-shrink:0;">{i}</span>'
        )
        code_span = f'<span style="white-space:pre;">{highlighted}</span>'
        if i == line_no:
            rows.append(
                f'<div id="cv-hl" style="display:flex;background:{bg};border-left:3px solid {border};'
                f'padding:1px 4px 1px 4px;">{num}{code_span}</div>'
            )
        else:
            rows.append(f'<div style="display:flex;padding:1px 4px 1px 7px;">{num}{code_span}</div>')

    code_html = "\n".join(rows)
    st.markdown(
        f'<div id="cv-box" style="background:#1e1e1e;padding:10px 6px;border-radius:6px;'
        f"font-family:'Consolas','Courier New','Liberation Mono',monospace;"
        f'font-size:14px;line-height:1.65;overflow-x:auto;overflow-y:auto;max-height:500px;">'
        f"{code_html}</div>",
        unsafe_allow_html=True,
    )
    components.html(
        """<script>
        var el = window.parent.document.getElementById('cv-hl');
        if (el) el.scrollIntoView({behavior:'instant',block:'center'});
        </script>""",
        height=0,
    )


# ── Page config + session state init ────────────────────────────────────

st.set_page_config(
    page_title="Visualizador Python",
    layout="wide",
    page_icon="\U0001f40d",
)

for _k, _v in {
    "source_code": list(EXAMPLES.values())[0],
    "stdin_text": "",
    "step_index": 0,
    "playing": False,
    "speed_ms": 500,
    "trace_result": None,
    "trace_hash": None,
}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── Header ──────────────────────────────────────────────────────────────

st.title("\U0001f40d Visualizador de Ejecución Python")


source: str = st.text_area(
    "código",
    height=180,
    key="source_code",
    label_visibility="collapsed",
)

stdin_text: str = st.text_area(
    "stdin (entrada para input())",
    height=68,
    key="stdin_text",
    placeholder="Datos que recibirá input(), una línea por llamada",
)

# ── Detect source edits (stop playback, clear stale trace) ──────────────

source_hash = hashlib.sha256((source + "\0" + stdin_text).encode()).hexdigest()

if (
    st.session_state.trace_hash is not None
    and st.session_state.trace_hash != source_hash
):
    st.session_state.playing = False
    st.session_state.step_index = 0
    st.session_state.trace_result = None
    st.session_state.trace_hash = None

# ── Play / Pause button (always visible) ──────────────────────────────


def _main_play() -> None:
    if st.session_state.trace_result is None:
        st.session_state._run_requested = True
    else:
        snaps = st.session_state.trace_result.snapshots
        if not st.session_state.playing and snaps:
            if st.session_state.step_index >= len(snaps) - 1:
                st.session_state.step_index = 0
        st.session_state.playing = not st.session_state.playing


_play_label = "⏸ Pausa" if st.session_state.playing else "▶ Play"
st.button(_play_label, key="btn_main_play", on_click=_main_play, use_container_width=True)

# ── Execute if requested ───────────────────────────────────────────────

if st.session_state.get("_run_requested"):
    st.session_state._run_requested = False
    with st.spinner("Trazando…"):
        st.session_state.trace_result = run_trace(source, stdin_text=stdin_text)
    st.session_state.trace_hash = source_hash
    st.session_state._traced_source = source
    st.session_state.step_index = 0
    st.session_state.playing = True

# ── Guard: no trace yet ─────────────────────────────────────────────────

result = st.session_state.trace_result
if result is None:
    st.stop()

# Termination warnings
if result.termination_reason == "exception" and result.error_message:
    st.error(result.error_message)
if result.termination_reason == "max_steps":
    st.warning("⚠ Traza incompleta: se alcanzó el límite de pasos.")
elif result.termination_reason == "timeout":
    st.warning("⚠ Traza incompleta: se alcanzó el timeout.")
elif result.termination_reason == "memory_limit":
    st.warning("⚠ Traza incompleta: se alcanzó el límite de memoria.")

if not result.snapshots:
    st.stop()

snapshots = result.snapshots
max_idx = len(snapshots) - 1


# ── Execution viewer (fragment: only this section reruns during playback) ──

@st.fragment
def _execution_viewer() -> None:
    _snapshots = st.session_state.trace_result.snapshots
    _max = len(_snapshots) - 1
    _source = st.session_state._traced_source
    _result = st.session_state.trace_result

    if st.session_state.step_index > _max:
        st.session_state.step_index = _max

    if st.session_state.get("_auto_advance"):
        st.session_state._auto_advance = False
        if st.session_state.step_index < _max:
            st.session_state.step_index += 1
        if st.session_state.step_index >= _max:
            st.session_state.playing = False

    def _go_first() -> None:
        st.session_state.step_index = 0
        st.session_state.playing = False

    def _go_prev() -> None:
        st.session_state.step_index = max(0, st.session_state.step_index - 1)

    def _go_next() -> None:
        st.session_state.step_index = min(_max, st.session_state.step_index + 1)

    def _go_last() -> None:
        st.session_state.step_index = _max
        st.session_state.playing = False

    st.slider("paso", 0, _max, key="step_index", label_visibility="collapsed")

    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 3])
    with c1:
        st.button("⏮ Inicio", key="btn_first", on_click=_go_first, use_container_width=True)
    with c2:
        st.button("◀ Atrás", key="btn_prev", on_click=_go_prev, use_container_width=True)
    with c3:
        st.button("▶ Sig.", key="btn_next", on_click=_go_next, use_container_width=True)
    with c4:
        st.button("⏭ Final", key="btn_last", on_click=_go_last, use_container_width=True)
    with c5:
        st.select_slider(
            "vel",
            options=[2000, 1000, 500, 250, 125],
            format_func=lambda ms: {
                2000: "0.25×",
                1000: "0.5×",
                500: "1×",
                250: "2×",
                125: "4×",
            }[ms],
            key="speed_ms",
            label_visibility="collapsed",
        )

    snap = _snapshots[st.session_state.step_index]
    current_frame = snap.call_stack[-1] if snap.call_stack else None

    lbl = f"**Paso {snap.step_index + 1} / {len(_snapshots)}** — "
    if snap.event == "line":
        lbl += f"Línea {snap.line_no} *(a punto de ejecutarse)*"
    elif snap.event == "return":
        fn = current_frame.function_name if current_frame else "<module>"
        if fn == "<module>":
            lbl += "Fin del programa"
        else:
            rv = snap.return_value.display if snap.return_value else "None"
            lbl += f"`{fn}` retornó `{rv}`"
    elif snap.event == "exception":
        lbl += f"❌ `{snap.exception_repr}`"
    elif snap.event == "call":
        fn = current_frame.function_name if current_frame else "?"
        lbl += f"Llamada a `{fn}`"
    st.markdown(lbl)

    col_code, col_state = st.columns([1, 1])

    with col_code:
        _code_viewer(_source, snap.line_no, snap.event)

        if snap.event == "return" and snap.return_value:
            fn = current_frame.function_name if current_frame else "<module>"
            if fn != "<module>":
                st.markdown(
                    f'<div style="background:#e8f5e9;padding:8px 12px;border-radius:6px;'
                    f'border-left:4px solid #4CAF50;margin-top:8px;">'
                    f"<strong>↩ {html_mod.escape(fn)}</strong> retornó "
                    f"<code>{html_mod.escape(snap.return_value.display)}</code></div>",
                    unsafe_allow_html=True,
                )

        if snap.event == "exception" and snap.exception_repr:
            st.markdown(
                f'<div style="background:#ffebee;padding:8px 12px;border-radius:6px;'
                f'border-left:4px solid #f44336;margin-top:8px;">'
                f"<strong>❌ Excepción en línea {snap.line_no}</strong>: "
                f"<code>{html_mod.escape(snap.exception_repr)}</code></div>",
                unsafe_allow_html=True,
            )

    with col_state:
        heap = snap.heap

        with st.expander("\U0001f4da Call Stack", expanded=True):
            if snap.call_stack:
                parts: list[str] = []
                for i, f in enumerate(snap.call_stack):
                    is_current = i == len(snap.call_stack) - 1
                    arrow = "→" if is_current else "│"
                    tag = " ← actual" if is_current else ""
                    indent = "&nbsp;" * (f.depth * 4)
                    style = "font-weight:bold;" if is_current else ""
                    parts.append(
                        f'<span style="{style}">{indent}<code>{arrow}</code> '
                        f"{html_mod.escape(f.function_name)} "
                        f"(línea {f.line_no}){tag}</span>"
                    )
                st.markdown("<br>".join(parts), unsafe_allow_html=True)
            else:
                st.caption("(vacío)")

        if current_frame and current_frame.locals:
            with st.expander(
                f"\U0001f4cb Locals — {current_frame.function_name}",
                expanded=True,
            ):
                st.markdown(
                    _var_table_html(current_frame.locals), unsafe_allow_html=True
                )

        if snap.globals:
            with st.expander("\U0001f30d Globals", expanded=True):
                st.markdown(
                    _var_table_html(snap.globals), unsafe_allow_html=True
                )

        id_to_names: dict[int, list[str]] = {}
        for v in snap.globals:
            if v.value.is_mutable:
                id_to_names.setdefault(v.value.obj_id, []).append(v.name)
        if current_frame:
            for v in current_frame.locals:
                if v.value.is_mutable:
                    id_to_names.setdefault(v.value.obj_id, []).append(v.name)
        aliased = {oid: names for oid, names in id_to_names.items() if len(names) > 1}
        if aliased:
            alias_parts: list[str] = []
            for oid, names in aliased.items():
                names_str = ", ".join(
                    f"<code>{html_mod.escape(n)}</code>" for n in names
                )
                alias_parts.append(f"{_badge(oid)} ← {names_str}")
            st.markdown(
                '<div style="background:#fff3e0;padding:8px 12px;border-radius:6px;'
                'border-left:4px solid #FF9800;margin-bottom:8px;">'
                "<strong>\U0001f517 Aliasing detectado</strong><br>"
                + "<br>".join(alias_parts)
                + "</div>",
                unsafe_allow_html=True,
            )

        if snap.user_definitions:
            with st.expander("\U0001f4e6 Definiciones", expanded=False):
                st.markdown(
                    _var_table_html(snap.user_definitions), unsafe_allow_html=True
                )

        seen_ids: set[int] = set()
        heap_entries: list[tuple[int, HeapEntry]] = []
        all_vars = list(snap.globals)
        if current_frame:
            all_vars.extend(current_frame.locals)
        for v in all_vars:
            oid = v.value.obj_id
            if v.value.is_mutable and oid is not None and oid not in seen_ids:
                entry = heap.get(oid)
                if entry is not None:
                    seen_ids.add(oid)
                    heap_entries.append((oid, entry))

        if heap_entries:
            with st.expander(
                f"\U0001f9e0 Heap ({len(heap_entries)})",
                expanded=len(heap_entries) <= 4,
            ):
                for oid, entry in heap_entries:
                    st.markdown(
                        f"**{html_mod.escape(entry.display)}** {_badge(oid)}",
                        unsafe_allow_html=True,
                    )
                    if entry.children:
                        st.markdown(
                            _heap_children_html(entry), unsafe_allow_html=True
                        )
                    else:
                        st.caption("(vacío)")
                    st.divider()

        stdout_text = _result.stdout[: snap.stdout_len]
        with st.expander("\U0001f4ac stdout", expanded=bool(stdout_text)):
            if stdout_text:
                st.code(stdout_text, language=None)
            else:
                st.caption("(sin salida)")

    if st.session_state.playing:
        if st.session_state.step_index < _max:
            time.sleep(st.session_state.speed_ms / 1000)
            st.session_state._auto_advance = True
            try:
                st.rerun(scope="fragment")
            except Exception:
                st.rerun()
        else:
            st.session_state.playing = False


_execution_viewer()
