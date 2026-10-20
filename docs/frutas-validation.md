# Validador editorial de Frutinovelas

Módulo aislado: `arthur.studio.frutas_validation.validar_guion(guion, catalogo)`.
Devuelve `Resultado(valido, motivos)` sin escribir archivos, llamar a servicios ni
modificar los argumentos. Se usa antes de montar o aprobar un Short; un resultado
inválido debe retener esa revisión y mostrar `motivos` a quien revisa.

## Contrato mínimo

`catalogo` es un mapa con `estado_inicial` (objeto narrativo → estado) y `planos`.
Cada plano declara `id`, `personaje`, `validado: true`, `gestos`,
`objetos_visibles` y `hechos_visibles`. Puede añadir `estado_visible` para
comprobar automáticamente que un objeto mostrado siga en las manos correctas.
El reparto admitido es Fina, Nico,
Vera, Mora, Berto y Kiro, en minúsculas.

`guion` contiene de ocho a doce `escenas`. Cada escena lleva `numero`
consecutivo, `personaje`, `plano`, `gesto`, `objetos`, `hechos`, `estado_antes`
y `cambios`. Las tres últimas colecciones pueden estar vacías. Un cambio
declara `objeto`, `desde`, `hasta` y `hecho`. El hecho debe aparecer en la
escena y en los hechos visibles del plano. El estado se arrastra entre escenas.

Ejemplo del cambio que devuelve una carpeta:

```python
{
    "numero": 2, "personaje": "fina", "plano": "fina-recibe-carpeta",
    "gesto": "camara", "objetos": ["carpeta"],
    "hechos": ["recibe_carpeta"], "estado_antes": {"carpeta": "vera"},
    "cambios": [{"objeto": "carpeta", "desde": "vera", "hasta": "fina",
                 "hecho": "recibe_carpeta"}],
}
```

## Límites e integración pendiente

El texto libre de la voz y los subtítulos no se interpreta. Quien prepare el
guion debe traducir toda afirmación visual relevante a `objetos` y `hechos`;
la revisión humana o visual debe comprobar que esa declaración es cierta.
Una foto fija con movimiento de cámara solo puede declarar gestos realmente
visibles. El validador no reconoce por sí mismo manos, accesorios ni defectos
de identidad en píxeles.

Un catálogo de otro reparto (otros personajes, sin `estado_inicial` ni `hechos_visibles`) no se debe pasar
directamente a este módulo: hace falta un catálogo compatible, más el adaptador del guion de Studio.
El montaje directo (`frutas-directo.md`, opción `--catalogo`) lo llama antes de usar ffmpeg y
saca la foto de cada escena del plano. No está conectado a autopilot, revisión ni publicación.

## Ejemplo local comprobado

`frutas-catalogo-ejemplo.json` reúne trece fotos de episodios de prueba, con los seis personajes del
reparto actual (las fotos no van en el repositorio). Las rutas son relativas a `raiz_recursos`
(`~/frutas-biblioteca` en el ejemplo; pon la tuya). Los gestos son
expresiones visibles de las fotos; no se declara movimiento corporal. Los
portapapeles muestran hojas blancas sin texto visible: eso no demuestra por
sí solo el estado legal de una inscripción. El estado inicial se sitúa tras el
episodio 6, cuando Fina recupera la carpeta y el formulario sigue sin firma.
Por ello `M_CARPETA`, usado en episodios anteriores, está en la biblioteca
pero no sirve en un guion posterior sin una transición visible y validada.

`frutas-guion-ejemplo.json` contiene ocho escenas nuevas apoyadas en esas
fotos y pasa `validar_guion`. Es una prueba del contrato, no un vídeo producido
ni una transcripción automática del episodio 8. No cambia objetos de manos:
el montaje de fotos no muestra una entrega. Para narrar una transición fuera
de campo hace falta definir una regla editorial y una revisión adicional;
el validador actual la retiene al no existir un hecho visible que la respalde.
