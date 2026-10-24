# La revisión de la noche y de la semana

## Qué es

Cada noche, ARTHUR te dice **qué hiciste hoy y qué quedó sin hacer**. Cada domingo, en lugar de eso, te da
**el resumen de la semana**. Y lleva un pequeño juego (puntos, nivel, racha e insignias) que puedes ocultar.

Regla de oro: **los números los cuenta el programa, nunca la IA.** La IA (Workers AI; si falla, el Ollama de casa)
solo escribe dos o tres frases amables, sin cifras. Si no hay IA, sale una frase fija. Y si ARTHUR no sabe algo
(por ejemplo, un bloque de estudio que no has marcado), lo dice: nunca lo da por fallado.

## Qué verás y cuándo

| Cuándo | Dónde | Qué |
|---|---|---|
| Siempre | Panel, tarjeta **«Cómo va hoy»** (debajo de las tareas) | Tareas, estudio y Repaso de hoy, en vivo |
| Siempre | Panel, **Calendario** | En cada evento que empieza por «Estudio», tres botones: ✓ hecho · ½ a medias · ✗ no |
| 08:00 | (nada que ver) | ARTHUR hace una «foto» de lo que toca hoy |
| 21:30, de lunes a sábado | Aviso al móvil + tarjeta | «Tu día: 4 de 6 cosas hechas · Repaso ✓ · racha 5 días» y dos o tres frases |
| Domingo 21:30 | Aviso al móvil + tarjeta | «Tu semana: 71 % de tareas · 6 días activos · nivel 4» (en lugar de la de la noche) |
| Lunes por la mañana | Resumen de la mañana | Una línea al final: «La semana pasada: 22 de 31 tareas (71 %) · 6 días activos.» |
| 23:30 (portátil) | Obsidian, `~/Cerebro/Diario/Revision/` | La nota del día (`2026-10-24.md`) y, el domingo, la de la semana (`Semana-2026-W43.md`) |

Si el servidor estaba apagado a las 21:30, la revisión se hace al encenderlo (solo ese mismo día). Si después de
las 21:30 marcas algo, la tarjeta se actualiza; el día se cierra de verdad a medianoche.

## Cómo cuenta

- **Lo que tocaba hoy** es la foto de las 08:00: las tareas con fecha de hoy y los bloques de estudio
  del calendario. Lo que apuntas después cuenta como **extra**: se ve, hace el día activo, pero no baja el %.
  Las tareas sin fecha y las casillas de Obsidian no tienen día: si las haces, son extra.
- Una tarea atrasada sigue visible en el panel. Solo baja el porcentaje el día en que vencía; si se termina
  después, aparece como extra de ese día.
- **Estudio**: ✓ cuenta entero, ½ cuenta medio. Sin marcar es «sin marcar», no «fallado». Puedes marcar los de hoy
  y los de ayer (por si se te olvida antes de medianoche). Volver a pulsar el mismo botón quita la marca.
- **Repaso**: si hiciste la sesión, cuántas preguntas y cuántos aciertos (lo dice el cerebro).
- Nunca se mezclan en una «nota del día»: cada cosa tiene su número. Sin tareas, pone «Sin tareas», no 0 %.
- Lo que hicieron Claude o Codex **no cuenta como tuyo**: va en la nota «Hecho con IA».

## El juego

Es el de la propuesta de Codex, con tus condiciones: nada se resta nunca, no hay avisos de miedo y se oculta con
el botón **«Ocultar juego»** (las cuentas siguen; al enseñarlo otra vez no has perdido nada).

**Puntos** (máximo 90 al día):

| Qué | Puntos |
|---|---|
| Cada pregunta de la sesión del Repaso | 10, hasta 5 al día (50) |
| Cada tarea del plan o bloque de estudio ✓ | 10, hasta 3 al día (30) |
| Día completo: todo lo del plan hecho y la sesión del Repaso | +10 |

Los extra y los ½ no dan puntos (sí cuentan para el día activo). Los puntos de un día ya cerrado solo pueden subir.

**Nivel**: subir del nivel *n* al *n + 1* cuesta *n + 99* puntos (del 1 al 2, 100; del 2 al 3, 101…). No hay nivel
máximo y nunca baja. Cada diez niveles cambia el rango: «Explorador 1» (niveles 1-10), «Explorador 2» (11-20)…
El nivel mide práctica, no es una nota.

**Racha**: días activos seguidos. Un día es **activo** si hiciste al menos una cosa (una tarea, un bloque ✓ o ½,
alguna pregunta del Repaso).

- **Comodín**: 1 por semana. Si un día no haces nada, te lo guarda solo. Vuelve cada lunes y no se acumula.
- **Hoy descanso**: el botón de la tarjeta. Ese día la racha se congela: ni sube ni se rompe. Se puede quitar.
- **Sin datos no es fallo**: si el servidor estuvo apagado o hay bloques sin marcar y nada más, el día queda «por
  confirmar» y no rompe la racha.
- Si se rompe: «Puedes retomar hoy». Nada más. Los días activos acumulados no bajan nunca.

**Insignias**: 12 de las de Codex (primer paso, diez encuentros, un día redondo, retomar…). En la tarjeta,
«Insignias» enseña las que tienes y lo que falta para las demás. Una insignia conseguida no se quita nunca. La de
«Mirar la semana» se consigue pulsando **«Leído»** en un resumen semanal.

## Cambiar la hora o el nombre de los bloques

En `ajustes.toml` del panel (el mismo fichero de las horas del resumen):

```
[revision]
hora = "21:30"                # la revisión de la noche (el domingo, la de la semana)
foto = "08:00"                # la foto de lo que toca hoy (tiene que ser antes que la hora)
prefijo_estudio = "Estudio"   # los eventos del calendario que empiezan así son bloques de estudio
```

Si el fichero es de antes y no tiene `[revision]`, se usan estos valores. Después de cambiarlo:
`docker compose restart panel-web`.
Si ya existe una línea `foto = "06:00"` en ese fichero, hay que actualizarla también: los ajustes expresos
no se sustituyen automáticamente.

## Para comprobar que funciona (en el servidor)

Solo líneas de estado, sin el contenido de tus tareas:

```
docker compose logs --since 24h panel-web | grep -E 'Revisión (noche|semana)'
```

Debe salir una línea por noche, con el motor (`workers-ai`, `local` o `plantilla`) y si el aviso se entregó.
En el portátil, tras las 23:30: `journalctl --user -u arthur-cerebro-diario --since today | grep 'revisión'`.

## Lo que no hace (todavía)

- No compara con la semana anterior.
- No hay insignias por asignatura ni por pregunta (necesitarían el detalle de cada pregunta del Repaso).
- La pausa es solo para hoy (un día cada vez).
