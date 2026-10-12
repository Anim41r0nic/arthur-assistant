"""Tests de los gestos: puntos de la mano → postura, trayectoria de la muñeca → movimiento, y la «fuente de
fotogramas».

Todo con DATOS FALSOS: manos y trayectorias dibujadas con listas de números. No hay cámara, ni modelo, ni OpenCV: el módulo
`arthur.interfaces.gestos` no los necesita (y un test de aquí comprueba que no los importa).
"""

import ast
import math
import random
import sys
import unittest
from contextlib import closing
from pathlib import Path

from arthur.interfaces.gestos import (
    MOTIONS,
    POSTURES,
    Frame,
    FrameSource,
    FrameSourceError,
    Gesture,
    HandLandmarks,
    ListFrameSource,
    TrackPoint,
    classify,
    classify_motion,
    features,
    from_wire,
    iter_readings,
    motion_scores,
    posture_scores,
    track_point,
)

# --- manos de mentira -------------------------------------------------------------------------------------------
# Coordenadas "de imagen" (la Y crece hacia ABAJO, como en la cámara), con la muñeca en (0, 0) y la palma
# (muñeca → nudillo del corazón) midiendo 1. Después se escala, gira, refleja y mueve.

MCPS = {"index": (-0.30, -0.95), "middle": (0.0, -1.0), "ring": (0.28, -0.95), "pinky": (0.52, -0.82)}
SPLAY = {"index": -0.10, "middle": 0.0, "ring": 0.08, "pinky": 0.18}  # abanico de los dedos extendidos (rad)
EXTENDED = (0.45, 0.27, 0.22)   # largo de cada falange, en la dirección del dedo
HALFWAY = (0.40, 0.05, 0.05)    # medio doblado: ni estirado ni cerrado
CURLED = (0.30, -0.15, -0.28)   # puño: la punta vuelve hacia la palma

THUMB_OUT = [(-0.30, -0.20), (-0.55, -0.45), (-0.82, -0.67), (-1.08, -0.88)]      # mano abierta
THUMB_UP = [(-0.30, -0.20), (-0.48, -0.48), (-0.50, -0.80), (-0.50, -1.15)]        # puño con el pulgar hacia arriba
THUMB_TUCKED = [(-0.30, -0.20), (-0.48, -0.48), (-0.25, -0.72), (-0.08, -0.62)]    # pulgar metido dentro del puño
THUMB_FOLDED = [(-0.30, -0.20), (-0.48, -0.48), (-0.40, -0.75), (-0.12, -0.85)]    # pulgar cruzado delante del puño
THUMB_TO_INDEX = [(-0.30, -0.20), (-0.48, -0.48), (-0.62, -0.85), (-0.62, -1.30)]  # pulgar estirado hacia el índice
THUMB_RELAXED = [(-0.30, -0.20), (-0.50, -0.42), (-0.62, -0.62), (-0.68, -0.80)]   # pulgar suelto, sin estirar
THUMB_ON_PHONE = [(-0.30, -0.20), (-0.50, -0.48), (-0.40, -0.80), (-0.15, -1.05)]  # pulgar sobre la pantalla
INDEX_HOOK = [(-0.30, -0.95), (-0.38, -1.30), (-0.48, -1.42), (-0.58, -1.38)]      # índice curvado hacia el pulgar
RELAXED = (0.40, 0.08, 0.02)    # mano suelta, en reposo: los dedos un poco doblados
TYPING = (0.38, 0.05, -0.02)    # dedos curvados sobre el teclado
WRAPPED = (0.38, 0.00, -0.10)   # dedos rodeando el móvil: más doblados, pero sin cerrarse (el móvil está en medio)


def finger_points(name: str, segments: tuple[float, float, float]) -> list[tuple[float, float]]:
    mcp = MCPS[name]
    splay = SPLAY[name] if segments == EXTENDED else 0.0
    direction = (math.sin(splay), -math.cos(splay))  # "arriba" en coordenadas de imagen es -Y
    points, (x, y) = [mcp], mcp
    for length in segments:
        x, y = x + direction[0] * length, y + direction[1] * length
        points.append((x, y))
    return points


def pose(*, thumb, fingers: dict[str, tuple[float, float, float]] | tuple[float, float, float]):
    """Los 21 puntos (en el orden de MediaPipe) de una mano ya colocada en el sistema de la muñeca."""
    if isinstance(fingers, tuple):
        fingers = {name: fingers for name in MCPS}
    points = [(0.0, 0.0), *thumb]
    for name in ("index", "middle", "ring", "pinky"):
        points += finger_points(name, fingers[name])
    assert len(points) == 21
    return points


def place(points, *, scale=0.2, degrees=0.0, mirror=False, origin=(0.5, 0.7)):
    """Escala, gira (alrededor de la muñeca), refleja (mano izquierda / imagen en espejo) y mueve la mano."""
    angle, placed = math.radians(degrees), []
    for x, y in points:
        x = -x if mirror else x
        x, y = x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)
        placed.append((origin[0] + x * scale, origin[1] + y * scale))
    return placed


def with_index(points, index_points):
    """La misma mano con otros cuatro puntos para el índice (para el ok y la pinza, que lo curvan)."""
    points = list(points)
    points[5:9] = index_points
    return points


def hand(points, **kwargs):
    confidence = kwargs.pop("confidence", 1.0)
    return HandLandmarks(place(points, **kwargs), confidence=confidence)


def only(*names, base=CURLED, then=EXTENDED):
    """Los dedos con `then` para los nombrados y `base` para el resto."""
    return {n: (then if n in names else base) for n in MCPS}


OPEN_PALM = pose(thumb=THUMB_OUT, fingers=EXTENDED)
THUMBS_UP = pose(thumb=THUMB_UP, fingers=CURLED)
FIST_THUMB_TUCKED = pose(thumb=THUMB_TUCKED, fingers=CURLED)
FIST = pose(thumb=THUMB_FOLDED, fingers=CURLED)
INDEX_UP = pose(thumb=THUMB_FOLDED, fingers=only("index"))
TWO_FINGERS = pose(thumb=THUMB_FOLDED, fingers=only("index", "middle"))
THUMB_PINKY = pose(thumb=THUMB_OUT, fingers=only("pinky"))
OK = with_index(pose(thumb=THUMB_TO_INDEX, fingers=EXTENDED), INDEX_HOOK)
PINCH = with_index(pose(thumb=THUMB_TO_INDEX, fingers=CURLED), INDEX_HOOK)
POSES = {
    Gesture.PALM: OPEN_PALM, Gesture.THUMB_UP: THUMBS_UP, Gesture.FIST: FIST, Gesture.INDEX_UP: INDEX_UP,
    Gesture.OK: OK, Gesture.PINCH: PINCH, Gesture.TWO_FINGERS: TWO_FINGERS, Gesture.THUMB_PINKY: THUMB_PINKY,
}
CLEAR = 0.8  # la `min_score` que propone docs/gestos.md: una postura clara tiene que superarla


class ClassifyTest(unittest.TestCase):
    def assertGesture(self, hand_, gesture, minimum=CLEAR):
        reading = classify(hand_)
        self.assertIsNotNone(reading, f"se esperaba {gesture}")
        self.assertEqual(reading.gesture, gesture)
        self.assertGreaterEqual(reading.score, minimum)
        self.assertLessEqual(reading.score, 1.0)

    def test_open_palm(self):
        self.assertGesture(hand(OPEN_PALM), Gesture.PALM)

    def test_thumbs_up(self):
        self.assertGesture(hand(THUMBS_UP), Gesture.THUMB_UP)

    def test_thumbs_down_is_the_thumbs_up_turned_over(self):
        self.assertGesture(hand(THUMBS_UP, degrees=180), Gesture.THUMB_DOWN)

    def test_every_posture_is_recognised_clearly(self):
        for gesture, points in POSES.items():
            with self.subTest(gesture.value):
                self.assertGesture(hand(points), gesture)

    def test_left_and_right_hand_are_the_same_gesture(self):
        for points, gesture in ((OPEN_PALM, Gesture.PALM), (THUMBS_UP, Gesture.THUMB_UP)):
            self.assertGesture(hand(points, mirror=True), gesture)
        self.assertGesture(hand(THUMBS_UP, degrees=180, mirror=True), Gesture.THUMB_DOWN)

    def test_size_and_position_in_the_image_do_not_matter(self):
        for scale, origin in ((0.05, (0.1, 0.9)), (0.4, (0.5, 0.5)), (120.0, (640.0, 480.0))):
            self.assertGesture(hand(OPEN_PALM, scale=scale, origin=origin), Gesture.PALM)
            self.assertGesture(hand(THUMBS_UP, scale=scale, origin=origin), Gesture.THUMB_UP)

    def test_a_slightly_tilted_hand_still_counts(self):
        for degrees in (-25, 25):
            self.assertGesture(hand(OPEN_PALM, degrees=degrees), Gesture.PALM)
            self.assertGesture(hand(THUMBS_UP, degrees=degrees), Gesture.THUMB_UP)

    def test_thumbs_up_and_down_are_never_mixed_up(self):
        self.assertEqual(classify(hand(THUMBS_UP)).gesture, Gesture.THUMB_UP)
        self.assertEqual(classify(hand(THUMBS_UP, degrees=180)).gesture, Gesture.THUMB_DOWN)

    # --- lo que NO es un gesto: aquí se juega la seguridad (un pulgar arriba que no era para ARTHUR) ------------

    def test_a_real_fist_is_never_a_thumbs_up(self):
        """Medidas reales (0.37.1): su ✊ empataba 0,50 «puño» / 0,50 «pulgar arriba», y uno es «No» y el otro «Sí»."""
        from arthur.interfaces.gestos.classify import HandFeatures, posture_scores

        for fingers, straight, length, along, across, gap in (
                ((0.64, 0.63, 0.53, 0.61), 0.83, 0.45, 0.89, 0.30, 0.22),
                ((0.64, 0.63, 0.54, 0.63), 0.78, 0.41, 0.78, 0.31, 0.16)):
            scores = posture_scores(HandFeatures(fingers, straight, length, along, across, gap, hand_tilt=15.0,
                                                 index_tilt=15.0, thumb_up_tilt=27.0, thumb_down_tilt=153.0))
            self.assertEqual(scores[Gesture.THUMB_UP], 0.0)
            self.assertGreater(scores[Gesture.FIST], 0.0)

    def test_a_fist_with_the_thumb_tucked_is_a_fist_not_a_thumb(self):
        """Antes de la 0.30.0 era «nada»; ahora es un puño. Lo que importa: nunca un pulgar arriba ni abajo."""
        self.assertGesture(hand(FIST_THUMB_TUCKED), Gesture.FIST)

    def test_one_half_bent_finger_makes_the_palm_nothing(self):
        for name in MCPS:
            fingers = {n: EXTENDED for n in MCPS} | {name: HALFWAY}
            reading = classify(hand(pose(thumb=THUMB_OUT, fingers=fingers)))
            if name == "index":
                # El índice tiene umbral propio (0.37.1): el estirado de una persona real mide 1,01-1,31 y este dibujado 1,07.
                # Queda como palma DUDOSA, por debajo de lo que cuenta: nunca salta sola.
                self.assertLess(reading.score, CLEAR)
            else:
                self.assertIsNone(reading, name)

    def test_one_missing_finger_makes_the_palm_nothing(self):
        for name in MCPS:
            fingers = {n: EXTENDED for n in MCPS} | {name: CURLED}
            self.assertIsNone(classify(hand(pose(thumb=THUMB_OUT, fingers=fingers))), name)

    def test_a_thumb_up_with_another_finger_out_is_nothing(self):
        """El gesto de «L» o de señalar con el pulgar levantado no es un «sí»."""
        for name in ("index", "middle", "ring"):
            self.assertIsNone(classify(hand(pose(thumb=THUMB_UP, fingers=only(name)))), name)
        # con el meñique fuera puede parecerse al «teléfono», pero nunca a un «sí»
        reading = classify(hand(pose(thumb=THUMB_UP, fingers=only("pinky"))))
        self.assertNotIn(reading.gesture if reading else None, (Gesture.THUMB_UP, Gesture.THUMB_DOWN))

    def test_a_half_closed_hand_with_the_thumb_up_is_nothing(self):
        self.assertIsNone(classify(hand(pose(thumb=THUMB_UP, fingers=HALFWAY))))

    def test_a_thumb_pointing_sideways_is_nothing(self):
        for degrees in (90, -90, 60, -60):
            self.assertIsNone(classify(hand(THUMBS_UP, degrees=degrees)), degrees)

    def test_a_palm_that_is_not_raised_is_nothing(self):
        for degrees in (90, -90, 180):
            self.assertIsNone(classify(hand(OPEN_PALM, degrees=degrees)), degrees)

    def test_a_degenerate_hand_is_nothing(self):
        self.assertIsNone(classify(HandLandmarks([(0.5, 0.5)] * 21)))  # todos los puntos en el mismo sitio
        self.assertIsNone(classify(hand(OPEN_PALM, scale=0.0)))

    def test_the_score_never_exceeds_the_confidence_of_the_hand_detector(self):
        sure = classify(hand(OPEN_PALM, confidence=1.0)).score
        unsure = classify(hand(OPEN_PALM, confidence=0.5)).score
        self.assertAlmostEqual(unsure, sure * 0.5)
        self.assertIsNone(classify(hand(OPEN_PALM, confidence=0.0)))

    def test_an_ambiguous_pose_scores_lower_than_a_clear_one(self):
        """El «casi» no se rechaza a ciegas: baja la seguridad, y `min_score` decide fuera de aquí."""
        clear = classify(hand(OPEN_PALM)).score
        tilted = classify(hand(OPEN_PALM, degrees=35)).score  # casi en el límite de lo que es «levantada»
        self.assertLess(tilted, clear)
        self.assertLess(tilted, CLEAR)

    def test_classify_is_deterministic_and_does_not_change_the_input(self):
        h = hand(THUMBS_UP)
        before = h.points
        self.assertEqual(classify(h), classify(h))
        self.assertEqual(h.points, before)


class PostureTest(unittest.TestCase):
    """Las posturas nuevas (0.30.0): manos izquierda y derecha, espejo, «casi» y falsos positivos."""

    def assertGesture(self, hand_, gesture, minimum=CLEAR):
        ClassifyTest.assertGesture(self, hand_, gesture, minimum)

    def assertNever(self, hand_, gestures, below=CLEAR, msg=None):
        """Ninguno de esos gestos llega a la seguridad `below` (por defecto, la `min_score` de docs/gestos.md)."""
        reading = classify(hand_)
        if reading is not None and reading.gesture in gestures:
            self.assertLess(reading.score, below, msg)

    def test_left_right_mirrored_and_tilted_hands(self):
        for gesture, points in POSES.items():
            for kwargs in ({"mirror": True}, {"degrees": 20}, {"degrees": -20, "mirror": True},
                           {"scale": 300.0, "origin": (640.0, 360.0)}):
                with self.subTest(gesture.value, **kwargs):
                    self.assertGesture(hand(points, **kwargs), gesture)

    def test_every_posture_wins_by_far(self):
        """En cada postura clara, ninguna otra de la lista pasa de 0: no hay dos gestos a la vez."""
        for gesture, points in POSES.items():
            scores = posture_scores(features(hand(points)))
            others = {g.value: s for g, s in scores.items() if g is not gesture and s > 0.0}
            self.assertEqual(others, {}, gesture.value)

    def test_a_fist_is_never_a_pinch(self):
        """En el puño la punta del pulgar queda cerca de la del índice: sin la condición del índice sería pinza."""
        self.assertEqual(posture_scores(features(hand(FIST)))[Gesture.PINCH], 0.0)

    def test_a_fist_with_the_thumb_to_the_side_is_not_a_fist(self):
        """Pulgar estirado y apuntando de lado: ni puño (el pulgar no está recogido) ni pulgar arriba."""
        self.assertIsNone(classify(hand(THUMBS_UP, degrees=90)))
        self.assertIsNone(classify(hand(THUMBS_UP, degrees=-90, mirror=True)))

    def test_a_fist_lying_down_is_nothing(self):
        for degrees in (90, -90, 180):
            self.assertIsNone(classify(hand(FIST, degrees=degrees)), degrees)

    # --- «casi» el gesto: baja la seguridad o desaparece, nunca se convierte en otro claro -----------------------

    def test_almost_ok_with_the_tips_a_bit_apart_scores_low(self):
        apart = [(x + 0.18, y) for x, y in INDEX_HOOK]  # las puntas a ~0,23 palmas (el 👌 de una persona real: 0,17)
        reading = classify(hand(with_index(pose(thumb=THUMB_TO_INDEX, fingers=EXTENDED), apart)))
        self.assertEqual(reading.gesture, Gesture.OK)
        self.assertLess(reading.score, CLEAR)

    def test_a_pinch_with_the_tips_far_apart_is_nothing(self):
        apart = [(x + 0.25, y) for x, y in INDEX_HOOK]
        self.assertIsNone(classify(hand(with_index(pose(thumb=THUMB_TO_INDEX, fingers=CURLED), apart))))

    def test_two_fingers_with_the_ring_half_bent_is_nothing(self):
        fingers = only("index", "middle") | {"ring": HALFWAY}
        self.assertIsNone(classify(hand(pose(thumb=THUMB_FOLDED, fingers=fingers))))

    def test_two_fingers_with_one_half_bent_is_not_two_fingers(self):
        fingers = only("index", "middle") | {"middle": HALFWAY}
        self.assertIsNone(classify(hand(pose(thumb=THUMB_FOLDED, fingers=fingers))))

    def test_index_up_almost_sideways_scores_low_and_pointing_sideways_is_nothing(self):
        self.assertLess(classify(hand(INDEX_UP, degrees=40)).score, CLEAR)  # el índice queda a ~34°
        for degrees in (60, -60, 90, 180):
            self.assertIsNone(classify(hand(INDEX_UP, degrees=degrees)), degrees)

    def test_index_up_with_the_thumb_out_is_nothing(self):
        """La «L» (índice y pulgar fuera) no es «índice arriba»."""
        self.assertIsNone(classify(hand(pose(thumb=THUMB_OUT, fingers=only("index")))))

    def test_thumb_pinky_with_the_ring_half_bent_is_nothing(self):
        fingers = only("pinky") | {"ring": HALFWAY}
        self.assertIsNone(classify(hand(pose(thumb=THUMB_OUT, fingers=fingers))))

    def test_thumb_pinky_without_the_thumb_is_nothing(self):
        self.assertIsNone(classify(hand(pose(thumb=THUMB_FOLDED, fingers=only("pinky")))))

    # --- falsos positivos: manos que NO están haciendo un gesto -----------------------------------------------

    def test_a_relaxed_hand_is_nothing(self):
        for kwargs in ({}, {"mirror": True}, {"degrees": 20}, {"degrees": -30, "mirror": True}):
            self.assertIsNone(classify(hand(pose(thumb=THUMB_RELAXED, fingers=RELAXED), **kwargs)), kwargs)

    def test_a_hand_on_the_keyboard_is_nothing(self):
        """Desde la cámara del portátil la mano que escribe sale de cualquier manera: se prueba en varias posturas,
        también aplastada en vertical (vista desde arriba, la mano se ve más corta)."""
        typing = pose(thumb=THUMB_RELAXED, fingers=TYPING)
        squashed = [(x, y * 0.5) for x, y in typing]
        for points in (typing, squashed):
            for degrees in (0, 30, -30, 150, 180, -150):
                for mirror in (False, True):
                    self.assertIsNone(classify(hand(points, degrees=degrees, mirror=mirror)), (degrees, mirror))

    def test_a_hand_holding_the_phone_never_reaches_min_score(self):
        """Lo peligroso sería leerla como pulgar arriba (= «sí»). Con estas manos dibujadas llega como mucho a 0,46:
        por debajo de `min_score`. ⚠ Es la primera cosa que Cada usuario debe comprobar con su móvil (docs/gestos.md)."""
        phone = pose(thumb=THUMB_ON_PHONE, fingers=WRAPPED)
        for degrees in range(-180, 180, 15):
            for mirror in (False, True):
                self.assertNever(hand(phone, degrees=degrees, mirror=mirror), POSTURES, msg=(degrees, mirror))

    def test_a_half_closed_hand_is_no_posture_at_all(self):
        for thumb in (THUMB_OUT, THUMB_UP, THUMB_FOLDED, THUMB_RELAXED):
            self.assertIsNone(classify(hand(pose(thumb=thumb, fingers=HALFWAY))))


class FeaturesTest(unittest.TestCase):
    def test_left_and_right_hand_give_the_same_measurements(self):
        for points in POSES.values():
            right, left = features(hand(points)), features(hand(points, mirror=True))
            for name in ("thumb_straightness", "thumb_length", "thumb_along", "thumb_across", "tips_gap",
                         "hand_tilt", "index_tilt", "thumb_up_tilt", "thumb_down_tilt"):
                self.assertAlmostEqual(getattr(right, name), getattr(left, name), msg=name)
            for a, b in zip(right.fingers, left.fingers):
                self.assertAlmostEqual(a, b)

    def test_measurements_do_not_depend_on_size_or_position(self):
        small, big = features(hand(OK, scale=0.05)), features(hand(OK, scale=500.0, origin=(1000.0, 800.0)))
        self.assertAlmostEqual(small.tips_gap, big.tips_gap)
        self.assertAlmostEqual(small.thumb_across, big.thumb_across)

    def test_a_degenerate_hand_has_no_measurements(self):
        self.assertIsNone(features(HandLandmarks([(0.5, 0.5)] * 21)))

    def test_measurements_are_not_coordinates(self):
        """Se pueden escribir en el registro para calibrar: no hay nada que diga dónde está la mano."""
        near, far = features(hand(FIST, origin=(0.1, 0.1))), features(hand(FIST, origin=(0.9, 0.9)))
        for name in ("thumb_along", "thumb_across", "tips_gap", "hand_tilt"):
            self.assertAlmostEqual(getattr(near, name), getattr(far, name), msg=name)


class WhitelistTest(unittest.TestCase):
    def test_gesture_names_are_the_ones_of_the_protocol(self):
        """Son los que viajarán por la tubería (docs/gestos.md): cambiarlos rompería la lista blanca."""
        self.assertEqual({g.value for g in Gesture}, {
            "palma", "pulgar_arriba", "pulgar_abajo", "puno", "indice_arriba", "circulo", "pinza", "dos_dedos",
            "pulgar_menique", "saludo", "mover_lateral",
        })

    def test_postures_and_motions_split_the_list(self):
        self.assertEqual(POSTURES | MOTIONS, set(Gesture))
        self.assertEqual(POSTURES & MOTIONS, set())
        self.assertEqual(MOTIONS, {Gesture.WAVE, Gesture.SWIPE})

    def test_names_are_plain_ascii(self):
        for gesture in Gesture:
            self.assertRegex(gesture.value, r"^[a-z_]+$")

    def test_from_wire_only_accepts_the_list(self):
        self.assertIs(from_wire("puno"), Gesture.FIST)
        self.assertIs(from_wire("saludo"), Gesture.WAVE)
        for bad in ("puño", "PUNO", " puno", "puno\n", "", "rm -rf", None, 3, b"puno", ["puno"]):
            self.assertIsNone(from_wire(bad), repr(bad))

    def test_the_names_live_in_a_single_place(self):
        """Ningún otro fichero del paquete escribe un nombre de gesto entre comillas: todos usan `Gesture`."""
        package = Path(__file__).resolve().parents[1] / "arthur" / "interfaces" / "gestos"
        names = {g.value for g in Gesture}
        found = []
        for path in sorted(package.glob("*.py")):
            if path.name == "names.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and node.value in names:
                    found.append(f"{path.name}: {node.value}")
        self.assertEqual(found, [])


# --- trayectorias de mentira ------------------------------------------------------------------------------------
# La muñeca en función del tiempo, en PALMAS, alrededor de un punto de la imagen. 15 fotogramas/s, palma de 0,2.

def trajectory(fx, seconds, *, fy=lambda t: 0.0, fps=15, palm=0.2, noise=0.0, seed=7, confidence=1.0,
               origin=(0.5, 0.6)):
    rng = random.Random(seed)  # «ruido» siempre igual: el test no puede fallar una vez de cada mil
    points = []
    for i in range(int(seconds * fps) + 1):
        t = i / fps
        x = fx(t) + rng.uniform(-noise, noise)
        y = fy(t) + rng.uniform(-noise, noise)
        points.append(TrackPoint(t, origin[0] + x * palm, origin[1] + y * palm, palm, confidence))
    return points


def sway(amplitude, hertz):
    return lambda t: amplitude * math.sin(2 * math.pi * hertz * t)


def sweep(palms_per_second):
    return lambda t: palms_per_second * t


WAVE = trajectory(sway(0.6, 2.0), 1.2)
SWIPE = trajectory(sweep(5.0), 0.5)


class MotionTest(unittest.TestCase):
    def assertMotion(self, track, gesture, minimum=CLEAR):
        reading = classify_motion(track)
        self.assertIsNotNone(reading, f"se esperaba {gesture}")
        self.assertEqual(reading.gesture, gesture)
        self.assertGreaterEqual(reading.score, minimum)
        self.assertLessEqual(reading.score, 1.0)
        return reading

    def test_wave(self):
        self.assertEqual(self.assertMotion(WAVE, Gesture.WAVE).direction, 0)

    def test_wave_with_a_shaky_detector(self):
        self.assertMotion(trajectory(sway(0.6, 2.0), 1.2, noise=0.08), Gesture.WAVE)

    def test_swipe_in_both_directions(self):
        self.assertEqual(self.assertMotion(SWIPE, Gesture.SWIPE).direction, 1)
        self.assertEqual(self.assertMotion(trajectory(sweep(-5.0), 0.5), Gesture.SWIPE).direction, -1)

    def test_mirrored_image_flips_the_swipe_and_keeps_the_wave(self):
        mirror = lambda track: [TrackPoint(p.timestamp, 1.0 - p.x, p.y, p.palm) for p in track]
        self.assertEqual(classify_motion(mirror(SWIPE)).direction, -1)
        self.assertEqual(classify_motion(mirror(WAVE)).gesture, Gesture.WAVE)

    def test_distance_to_the_camera_does_not_matter(self):
        """Lejos (palma pequeña) o cerca (palma grande): mismo gesto, porque todo va en palmas."""
        for palm in (0.05, 0.5, 120.0):
            self.assertMotion(trajectory(sway(0.6, 2.0), 1.2, palm=palm), Gesture.WAVE)
            self.assertMotion(trajectory(sweep(5.0), 0.5, palm=palm), Gesture.SWIPE)

    def test_the_score_never_exceeds_the_confidence_of_the_hand_detector(self):
        reading = classify_motion(trajectory(sway(0.6, 2.0), 1.2, confidence=0.5))
        self.assertAlmostEqual(reading.score, 0.5)
        self.assertIsNone(classify_motion(trajectory(sway(0.6, 2.0), 1.2, confidence=0.0)))

    # --- «casi» ---------------------------------------------------------------------------------------------

    def test_a_small_wave_scores_low(self):
        reading = classify_motion(trajectory(sway(0.25, 2.0), 1.2))
        self.assertEqual(reading.gesture, Gesture.WAVE)
        self.assertLess(reading.score, CLEAR)

    def test_a_short_swipe_scores_low(self):
        reading = classify_motion(trajectory(sweep(3.0), 0.5))  # 1,5 palmas: poco más del mínimo
        self.assertEqual(reading.gesture, Gesture.SWIPE)
        self.assertLess(reading.score, CLEAR)

    def test_a_single_back_and_forth_is_neither(self):
        """Ida y vuelta una vez: ni barrido (ha vuelto) ni saludo (le falta otra ida). Mejor nada que adivinar."""
        self.assertIsNone(classify_motion(trajectory(sway(0.6, 1.0), 0.75)))
        there_and_back = lambda t: 5.0 * t if t < 0.5 else 5.0 * (1.0 - t)
        self.assertIsNone(classify_motion(trajectory(there_and_back, 1.0)))

    # --- falsos positivos ------------------------------------------------------------------------------------

    def test_a_still_hand_that_trembles_is_nothing(self):
        self.assertIsNone(classify_motion(trajectory(lambda t: 0.0, 1.5, noise=0.12)))

    def test_moving_up_and_down_is_nothing(self):
        """Rascarse, coger el móvil, ajustarse las gafas: sobre todo vertical."""
        self.assertIsNone(classify_motion(trajectory(lambda t: 0.1 * math.sin(6 * t), 1.5, fy=sway(0.8, 2.0))))
        self.assertIsNone(classify_motion(trajectory(lambda t: 0.0, 0.6, fy=sweep(5.0))))

    def test_a_diagonal_sweep_is_not_a_swipe(self):
        self.assertIsNone(classify_motion(trajectory(sweep(5.0), 0.5, fy=sweep(5.0))))

    def test_a_slow_drift_is_not_a_swipe(self):
        """La mano que se desliza despacio hasta el ratón o el teclado."""
        self.assertIsNone(classify_motion(trajectory(sweep(0.8), 2.5)))

    def test_the_whole_arm_swinging_is_not_a_wave(self):
        """Estirarse o hacer gestos grandes al hablar: tramos de varias palmas."""
        self.assertIsNone(classify_motion(trajectory(sway(3.0, 1.0), 2.0)))

    def test_a_very_slow_sway_is_not_a_wave(self):
        self.assertIsNone(classify_motion(trajectory(sway(0.6, 0.4), 5.0)))

    def test_a_broken_trajectory_is_nothing(self):
        """Si la mano desaparece un segundo, no se pegan los trozos: puede ser otra mano u otra persona. (Medio
        segundo sí se tolera desde la 0.38.1: la cámara de una persona real pierde la mano 0,6-0,7 s al moverse.)"""
        track = WAVE[:8] + [TrackPoint(p.timestamp + 1.0, p.x, p.y, p.palm) for p in WAVE[8:]]
        self.assertIsNone(classify_motion(track))

    def test_too_few_points_or_no_movement_is_nothing(self):
        self.assertIsNone(classify_motion([]))
        self.assertIsNone(classify_motion(SWIPE[:3]))
        self.assertIsNone(classify_motion(trajectory(lambda t: 0.0, 1.0)))
        self.assertIsNone(classify_motion([TrackPoint(i / 15, 0.5, 0.5, 0.0) for i in range(10)]))

    # --- entradas -------------------------------------------------------------------------------------------

    def test_timestamps_must_go_forward(self):
        with self.assertRaises(ValueError):
            classify_motion([SWIPE[1], SWIPE[0], *SWIPE[2:]])
        with self.assertRaises(ValueError):
            classify_motion([SWIPE[0], SWIPE[0], *SWIPE[1:]])

    def test_track_points_reject_bad_numbers(self):
        for bad in (math.nan, math.inf, "1", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                TrackPoint(bad, 0.5, 0.5, 0.2)
            with self.assertRaises(ValueError, msg=repr(bad)):
                TrackPoint(0.0, 0.5, bad, 0.2)
        with self.assertRaises(ValueError):
            TrackPoint(0.0, 0.5, 0.5, -0.2)
        with self.assertRaises(ValueError):
            TrackPoint(0.0, 0.5, 0.5, 0.2, confidence=1.5)

    def test_track_point_takes_only_the_middle_fingertip_and_the_palm_size(self):
        h = hand(OPEN_PALM, scale=0.2, origin=(0.3, 0.7), confidence=0.9)
        point = track_point(2.5, h)
        self.assertEqual((point.timestamp, point.x, point.y, point.confidence), (2.5, *h.points[12], 0.9))
        self.assertAlmostEqual(point.palm, 0.2)

    def test_hands_from_frames_become_a_wave(self):
        """De punta a punta: manos (no solo puntos sueltos) que se mueven de lado a lado → saludo."""
        track = [track_point(p.timestamp, hand(OPEN_PALM, scale=0.2, origin=(p.x, p.y))) for p in WAVE]
        self.assertEqual(classify_motion(track).gesture, Gesture.WAVE)

    def test_motion_is_deterministic_and_does_not_change_the_input(self):
        track = list(WAVE)
        self.assertEqual(classify_motion(track), classify_motion(track))
        self.assertEqual(track, WAVE)
        self.assertEqual(motion_scores(track), motion_scores(tuple(track)))


class MotionTrackerTest(unittest.TestCase):
    """El seguidor de la muñeca del proceso de la cámara: manos dibujadas a 6 fotogramas/s, como la cámara real."""

    def frames(self, track, pose=OPEN_PALM):
        return [Frame(p.timestamp, hand(pose, scale=p.palm, origin=(p.x, p.y))) for p in track]

    def readings(self, frames):
        from arthur.interfaces.gestos.tracker import MotionTracker

        return [r for _, r in iter_readings(ListFrameSource(frames), MotionTracker())]

    def test_a_wave_comes_out_once_as_a_wave_and_never_as_a_palm(self):
        readings = self.readings(self.frames(trajectory(sway(0.6, 1.5), 2.0, fps=6)))
        names = [r.gesture for r in readings if r is not None]
        self.assertEqual(names.count(Gesture.WAVE), 1)
        # La mano abierta en movimiento NO es «calla». Solo el primer fotograma (un punto aún no es movimiento) sale
        # como palma, y uno suelto nunca llega a «mantener 1 s».
        self.assertNotIn(Gesture.PALM, [r.gesture for r in readings[1:] if r is not None])

    def test_a_swipe_comes_out_as_a_swipe(self):
        readings = self.readings(self.frames(trajectory(sweep(3.0), 0.8, fps=6), pose=FIST))
        self.assertIn(Gesture.SWIPE, [r.gesture for r in readings if r is not None])

    def test_a_still_hand_keeps_its_posture(self):
        still = self.frames(trajectory(lambda t: 0.0, 1.5, fps=6))
        self.assertTrue(all(r is not None and r.gesture is Gesture.PALM for r in self.readings(still)))

    def test_waving_by_turning_the_hand_on_a_still_wrist_is_a_wave(self):
        """Como saluda una persona real (0.38.1): la muñeca quieta y la mano girando ±30° sobre ella, a 5 fotogramas/s y con un
        fotograma perdido de vez en cuando."""
        frames = []
        for i in range(int(2.0 * 5) + 1):
            t = i / 5
            degrees = 30 * math.sin(2 * math.pi * 1.5 * t)
            frames.append(Frame(t, None if i % 4 == 3 else hand(OPEN_PALM, degrees=degrees, origin=(0.5, 0.7))))
        names = [r.gesture for r in self.readings(frames) if r is not None]
        self.assertIn(Gesture.WAVE, names)
        self.assertNotIn(Gesture.PALM, names[1:])

    def test_losing_the_hand_breaks_the_trajectory(self):
        from arthur.interfaces.gestos.tracker import MotionTracker

        frames = self.frames(trajectory(sway(0.6, 1.5), 1.4, fps=6))
        self.assertIn(Gesture.WAVE, [r.gesture for r in self.readings(frames) if r is not None])  # sin hueco, sí
        half = len(frames) // 2
        gap = frames[:half] + [Frame(frames[half - 1].timestamp + 0.2 * (i + 1), None) for i in range(5)] + [
            Frame(f.timestamp + 1.0, f.hand) for f in frames[half:]]  # más de MAX_GAP_S (0,7 s) sin mano
        tracker = MotionTracker()
        out = [tracker.feed(f.timestamp, f.hand) for f in gap]
        # cada mitad por separado es demasiado corta para un saludo claro: no sale ninguno
        self.assertNotIn(Gesture.WAVE, [r.gesture for r in out if r is not None])


class HandLandmarksTest(unittest.TestCase):
    def test_needs_exactly_21_points(self):
        for count in (0, 20, 22):
            with self.assertRaises(ValueError):
                HandLandmarks([(0.5, 0.5)] * count)

    def test_rejects_points_that_are_not_finite_numbers(self):
        for bad in (math.nan, math.inf, -math.inf, "0.5", None):
            points = [(0.5, 0.5)] * 20 + [(bad, 0.5)]
            with self.assertRaises(ValueError, msg=repr(bad)):
                HandLandmarks(points)

    def test_rejects_a_point_without_two_coordinates(self):
        with self.assertRaises(ValueError):
            HandLandmarks([(0.5, 0.5)] * 20 + [(0.5,)])

    def test_accepts_a_third_coordinate_and_ignores_it(self):
        with_depth = HandLandmarks([(x, y, 9.9) for x, y in place(OPEN_PALM)])
        self.assertEqual(with_depth.points, HandLandmarks(place(OPEN_PALM)).points)

    def test_confidence_must_be_between_0_and_1(self):
        for bad in (-0.1, 1.1, math.nan):
            with self.assertRaises(ValueError, msg=str(bad)):
                HandLandmarks([(0.5, 0.5)] * 21, confidence=bad)

    def test_points_are_immutable(self):
        h = hand(OPEN_PALM)
        with self.assertRaises(AttributeError):
            h.points = ()
        self.assertIsInstance(h.points, tuple)

    def test_repr_never_shows_the_points(self):
        """Un `log.debug(hand)` despistado no puede dejar la forma de la mano en el registro (docs/gestos.md)."""
        h = hand(OPEN_PALM)
        text = repr(h) + str(h)
        for x, y in h.points:
            self.assertNotIn(str(x), text)
            self.assertNotIn(str(y), text)


class FrameSourceTest(unittest.TestCase):
    def frames(self):
        return [Frame(0.0, None), Frame(0.1, hand(OPEN_PALM)), Frame(0.2, hand(THUMBS_UP)), Frame(0.3, None)]

    def test_the_interface_cannot_be_instantiated_without_implementing_it(self):
        with self.assertRaises(TypeError):
            FrameSource()

    def test_reads_in_order_and_ends_with_none(self):
        source = ListFrameSource(self.frames())
        source.open()
        self.assertEqual([source.read() for _ in range(4)], self.frames())
        self.assertIsNone(source.read())
        self.assertIsNone(source.read())  # al acabar, sigue acabado
        source.close()

    def test_cannot_read_while_closed(self):
        source = ListFrameSource(self.frames())
        with self.assertRaises(FrameSourceError):
            source.read()
        source.open()
        source.close()
        with self.assertRaises(FrameSourceError):
            source.read()

    def test_close_can_be_repeated(self):
        source = ListFrameSource(self.frames())
        source.open()
        source.close()
        source.close()
        self.assertFalse(source.is_open)

    def test_with_opens_and_always_closes(self):
        source = ListFrameSource(self.frames())
        with source as opened:
            self.assertIs(opened, source)
            self.assertTrue(source.is_open)
        self.assertFalse(source.is_open)
        with self.assertRaises(RuntimeError):
            with source:
                raise RuntimeError("fallo a mitad")
        self.assertFalse(source.is_open)

    def test_a_source_that_fails_to_open_is_closed_anyway(self):
        """Con una cámara real, un `open()` a medias podría dejarla encendida: se cierra siempre."""

        class Broken(FrameSource):
            closed = 0

            def open(self):
                raise FrameSourceError("no hay cámara")

            def read(self):
                return None

            def close(self):
                Broken.closed += 1

        with self.assertRaises(FrameSourceError):
            with Broken():
                self.fail("no debería entrar")
        self.assertEqual(Broken.closed, 1)

    def test_iter_readings_turns_frames_into_gestures(self):
        readings = list(iter_readings(ListFrameSource(self.frames())))
        self.assertEqual([t for t, _ in readings], [0.0, 0.1, 0.2, 0.3])
        self.assertEqual([r.gesture if r else None for _, r in readings],
                         [None, Gesture.PALM, Gesture.THUMB_UP, None])

    def test_iter_readings_closes_the_source_when_the_consumer_stops_early(self):
        source = ListFrameSource(self.frames())
        with closing(iter_readings(source)) as readings:
            next(readings)
            self.assertTrue(source.is_open)
        self.assertFalse(source.is_open)

    def test_iter_readings_closes_the_source_when_reading_fails(self):
        class Failing(ListFrameSource):
            def read(self):
                raise FrameSourceError("la cámara se ha desconectado")

        source = Failing([])
        with self.assertRaises(FrameSourceError):
            list(iter_readings(source))
        self.assertFalse(source.is_open)


class NoNewDependenciesTest(unittest.TestCase):
    def test_the_package_only_imports_the_standard_library_and_arthur(self):
        """Regla de CLAUDE.md: núcleo con SOLO biblioteca estándar. Aquí aún no se toca ni la cámara ni OpenCV."""
        package = Path(__file__).resolve().parents[1] / "arthur" / "interfaces" / "gestos"
        files = sorted(package.glob("*.py"))
        self.assertTrue(files)
        foreign = []
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for name in names:
                    top = name.split(".")[0]
                    if top not in sys.stdlib_module_names and top != "arthur":
                        foreign.append(f"{path.name}: {name}")
        self.assertEqual(foreign, [])


if __name__ == "__main__":
    unittest.main()
