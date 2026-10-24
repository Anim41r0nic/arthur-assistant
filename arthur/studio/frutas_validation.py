"""Validación editorial de Frutinovelas (2026-10-17): cada escena solo puede contar lo que su plano enseña.

Por qué existe: en el piloto, las frases de las escenas 7/8 narraban acciones (sacar un anillo, un recibo) que
la imagen no mostraba. Aquí el guion declara en campos cerrados qué plano usa, qué gesto, qué objetos y qué
hechos; el catálogo de planos (revisado a ojo una vez) dice qué se ve de verdad en cada foto. Si una escena pide
algo que su plano no tiene, el guion se retiene con un motivo legible para Marc.

Límites (a propósito): no interpreta el texto libre de la voz ni mira píxeles. Quien escribe el guion (Gemini o
una persona) traduce lo que afirma a `objetos` y `hechos`; la revisión humana comprueba que eso sea cierto.
Sin red, sin ficheros y sin modificar los argumentos: se puede llamar tantas veces como haga falta.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


# El reparto operativo (PERSONAJES.md). Lola, Mara y Teo de biblioteca/catalogo.json son un prototipo de otro
# reparto: si llegan aquí, se rechazan en vez de colarse como si fueran estos personajes.
REPARTO = frozenset({"fina", "nico", "vera", "mora", "berto", "kiro"})
# 8–12 escenas de unos 4 s: cabe en un Short de menos de 60 s y deja tiempo a cada frase.
MIN_ESCENAS = 8
MAX_ESCENAS = 12


@dataclass(frozen=True)
class Resultado:
    valido: bool
    motivos: tuple[str, ...]


# El guion puede venir de un modelo de lenguaje: cualquier campo puede tener un tipo inesperado. Estas dos
# ayudas devuelven None en vez de fallar, para que el motivo salga como texto y no como un error de Python.
def _lista(valor: object) -> list[str] | None:
    if not isinstance(valor, list) or any(not isinstance(x, str) or not x for x in valor):
        return None
    return valor


def _mapa_texto(valor: object) -> dict[str, str] | None:
    if not isinstance(valor, Mapping) or any(
        not isinstance(k, str) or not k or not isinstance(v, str) or not v
        for k, v in valor.items()
    ):
        return None
    return dict(valor)


def validar_guion(guion: Mapping, catalogo: Mapping) -> Resultado:
    """Devuelve motivos legibles; nunca modifica el guion ni el catálogo.

    Catálogo: ``planos`` es una lista con id, personaje, validado,
    objetos_visibles, gestos y hechos_visibles. ``estado_visible`` es opcional
    y asigna objetos cuyo portador o estado sí puede verse en ese plano.
    ``estado_inicial`` asigna
    cada objeto narrativo a un estado (p. ej. ``carpeta: fina``).

    Escena: numero, personaje, plano, gesto, objetos, hechos, estado_antes y
    cambios. Cada ``cambio`` tiene objeto, desde, hasta y hecho; el hecho
    debe figurar entre los hechos visibles del plano. Un corte fuera de campo
    exige otro plano que muestre el nuevo estado antes de afirmarlo.
    """
    motivos: list[str] = []
    if not isinstance(guion, Mapping) or not isinstance(catalogo, Mapping):
        return Resultado(False, ("El guion y el catálogo deben ser mapas.",))
    planos_raw = catalogo.get("planos")
    if not isinstance(planos_raw, list):
        return Resultado(False, ("El catálogo no contiene una lista de planos.",))
    planos: dict[str, Mapping] = {}
    for indice, plano in enumerate(planos_raw, 1):
        if not isinstance(plano, Mapping) or not isinstance(plano.get("id"), str) or not plano["id"]:
            motivos.append(f"Catálogo: plano {indice} sin ID válido.")
            continue
        if plano["id"] in planos:
            motivos.append(f"Catálogo: ID de plano repetido: {plano['id']}.")
        planos[plano["id"]] = plano
    # Copia propia del estado: se va actualizando escena a escena (quién tiene cada objeto) sin tocar el catálogo.
    estado = _mapa_texto(catalogo.get("estado_inicial"))
    if estado is None:
        motivos.append("Catálogo: falta estado_inicial con valores de texto.")
        estado = {}
    escenas = guion.get("escenas")
    if not isinstance(escenas, list):
        return Resultado(False, tuple(motivos + ["El guion no contiene una lista de escenas."]))
    if not MIN_ESCENAS <= len(escenas) <= MAX_ESCENAS:
        motivos.append(f"El guion debe tener entre {MIN_ESCENAS} y {MAX_ESCENAS} escenas; tiene {len(escenas)}.")

    for indice, escena in enumerate(escenas, 1):
        prefijo = f"Escena {indice}:"
        if not isinstance(escena, Mapping):
            motivos.append(f"{prefijo} debe ser un mapa.")
            continue
        # True == 1 en Python: sin la comprobación de bool, «numero: true» pasaría por la escena 1.
        if escena.get("numero") != indice or isinstance(escena.get("numero"), bool):
            motivos.append(f"{prefijo} número ausente o fuera de secuencia.")
        personaje = escena.get("personaje")
        if not isinstance(personaje, str) or personaje not in REPARTO:
            motivos.append(f"{prefijo} personaje fuera del reparto: {personaje!r}.")
        plano_id = escena.get("plano")
        plano = planos.get(plano_id) if isinstance(plano_id, str) else None
        if plano is None:
            motivos.append(f"{prefijo} plano inexistente: {plano_id!r}.")
        else:
            if plano.get("personaje") != personaje:
                motivos.append(f"{prefijo} el plano {plano_id!r} muestra a {plano.get('personaje')!r}, no a {personaje!r}.")
            if plano.get("validado") is not True:
                motivos.append(f"{prefijo} el plano {plano_id!r} no está validado visualmente.")
            for campo in ("gestos", "objetos_visibles", "hechos_visibles"):
                if _lista(plano.get(campo)) is None:
                    motivos.append(f"{prefijo} el plano {plano_id!r} no declara {campo} como lista de textos.")

        gesto = escena.get("gesto")
        if not isinstance(gesto, str) or not gesto:
            motivos.append(f"{prefijo} falta un gesto declarado.")
        elif plano is not None and gesto not in (_lista(plano.get("gestos")) or []):
            motivos.append(f"{prefijo} el gesto {gesto!r} no se ve en el plano {plano_id!r}.")
        for campo, catalogo_campo in (("objetos", "objetos_visibles"), ("hechos", "hechos_visibles")):
            pedidos = _lista(escena.get(campo))
            if pedidos is None:
                motivos.append(f"{prefijo} {campo} debe ser una lista de textos, aunque esté vacía.")
                continue
            disponibles = (_lista(plano.get(catalogo_campo)) or []) if plano is not None else []
            for pedido in pedidos:
                if pedido not in disponibles:
                    tipo = "objeto" if campo == "objetos" else "hecho"
                    motivos.append(f"{prefijo} el {tipo} {pedido!r} no se ve en el plano {plano_id!r}.")

        antes = _mapa_texto(escena.get("estado_antes"))
        if antes is None:
            motivos.append(f"{prefijo} estado_antes debe ser un mapa de textos.")
            antes = {}
        for objeto, esperado in antes.items():
            if objeto not in estado:
                motivos.append(f"{prefijo} estado del objeto desconocido {objeto!r}.")
            elif estado[objeto] != esperado:
                motivos.append(f"{prefijo} continuidad rota: {objeto!r} está en {estado[objeto]!r}, no en {esperado!r}.")
        cambios = escena.get("cambios")
        if not isinstance(cambios, list):
            motivos.append(f"{prefijo} cambios debe ser una lista.")
            continue
        # Un objeto solo cambia de manos si el plano ENSEÑA ese hecho (p. ej. «recibe_carpeta») y la escena lo
        # declara. Un corte fuera de campo no basta: hace falta otro plano que lo muestre.
        cambiados: set[str] = set()
        for cambio in cambios:
            if not isinstance(cambio, Mapping):
                motivos.append(f"{prefijo} cambio de estado mal formado.")
                continue
            objeto, origen, destino, hecho = (cambio.get(k) for k in ("objeto", "desde", "hasta", "hecho"))
            if not all(isinstance(x, str) and x for x in (objeto, origen, destino, hecho)):
                motivos.append(f"{prefijo} cambio sin objeto, desde, hasta o hecho válido.")
                continue
            if objeto in cambiados:
                motivos.append(f"{prefijo} el objeto {objeto!r} cambia dos veces.")
                continue
            cambiados.add(objeto)
            if objeto not in estado:
                motivos.append(f"{prefijo} cambia un objeto desconocido: {objeto!r}.")
            elif estado[objeto] != origen:
                motivos.append(f"{prefijo} continuidad rota: {objeto!r} está en {estado[objeto]!r}, no en {origen!r}.")
            elif plano is None or hecho not in (_lista(plano.get("hechos_visibles")) or []) or hecho not in (_lista(escena.get("hechos")) or []):
                motivos.append(f"{prefijo} el cambio de {objeto!r} carece del hecho visible {hecho!r}.")
            else:
                estado[objeto] = destino
        # Lo que la foto muestra fijo (p. ej. la carpeta en manos de Mora) debe cuadrar con el estado tras la
        # escena: así no se reutiliza un plano antiguo que contradice lo ya contado.
        if plano is not None and "estado_visible" in plano:
            visible = _mapa_texto(plano["estado_visible"])
            if visible is None:
                motivos.append(f"{prefijo} estado_visible del plano {plano_id!r} debe ser un mapa de textos.")
            else:
                for objeto, valor in visible.items():
                    if objeto not in estado:
                        motivos.append(f"{prefijo} el plano {plano_id!r} muestra estado de un objeto desconocido: {objeto!r}.")
                    elif estado[objeto] != valor:
                        motivos.append(f"{prefijo} continuidad rota: el plano {plano_id!r} muestra {objeto!r} en {valor!r}, pero el estado es {estado[objeto]!r}.")
    return Resultado(not motivos, tuple(motivos))
