"""Pruebas del parser del esquema y de su AST (Fase 2)."""
import json

import pytest

from compilador.lexico.esquema import PALABRAS_CLAVE
from compilador.lexico.lexer import ErrorLexico
from compilador.sintactico.arbol import derivar
from compilador.sintactico.ast_esquema import analizar_esquema
from compilador.sintactico.gramaticas import FORMAS
from compilador.sintactico.parser import ErrorSintactico

PERSONA = b'''{
  "type": "object",
  "properties": {
    "edad": {"type": "integer", "minimum": 0},
    "type": {"enum": ["a", "items"]}
  },
  "required": ["edad"],
  "x-nota": {"a": [1, null]},
  "additionalProperties": false
}'''

# Esquemas válidos sintácticamente que cubren todas las formas de la gramática
ESQUEMAS = [
    b'true', b'false', b'{}', PERSONA,
    b'{"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:x", "title": "t", "$comment": "c"}',
    b'{"type": ["string", "null"], "minLength": 1, "maxLength": 10, "pattern": "^a+$", "format": "email"}',
    b'{"type": "array", "items": {"type": "number"}, "prefixItems": [true, {"const": 1}], "minItems": 0, "uniqueItems": true}',
    b'{"$defs": {"nodo": {"type": "object", "properties": {"hijos": {"type": "array", "items": {"$ref": "#/$defs/nodo"}}}}}, "$ref": "#/$defs/nodo"}',
    b'{"allOf": [{"minimum": 1}], "anyOf": [true, false], "oneOf": [{}, {"not": {}}], "if": {}, "then": true, "else": false}',
    b'{"dependentRequired": {"a": ["b", "c"], "d": []}, "dependentSchemas": {"a": {"required": []}}, "required": []}',
    b'{"const": {"type": "type", "required": [1, 2.5e3, -0, true, null, {"items": []}]}, "default": "properties"}',
    b'{"enum": [], "examples": [1, "a", [[]], {}], "minimum": -1.5, "exclusiveMaximum": 1e400, "multipleOf": 0.01}',
    b'{"properties": {"properties": {"properties": {}}, "$ref": true, "": false}, "patternProperties": {"^x": {}}}',
    b'{"desconocida": 1, "otra": {"type": 5, "properties": []}, "$vocabulary": {"urn:v": true}}',
    b'{"contains": {}, "minContains": 1, "maxContains": 2, "propertyNames": {"maxLength": 3}, "deprecated": false}',
]


@pytest.mark.parametrize('fuente', ESQUEMAS)
def test_el_ast_conserva_el_documento(fuente):
    assert analizar_esquema(fuente).a_python() == json.loads(fuente)
    derivar(fuente)  # todo esquema es también JSON según la gramática de payloads


def test_estructura_del_ast():
    e = analizar_esquema(PERSONA)
    assert (e.booleano, e.inicio, e.fin) == (None, 0, len(PERSONA))
    assert [(c.nombre, c.forma, c.conocida) for c in e.claves] == [
        ('type', 'valor', True), ('properties', 'mapa', True), ('required', 'valor', True),
        ('x-nota', 'valor', False), ('additionalProperties', 'esquema', True),
    ]
    tipo = e.clave('type')
    assert (tipo.inicio, tipo.fin) == (4, 10)
    assert (tipo.valor.clase, tipo.valor.dato) == ('cadena', 'object')

    edad, otra = e.clave('properties').valor
    assert (edad.nombre, edad.inicio, edad.fin) == ('edad', PERSONA.index(b'"edad"'), PERSONA.index(b'"edad"') + 6)
    assert edad.esquema.clave('minimum').valor.dato == '0'       # el número se conserva como texto
    assert otra.nombre == 'type'                                  # una propiedad puede llamarse como una palabra clave
    enum = otra.esquema.clave('enum').valor
    assert enum.clase == 'arreglo' and [v.dato for v in enum.dato] == ['a', 'items']
    assert PERSONA[enum.inicio:enum.fin] == b'["a", "items"]'

    requeridas = e.clave('required').valor
    assert requeridas.clase == 'arreglo' and requeridas.dato[0].dato == 'edad'
    nota = e.clave('x-nota').valor
    assert nota.clase == 'objeto' and nota.dato[0][0] == 'a' and nota.dato[0][1].dato[1].clase == 'nulo'
    assert e.clave('additionalProperties').valor.booleano is False
    assert e.clave('no-existe') is None


def test_esquemas_booleanos_y_listas():
    assert analizar_esquema(b' true ').booleano is True
    e = analizar_esquema(b'{"anyOf": [true, {"type": "null"}, false]}')
    ramas = e.clave('anyOf')
    assert ramas.forma == 'lista'
    assert [r.booleano for r in ramas.valor] == [True, None, False]
    assert ramas.valor[1].clave('type').valor.dato == 'null'


def test_claves_repetidas_se_conservan():
    e = analizar_esquema(b'{"type": "string", "type": "number"}')
    assert [c.valor.dato for c in e.claves] == ['string', 'number']  # el análisis semántico decidirá qué hacer


def test_cada_palabra_clave_tiene_su_forma():
    formas = {'kw_esquema': ('esquema', b'{}'), 'kw_mapa': ('mapa', b'{}'), 'kw_lista': ('lista', b'[{}]'),
              'kw_numero': ('valor', b'1'), 'kw_cadena': ('valor', b'"x"'), 'kw_booleano': ('valor', b'true'),
              'kw_arreglo': ('valor', b'[]'), 'kw_valor': ('valor', b'null')}
    probadas = 0
    for grupo, palabras in FORMAS.items():
        forma, ejemplo = formas[grupo]
        for palabra in palabras:
            clave = analizar_esquema(b'{"%s": %s}' % (palabra.encode(), ejemplo)).claves[0]
            assert (clave.nombre, clave.forma, clave.conocida) == (palabra, forma, True)
            probadas += 1
    assert probadas == len(PALABRAS_CLAVE) - 3  # faltan type, required y dependentRequired


FORMA_ESQUEMA = ['FALSE', 'LLAVE_ABRE', 'TRUE']
CADENA_O_PALABRA = sorted(['CADENA'] + [f'"{p}"' for p in PALABRAS_CLAVE])


@pytest.mark.parametrize('fuente, pos, encontrado, esperados', [
    (b'', 0, 'FIN', FORMA_ESQUEMA),
    (b'[]', 0, 'CORCHETE_ABRE', FORMA_ESQUEMA),
    (b'"type"', 0, '"type"', FORMA_ESQUEMA),
    (b'{"type": 5}', 9, 'NUMERO', sorted(CADENA_O_PALABRA + ['CORCHETE_ABRE'])),
    (b'{"type": ["a", 1]}', 15, 'NUMERO', CADENA_O_PALABRA),
    (b'{"properties": []}', 15, 'CORCHETE_ABRE', ['LLAVE_ABRE']),
    (b'{"properties": {"a": 1}}', 21, 'NUMERO', FORMA_ESQUEMA),
    (b'{"items": [{}]}', 10, 'CORCHETE_ABRE', FORMA_ESQUEMA),
    (b'{"allOf": []}', 11, 'CORCHETE_CIERRA', FORMA_ESQUEMA),
    (b'{"allOf": {}}', 10, 'LLAVE_ABRE', ['CORCHETE_ABRE']),
    (b'{"minimum": "1"}', 12, 'CADENA', ['NUMERO']),
    (b'{"pattern": 1}', 12, 'NUMERO', CADENA_O_PALABRA),
    (b'{"uniqueItems": 1}', 16, 'NUMERO', ['FALSE', 'TRUE']),
    (b'{"enum": "a"}', 9, 'CADENA', ['CORCHETE_ABRE']),
    (b'{"required": "a"}', 13, 'CADENA', ['CORCHETE_ABRE']),
    (b'{"required": ["a", 1]}', 19, 'NUMERO', CADENA_O_PALABRA),
    (b'{"dependentRequired": {"a": "b"}}', 28, 'CADENA', ['CORCHETE_ABRE']),
    (b'{"type": "string",}', 18, 'LLAVE_CIERRA', CADENA_O_PALABRA),
    (b'{"type" "string"}', 8, 'CADENA', ['DOS_PUNTOS']),
    (b'{"type": "a"} {}', 14, 'LLAVE_ABRE', ['FIN']),
    (b'{"const": [1,]}', 13, 'CORCHETE_CIERRA',
     sorted(CADENA_O_PALABRA + ['CORCHETE_ABRE', 'FALSE', 'LLAVE_ABRE', 'NULL', 'NUMERO', 'TRUE'])),
])
def test_errores_sintacticos_del_esquema(fuente, pos, encontrado, esperados):
    with pytest.raises(ErrorSintactico) as e:
        analizar_esquema(fuente)
    assert (e.value.pos, e.value.encontrado, e.value.esperados) == (pos, encontrado, esperados)


def test_error_lexico_en_el_esquema():
    with pytest.raises(ErrorLexico) as e:
        analizar_esquema(b'{"type": "a\tb"}')
    assert e.value.pos == 11


def test_limite_de_anidamiento_del_esquema():
    anidar = lambda n: b'{"const": ' + b'[' * n + b']' * n + b'}'
    assert analizar_esquema(anidar(63)).clave('const').valor.clase == 'arreglo'
    with pytest.raises(ErrorSintactico) as e:
        analizar_esquema(anidar(64))
    assert e.value.motivo == 'anidamiento'

    defs = b'{"properties": {"a": ' * 40 + b'true' + b'}}' * 40  # 80 llaves anidadas
    with pytest.raises(ErrorSintactico) as e:
        analizar_esquema(defs)
    assert e.value.motivo == 'anidamiento'


def test_muchas_propiedades_sin_recursion():
    fuente = b'{"properties": {' + b', '.join(b'"p%d": {"type": "integer"}' % i for i in range(5000)) + b'}}'
    e = analizar_esquema(fuente)
    assert len(e.clave('properties').valor) == 5000
