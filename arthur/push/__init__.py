"""Avisos al móvil (fase S6, docs/servidor.md): Web Push, el estándar de los navegadores.

  subscription.py  comprobar una suscripción (solo biblioteca estándar: lo usan el core y arthur-push)
  webpush.py       cifrar el aviso (RFC 8291) y firmarlo (VAPID, RFC 8292): necesita `cryptography`,
                   por eso solo lo carga el contenedor arthur-push, nunca el core
  push_server.py   arthur-push, el contenedor que los envía (Dockerfile.push)
"""
