"""Un solo punto de entrada para los dos formatos: JSON Schema y XSD.

El formato se reconoce por el primer carácter del esquema: un XSD es un documento XML y empieza por «<»;
un esquema JSON empieza por «{» (o es true o false). Con el esquema compilado, lo demás (qué intérprete
usar, qué generador de casos) se decide mirando el autómata.
"""
from .automata.compilar import Compilado, compilar
from .automata.interprete import Validador, validar
from .generador.documentos_xml import generar_casos_xml
from .generador.payloads import generar_casos as generar_casos_json
from .xsd.compilar import compilar_xsd
from .xsd.interprete import ValidadorXML, validar_xml

_BOM = b'\xef\xbb\xbf'


def es_xml(fuente: bytes) -> bool:
    """True si el texto (un esquema o un documento) es XML."""
    return fuente.removeprefix(_BOM).lstrip(b' \t\r\n').startswith(b'<')


def compilar_esquema(fuente: bytes) -> Compilado:
    """Compila un esquema JSON o un XSD. Lanza ErrorLexico, ErrorSintactico o ErrorDeEsquema."""
    return compilar_xsd(fuente) if es_xml(fuente) else compilar(fuente)


def validador_de(compilado: Compilado, **opciones):
    """Intérprete en streaming (feed/finish) para el autómata del esquema."""
    return (ValidadorXML if compilado.xml else Validador)(compilado.automata, **opciones)


def validar_documento(compilado: Compilado, datos: bytes, cortes=(), **opciones):
    return (validar_xml if compilado.xml else validar)(compilado.automata, datos, cortes, **opciones)


def generar_casos(compilado: Compilado, validos=5, invalidos=5, malformados=3, semilla=0):
    return (generar_casos_xml if compilado.xml else generar_casos_json)(compilado, validos, invalidos, malformados, semilla)
