"""Tests de contactos, mensajes preparados y notificaciones. Navegador y D-Bus FALSOS."""

import sys
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from arthur.brain.base import BrainReply
from arthur.core.assistant import Assistant
from arthur.core.config import Config, ConfigError, NotificationsConfig, config_from_dict
from arthur.core.events import ALL, EventBus
from arthur.core.router import ToolRouter
from arthur.core.state import State, StateMachine
from arthur.integrations import notifications as notifications_module
from arthur.integrations.notifications import Notification, NotificationWatcher, classify, parse_monitor_output
from arthur.memory.store import MemoryStore, MemoryStoreError, normalize_phone
from arthur.security.audit import AuditLog
from arthur.security.policy import Policy
from arthur.tools.base import Risk, ToolCall
from arthur.tools.messaging import build_messaging_tools
from arthur.tools.notifications import build_notification_tools
from arthur.tools.registry import ToolRegistry

from .helpers import ScriptedConfirmer, make_router
from .test_desktop import FakePrograms


def router_with(tools, confirmer):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolRouter(registry, Policy(Config().security), confirmer, AuditLog(None), EventBus())


class ContactsAndComposeTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore.in_memory()
        self.addCleanup(self.store.close)
        self.programs = FakePrograms()
        self.confirmer = ScriptedConfirmer()
        self.router = router_with(build_messaging_tools(self.store, self.programs), self.confirmer)

    def call(self, tool, **args):
        return self.router.execute(ToolCall(tool, args))

    def opened_url(self):
        kind, argv = self.programs.calls[-1]
        self.assertEqual((kind, argv[0]), ("launch", "xdg-open"))
        return urllib.parse.urlsplit(argv[1]), urllib.parse.parse_qs(urllib.parse.urlsplit(argv[1]).query)

    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("612 34 56 78"), "34612345678")
        self.assertEqual(normalize_phone("+44 7700 900123"), "447700900123")
        self.assertEqual(normalize_phone("0034 612345678"), "34612345678")
        for bad in ("abc", "12", "6123456789012345678", "612-34; rm -rf"):
            with self.subTest(bad=bad), self.assertRaises(MemoryStoreError):
                normalize_phone(bad)

    def test_save_contact_needs_confirmation(self):
        self.assertEqual(self.call("save_contact", name="Mamá", phone="612345678").status, "cancelled")
        self.assertEqual(self.store.find_contacts(), [])
        self.confirmer.answers = [True]
        self.assertEqual(self.call("save_contact", name="Mamá", phone="612345678").status, "ok")
        self.assertIn("34612345678", self.call("list_contacts").output)
        self.assertEqual(self.call("save_contact", name="X", phone="no-es-un-numero").status, "denied")

    def test_compose_whatsapp_to_a_contact(self):
        self.store.add_contact("Mamá", phone="612345678")
        result = self.call("compose_whatsapp", to="mamá", text="Llego a las 9 & cenamos?")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.confirmer.requests, [])  # solo prepara: no hace falta confirmar
        url, query = self.opened_url()
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "web.whatsapp.com", "/send"))
        self.assertEqual(query, {"phone": ["34612345678"], "text": ["Llego a las 9 & cenamos?"]})
        self.assertIn("pulsa enviar", result.output)

    def test_compose_whatsapp_to_a_number(self):
        self.call("compose_whatsapp", to="+44 7700 900123", text="hi")
        self.assertEqual(self.opened_url()[1]["phone"], ["447700900123"])

    def test_compose_email(self):
        self.store.add_contact("Profe BD", email="profe@uni.es")
        result = self.call("compose_email", to="profe bd", subject="Duda práctica 2", body="Hola,\n¿la entrega es el lunes?")
        self.assertEqual(result.status, "ok")
        url, query = self.opened_url()
        self.assertEqual((url.netloc, url.path), ("mail.google.com", "/mail/"))
        self.assertEqual(query["to"], ["profe@uni.es"])
        self.assertEqual(query["su"], ["Duda práctica 2"])
        self.assertEqual(query["body"], ["Hola,\n¿la entrega es el lunes?"])
        self.assertEqual(query["view"], ["cm"])

    def test_unknown_contact_or_missing_data_opens_nothing(self):
        self.store.add_contact("Laura", phone="612345678")
        self.assertIn("No tengo a «Pedro»", self.call("compose_whatsapp", to="Pedro", text="hola").output)
        self.assertIn("No tengo el correo de Laura", self.call("compose_email", to="Laura", subject="a", body="b").output)
        self.assertEqual(self.programs.calls, [])


DBUS_OUTPUT = '''signal time=1.0 sender=org.freedesktop.DBus -> destination=:1.9 serial=2 path=/org/freedesktop/DBus; interface=org.freedesktop.DBus; member=NameAcquired
   string ":1.9"
method call time=2.0 sender=:1.80 -> destination=:1.14 serial=7 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "Firefox"
   uint32 0
   string ""
   string "Laura"
   string "¿Quedamos "mañana"?

web.whatsapp.com"
   array [
   ]
   array [
      dict entry(
         string "urgency"
         variant             byte 1
      )
   ]
   int32 -1
'''


class NotificationParsingTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_monitor_output(DBUS_OUTPUT.splitlines(True)),
                         [("Firefox", "Laura", '¿Quedamos "mañana"?\n\nweb.whatsapp.com')])

    def test_diagnostic_metadata_never_includes_title_or_text(self):
        from arthur.integrations.notifications import technical_lines
        for sample in (DBUS_OUTPUT, DBUS_OUTPUT.replace('"Laura"', '"Laura\nsegunda línea"')):
            joined = " ".join(technical_lines(sample.splitlines(True)))
            for private in ("Laura", "segunda", "Quedamos", "mañana", "whatsapp"):
                self.assertNotIn(private, joined)
            self.assertIn("urgency", joined)

    def test_classify(self):
        self.assertEqual(classify("Firefox", "Laura", "hola\n\nweb.whatsapp.com"), "whatsapp")
        self.assertEqual(classify("Firefox", "Nuevo correo", "mail.google.com"), "gmail")
        self.assertEqual(classify("Steam", "Descarga", "lista"), "otra")
        self.assertEqual(classify("Firefox", "Laura", "¿Cenamos?"), "web")  # Firefox real: sin pistas

    def test_watcher_with_fake_dbus_monitor(self):
        got = []
        code = f"import sys; sys.stdout.write({DBUS_OUTPUT!r}); sys.stdout.flush(); import time; time.sleep(5)"
        watcher = NotificationWatcher(got.append, command=[sys.executable, "-c", code])
        watcher.start()
        deadline = time.monotonic() + 3
        while not got and time.monotonic() < deadline:
            time.sleep(0.02)
        watcher.stop()
        self.assertEqual([(n.summary, n.source) for n in got], [("Laura", "whatsapp")])
        self.assertEqual(len(watcher.latest("whatsapp")), 1)

    def test_it_says_so_when_dbus_monitor_dies(self):
        """Si dbus-monitor termina, ya no llegan avisos: antes el hilo acababa sin decir nada y ARTHUR parecía
        seguir escuchando. Ahora queda un aviso en el registro (nunca con texto de mensajes)."""
        watcher = NotificationWatcher(lambda n: None, command=[sys.executable, "-c", "pass"])
        with self.assertLogs("arthur.integrations.notifications", "WARNING") as logs:
            watcher.start()
            deadline = time.monotonic() + 3
            while not logs.records and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertIn("dbus-monitor ha terminado", logs.output[0])
        watcher.stop()

    def test_stopping_on_purpose_is_not_reported_as_a_failure(self):
        watcher = NotificationWatcher(lambda n: None, command=[sys.executable, "-c", "import time; time.sleep(5)"])
        watcher.start()
        with self.assertNoLogs("arthur.integrations.notifications", "WARNING"):
            watcher.stop()
            time.sleep(0.3)

    def test_a_notification_that_breaks_the_parser_does_not_stop_the_listening(self):
        got = []
        code = (f"import sys, time; sys.stdout.write({DBUS_OUTPUT!r} * 2); sys.stdout.flush(); time.sleep(5)")
        watcher = NotificationWatcher(got.append, command=[sys.executable, "-c", code])
        real, calls = notifications_module.classify, []

        def flaky(app, summary, body):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("notificación rara")
            return real(app, summary, body)

        with mock.patch.object(notifications_module, "classify", flaky), self.assertLogs(
                "arthur.integrations.notifications", "ERROR"):
            watcher.start()
            deadline = time.monotonic() + 3
            while not got and time.monotonic() < deadline:
                time.sleep(0.02)
            watcher.stop()
        self.assertEqual([n.summary for n in got], ["Laura"])  # la segunda sí llegó

    def test_own_notifications_are_ignored(self):
        got = []
        watcher = NotificationWatcher(got.append)
        watcher._handle(DBUS_OUTPUT.replace('"Firefox"', '"ARTHUR"').splitlines(True))
        self.assertEqual(got, [])


class RecentMessagesToolTest(unittest.TestCase):
    def setUp(self):
        self.watcher = NotificationWatcher(lambda n: None)
        self.watcher.recent.append(Notification("Firefox", "Laura", "¿Cenamos?\n\nweb.whatsapp.com", "whatsapp"))
        self.watcher.recent.append(Notification("Firefox", "Banco", "Tu código es 1234", "otra"))
        self.confirmer = ScriptedConfirmer()
        self.router = router_with(build_notification_tools(self.watcher), self.confirmer)

    def call(self, **args):
        return self.router.execute(ToolCall("recent_messages", args))

    def test_who_wrote_without_text(self):
        result = self.call(source="whatsapp")
        self.assertEqual(result.status, "ok")
        self.assertIn("Laura (WhatsApp, ahora mismo)", result.output)
        self.assertNotIn("Cenamos", result.output)
        self.assertNotIn("Banco", result.output)
        self.assertEqual(self.confirmer.requests, [])

    def test_text_is_private_and_needs_confirmation(self):
        self.assertEqual(self.call(include_text=True).status, "cancelled")
        self.assertEqual(self.confirmer.requests[0].risk, Risk.PRIVATE)
        self.confirmer.answers = [True]
        output = self.call(include_text=True).output
        self.assertIn("Laura (WhatsApp, ahora mismo): «¿Cenamos?»", output)

    def test_bad_source(self):
        self.assertEqual(self.call(source="telegram").status, "error")


class EchoBrain:
    name = label = "eco"

    def reply(self, conversation):
        return BrainReply("eco")


class RepeatedNotificationTest(unittest.TestCase):
    """Firefox a veces repite la misma notificación: se ignora si es idéntica y llega en menos de un minuto."""

    def test_identical_notification_within_a_minute_is_a_repeat(self):
        from arthur.app import is_repeat

        seen = {}
        key = ("whatsapp", "Laura", "hola")
        self.assertFalse(is_repeat(seen, key, now=1000.0))
        self.assertTrue(is_repeat(seen, key, now=1030.0))
        self.assertTrue(is_repeat(seen, key, now=1059.0))
        self.assertFalse(is_repeat(seen, key, now=1061.0))  # pasado el minuto del primero, vuelve a contar

    def test_different_notifications_are_not_repeats(self):
        from arthur.app import is_repeat

        seen = {}
        self.assertFalse(is_repeat(seen, ("whatsapp", "Laura", "hola"), now=1000.0))
        self.assertFalse(is_repeat(seen, ("whatsapp", "Laura", "adiós"), now=1001.0))
        self.assertFalse(is_repeat(seen, ("whatsapp", "Ana", "hola"), now=1002.0))

    def test_old_ones_are_forgotten(self):
        """`seen` guarda el texto de los mensajes: si no se limpiara, crecería con cada mensaje distinto y
        los mantendría en RAM todo el tiempo que ARTHUR esté en marcha (el README promete solo las últimas 50)."""
        from arthur.app import is_repeat

        seen = {}
        for n in range(500):
            is_repeat(seen, ("whatsapp", "Laura", f"mensaje {n}"), now=1000.0 + n)
        self.assertLessEqual(len(seen), 60)  # solo lo del último minuto
        is_repeat(seen, ("whatsapp", "Laura", "otro"), now=9999.0)
        self.assertEqual(list(seen), [("whatsapp", "Laura", "otro")])


class AnnounceTest(unittest.TestCase):
    def make(self, **notif_cfg):
        from arthur.app import attach_announcements

        bus = EventBus()
        events = []
        bus.subscribe(ALL, events.append)
        assistant = Assistant(EchoBrain(), make_router([]), bus, StateMachine(bus), 20)
        watcher = NotificationWatcher(lambda n: None)
        config = Config(notifications=NotificationsConfig(**notif_cfg))
        attach_announcements(watcher, bus, assistant, config, gather=0.1)
        return watcher, events, assistant

    def wait_messages(self, events, n):
        deadline = time.monotonic() + 2
        while sum(e.type == "assistant_message" for e in events) < n and time.monotonic() < deadline:
            time.sleep(0.02)
        return [e.data["text"] for e in events if e.type == "assistant_message"]

    def test_announces_who_wrote_once_per_minute(self):
        watcher, events, assistant = self.make()
        note = Notification("Firefox", "Laura", "secreto", "whatsapp")
        watcher.on_notification(note)
        watcher.on_notification(note)  # repetido: no se vuelve a avisar
        texts = self.wait_messages(events, 1)
        time.sleep(0.2)
        self.assertEqual(texts, ["Te ha escrito Laura por WhatsApp."])
        self.assertNotIn("secreto", texts[0])
        self.assertEqual(assistant.state, State.IDLE)

    def test_firefox_notifications_are_announced_without_guessing_the_app(self):
        watcher, events, _ = self.make()
        watcher.on_notification(Notification("Firefox", "Laura", "secreto", "web"))
        texts = self.wait_messages(events, 1)
        self.assertEqual(texts, ["Tienes un mensaje de Laura."])

    def test_other_sources_are_not_announced(self):
        watcher, events, _ = self.make()
        watcher.on_notification(Notification("Steam", "Oferta", "x", "otra"))
        time.sleep(0.2)
        self.assertEqual([e.type for e in events], ["message_received"])

    def test_busy_assistant_is_not_interrupted(self):
        watcher, events, assistant = self.make()
        assistant._lock.acquire()
        try:
            self.assertFalse(assistant.announce("Te ha escrito X"))
        finally:
            assistant._lock.release()

    def test_several_messages_are_said_together_with_their_text(self):
        watcher, events, _ = self.make(announce_text=True)
        watcher.on_notification(Notification("Firefox", "Ana", "Hola\n\nweb.whatsapp.com", "web"))
        watcher.on_notification(Notification("Firefox", "Ana", "¿Vienes luego?", "web"))
        watcher.on_notification(Notification("Firefox", "Luis", "Ok", "web"))
        texts = self.wait_messages(events, 1)
        self.assertEqual(texts, ["Mensaje de Ana: Hola. ¿Vienes luego?. Mensaje de Luis: Ok."])

    def test_counts_messages_without_reading_them(self):
        watcher, events, _ = self.make()
        watcher.on_notification(Notification("Firefox", "Ana", "uno", "web"))
        watcher.on_notification(Notification("Firefox", "Ana", "dos", "web"))
        self.assertEqual(self.wait_messages(events, 1), ["Tienes 2 mensajes de Ana."])

    def test_messages_wait_while_arthur_is_busy(self):
        watcher, events, assistant = self.make()
        assistant._lock.acquire()  # ARTHUR ocupado (p. ej. escuchándote)
        watcher.on_notification(Notification("Firefox", "Laura", "x", "whatsapp"))
        time.sleep(0.4)
        self.assertEqual(self.wait_messages(events, 0), [])
        assistant._lock.release()
        deadline = time.monotonic() + 3
        while not self.wait_messages(events, 1) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.wait_messages(events, 1), ["Te ha escrito Laura por WhatsApp."])

    def test_long_text_is_cut(self):
        from arthur.app import MAX_SPOKEN_TEXT, announcement

        text = announcement([Notification("Firefox", "Ana", "palabra " * 100, "web")], read_text=True)
        self.assertLess(len(text), MAX_SPOKEN_TEXT + 30)
        self.assertTrue(text.endswith("…."))

    def test_config(self):
        self.assertEqual(config_from_dict({}).notifications.announce, ("whatsapp", "web"))
        with self.assertRaises(ConfigError):
            config_from_dict({"notifications": {"announce": ["telegram"]}})


if __name__ == "__main__":
    unittest.main()
