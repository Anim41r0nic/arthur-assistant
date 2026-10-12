"""Que ARTHUR no diga que ha hecho algo que no ha hecho (fase S5, paso 4).

Pasó de verdad (2026-10-12): «abre Code::Blocks» → el modelo miró la lista de aplicaciones y contestó «lo abro»
SIN pedir la herramienta que abre apps. No se abrió nada y ARTHUR dijo que sí.

Aquí solo se reconoce si una respuesta AFIRMA haber hecho algo («he abierto…», «abro…», «¡hecho!», «recordatorio
creado»). Quién comprueba si de verdad se hizo es el asistente (core/assistant.py): mira si en esa petición salió
bien alguna herramienta que hace cosas (no las de solo mirar). Las negaciones («no lo he abierto») no cuentan.
Es a propósito sencillo: preferimos avisar de más una vez (cuesta una vuelta del modelo) a dejar pasar una mentira.
"""

from __future__ import annotations

import re

# Participios de acciones: «he abierto», «lo he creado», «te lo he apuntado»… pero no «no lo he abierto».
_PARTICIPLES = (
    "abierto|cerrado|creado|borrado|eliminado|quitado|enviado|mandado|guardado|apuntado|anotado|movido|cambiado|"
    "puesto|subido|bajado|pausado|parado|reanudado|programado|añadido|instalado|escrito|activado|desactivado|"
    "cancelado|configurado|actualizado|copiado|silenciado|reproducido"
)
_DONE = re.compile(rf"\b(?:he|hemos)\s+(?:{_PARTICIPLES})\b")
# Lo que justo antes lo niega: «no he…», «no lo he…», «aún no te lo he…», «nunca la he…».
_NEGATED = re.compile(r"\b(?:no|nunca|ni)(?:\s+(?:te|se|me))?(?:\s+(?:lo|la|los|las))?\s*$")
# Presente en primera persona al empezar una frase o tras «vale/listo/ya/ahora…»: «Abro Code::Blocks», «Vale, te
# lo apunto». Al principio de frase para no confundir «el cambio» o «creo que…» con una acción.
_PRESENT = (
    "abro|cierro|borro|elimino|quito|envío|mando|guardo|apunto|anoto|muevo|cambio|pongo|subo|bajo|pauso|paro|"
    "programo|añado|instalo|activo|desactivo|cancelo|copio|silencio|abriendo|cerrando|creando|borrando|enviando|"
    "guardando|apuntando|moviendo|cambiando|poniendo|subiendo|bajando|pausando|programando|añadiendo|instalando"
)
_DOING = re.compile(
    rf"(?:^|[.!?¡¿;:]\s*|\b(?:vale|listo|perfecto|ya|ahora|enseguida|claro|hecho),?\s+)"
    rf"(?:(?:te |se |me )?(?:lo |la |los |las )?)(?:{_PRESENT})\b"
)
_SHORT = re.compile(r"^[¡!\s]*(?:hecho|listo)\b")
_CREATED = re.compile(
    r"\b(?:recordatorio|evento|cita|alarma|nota|aviso|borrador)\s+"
    r"(?:creado|creada|guardado|guardada|programado|programada|puesto|puesta|añadido|añadida|borrado|borrada)\b"
)

NUDGE = (
    "[Aviso del sistema, no del usuario] Tu respuesta dice que has hecho algo, pero en esta petición no se ha "
    "ejecutado ninguna acción. Si se puede hacer con tus herramientas, llama ahora a la adecuada. Si no se puede, "
    "dile al usuario con sinceridad que no lo has hecho y por qué. No digas que lo has hecho."
)
HONEST_REPLY = ("Perdona, no lo he hecho: no he llegado a usar ninguna herramienta para ello. "
                "Prueba a pedírmelo de otra forma.")


def claims_action(text: str) -> bool:
    """¿Dice esta respuesta que ARTHUR ha hecho (o está haciendo) algo?"""
    t = " ".join(text.lower().split())
    done = any(not _NEGATED.search(t[max(0, m.start() - 20):m.start()]) for m in _DONE.finditer(t))
    return bool(done or _DOING.search(t) or _SHORT.search(t) or _CREATED.search(t))
