"""Pruebas del análisis semántico del esquema (Fase 3)."""
import json

import pytest

from compilador.lexico.esquema import PALABRAS_CLAVE
from compilador.semantico.analisis import analizar_fuente
from compilador.semantico.ciclos import TODOS, componentes
from compilador.semantico.clasificacion import CATEGORIAS

ARBOL = b'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$defs": {
    "nodo": {
      "type": "object",
      "properties": {
        "valor": {"type": "integer", "minimum": 0},
        "hijos": {"type": "array", "items": {"$ref": "#/$defs/nodo"}, "maxItems": 8}
      },
      "required": ["valor"]
    },
    "nombre": {"type": "string", "minLength": 1}
  },
  "type": "object",
  "properties": {
    "raiz": {"$ref": "#/$defs/nodo"},
    "autor": {"$ref": "#/$defs/nombre"}
  }
}'''


def analizar(esquema):
    """Analiza un esquema dado como objeto de Python."""
    return analizar_fuente(json.dumps(esquema).encode())


def codigos(esquema):
    return [(d.severidad, d.codigo) for d in analizar(esquema).diagnosticos]


# ---------------------------------------------------------------------------
# Tabla de símbolos y referencias
# ---------------------------------------------------------------------------
def test_tabla_de_simbolos():
    a = analizar_fuente(ARBOL)
    assert a.ok and a.diagnosticos == []
    assert list(a.tabla.simbolos) == [
        '', '/$defs/nodo', '/$defs/nodo/properties/valor', '/$defs/nodo/properties/hijos',
        '/$defs/nodo/properties/hijos/items', '/$defs/nombre', '/properties/raiz', '/properties/autor',
    ]
    assert a.tabla.simbolos[''] is a.raiz
    nodo = a.tabla.simbolos['/$defs/nodo']
    assert ARBOL[nodo.inicio:nodo.inicio + 1] == b'{' and a.tabla.ruta(nodo) == '/$defs/nodo'


def test_referencias_y_recursion():
    a = analizar_fuente(ARBOL)
    s = a.tabla.simbolos
    destinos = {a.tabla.ruta(e): a.tabla.ruta(a.tabla.referencias[id(e)])
                for e in a.tabla.esquemas if id(e) in a.tabla.referencias}
    assert destinos == {'/$defs/nodo/properties/hijos/items': '/$defs/nodo',
                        '/properties/raiz': '/$defs/nodo', '/properties/autor': '/$defs/nombre'}
    # un solo grupo recursivo: nodo → hijos → items → nodo
    assert [[a.tabla.ruta(e) for e in g] for g in a.grupos] == [
        ['/$defs/nodo', '/$defs/nodo/properties/hijos', '/$defs/nodo/properties/hijos/items']]
    assert a.es_recursivo(s['/$defs/nodo']) and not a.es_recursivo(s['']) and not a.es_recursivo(s['/properties/raiz'])
    # solo el $ref que cierra el ciclo es una llamada recursiva; los otros dos se pueden expandir
    assert a.es_llamada_recursiva(s['/$defs/nodo/properties/hijos/items'])
    assert not a.es_llamada_recursiva(s['/properties/raiz'])
    assert not a.es_llamada_recursiva(s['/properties/autor'])


def test_formas_de_ref():
    a = analizar({
        '$id': 'https://ejemplo.org/esquema',
        '$defs': {'a/b': {'type': 'null'}, 'c~d': True, 'con espacio': False, 'x': {'$anchor': 'ancla', 'type': 'string'}},
        'properties': {
            'p1': {'$ref': '#'}, 'p2': {'$ref': '#/$defs/a~1b'}, 'p3': {'$ref': '#/$defs/c~0d'},
            'p4': {'$ref': '#/$defs/con%20espacio'}, 'p5': {'$ref': '#ancla'},
            'p6': {'$ref': 'https://ejemplo.org/esquema#/$defs/x'}, 'p7': {'$ref': '#/properties/p5'},
        },
    })
    assert a.ok, a.diagnosticos
    destino = lambda p: a.tabla.ruta(a.tabla.referencias[id(a.tabla.simbolos['/properties/' + p])])
    assert [destino(f'p{i}') for i in range(1, 8)] == [
        '', '/$defs/a~1b', '/$defs/c~0d', '/$defs/con espacio', '/$defs/x', '/$defs/x', '/properties/p5']


@pytest.mark.parametrize('ref, codigo', [
    ('https://otro.org/esquema.json', 'ref-remota'),
    ('otro.json#/a', 'ref-remota'),
    ('#/$defs/no-existe', 'ref-no-resuelta'),
    ('#/properties', 'ref-no-resuelta'),          # existe, pero no es un esquema
    ('#/$defs/a/type', 'ref-no-resuelta'),
    ('#no-hay-ancla', 'ref-no-resuelta'),
    ('#/definitions/a', 'ref-no-resuelta'),
])
def test_referencias_invalidas(ref, codigo):
    a = analizar({'$defs': {'a': {'type': 'null'}}, 'properties': {'p': {'$ref': ref}}})
    (d,) = a.errores
    assert d.codigo == codigo and d.ruta == '/properties/p'
    assert json.loads(json.dumps(ref)) in d.mensaje
    if 'definitions' in ref:
        assert '$defs' in d.mensaje


def test_posicion_del_diagnostico():
    fuente = b'{"properties": {"a": {"$ref": "#/x"}}}'
    (d,) = analizar_fuente(fuente).errores
    assert fuente[d.inicio:d.fin] == b'"#/x"'


# ---------------------------------------------------------------------------
# Metaesquema
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('esquema, esperado', [
    ({'type': 'strin'}, ('error', 'tipo-desconocido')),
    ({'type': ['string', 'entero']}, ('error', 'tipo-desconocido')),
    ({'type': []}, ('error', 'tipo-vacio')),
    ({'type': ['string', 'null', 'string']}, ('error', 'repetido')),
    ({'minLength': -1}, ('error', 'limite-invalido')),
    ({'maxItems': 2.5}, ('error', 'limite-invalido')),
    ({'minProperties': 1e-3}, ('error', 'limite-invalido')),
    ({'maxLength': 4294967295}, ('error', 'limite-fuera-de-rango')),
    ({'multipleOf': 0}, ('error', 'multiplo-invalido')),
    ({'multipleOf': -2}, ('error', 'multiplo-invalido')),
    ({'required': ['a', 'b', 'a']}, ('error', 'repetido')),
    ({'dependentRequired': {'a': ['b', 'b']}}, ('error', 'repetido')),
    ({'$schema': 'http://json-schema.org/draft-07/schema#'}, ('error', 'dialecto')),
    ({'properties': {'a': {'$schema': 'https://json-schema.org/draft/2020-12/schema'}}}, ('error', 'solo-en-la-raiz')),
    ({'items': {'$id': 'urn:x'}}, ('error', 'solo-en-la-raiz')),
    ({'unevaluatedProperties': False}, ('error', 'no-soportada')),
    ({'$dynamicRef': '#x'}, ('error', 'no-soportada')),
    ({'$anchor': '1x'}, ('error', 'ancla-invalida')),
    ({'$anchor': 'a', 'not': {'$anchor': 'a'}}, ('error', 'ancla-duplicada')),
    ({'enum': []}, ('aviso', 'enum-vacio')),
    ({'enum': [1, 'a', 1.0]}, ('aviso', 'enum-repetido')),
    ({'enum': [{'a': 1, 'b': 2}, {'b': 2, 'a': 1}]}, ('aviso', 'enum-repetido')),
    ({'minLength': 5, 'maxLength': 4}, ('aviso', 'intervalo-vacio')),
    ({'minItems': 2, 'maxItems': 1}, ('aviso', 'intervalo-vacio')),
    ({'minimum': 10, 'maximum': 9.5}, ('aviso', 'intervalo-vacio')),
    ({'exclusiveMinimum': 1, 'maximum': 1}, ('aviso', 'intervalo-vacio')),
    ({'minimum': 0, 'exclusiveMinimum': 5, 'exclusiveMaximum': 5}, ('aviso', 'intervalo-vacio')),
    ({'mi-clave': 1}, ('aviso', 'clave-desconocida')),
    ({'definitions': {}}, ('aviso', 'clave-desconocida')),
])
def test_diagnosticos_del_metaesquema(esquema, esperado):
    assert codigos(esquema) == [esperado]


@pytest.mark.parametrize('esquema', [
    {'type': ['string', 'null'], 'minLength': 0, 'maxLength': 4294967294},
    {'minItems': 1.0, 'maxItems': 1e2, 'multipleOf': 0.01},      # 1.0 y 1e2 son enteros
    {'minimum': 1, 'maximum': 1}, {'exclusiveMinimum': 1, 'exclusiveMaximum': 1.5},
    {'enum': [1, '1', [1], None, True]}, {'required': []}, {'$schema': 'https://json-schema.org/draft/2020-12/schema#'},
    {'$id': 'urn:x', '$anchor': 'raiz', '$comment': 'c', 'title': 't', 'format': 'email', 'default': {'x': [1]}},
])
def test_esquemas_sin_diagnosticos(esquema):
    assert codigos(esquema) == []


def test_numeros_enormes():
    # json.dumps no sabe escribir 1e400, por eso el esquema va como texto
    assert analizar_fuente(b'{"minimum": -1e400, "maximum": 1e400, "maxItems": 4e9}').diagnosticos == []
    assert analizar_fuente(b'{"maxItems": 1e400}').errores[0].codigo == 'limite-fuera-de-rango'
    assert analizar_fuente(b'{"minItems": 1e-400}').errores[0].codigo == 'limite-invalido'
    enorme = b'1e' + b'9' * 40
    assert analizar_fuente(b'{"maximum": %s}' % enorme).errores[0].codigo == 'numero-fuera-de-rango'
    assert analizar_fuente(b'{"maxLength": %s}' % enorme).errores[0].codigo == 'limite-invalido'


def test_claves_y_entradas_duplicadas():
    fuente = b'{"type": "string", "type": "null", "properties": {"a": {}, "b": true, "a": false}}'
    a = analizar_fuente(fuente)
    assert [(d.codigo, d.inicio) for d in a.errores] == [
        ('clave-duplicada', fuente.index(b'"type"', 2)), ('entrada-duplicada', fuente.rindex(b'"a"'))]
    assert analizar_fuente(b'{"dependentRequired": {"a": [], "a": []}}').errores[0].codigo == 'entrada-duplicada'


def test_se_recogen_todos_los_diagnosticos():
    a = analizar({'type': 'x', 'minLength': -1, 'nota': 1, 'properties': {'p': {'$ref': '#/nada'}}})
    assert [d.codigo for d in a.diagnosticos] == ['tipo-desconocido', 'limite-invalido', 'clave-desconocida', 'ref-no-resuelta']
    assert not a.ok and len(a.errores) == 3 and len(a.avisos) == 1


# ---------------------------------------------------------------------------
# Ciclos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('esquema, rutas', [
    ({'$ref': '#'}, '#'),
    ({'$defs': {'a': {'$ref': '#/$defs/b'}, 'b': {'$ref': '#/$defs/a'}}}, '/$defs/a → /$defs/b'),
    ({'allOf': [{'type': 'object'}, {'$ref': '#'}]}, '# → /allOf/1'),
    ({'$defs': {'a': {'anyOf': [{'type': 'null'}, {'not': {'$ref': '#/$defs/a'}}]}}},
     '/$defs/a → /$defs/a/anyOf/1 → /$defs/a/anyOf/1/not'),
    ({'if': {'$ref': '#/then'}, 'then': {'oneOf': [{'$ref': '#/if'}]}}, '/if → /then → /then/oneOf/0'),
])
def test_ciclos_no_productivos(esquema, rutas):
    (d,) = analizar(esquema).errores
    assert d.codigo == 'ciclo-de-referencias'
    assert d.mensaje.endswith(': ' + rutas)


@pytest.mark.parametrize('esquema', [
    {'type': 'object', 'properties': {'hijo': {'$ref': '#'}}},                       # árbol: la propiedad es opcional
    {'type': 'array', 'items': {'$ref': '#'}},                                       # el arreglo vacío es el caso base
    {'type': 'object', 'required': ['sig'],
     'properties': {'sig': {'anyOf': [{'$ref': '#'}, {'type': 'null'}]}}},           # lista enlazada terminada en null
    {'required': ['a'], 'properties': {'a': {'$ref': '#'}}},                         # sin "type": un número ya la cumple
    {'type': 'array', 'minItems': 1, 'prefixItems': [{'type': 'null'}], 'items': {'$ref': '#'}},
    {'additionalProperties': {'$ref': '#'}, 'type': 'object'},
])
def test_recursion_productiva(esquema):
    a = analizar(esquema)
    assert a.ok, a.diagnosticos
    assert len(a.grupos) == 1 and a.es_recursivo(a.raiz)


@pytest.mark.parametrize('esquema', [
    {'type': 'object', 'required': ['a'], 'properties': {'a': {'$ref': '#'}}},
    {'type': 'array', 'minItems': 1, 'items': {'$ref': '#'}},
    {'type': 'array', 'minItems': 2, 'prefixItems': [{'type': 'null'}, {'$ref': '#'}]},
    {'type': 'object', 'required': ['x'], 'additionalProperties': {'$ref': '#'}},
    {'type': 'object', 'required': ['sig'], 'properties': {'sig': {'allOf': [{'$ref': '#'}, {'type': 'object'}]}}},
    {'type': ['object', 'array'], 'required': ['a'], 'properties': {'a': {'$ref': '#'}}, 'minItems': 1, 'items': {'$ref': '#'}},
])
def test_recursion_sin_caso_base(esquema):
    (d,) = analizar(esquema).errores
    assert (d.codigo, d.ruta) == ('recursion-sin-caso-base', '')


def test_esquema_falso_no_es_error():
    # un esquema que no acepta nada es válido mientras no sea por una recursión infinita
    assert analizar({'properties': {'a': False}, 'required': ['a'], 'type': 'object'}).ok


def test_componentes_fuertemente_conexas():
    class N:
        def __init__(self, nombre):
            self.nombre, self.suc = nombre, []
    n = {c: N(c) for c in 'abcdef'}
    for origen, destino in ['ab', 'bc', 'ca', 'cd', 'de', 'ed', 'ff']:
        n[origen].suc.append(n[destino])
    grupos = {''.join(sorted(x.nombre for x in g)) for g in componentes(list(n.values()), lambda x: x.suc)}
    assert grupos == {'abc', 'de', 'f'}


def test_cadena_larga_de_referencias_sin_recursion():
    k = 3000
    defs = {f'd{i}': {'$ref': f'#/$defs/d{i + 1}'} for i in range(k)}
    defs[f'd{k}'] = {'type': 'string'}
    a = analizar({'$defs': defs, '$ref': '#/$defs/d0'})
    assert a.ok and a.grupos == []
    assert a.tipos[id(a.raiz)] == {'string'}          # el tipo se propaga por toda la cadena
    defs[f'd{k}'] = {'$ref': '#/$defs/d0'}             # ahora es un ciclo de 3001 esquemas
    (d,) = analizar({'$defs': defs}).errores
    assert d.codigo == 'ciclo-de-referencias'


# ---------------------------------------------------------------------------
# Tipos y clasificación
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('esquema, tipos', [
    (True, TODOS), (False, set()), ({}, TODOS),
    ({'type': 'integer'}, {'number'}), ({'type': ['string', 'null']}, {'string', 'null'}),
    ({'const': [1]}, {'array'}), ({'enum': [1, 'a', None]}, {'number', 'string', 'null'}),
    ({'type': 'string', 'const': 1}, set()),
    ({'allOf': [{'type': ['string', 'number']}, {'type': ['number', 'null']}]}, {'number'}),
    ({'anyOf': [{'type': 'string'}, {'type': 'object'}]}, {'string', 'object'}),
    ({'$ref': '#/$defs/a', '$defs': {'a': {'type': 'boolean'}}}, {'boolean'}),
    ({'properties': {'a': {'$ref': '#'}}}, TODOS),
])
def test_tipos_posibles(esquema, tipos):
    a = analizar(esquema)
    assert a.tipos[id(a.raiz)] == tipos


def test_todas_las_palabras_clave_tienen_categoria():
    assert sorted(CATEGORIAS) == sorted(PALABRAS_CLAVE)
    por_categoria = {}
    for palabra, (categoria, _) in CATEGORIAS.items():
        por_categoria.setdefault(categoria, []).append(palabra)
    assert {c: len(p) for c, p in por_categoria.items()} == {'a': 13, 'b': 8, 'c': 16, 'anotacion': 16, 'no-soportada': 4}


def test_clasificacion_del_arbol():
    a = analizar_fuente(ARBOL)
    usos = {(u.ruta, u.palabra): (u.categoria, u.estrategia) for u in a.usos}
    assert usos['', '$schema'][0] == 'anotacion'
    assert usos['', 'type'][0] == 'a' and usos['/$defs/nodo', 'required'][0] == 'b'
    assert usos['/$defs/nodo/properties/hijos', 'maxItems'] == ('b', 'contador en el marco de pila')
    assert usos['/$defs/nodo/properties/hijos/items', '$ref'] == ('a', 'llamada recursiva: apila el estado de retorno')
    assert usos['/properties/raiz', '$ref'] == ('a', 'expansión en línea')
    assert a.nivel == 'b'


DESPACHO_PARCIAL = 'despacho por el primer token; diferida si dos ramas admiten el mismo tipo'


@pytest.mark.parametrize('esquema, palabra, categoria, estrategia', [
    ({'anyOf': [{'type': 'string'}, {'type': 'number'}]}, 'anyOf', 'c', 'despacho determinista por el primer token'),
    ({'oneOf': [{'type': 'integer'}, {'type': 'number'}]}, 'oneOf', 'c', DESPACHO_PARCIAL),  # ambas empiezan por NUMERO
    ({'anyOf': [{'type': 'object'}, {}]}, 'anyOf', 'c', DESPACHO_PARCIAL),
    ({'allOf': [{'type': 'string'}, {'type': 'number'}]}, 'allOf', 'c', 'producto de autómatas'),
    ({'pattern': '^a+$'}, 'pattern', 'c', 'verificación diferida'),
    ({'not': {}}, 'not', 'c', 'verificación diferida'),
    ({'uniqueItems': True}, 'uniqueItems', 'c', 'verificación diferida'),
    ({'uniqueItems': False}, 'uniqueItems', 'anotacion', 'sin efecto en la validación'),
    ({'enum': ['a', 1, None]}, 'enum', 'a', 'AFD de los valores permitidos'),
    ({'enum': ['a', 1, [1, 2]]}, 'enum', 'c', 'verificación diferida'),
    ({'const': 'a'}, 'const', 'a', 'AFD del valor permitido'),
    ({'const': {'a': 1}}, 'const', 'c', 'verificación diferida'),
    ({'mi-clave': 1}, 'mi-clave', 'desconocida', 'se ignora'),
])
def test_estrategia_segun_el_uso(esquema, palabra, categoria, estrategia):
    (uso,) = [u for u in analizar(esquema).usos if u.palabra == palabra]
    assert (uso.categoria, uso.estrategia) == (categoria, estrategia)


@pytest.mark.parametrize('esquema, nivel', [
    ({}, None), ({'title': 'x', 'nota': 1}, None),
    ({'type': 'object', 'properties': {'a': {'enum': [1, 2]}}}, 'a'),
    ({'type': 'array', 'items': {'type': 'number', 'minimum': 0}, 'minItems': 1}, 'b'),
    ({'type': 'string', 'pattern': 'x'}, 'c'),
])
def test_nivel_del_esquema(esquema, nivel):
    assert analizar(esquema).nivel == nivel


def test_informe():
    informe = analizar_fuente(ARBOL).informe(ARBOL)
    assert 'DIAGNÓSTICOS: 0 errores, 0 avisos' in informe
    assert 'TABLA DE SÍMBOLOS: 8 esquemas' in informe
    assert 'llamada recursiva' in informe and 'expansión' in informe
    assert 'NIVEL: DPDA con registros acotados' in informe
    assert 'RECURSIÓN: 1 grupo recursivo' in informe
    # las categorías salen en orden: (a), (b), (c) y después las que no afectan a la validación
    assert informe.index('(a)           type') < informe.index('(b)           required') < informe.index('anotacion     $schema')
    fuente = b'{\n  "type": "x"\n}'
    con_error = analizar_fuente(fuente)
    assert '2:11  tipo-desconocido' in con_error.informe(fuente)   # línea 2, columna 11
    assert 'byte 12  tipo-desconocido' in con_error.informe()
