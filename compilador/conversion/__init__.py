"""Conversión de esquemas: JSON Schema ↔ XSD, e inferencia de un esquema a partir de documentos de ejemplo.

    inferir_json      documentos JSON → JSON Schema (con la biblioteca GenSON)
    inferir_xsd       documentos XML  → XSD
    xsd_a_json        XSD             → JSON Schema
    json_a_xsd        JSON Schema     → XSD
    documentos        un documento XML ↔ su equivalente en JSON, con la convención de convencion.py

Los esquemas que salen están dentro del subconjunto que acepta el compilador, así que se pueden compilar
y usar enseguida. Lo que el otro lenguaje no puede expresar no se calla: queda en los avisos de la
conversión, con la ruta del esquema de entrada donde estaba.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Aviso:
    ruta: str          # JSON Pointer (esquema JSON) o ruta del XSD donde está lo que no se tradujo tal cual
    mensaje: str


@dataclass
class Conversion:
    texto: str                     # el esquema resultante
    formato: str                   # 'json' o 'xsd'
    avisos: list = field(default_factory=list)


class ErrorDeConversion(Exception):
    """La entrada no se puede convertir: no es válida o no cumple la convención. errores: lista de textos."""

    def __init__(self, mensaje, errores=()):
        super().__init__(mensaje)
        self.errores = list(errores)


class Avisos(list):
    def agregar(self, ruta, mensaje):
        aviso = Aviso(ruta or '/', mensaje)
        if aviso not in self:
            self.append(aviso)
