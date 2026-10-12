"""Decir la respuesta a frases, según el modelo la va escribiendo (voz a trozos, 0.56.0).

En el servidor, escribir es lo lento (~4 palabras/s con la gráfica integrada): una respuesta de 45 palabras tarda
~11 s en estar entera. En vez de esperar, cada frase terminada sale ya hacia la voz, y ARTHUR empieza a hablar
mientras el modelo sigue con la siguiente.

La regla de honestidad (core/honesty.py) sigue mandando: mientras en esta petición no se haya hecho nada de verdad,
una frase que AFIRMA haber hecho algo («Abro Firefox») no se dice. Se para ahí y el asistente decide como siempre
(avisar al modelo o responder con sinceridad); al final, `finish` dice lo que falte de la respuesta definitiva.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Fin de frase: . ! ? … ; : seguidos de espacio, o un salto de línea. Un «3.5» o «p.ej.» sin espacio no corta.
SENTENCE_END = re.compile(r"[.!?…;:]+[\"'»)\]]*(?=\s)|\n+")
MIN_CHARS = 12  # «Vale.» suelta no va sola a la voz: se junta con la siguiente (cada trozo de voz cuesta algo)
# El PRIMER trozo de cada respuesta puede cortarse también en una coma, si ya es así de largo: medido el 2026-10-12,
# la primera frase del modelo tenía 107 letras (~6 s escribiéndola) y ARTHUR callaba hasta tenerla entera. El resto
# va por frases enteras, que suenan más naturales.
FIRST_CLAUSE = re.compile(r",(?=\s)")
FIRST_MIN_CHARS = 30


class SpokenStream:
    def __init__(self, emit: Callable[[str], None], may_say: Callable[[str], bool],
                 clean: Callable[[str], str] = lambda text: text) -> None:
        """`emit`: recibe cada frase lista para decir. `may_say(texto dicho hasta ahora + frase)`: ¿se puede decir?
        `clean`: lo que se quita antes de hablar (p. ej. los «#N» del calendario)."""
        self.emit = emit
        self.may_say = may_say
        self.clean = clean
        self.new_pass()

    def new_pass(self) -> None:
        """Otra vuelta del modelo (tras usar herramientas o tras el aviso de honestidad): lo de la vuelta anterior
        que no llegó a ser frase entera se descarta, y vuelve a poder decirse."""
        self._buffer = ""
        self._said = ""       # lo dicho en ESTA vuelta (para saber qué falta al final)
        self._blocked = False

    def feed(self, delta: str) -> None:
        if self._blocked:
            return
        self._buffer += delta
        while not self._blocked:
            match = next((m for m in SENTENCE_END.finditer(self._buffer)
                          if len(self._buffer[:m.end()].strip()) >= MIN_CHARS), None)
            if not self._said:  # aún no ha dicho nada: vale cortar en una coma si ya hay bastante
                comma = next((m for m in FIRST_CLAUSE.finditer(self._buffer)
                              if len(self._buffer[:m.end()].strip()) >= FIRST_MIN_CHARS), None)
                if comma is not None and (match is None or comma.end() < match.end()):
                    match = comma
            if match is None:
                return  # aún no hay una frase entera (o es muy corta: se junta con lo que venga)
            sentence, self._buffer = self._buffer[:match.end()], self._buffer[match.end():]
            self._say(sentence)

    def finish(self, final_text: str) -> None:
        """La respuesta definitiva (la que devuelve el asistente): se dice lo que aún no se haya dicho de ella.
        Si es otra distinta (la sincera, o el resultado directo de una herramienta), entera."""
        rest = final_text[len(self._said):] if final_text.startswith(self._said) else final_text
        text = self.clean(rest).strip()
        if text:
            self.emit(text)
        self._said = final_text
        self._buffer = ""

    def _say(self, sentence: str) -> None:
        if not self.may_say(self._said + sentence):
            self._blocked = True  # afirma algo que no se ha hecho: ni esta ni las siguientes de esta vuelta
            return
        self._said += sentence
        text = self.clean(sentence).strip()
        if text:
            self.emit(text)
