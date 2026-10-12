"""Matemáticas del detector de manos real, SIN cámara y sin los modelos (se prueba con números sintéticos).

Se salta solo si no están numpy y cv2 (python-opencv): en ese caso el detector no se puede usar.
"""

import math
import unittest

try:
    import cv2
    import numpy as np

    from arthur.interfaces.camara import detector
except ImportError:  # pragma: no cover
    detector = None


@unittest.skipIf(detector is None, "faltan numpy o cv2")
class AnchorsTest(unittest.TestCase):
    def test_grid_layout(self):
        a = detector.ANCHORS
        self.assertEqual(a.shape, (2016, 2))
        np.testing.assert_allclose(a[0], [0.5 / 24, 0.5 / 24])
        np.testing.assert_allclose(a[1], a[0])                 # 2 por celda en la cuadrícula de 24
        np.testing.assert_allclose(a[2], [1.5 / 24, 0.5 / 24])
        np.testing.assert_allclose(a[1151], [23.5 / 24, 23.5 / 24])
        np.testing.assert_allclose(a[1152], [0.5 / 12, 0.5 / 12])
        np.testing.assert_allclose(a[1152 + 5], a[1152])       # 6 por celda en la de 12
        np.testing.assert_allclose(a[1152 + 6], [1.5 / 12, 0.5 / 12])
        np.testing.assert_allclose(a[2015], [11.5 / 12, 11.5 / 12])


@unittest.skipIf(detector is None, "faltan numpy o cv2")
class DecodePalmTest(unittest.TestCase):
    def raw(self, index, logit=4.0, delta=(0, 0, 19.2, 19.2)):
        regression = np.zeros((1, 2016, 18), dtype=np.float32)
        scores = np.full((1, 2016, 1), -10.0, dtype=np.float32)
        regression[0, index, :4] = delta
        scores[0, index, 0] = logit
        return regression, scores

    def test_best_anchor_wins_and_letterbox_is_undone(self):
        regression, scores = self.raw(0)  # ancla 0 = (0,5/24 , 0,5/24) del cuadrado
        palm = detector.decode_palm(regression, scores, (640, 480))
        side, pad_y = 640, 80  # 640x480: el cuadrado es de 640 con 80 px de banda arriba y abajo
        self.assertAlmostEqual(palm.center[0], 0.5 / 24 * side, places=3)
        self.assertAlmostEqual(palm.center[1], 0.5 / 24 * side - pad_y, places=3)
        self.assertAlmostEqual(palm.size[0], 0.1 * side, places=3)
        self.assertGreater(palm.score, 0.98)

    def test_weak_scores_find_nothing(self):
        regression, scores = self.raw(10, logit=-3.0)  # sigmoide ≈ 0,05
        self.assertIsNone(detector.decode_palm(regression, scores, (640, 480)))


@unittest.skipIf(detector is None, "faltan numpy o cv2")
class HandRoiTest(unittest.TestCase):
    def palm(self, degrees, wrist=(300.0, 300.0), length=60.0):
        r = math.radians(degrees)
        direction = np.array([math.sin(r), -math.cos(r)])  # hacia dónde apuntan los dedos
        points = np.zeros((7, 2))
        points[0] = wrist
        points[2] = np.array(wrist) + length * direction
        center = np.array(wrist) + 0.5 * length * direction
        return detector.Palm(tuple(center), (length, length), points, 0.9)

    def test_the_hand_ends_up_vertical_whatever_its_tilt(self):
        for degrees in (-150, -90, -45, -10, 0, 10, 45, 90, 135, 170):
            palm = self.palm(degrees)
            matrix = detector.hand_roi_matrix(palm)
            inverse = cv2.invertAffineTransform(matrix)  # imagen → recorte
            wrist, middle = detector.map_points(inverse, palm.points[[0, 2]])
            self.assertAlmostEqual(wrist[0], middle[0], delta=0.5, msg=f"{degrees}°")  # misma x: vertical
            self.assertLess(middle[1], wrist[1] - 5, msg=f"{degrees}°")                 # los dedos, arriba
            self.assertTrue(0 < wrist[0] < 224 and 0 < wrist[1] < 224, msg=f"{degrees}°")  # la muñeca cabe

    def test_crop_is_bigger_than_the_palm_and_roundtrips(self):
        palm = self.palm(20)
        matrix = detector.hand_roi_matrix(palm)
        inverse = cv2.invertAffineTransform(matrix)
        points = np.array([[10.0, 20.0], [200.0, 150.0]])
        np.testing.assert_allclose(detector.map_points(inverse, detector.map_points(matrix, points)), points, atol=1e-6)
        wrist, middle = detector.map_points(inverse, palm.points[[0, 2]])
        self.assertAlmostEqual(abs(wrist[1] - middle[1]), 60 / (60 * detector.ROI_SCALE / 224), delta=0.5)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(detector is None, "faltan numpy o cv2")
class TrackingRoiTest(unittest.TestCase):
    def hand_points(self, degrees, center=(320.0, 240.0), size=150.0):
        """21 puntos de una mano sintética: muñeca abajo, base del corazón (punto 9) arriba, girada y colocada."""
        local = np.zeros((21, 2))
        local[0] = (0, size * 0.5)                       # muñeca
        local[9] = (0, 0)                                # base del corazón
        for i in range(21):
            if i not in (0, 9):
                local[i] = ((i % 5 - 2) * size * 0.12, -(i / 21) * size * 0.9)  # dedos hacia arriba
        r = math.radians(degrees)
        rot = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
        return local @ rot.T + np.array(center)

    def test_tracking_crop_is_upright_and_contains_the_whole_hand(self):
        for degrees in (-120, -45, 0, 30, 90, 160):
            points = self.hand_points(degrees)
            inverse = cv2.invertAffineTransform(detector.tracking_roi_matrix(points))
            crop = detector.map_points(inverse, points)
            self.assertAlmostEqual(crop[0][0], crop[9][0], delta=0.5, msg=f"{degrees}°")  # vertical
            self.assertLess(crop[9][1], crop[0][1] - 5, msg=f"{degrees}°")
            self.assertGreater(crop.min(), 5, msg=f"{degrees}°")      # la mano cabe entera en 224x224...
            self.assertLess(crop.max(), 219, msg=f"{degrees}°")
            self.assertGreater(crop[:, 1].max() - crop[:, 1].min(), 60, msg=f"{degrees}°")  # ...y la llena un poco


@unittest.skipIf(detector is None, "faltan numpy o cv2")
class CameraSourceTest(unittest.TestCase):
    """La cámara y el detector son FALSOS: la cámara real solo se enciende con un script de prueba aparte."""

    class FakeCapture:
        def __init__(self, frames=3, opened=True, still=False):
            self.frames, self.opened, self.released, self.still, self.flip = frames, opened, 0, still, False

        def isOpened(self):
            return self.opened

        def read(self):
            if self.frames <= 0:
                return False, None
            self.frames -= 1
            if self.still:  # habitación quieta: siempre la misma imagen
                return True, np.zeros((480, 640, 3), dtype=np.uint8)
            self.flip = not self.flip  # algo se mueve: la imagen cambia entera en cada fotograma
            return True, np.full((480, 640, 3), 255 if self.flip else 0, dtype=np.uint8)

        def release(self):
            self.released += 1

    class FakeDetector:
        def __init__(self, answers):
            self.answers = list(answers)

        def detect(self, image):
            return self.answers.pop(0)

    def make(self, answers, **kw):
        from arthur.interfaces.camara import camera
        self.now, self.slept = 0.0, []
        self.capture = self.FakeCapture(**kw)

        def sleep(seconds):
            self.slept.append(seconds)
            self.now += seconds

        return camera.CameraFrameSource(self.FakeDetector(answers), capture_factory=lambda: self.capture,
                                        clock=lambda: self.now, sleep=sleep)

    def hand(self):
        from arthur.interfaces.gestos import HandLandmarks
        return HandLandmarks(tuple((float(i), float(i)) for i in range(21)))

    def test_frames_then_none_and_the_camera_is_released(self):
        source = self.make([None, self.hand(), None])
        with source:
            frames = [source.read() for _ in range(4)]
        self.assertIsNone(frames[3])
        self.assertIsNone(frames[0].hand)
        self.assertIsNotNone(frames[1].hand)
        self.assertEqual(self.capture.released, 1)  # al salir del with, la cámara se apaga

    def test_idle_is_slow_and_a_hand_speeds_it_up(self):
        from arthur.interfaces.camara import camera
        source = self.make([None, None, self.hand(), None, None], frames=5)
        with source:
            for _ in range(5):
                source.read()
        slow, fast = 1 / camera.IDLE_FPS, 1 / camera.ACTIVE_FPS
        self.assertAlmostEqual(self.slept[-1], fast, places=3)  # tras ver la mano, ritmo alto
        self.assertAlmostEqual(self.slept[0], slow, places=3)

    def test_failing_to_open_leaves_nothing_on(self):
        from arthur.interfaces.gestos import FrameSourceError
        source = self.make([], opened=False)
        with self.assertRaises(FrameSourceError):
            with source:
                pass
        self.assertGreaterEqual(self.capture.released, 1)

    def test_reading_without_opening_is_an_error(self):
        from arthur.interfaces.gestos import FrameSourceError
        with self.assertRaises(FrameSourceError):
            self.make([]).read()

    def test_a_still_room_never_reaches_the_detector(self):
        source = self.make([None] * 10, frames=6, still=True)
        with source:
            frames = [source.read() for _ in range(6)]
        # solo el primer fotograma (no hay con qué compararlo) se analiza; el resto, quieto y sin mano, se salta
        self.assertEqual((source.analyzed, source.skipped), (1, 5))
        self.assertTrue(all(f.hand is None for f in frames[1:]))

    def test_motion_wakes_the_detector_and_a_seen_hand_keeps_it_awake(self):
        source = self.make([self.hand()] * 10, frames=4, still=False)
        with source:
            for _ in range(4):
                source.read()
        self.assertEqual((source.analyzed, source.skipped), (4, 0))

    def test_a_seen_hand_keeps_the_detector_awake_even_in_a_still_room(self):
        source = self.make([self.hand()] * 10, frames=4, still=True)
        with source:
            frames = [source.read() for _ in range(4)]
        self.assertEqual(source.skipped, 0)  # la mano sigue ahí aunque la imagen no cambie: hay que seguir mirándola
        self.assertTrue(all(f.hand is not None for f in frames))
