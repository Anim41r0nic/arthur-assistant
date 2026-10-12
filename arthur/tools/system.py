"""Información del sistema. Solo lectura, sin ejecutar programas: lee /proc."""

import os
import platform
import shutil
from pathlib import Path

from .base import Risk, Tool


def _meminfo() -> dict[str, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        values[key] = int(rest.split()[0])  # en KiB
    return values


def _cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.partition(":")[2].strip()
    return platform.processor() or "desconocido"


def _gib(n_bytes: float) -> str:
    return f"{n_bytes / 1024**3:.1f} GiB"


def get_system_info(_args: dict) -> str:
    uptime_s = float(Path("/proc/uptime").read_text().split()[0])
    hours, minutes = divmod(int(uptime_s) // 60, 60)
    mem = _meminfo()
    disk = shutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()
    return "\n".join([
        f"Equipo: {platform.node()}",
        f"Kernel: {platform.release()}",
        f"CPU: {_cpu_model()} ({os.cpu_count()} hilos)",
        f"Carga media (1/5/15 min): {load1:.2f} / {load5:.2f} / {load15:.2f}",
        f"RAM: {_gib((mem['MemTotal'] - mem['MemAvailable']) * 1024)} en uso de {_gib(mem['MemTotal'] * 1024)}"
        f" ({_gib(mem['MemAvailable'] * 1024)} disponibles)",
        f"Disco /: {_gib(disk.used)} usados de {_gib(disk.total)} ({_gib(disk.free)} libres)",
        f"Encendido desde hace: {hours} h {minutes} min",
    ])


def build_system_tools() -> list[Tool]:
    return [
        Tool(
            name="get_system_info",
            description="RAM, disco, CPU, carga y tiempo encendido del equipo.",
            risk=Risk.READ,
            run=get_system_info,
        )
    ]
