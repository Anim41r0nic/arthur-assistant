"""Fuentes del panel (Obsidian, Google, Studio): cada una lanza SourceError cuando no puede dar datos."""


class SourceError(Exception):
    """Una fuente no está disponible o la petición no es válida; el mensaje se puede enseñar a Marc."""
