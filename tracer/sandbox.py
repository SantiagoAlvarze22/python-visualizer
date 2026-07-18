"""Limites contra errores honestos del usuario -- NO un sandbox de seguridad.

Esto corre localmente y quien escribe el codigo es el mismo usuario que
lo ejecuta, asi que no hay un modelo de amenaza que defender: no hay
restriccion de imports, de acceso a disco/red, ni de llamadas al
sistema. Lo unico que este modulo evita es que un error honesto (un
`while True` sin corte, un bucle que tarda demasiado) cuelgue la app
indefinidamente.
"""
from __future__ import annotations

import time


class StepLimitExceeded(Exception):
    """Se alcanzo el numero maximo de pasos configurado (ej. un `while True`)."""


class TimeoutExceeded(Exception):
    """Se alcanzo el tiempo maximo de ejecucion configurado."""


class MemoryLimitExceeded(Exception):
    """La traza acumulo demasiados objetos unicos en el heap."""


class ExecutionLimiter:
    """Chequeo barato de limites, pensado para llamarse en cada evento 'line'."""

    def __init__(self, max_steps: int, timeout_seconds: float) -> None:
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self._start_time = time.monotonic()

    def check(self, step_count: int) -> None:
        if step_count >= self.max_steps:
            raise StepLimitExceeded(f"Se alcanzo el limite de {self.max_steps} pasos")
        if time.monotonic() - self._start_time > self.timeout_seconds:
            raise TimeoutExceeded(f"Se alcanzo el timeout de {self.timeout_seconds}s")
