"""Del texto de un esquema XSD al autómata verificado y minimizado (Fase 4, XML).

    python -m compilador.xsd esquema.xsd                  # definición formal de M y tabla δ
    python -m compilador.xsd esquema.xsd --dot            # diagrama en Graphviz
    python -m compilador.xsd esquema.xsd documento.xml    # además valida el documento
"""
from ..automata.compilar import Compilado, ErrorDeEsquema, ensamblar
from .automata import construir_xml
from .modelo import analizar_xsd


def compilar_xsd(fuente: bytes) -> Compilado:
    """Lexer → parser → análisis semántico → construcción → verificación → minimización.
    Lanza ErrorLexico, ErrorSintactico o ErrorDeEsquema."""
    analisis = analizar_xsd(fuente)
    if not analisis.ok:
        raise ErrorDeEsquema(analisis)
    return ensamblar(analisis, construir_xml(analisis))
