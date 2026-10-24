"""Los perfiles de Studio: a qué canal de YouTube va cada vídeo (separado del idioma).

Antes el idioma del guion decidía el canal (`es` → el canal de estoicismo en español, `en` → el de inglés). Con
Frutinovelas (2026-10-17) hay un segundo canal en español, así que el idioma ya no basta: con solo `es`, un vídeo de
Frutinovelas podría acabar en el canal de estoicismo. Ahora cada guion y cada revisión llevan un PERFIL estable
y el publicador elige el permiso de YouTube por perfil.

Compatibilidad: los guiones sin `perfil` siguen yendo por idioma a estoicismo (`DEFAULT_BY_LANGUAGE`), las
revisiones antiguas se migran por su canal (store.py, esquema 6) y los permisos guardados con el nombre viejo
(`youtube-oauth-es`, `youtube-canal-es.json`) se siguen leyendo (`legacy`). `es` y `en` valen como alias en la
terminal (`--studio-conectar-youtube es`).

Este módulo no importa nada de Studio: lo usan project.py, review.py y publish.py sin ciclos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# El @ de cada canal sale de la configuración (.env del servidor, ver .env.example), no del código: así cada
# instalación sube a SUS canales. Los valores por defecto son de ejemplo y no existen: con ellos la comprobación
# del canal antes de subir falla («el permiso es de otro canal») y no se sube nada hasta poner el @ de verdad.
HANDLE_ENV = {
    "estoicismo_es": ("ARTHUR_STUDIO_CANAL_ESTOICISMO_ES", "@TuCanalEstoicismo"),
    "estoicismo_en": ("ARTHUR_STUDIO_CANAL_ESTOICISMO_EN", "@TuCanalStoicEN"),
    "frutinovelas": ("ARTHUR_STUDIO_CANAL_FRUTINOVELAS", "@TuCanalFrutinovelas"),
}


def handle_for(profile_id: str, env=os.environ) -> str:
    """El @ del canal de un perfil: el de su variable o, si no está, el de ejemplo."""
    name, default = HANDLE_ENV[profile_id]
    value = (env.get(name) or "").strip()
    return value if value.startswith("@") else default


@dataclass(frozen=True)
class Profile:
    id: str              # nombre interno estable (va en la base de datos y en los ficheros de permiso)
    name: str            # lo que ve Marc en la página
    language: str        # idioma de los guiones de este canal
    handle: str          # el @ público que se comprueba antes de cada subida
    legacy: str = ""     # nombre viejo de sus ficheros de permiso (`es`/`en`); vacío en los perfiles nuevos
    publishing: bool = True   # False: no se sube nada a este canal (ni a mano ni el piloto) hasta activarlo
    note: str = ""       # lo que se enseña mientras no está conectado
    # Lo que hace el canal: «largo» (vídeo largo + su corto, como estoicismo) o «corto» (solo Shorts, como
    # Frutinovelas). Solo a un canal de cortos se le pueden importar Shorts hechos fuera (importer.py).
    product: str = "largo"
    # Subidas como mucho por día (0 = sin límite). Con límite, lo aprobado de ese canal no sale al pulsar «Aprobar»:
    # espera en cola y autopilot.drip_due saca el más antiguo a la hora de publicar, uno cada día.
    per_day: int = 0


PROFILES: dict[str, Profile] = {p.id: p for p in (
    Profile("estoicismo_es", "Estoicismo · español", "es", handle_for("estoicismo_es"), legacy="es"),
    Profile("estoicismo_en", "Estoicismo · inglés", "en", handle_for("estoicismo_en"), legacy="en"),
    # Canal de solo Shorts. Su id interno se fija al conectarlo. Publicación activada y UN vídeo al día (a las 18:00
    # y en público, lo importado incluido; lo importado sigue necesitando su «Aprobar»). Con el @ de ejemplo la
    # comprobación del canal falla antes de subir, así que nada sale hasta configurar el canal de verdad.
    Profile("frutinovelas", "Frutinovelas · español", "es", handle_for("frutinovelas"), publishing=True,
            note="Canal creado · pendiente de conectar", product="corto", per_day=1),
)}
ORDER = tuple(PROFILES)   # el orden de la página: estoicismo es, estoicismo en, frutinovelas
ALIASES = {p.legacy: p.id for p in PROFILES.values() if p.legacy}
# Un guion sin `perfil` es de estoicismo, como todos los que había antes de Frutinovelas.
DEFAULT_BY_LANGUAGE = {"es": "estoicismo_es", "en": "estoicismo_en"}


class UnknownProfile(ValueError):
    pass


def get(name: str) -> Profile:
    """Perfil por su nombre interno o por el alias viejo (`es`, `en`)."""
    profile = PROFILES.get(ALIASES.get(name, name))
    if profile is None:
        raise UnknownProfile(f"No hay ningún canal «{name}» (hay: {', '.join(ORDER)})")
    return profile


def for_script(perfil: str, idioma: str) -> Profile:
    """El perfil de un guion: el que dice su campo `perfil` o, si no lo tiene, el de estoicismo de su idioma."""
    if perfil:
        return get(perfil)
    if idioma not in DEFAULT_BY_LANGUAGE:
        raise UnknownProfile(f"No hay canal para el idioma «{idioma}»: pon `perfil = \"…\"` en el guion")
    return PROFILES[DEFAULT_BY_LANGUAGE[idioma]]


def for_review(profile: str, channel: str, language: str) -> Profile:
    """El perfil de una revisión guardada. Las de antes del esquema 6 sin perfil se deducen por su canal (el @ es lo
    que se aprobó), y si ni eso, por idioma: así siguen yendo exactamente al mismo canal que antes."""
    if profile:
        return get(profile)
    for p in PROFILES.values():
        if p.handle == channel:
            return p
    return for_script("", language)
