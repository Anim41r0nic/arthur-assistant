"""Límites de uso del chat público: por IP (ventanas deslizantes) y cupos del día.

Todo vive en memoria: si el servicio se reinicia, los contadores empiezan de cero.
Para una web personal es aceptable y evita guardar IPs en disco.
"""

from __future__ import annotations

import ipaddress
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")

# Con más IPs que esto en una tabla se barren las caducadas: quien rota de IP
# (p. ej. un /64 de IPv6) no puede hacer crecer la memoria sin límite.
MAX_IPS_POR_TABLA = 10_000
# El barrido es O(n) con el cerrojo global cogido: no se repite en cada llamada
# (un atacante rotando IPs haría pagar el recorrido a todo el mundo), solo si
# pasó este tiempo o si la tabla se ha duplicado desde el último barrido.
INTERVALO_BARRIDO = 60.0


def clave_cliente(ip: str) -> str:
    """IPv4 tal cual; IPv6 colapsada a su /64.

    Quien tiene una red IPv6 dispone de 2^64 direcciones y rotaría de una a otra
    para esquivar el límite; el /64 es lo que identifica de verdad a un cliente.
    """
    try:
        dir_ip = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if dir_ip.version == 6:
        # Un socket de doble pila entrega los clientes IPv4 como ::ffff:a.b.c.d;
        # sin desmapear, todos caerían en el mismo ::/64 y compartirían cupo.
        if dir_ip.ipv4_mapped is not None:
            return str(dir_ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(dir_ip)


def _entero_positivo(env: Mapping[str, str], clave: str, defecto: int) -> int:
    # Un valor roto en el entorno no debe dejar la web sin límites ni tumbarla:
    # cae al valor seguro por defecto.
    try:
        valor = int(env.get(clave, ""))
    except (TypeError, ValueError):
        return defecto
    return valor if valor > 0 else defecto


@dataclass(frozen=True)
class Limits:
    per_hour: int = 20
    sessions_per_10min: int = 3
    contacts_per_day: int = 3
    # Respuestas con Workers AI al día (UTC). La capa gratis son 10 000 neuronas/día; con Gemma 4 26B una respuesta
    # del chat (unos 2000 tokens de entrada con conocimiento.md y ~200 de salida) gasta unas 22: 400 × 22 = 8800,
    # con margen. Si se cambia de modelo o crece mucho conocimiento.md, hay que recalcularlo (ver docs/webchat.md).
    workers_ai_per_day: int = 400
    hard_per_day: int = 600

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Limits":
        d = cls()
        return cls(
            per_hour=_entero_positivo(env, "ARTHUR_WEBCHAT_POR_HORA", d.per_hour),
            sessions_per_10min=_entero_positivo(env, "ARTHUR_WEBCHAT_PASES", d.sessions_per_10min),
            contacts_per_day=_entero_positivo(env, "ARTHUR_WEBCHAT_CONTACTOS", d.contacts_per_day),
            workers_ai_per_day=_entero_positivo(env, "ARTHUR_WEBCHAT_CF_DIA", d.workers_ai_per_day),
            hard_per_day=_entero_positivo(env, "ARTHUR_WEBCHAT_TOPE_DIA", d.hard_per_day),
        )


class RateLimiter:
    def __init__(self, limits: Limits, now: Callable[[], datetime]) -> None:
        self._limits = limits
        self._now = now
        self._lock = threading.Lock()
        self._mensajes: dict[str, deque[float]] = {}
        self._pases: dict[str, deque[float]] = {}
        self._contactos: dict[str, deque[float]] = {}
        self._barridos: dict[int, tuple[float, int]] = {}  # id(tabla) -> (cuándo, tamaño tras barrer)
        self._dia: object = None
        self._dia_utc: object = None
        self._total = 0
        self._workers_ai = 0

    def _ventana(self, tabla: dict[str, deque[float]], ip: str, maximo: int, segundos: float) -> bool:
        # Ventana deslizante: se descartan las marcas viejas y se cuenta lo que queda.
        ahora = self._now().timestamp()
        with self._lock:
            if len(tabla) > MAX_IPS_POR_TABLA:
                ultimo, tamano = self._barridos.get(id(tabla), (float("-inf"), 0))
                if ahora - ultimo >= INTERVALO_BARRIDO or len(tabla) >= 2 * tamano:
                    self._barrer(tabla, ahora, segundos)
                    self._barridos[id(tabla)] = (ahora, len(tabla))
            marcas = tabla.setdefault(clave_cliente(ip), deque())
            while marcas and ahora - marcas[0] >= segundos:
                marcas.popleft()
            if len(marcas) >= maximo:
                return False
            marcas.append(ahora)
            return True

    @staticmethod
    def _barrer(tabla: dict[str, deque[float]], ahora: float, segundos: float) -> None:
        # Se llama con el cerrojo cogido. La marca más reciente es la última:
        # si ya caducó, toda la ventana de esa IP está vacía y se borra la clave.
        for clave in [k for k, m in tabla.items() if not m or ahora - m[-1] >= segundos]:
            del tabla[clave]

    def allow_message(self, ip: str) -> bool:
        return self._ventana(self._mensajes, ip, self._limits.per_hour, 3600)

    def allow_session(self, ip: str) -> bool:
        return self._ventana(self._pases, ip, self._limits.sessions_per_10min, 600)

    def allow_contact(self, ip: str) -> bool:
        return self._ventana(self._contactos, ip, self._limits.contacts_per_day, 86400)

    def _rueda_dia(self) -> None:
        # Se llama con el cerrojo cogido. Dos días distintos a propósito:
        # - el tope duro cuenta por día de Madrid (el día de Marc, no el del servidor);
        # - el cupo de Workers AI, por día UTC, porque Cloudflare renueva la capa gratis a las 00:00 UTC
        #   (las 01:00 o 02:00 en Madrid). Con el de Madrid, entre medianoche y esa hora se gastaría el cupo
        #   de un día que para Cloudflare todavía no ha empezado.
        ahora = self._now()
        hoy = ahora.astimezone(MADRID).date()
        if hoy != self._dia:
            self._dia = hoy
            self._total = 0
        hoy_utc = ahora.astimezone(timezone.utc).date()
        if hoy_utc != self._dia_utc:
            self._dia_utc = hoy_utc
            self._workers_ai = 0

    def reserve_answer(self) -> Literal["workers-ai", "local", "fijo"]:
        """Reserva un hueco del día y dice qué motor responde."""
        with self._lock:
            self._rueda_dia()
            if self._total >= self._limits.hard_per_day:
                return "fijo"
            self._total += 1
            if self._workers_ai < self._limits.workers_ai_per_day:
                self._workers_ai += 1
                return "workers-ai"
            return "local"

    def release_workers_ai(self) -> None:
        """Devuelve el hueco de Workers AI cuando falla (el total no se toca:
        la respuesta se dará igualmente con el motor local)."""
        with self._lock:
            self._rueda_dia()
            if self._workers_ai > 0:
                self._workers_ai -= 1
