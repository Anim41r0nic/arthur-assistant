"""Cerebro de reglas: provisional, sin IA y sin red.

Sirve para desarrollar y probar el núcleo y las herramientas antes de conectar
un LLM. Reconoce unas pocas frases mediante expresiones regulares y, como haría
un LLM, puede proponer llamadas a herramientas.
"""

import re
import shlex
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime

from ..tools.base import ToolCall
from .base import BrainReply, Message

WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)

# Reglas que usan herramientas. Se aplican sobre el texto original (no normalizado)
# para conservar mayúsculas y tildes en rutas y contenidos.
TOOL_RULES = [
    (re.compile(r"^(?:ve|ir|cambia|pasa)\s+al\s+escritorio\s+(?P<n>\d{1,2})\s*$", re.I),
     lambda m: ToolCall("switch_workspace", {"number": int(m["n"])})),
    (re.compile(r"^(?:manda|mueve|lleva)\s+(?P<app>.+?)\s+al\s+escritorio\s+(?P<n>\d{1,2})\s*$", re.I),
     lambda m: ToolCall("move_window", {"app": m["app"], "to_workspace": int(m["n"])})),
    (re.compile(r"^cierra\s+(?:la ventana de\s+)?(?P<app>.+?)\s*$", re.I),
     lambda m: ToolCall("close_window", {"app": m["app"]})),
    (re.compile(r"^(?:recuerda|apunta)\s+(?:que\s+)?(?P<content>.+?)\.?\s*$", re.I | re.S),
     lambda m: ToolCall("remember", {"content": m["content"]})),
    (re.compile(r"^(?:olvida|borra)\s+(?:el\s+)?recuerdo\s+#?(?P<id>\d+)\s*$", re.I),
     lambda m: ToolCall("forget", {"id": int(m["id"])})),
    (re.compile(r"^(?:abre|abrir|lanza|inicia)\s+(?:la aplicaci[oó]n\s+|el programa\s+)?(?P<name>[^/~].*?)\s*$", re.I),
     lambda m: ToolCall("open_application", {"name": m["name"]})),
    (re.compile(r"^(?:lista|listar|muestra)\s+(?:la carpeta\s+|el directorio\s+)?(?P<path>[~/].*?)\s*$", re.I),
     lambda m: ToolCall("list_directory", {"path": m["path"]})),
    (re.compile(r"^(?:lee|leer)\s+(?:el\s+)?(?:archivo\s+|fichero\s+)?(?P<path>[~/].*?)\s*$", re.I),
     lambda m: ToolCall("read_file", {"path": m["path"]})),
    (re.compile(r"^(?:crea|escribe)\s+(?:el\s+|un\s+)?(?:archivo|fichero)\s+(?P<path>[~/]\S*)\s+con\s+(?:el texto\s+)?(?P<content>.+)$", re.I | re.S),
     lambda m: ToolCall("write_file", {"path": m["path"], "content": m["content"]})),
    (re.compile(r"^(?:reemplaza|sobrescribe)\s+(?:el\s+)?(?:archivo|fichero)\s+(?P<path>[~/]\S*)\s+con\s+(?:el texto\s+)?(?P<content>.+)$", re.I | re.S),
     lambda m: ToolCall("write_file", {"path": m["path"], "content": m["content"], "overwrite": True})),
]


def normalize(text: str) -> str:
    """Minúsculas y sin tildes: 'Qué HORA es' → 'que hora es'."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


class RuleBrain:
    name = "rules"
    label = "reglas (sin IA)"

    def __init__(self, assistant_name: str, now: Callable[[], datetime] = datetime.now) -> None:
        self._assistant_name = assistant_name
        self._now = now
        # El orden importa: gana la primera regla que encaje.
        self._text_rules: list[tuple[re.Pattern[str], Callable[[], str | BrainReply]]] = [
            (re.compile(r"\b(quien eres|como te llamas)\b"), self._identity),
            (re.compile(r"\b(que recuerdas|que sabes de mi)\b"),
             lambda: BrainReply("Miro en mi memoria.", (ToolCall("list_memories", {}),))),
            (re.compile(r"\b(ayuda|que puedes hacer|que sabes hacer)\b"), self._help),
            (re.compile(r"\b(informacion|info|estado|datos) del (sistema|equipo|ordenador)\b"),
             lambda: BrainReply("Consulto el sistema.", (ToolCall("get_system_info", {}),))),
            (re.compile(r"\bhora\b"), self._time),
            (re.compile(r"\b(fecha|que dia)\b"), self._date),
            (re.compile(r"\bgracias\b"), lambda: "De nada."),
            (re.compile(r"^(hola|buenas|buenos dias|hey)\b"), lambda: "Hola. ¿En qué puedo ayudarte?"),
        ]

    def reply(self, conversation: Sequence[Message]) -> BrainReply:
        last = conversation[-1]
        if last.role == "tool":
            return self._report_tool_results(conversation)

        text = last.content.strip()
        command = re.match(r"^(?:ejecuta|ejecutar)\s+(?P<cmd>.+)$", text, re.I | re.S)
        if command:
            try:
                argv = shlex.split(command["cmd"])
            except ValueError as e:
                return BrainReply(f"No entiendo ese comando: {e}")
            return BrainReply("Ejecuto el comando.", (ToolCall("run_command", {"command": argv}),))
        for pattern, build in TOOL_RULES:
            match = pattern.match(text)
            if match:
                return BrainReply("Usaré una herramienta.", (build(match),))

        normalized = normalize(text)
        for pattern, handler in self._text_rules:
            if pattern.search(normalized):
                result = handler()
                return result if isinstance(result, BrainReply) else BrainReply(result)
        return BrainReply("Todavía no sé hacer eso. Pregúntame «¿qué puedes hacer?».")

    @staticmethod
    def _report_tool_results(conversation: Sequence[Message]) -> BrainReply:
        # Un LLM resumiría los resultados; el cerebro de reglas los muestra tal cual.
        results = []
        for message in reversed(conversation):
            if message.role != "tool":
                break
            results.append(message.content)
        return BrainReply("\n\n".join(reversed(results)))

    def _identity(self) -> str:
        return (
            f"Soy {self._assistant_name}, tu asistente personal. "
            "Ahora mismo funciono con un cerebro de reglas muy sencillo."
        )

    def _help(self) -> str:
        return (
            "De momento entiendo:\n"
            "  · saludos, «¿qué hora es?», «¿qué día es hoy?», «¿quién eres?»\n"
            "  · «información del sistema»   · «abre firefox»\n"
            "  · «lista ~/Proyectos»   · «lee ~/Proyectos/notas.txt»\n"
            "  · «crea el archivo ~/Proyectos/notas.txt con hola»\n"
            "  · «sobrescribe el archivo ~/Proyectos/notas.txt con adiós»\n"
            "  · «ejecuta df -h»  (solo programas permitidos, en un sandbox)\n"
            "  · «recuerda que prefiero respuestas cortas»   · «¿qué recuerdas de mí?»"
        )

    def _time(self) -> str:
        return f"Son las {self._now():%H:%M}."

    def _date(self) -> str:
        now = self._now()
        return f"Hoy es {WEEKDAYS[now.weekday()]}, {now.day} de {MONTHS[now.month - 1]} de {now.year}."
