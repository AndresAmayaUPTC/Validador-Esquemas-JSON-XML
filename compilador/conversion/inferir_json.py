"""Documentos JSON de ejemplo → JSON Schema, con la biblioteca GenSON.

    python -m compilador.conversion inferir ejemplo1.json ejemplo2.json ...

GenSON recorre cada documento y fusiona lo que ve: en un objeto, required es la intersección de las
claves de todos los ejemplos; en un arreglo, items admite cualquiera de los tipos de sus elementos; un
campo que fue entero en un ejemplo y decimal en otro queda como «number». Con un solo ejemplo no se puede
saber qué es opcional ni qué valores varían, así que conviene darle varios.

El esquema que sale usa type, properties, required, items y anyOf: todo está en el subconjunto que
compila el compilador.
"""
import json

from genson import SchemaBuilder

from ..semantico.metaesquema import DIALECTO
from . import Avisos, Conversion, ErrorDeConversion


def inferir_json(documentos) -> Conversion:
    """documentos: textos (bytes o str) de uno o más documentos JSON."""
    if not documentos:
        raise ErrorDeConversion('hace falta al menos un documento de ejemplo')
    constructor = SchemaBuilder(schema_uri=DIALECTO)
    for i, doc in enumerate(documentos, 1):
        texto = doc.decode('utf-8-sig') if isinstance(doc, bytes) else doc
        try:
            constructor.add_object(json.loads(texto))
        except (ValueError, UnicodeDecodeError) as e:
            raise ErrorDeConversion(f'el ejemplo {i} no es JSON válido: {e}', [str(e)]) from e
    avisos = Avisos()
    if len(documentos) == 1:
        avisos.agregar('/', 'con un solo ejemplo todas las propiedades que aparecen quedan como obligatorias')
    return Conversion(json.dumps(constructor.to_schema(), indent=2, ensure_ascii=False), 'json', list(avisos))
