"""Creator Studio, paso 2: cliente de YouTube (solo lectura) e investigador de nichos.

YouTube FALSO (un «opener» que contesta con datos fijos) y reloj falso: nada sale a internet ni gasta cuota.
"""

import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from arthur.studio import research, youtube
from arthur.studio.jobs import JobRunner
from arthur.studio.store import StudioStore

KEY = "AIza" + "x" * 35
NOW = datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeYouTube:
    """Dos nichos: «gatos» (canales pequeños con vídeos que revientan) y «coches» (canales grandes, lo normal)."""

    def __init__(self):
        self.requests = []
        self.fail = None  # (código, cuerpo) para simular errores de Google
        self.videos = {}
        self.channels = {}
        self.uploads = {}
        self.search = {}
        for n in range(10):
            # gatos: canal pequeño (5.000 subs), vídeo con 50.000 visitas; lo normal del canal, 2.000
            self._add("gatos", f"gat{n}", f"UCg{n}", subs=5_000, views=50_000, normal=2_000, seconds=600)
            # coches: canal grande (2 M subs), vídeo con 300.000 visitas; lo normal, 250.000
            self._add("coches", f"coc{n}", f"UCc{n}", subs=2_000_000, views=300_000, normal=250_000, seconds=40)
        # Un vídeo brasileño que se cuela en «gatos» y un canal diminuto (lo normal: 40 visitas).
        self._add("gatos", "gatpt", "UCpt", subs=900_000, views=4_000_000, normal=500_000, seconds=500, language="pt-BR")
        self._add("gatos", "gatmini", "UCmini", subs=3_000, views=600_000, normal=40, seconds=700)
        self._add("gatos", "gatzxx", "UCzxx", subs=50_000, views=80_000, normal=20_000, seconds=300, language="zxx")

    def _add(self, query, vid, cid, subs, views, normal, seconds, language=None):
        self.search.setdefault(query, []).append(vid)
        self.videos[vid] = {"id": vid, "snippet": {"title": f"Vídeo {vid} | [raro]", "channelId": cid,
                                                     "channelTitle": f"Canal {cid}", "publishedAt": iso(10),
                                                     **({"defaultAudioLanguage": language} if language else {})},
                            "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"},
                            "contentDetails": {"duration": f"PT{seconds // 60}M{seconds % 60}S"}}
        self.channels[cid] = {"id": cid, "snippet": {"title": f"Canal {cid}"},
                              "statistics": {"subscriberCount": str(subs), "videoCount": "50", "viewCount": "1"},
                              "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}
        old = [f"{vid}o{i}" for i in range(5)]
        self.uploads["UU" + cid[2:]] = [vid, *old]
        for i, oid in enumerate(old):
            self.videos[oid] = {"id": oid, "snippet": {"title": "viejo", "channelId": cid, "channelTitle": "",
                                                         "publishedAt": iso(30 + i)},
                                "statistics": {"viewCount": str(normal)}, "contentDetails": {"duration": "PT5M"}}

    def open(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        params = dict(urllib.parse.parse_qsl(url.query))
        endpoint = url.path.rsplit("/", 1)[-1]
        self.requests.append((endpoint, params, dict(request.header_items()), request.full_url))
        if self.fail:
            code, body = self.fail
            raise urllib.error.HTTPError(request.full_url, code, "error", {}, io.BytesIO(body.encode()))
        if endpoint == "search":
            limits = {"short": (0, 239), "medium": (240, 1200), "long": (1201, 10**6)}
            low, high = limits.get(params.get("videoDuration"), (0, 10**6))
            items = [{"id": {"videoId": v}} for v in self.search.get(params["q"], [])
                     if low <= youtube.parse_duration(self.videos[v]["contentDetails"]["duration"]) <= high]
            body = {"items": items, "pageInfo": {"totalResults": 1_000_000}}
        elif endpoint == "videos":
            body = {"items": [self.videos[i] for i in params["id"].split(",") if i in self.videos]}
        elif endpoint == "channels":
            body = {"items": [self.channels[i] for i in params["id"].split(",") if i in self.channels]}
        elif endpoint == "playlistItems":
            body = {"items": [{"contentDetails": {"videoId": v}} for v in self.uploads.get(params["playlistId"], [])]}
        else:
            raise AssertionError(endpoint)
        return FakeResponse(json.dumps(body).encode())


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class KeyTest(unittest.TestCase):
    def test_key_file_is_private_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio" / "youtube-key"
            youtube.save_key(f"  {KEY}\n", path)
            self.assertEqual(youtube.load_key(path), KEY)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            with self.assertRaises(ValueError):
                youtube.save_key("no es una clave", path)
            self.assertEqual(youtube.load_key(path), KEY)  # la buena sigue ahí
            self.assertIsNone(youtube.load_key(Path(tmp) / "no-existe"))

    def test_quota_day_follows_california(self):
        # Antes de las 8:00 UTC (9:00 en España en invierno) aún es el día anterior en California.
        self.assertEqual(youtube.quota_day(datetime(2026, 10, 12, 7, 59, tzinfo=timezone.utc)), "2026-10-11")
        self.assertEqual(youtube.quota_day(datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)), "2026-10-12")

    def test_parse_duration(self):
        self.assertEqual(youtube.parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(youtube.parse_duration("PT45S"), 45)
        self.assertEqual(youtube.parse_duration("P1DT1S"), 86401)
        self.assertIsNone(youtube.parse_duration("P"))
        self.assertIsNone(youtube.parse_duration("raro"))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.store = StudioStore.in_memory()
        self.fake = FakeYouTube()
        self.clock = Clock()
        self.client = youtube.YouTubeClient(KEY, self.store, opener=self.fake, now=self.clock)

    def tearDown(self):
        self.store.close()

    def test_key_goes_in_header_never_in_url(self):
        self.client.videos(["gat0"])
        endpoint, params, headers, url = self.fake.requests[0]
        self.assertEqual(headers.get("X-goog-api-key"), KEY)
        self.assertNotIn(KEY, url)
        self.assertNotIn("key", params)

    def test_cache_avoids_paying_twice(self):
        self.client.search_videos("gatos", NOW - timedelta(days=90))
        self.client.search_videos("gatos", NOW - timedelta(days=90))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.client.used_today(), 100)
        # Pasadas 12 horas se vuelve a preguntar (y a pagar).
        self.clock.now = NOW + timedelta(hours=13)
        self.client.search_videos("gatos", NOW - timedelta(days=90))
        self.assertEqual(len(self.fake.requests), 2)

    def test_stops_before_going_over_budget(self):
        client = youtube.YouTubeClient(KEY, self.store, opener=self.fake, now=self.clock, budget=150)
        client.search_videos("gatos", NOW)
        with self.assertRaises(youtube.QuotaExceeded):
            client.search_videos("coches", NOW)
        self.assertEqual(len(self.fake.requests), 1)  # la segunda ni se envió
        client.videos(["gat0"])  # lo barato aún cabe
        self.assertEqual(client.used_today(), 101)

    def test_google_errors_are_explained(self):
        cases = [
            (403, '{"error":{"errors":[{"reason":"quotaExceeded"}]}}', youtube.QuotaExceeded),
            (400, '{"error":{"errors":[{"reason":"keyInvalid"}]}}', youtube.YouTubeRefused),
            (403, '{"error":{"errors":[{"reason":"accessNotConfigured"}]}}', youtube.YouTubeRefused),
        ]
        for n, (code, body, kind) in enumerate(cases):
            self.fake.fail = (code, body)
            with self.assertRaises(kind):
                self.client.videos([f"gat{n}"])
        self.fake.fail = (503, "ocupado")
        with self.assertRaises(youtube.YouTubeError) as caught:
            self.client.videos(["gat9"])
        self.assertNotIsInstance(caught.exception, youtube.YouTubeRefused)  # pasajero: se reintenta
        self.assertNotIn(KEY, str(caught.exception))

    def test_failed_requests_still_count(self):
        self.fake.fail = (503, "ocupado")
        with self.assertRaises(youtube.YouTubeError):
            self.client.search_videos("gatos", NOW)
        self.assertEqual(self.client.used_today(), 100)

    def test_rejects_bad_key_and_ids(self):
        with self.assertRaises(youtube.YouTubeRefused):
            youtube.YouTubeClient("corta", self.store)
        self.assertEqual(self.client.videos(["../../etc", "a b"]), [])
        self.assertEqual(self.fake.requests, [])


class ResearchTest(unittest.TestCase):
    def setUp(self):
        self.store = StudioStore.in_memory()
        self.fake = FakeYouTube()
        self.clock = Clock()
        self.client = youtube.YouTubeClient(KEY, self.store, opener=self.fake, now=self.clock)
        steps = research.research_steps(lambda: self.client, now=self.clock)
        self.runner = JobRunner(self.store, {research.KIND: steps}, now=lambda: self.clock.now.replace(tzinfo=None))

    def tearDown(self):
        self.store.close()

    def run_job(self, nichos=("gatos", "coches")):
        job = self.runner.submit(research.KIND, research.validate_params({"nichos": list(nichos)}))
        self.runner.run_once()
        return self.store.get_job(job.id)

    def test_full_report(self):
        job = self.run_job()
        self.assertEqual(job.status, "done", job.error)
        result = next(s.output for s in self.store.steps(job.id) if s.name == "informe")
        gatos, coches = result["nichos"]
        # Cada formato con su búsqueda: los gatos son vídeos largos y los coches, cortos.
        largos, cortos = gatos["formatos"]["largos"], coches["formatos"]["cortos"]
        self.assertEqual(len(largos["videos"]), 12)  # 10 + diminuto + «sin idioma»; el brasileño va aparte
        self.assertIsNone(gatos["formatos"]["cortos"]["puntuacion"]["valor"])
        self.assertIsNone(coches["formatos"]["largos"]["puntuacion"]["valor"])
        self.assertEqual(largos["videos"][0]["veces_lo_normal"], 25.0)
        self.assertEqual(largos["metricas"]["parte_canales_pequenos"]["valor"], 1.0)
        self.assertEqual(cortos["metricas"]["parte_destacados"]["valor"], 0.0)
        self.assertEqual(largos["puntuacion"]["tipo"], "hipótesis")
        md = result["markdown"]
        for text in ("observado", "estimado", "hipótesis", "Lo que no sabemos", "https://youtu.be/gat0", "×25.0",
                     "Comparación: vídeos largos (4-20 min)", "Comparación: vídeos cortos (menos de 4 min)"):
            self.assertIn(text, md)
        self.assertNotIn("| [raro]", md)  # los títulos no rompen la tabla
        self.assertLessEqual(self.client.used_today(), research.estimated_cost(2))

    def test_other_languages_are_kept_apart_not_dropped(self):
        job = self.run_job(["gatos"])
        result = next(s.output for s in self.store.steps(job.id) if s.name == "informe")
        others = result["nichos"][0]["otros_idiomas"]
        self.assertEqual([(v["id"], v["idioma"]) for v in others], [("gatpt", "pt-br")])
        self.assertIn("En otros idiomas", result["markdown"])
        self.assertIn("https://youtu.be/gatpt", result["markdown"])

    def test_tiny_channels_get_no_absurd_ratio(self):
        job = self.run_job(["gatos"])
        result = next(s.output for s in self.store.steps(job.id) if s.name == "informe")
        mini = next(v for v in result["nichos"][0]["formatos"]["largos"]["videos"] if v["id"] == "gatmini")
        self.assertIsNone(mini["veces_lo_normal"])  # 600.000 / 40 = ×15.000 no significa nada
        self.assertEqual(mini["lo_normal_del_canal"], 40)

    def test_baseline_is_the_median_of_older_uploads(self):
        job = self.run_job(["gatos"])
        medians = next(s.output for s in self.store.steps(job.id) if s.name == "referencias")["medianas"]
        # 5 vídeos viejos de 2.000 y el destacado de 50.000: la mediana no se deja arrastrar por él.
        self.assertEqual(medians["UCg0"], {"mediana": 2_000, "muestra": 6})

    def test_repeating_uses_cache(self):
        self.run_job()
        spent = self.client.used_today()
        self.run_job()
        self.assertEqual(self.client.used_today(), spent)

    def test_bad_key_fails_without_retrying(self):
        self.fake.fail = (400, '{"error":{"errors":[{"reason":"keyInvalid"}]}}')
        job = self.run_job()
        self.assertEqual(job.status, "failed")
        self.assertIn("clave", job.error)
        self.assertEqual(len(self.fake.requests), 1)

    def test_temporary_error_is_retried_later(self):
        self.fake.fail = (503, "ocupado")
        job = self.run_job()
        self.assertEqual(job.status, "queued")
        self.fake.fail = None
        self.clock.now = NOW + timedelta(minutes=5)
        self.runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "done")

    def test_validate_params(self):
        ok = research.validate_params({"nichos": ["gatos", {"nombre": "Coches", "busqueda": "coches eléctricos"}]})
        self.assertEqual(ok["nichos"][1], {"nombre": "Coches", "busqueda": "coches eléctricos"})
        self.assertEqual((ok["dias"], ok["region"], ok["idioma"]), (90, "ES", "es"))
        for bad in ({"nichos": []}, {"nichos": ["a"] * 6}, {"nichos": ["a", "A"]}, {"nichos": ["a"], "dias": 3},
                    {"nichos": ["a"], "region": "españa"}, {"nichos": [""]}, {"nichos": ["a"], "dias": True}):
            with self.assertRaises(ValueError, msg=bad):
                research.validate_params(bad)

    def test_too_few_videos_gives_no_score(self):
        searches = {"largos": {"ids": ["a"]}, "cortos": {"ids": []}}
        result = research.analyze([{"nombre": "x", "busqueda": "x", "formatos": searches}],
                                  [{"id": "a", "title": "t", "views": 5, "published_at": iso(3)}], [], {}, NOW)
        self.assertIsNone(result["nichos"][0]["formatos"]["largos"]["puntuacion"]["valor"])
        self.assertIn("sin datos", research.render_report(result))


class CommandTest(unittest.TestCase):
    def test_investigar_without_key_explains_what_to_do(self):
        from arthur.__main__ import main

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["--studio-investigar", "gatos"])
            self.assertEqual(code, 1)
            self.assertIn("--studio-clave-youtube", err.getvalue())

    def test_save_key_command(self):
        from arthur.__main__ import main

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}), \
                mock.patch("getpass.getpass", return_value=KEY), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--studio-clave-youtube"]), 0)
            self.assertNotIn(KEY, out.getvalue())
            self.assertEqual(youtube.load_key(Path(tmp) / "arthur" / "studio" / "youtube-key"), KEY)


if __name__ == "__main__":
    unittest.main()
