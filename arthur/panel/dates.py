"""Fechas en español dentro del texto de una tarea («llamar a Ana mañana», «examen el 20»).

Sin reloj propio: `today` entra como parámetro para que las pruebas sean exactas.
"""

import calendar
import re
import unicodedata
from datetime import date, timedelta

DIAS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")  # índice = date.weekday()

# Se busca sobre una copia sin acentos ni mayúsculas, pero de la MISMA longitud que el original, para que las
# posiciones encontradas sirvan para recortar el texto de Marc tal como lo escribió.
_EXPRESION = re.compile(
    r"\b(?:(?P<rel>pasado\s+manana|manana|hoy)"
    r"|(?:el\s+)?(?P<dia>" + "|".join(DIAS) + r")"
    r"|el\s+(?P<num>\d{1,2})(?![\d:/.]))\b"
)


def _plano(texto: str) -> str:
    """Minúsculas y sin acentos, carácter a carácter (así no cambia la longitud)."""
    return "".join(unicodedata.normalize("NFD", c.lower())[0] for c in texto)


def _siguiente_dia_del_mes(numero: int, hoy: date) -> date | None:
    """El próximo día `numero` del mes (hoy incluido); salta los meses que no lo tienen (el 31 en noviembre)."""
    año, mes = hoy.year, hoy.month
    for _ in range(14):
        if numero <= calendar.monthrange(año, mes)[1]:
            candidata = date(año, mes, numero)
            if candidata >= hoy:
                return candidata
        mes += 1
        if mes > 12:
            año, mes = año + 1, 1
    return None


def parse_task(text: str, today: date) -> tuple[str, date | None]:
    """Devuelve (título sin la expresión de fecha, fecha o None). Si hay varias expresiones, vale la última."""
    ultima = None
    for m in _EXPRESION.finditer(_plano(text)):
        if m["num"] is not None and not 1 <= int(m["num"]) <= 31:
            continue  # «el 0» o «el 32» no son un día: se dejan como texto
        ultima = m
    if ultima is None:
        return text, None

    if ultima["rel"]:
        rel = ultima["rel"].split()[0]
        dias = {"hoy": 0, "manana": 1, "pasado": 2}[rel]
        fecha = today + timedelta(days=dias)
    elif ultima["dia"]:
        # El mismo día de la semana significa la semana que viene: «el viernes» dicho en viernes no es hoy.
        falta = (DIAS.index(ultima["dia"]) - today.weekday()) % 7 or 7
        fecha = today + timedelta(days=falta)
    else:
        fecha = _siguiente_dia_del_mes(int(ultima["num"]), today)
        if fecha is None:
            return text, None

    titulo = " ".join((text[:ultima.start()] + " " + text[ultima.end():]).split())
    return (titulo or text), fecha
