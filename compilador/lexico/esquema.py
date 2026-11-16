"""Lexer del esquema (Fase 1, paso 6).

Un JSON Schema es un documento JSON, así que se tokeniza con el mismo lexer (sección 1.8 del
documento). Lo único que cambia es el alfabeto de tokens: una CADENA cuyo valor es una palabra clave
del vocabulario de JSON Schema se convierte en el token PALABRA_CLAVE.

El lexer no sabe si esa cadena está en posición de clave o de valor, ni si es el nombre de una
propiedad del usuario (por ejemplo "type" dentro de "properties"). Eso lo decide la gramática de la
Fase 2, que acepta PALABRA_CLAVE en cualquier lugar donde acepta CADENA.
"""
from dataclasses import replace

from .lexer import Token, tokenizar

# Vocabulario de JSON Schema Draft 2020-12. Que una palabra esté aquí no significa que el compilador
# la soporte: eso se decide en el análisis semántico (Fase 3).
PALABRAS_CLAVE = (
    # núcleo
    '$schema', '$id', '$ref', '$anchor', '$dynamicRef', '$dynamicAnchor', '$vocabulary', '$comment', '$defs',
    # aplicadores
    'allOf', 'anyOf', 'oneOf', 'not', 'if', 'then', 'else', 'dependentSchemas', 'prefixItems', 'items',
    'contains', 'properties', 'patternProperties', 'additionalProperties', 'propertyNames',
    'unevaluatedItems', 'unevaluatedProperties',
    # validación
    'type', 'enum', 'const', 'multipleOf', 'maximum', 'exclusiveMaximum', 'minimum', 'exclusiveMinimum',
    'maxLength', 'minLength', 'pattern', 'maxItems', 'minItems', 'uniqueItems', 'maxContains', 'minContains',
    'maxProperties', 'minProperties', 'required', 'dependentRequired',
    # anotaciones, formato y contenido
    'title', 'description', 'default', 'deprecated', 'readOnly', 'writeOnly', 'examples', 'format',
    'contentEncoding', 'contentMediaType', 'contentSchema',
)


class AFDPalabras:
    """AFD con forma de trie que reconoce un conjunto finito de palabras, byte a byte.

    El estado 0 es el inicial; paso() devuelve None cuando no hay transición. acepta[q] es el índice
    de la palabra reconocida en el estado q, o None. Se reutilizará en la Fase 4 para los nombres de
    propiedad de cada objeto del esquema.
    """

    def __init__(self, palabras):
        self.palabras = tuple(palabras)
        self.delta, self.acepta = [{}], [None]
        for i, palabra in enumerate(self.palabras):
            q = 0
            for b in palabra.encode('utf-8'):
                if b not in self.delta[q]:
                    self.delta[q][b] = len(self.delta)
                    self.delta.append({})
                    self.acepta.append(None)
                q = self.delta[q][b]
            assert self.acepta[q] is None, f'palabra repetida: {palabra}'
            self.acepta[q] = i

    def paso(self, q, b):
        return self.delta[q].get(b)

    def reconocer(self, datos: bytes):
        """Índice de la palabra igual a datos, o None."""
        q = 0
        for b in datos:
            q = self.paso(q, b)
            if q is None:
                return None
        return self.acepta[q]


_AFD_PALABRAS = AFDPalabras(PALABRAS_CLAVE)


def clasificar(token: Token) -> Token:
    """Convierte en PALABRA_CLAVE la CADENA cuyo valor decodificado es una palabra clave."""
    if token.tipo == 'CADENA' and _AFD_PALABRAS.reconocer(token.valor.encode('utf-8')) is not None:
        return replace(token, tipo='PALABRA_CLAVE')
    return token


def tokenizar_esquema(datos: bytes) -> list:
    """Tokens de un JSON Schema. Lanza ErrorLexico si el esquema no es léxicamente válido."""
    tokens, error = tokenizar(datos)
    if error:
        raise error
    return [clasificar(t) for t in tokens]
