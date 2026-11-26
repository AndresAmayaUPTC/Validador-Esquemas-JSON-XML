"""Pruebas del autómata de pila ponderado (Fase 4).

El oráculo es la biblioteca jsonschema: un validador independiente que construye el árbol completo del
documento. El autómata debe dar el mismo veredicto leyendo los tokens una sola vez.
"""
import json
import random
from dataclasses import replace

import jsonschema
import pytest

from compilador.automata import nodos as modulo_nodos
from compilador.automata.compilar import ErrorDeEsquema, a_dot, compilar, describir, filas_delta
from compilador.automata.dpda import Transicion, construir
from compilador.automata.formas import costo
from compilador.automata.interprete import validar
from compilador.automata.verificacion import minimizar, verificar
from compilador.semantico.analisis import analizar_fuente
from pruebas.corpus import particion
from pruebas.generador import generar

PERSONA = {
    'type': 'object',
    'properties': {
        'edad': {'type': 'integer', 'minimum': 0, 'maximum': 150},
        'nombre': {'type': 'string', 'minLength': 1, 'maxLength': 5},
        'activo': {'type': 'boolean'},
        'rol': {'enum': ['admin', 'user', 3, None]},
    },
    'required': ['edad', 'nombre'],
    'additionalProperties': False,
}
ARBOL = {
    '$defs': {'nodo': {'type': 'object', 'required': ['v'], 'additionalProperties': False, 'properties': {
        'v': {'type': 'integer'}, 'h': {'type': 'array', 'items': {'$ref': '#/$defs/nodo'}, 'maxItems': 2}}}},
    '$ref': '#/$defs/nodo',
}

# Esquemas que el autómata verifica por completo: su veredicto debe coincidir siempre con el del oráculo.
EXACTOS = {
    'persona': PERSONA,
    'arbol': ARBOL,
    'arreglo': {'type': 'array', 'items': {'type': 'number', 'exclusiveMinimum': 0}, 'minItems': 1, 'maxItems': 3},
    'prefijo': {'type': 'array', 'prefixItems': [{'type': 'string'}, {'type': 'integer'}], 'items': {'type': 'boolean'},
                'minItems': 1},
    'lista': {'type': 'object', 'required': ['sig'], 'properties': {
        'sig': {'anyOf': [{'$ref': '#'}, {'type': 'null'}]}, 'v': {'type': 'number'}}},
    'despacho': {'anyOf': [{'type': 'string', 'maxLength': 2}, {'type': 'integer', 'minimum': 2}, {'type': 'null'}]},
    'una': {'oneOf': [{'type': 'object', 'required': ['a']}, {'type': 'array', 'minItems': 2}, {'const': 'x'}]},
    'producto': {'allOf': [
        {'properties': {'a': {'type': 'integer'}}, 'required': ['a']},
        {'properties': {'a': {'minimum': 5}, 'b': {'type': 'string'}}, 'additionalProperties': {'type': 'null'}}]},
    'adicional': {'allOf': [{'properties': {'a': True}, 'additionalProperties': False}, {'properties': {'c': True}}]},
    'ref_con_hermanos': {'$ref': '#/$defs/pos', 'maximum': 10, '$defs': {'pos': {'type': 'number', 'minimum': 0}}},
    'dependencias': {'type': 'object', 'dependentRequired': {'a': ['b'], 'b': ['c']}, 'minProperties': 1, 'maxProperties': 3},
    'enum': {'enum': [1, 2.5, 'a', 'ab', True, None]},
    'tipos': {'type': ['string', 'integer'], 'minLength': 2, 'maximum': 4},
    'entero': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'number'}}},
    'constantes': {'type': 'object', 'properties': {
        'a': {'const': 'x'}, 'b': {'const': 0}, 'c': {'const': None}, 'x': {'const': False}}},
    'booleanos': {'type': 'object', 'properties': {'a': False, 'b': True}, 'additionalProperties': {'type': 'integer'}},
    'nombres': {'type': 'object', 'properties': {'ñ': {'type': 'null'}, 'a b': {'type': 'null'}, '': {'type': 'null'}},
                'required': ['ñ']},
    'anidado': {'type': 'array', 'items': {'type': 'object', 'required': ['a'], 'properties': {
        'a': {'type': 'array', 'prefixItems': [{'const': 1}], 'items': False}}}},
    'limites': {'type': 'number', 'exclusiveMinimum': 1, 'exclusiveMaximum': 3},
    'libre': {},
}

# Esquemas fuera del DPDA puro que se verifican por completo con las estrategias de respaldo: AFD de las
# expresiones regulares, aritmética exacta, huellas de los valores y sub-ejecuciones (producto de
# autómatas en ejecución). Su veredicto también debe coincidir siempre con el del oráculo.
RESPALDO = {
    'patron': {'type': 'object', 'properties': {'a': {'type': 'string', 'pattern': '^a+$', 'maxLength': 3}}, 'required': ['a']},
    'unicos': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'integer'}},
    'negacion': {'not': {'type': 'string'}, 'type': ['string', 'integer']},
    'condicional': {'if': {'type': 'integer'}, 'then': {'minimum': 5}, 'else': {'type': 'string'}},
    'ambiguo': {'anyOf': [{'type': 'integer'}, {'type': 'number', 'minimum': 5}, {'type': 'string'}]},
    'enum_con_arreglos': {'enum': [[1, 2], {'a': 1}, 'x', 3]},
    'multiplo': {'type': 'number', 'multipleOf': 0.5, 'minimum': 0},
    'contiene': {'type': 'array', 'contains': {'const': 1}, 'maxItems': 3},
    'patrones': {'type': 'object', 'patternProperties': {'^x': {'type': 'integer'}}, 'additionalProperties': False,
                 'properties': {'a': {'type': 'null'}}},
    'nombres': {'propertyNames': {'maxLength': 2}, 'dependentSchemas': {'a': {'required': ['b']}}, 'required': ['c']},
    'una_ambigua': {'oneOf': [{'type': 'integer', 'minimum': 0}, {'type': 'number', 'maximum': 10},
                              {'type': 'object', 'required': ['a']}, {'type': 'object', 'required': ['b']}]},
    'anidados': {'type': 'array', 'items': {'anyOf': [
        {'type': 'object', 'properties': {'v': {'not': {'const': 0}}}, 'required': ['v']},
        {'type': 'object', 'properties': {'w': {'type': 'array', 'uniqueItems': True, 'contains': {'type': 'string'}}}}]}},
    'contadores': {'type': 'array', 'contains': {'type': 'integer'}, 'minContains': 2, 'maxContains': 3,
                   'allOf': [{'contains': {'type': 'string'}}]},
    'claves': {'type': 'object', 'propertyNames': {'pattern': '^[a-z]+$'},
               'patternProperties': {'^a': {'type': 'integer'}, 'b$': {'minimum': 3}},
               'properties': {'ab': {'maximum': 10}}, 'additionalProperties': {'type': 'string'}},
}

# Esquemas con palabras clave que quedan en verificación diferida: el autómata comprueba solo una parte.
DIFERIDOS = {
    'patron_no_regular': {'type': 'object', 'properties': {'a': {'type': 'string', 'pattern': '^(a+)b\\1$', 'maxLength': 5}},
                          'required': ['a']},
    'multiplo_largo': {'type': 'number', 'multipleOf': 1234567890123456789012, 'minimum': 0},   # 22 dígitos
    'anticipacion': {'type': 'object', 'patternProperties': {'^(?=x)': {'type': 'integer'}},
                     'properties': {'n': {'type': 'null'}}},
}

_COMPILADOS = {}


def compilado(esquema):
    clave = json.dumps(esquema, sort_keys=True)
    if clave not in _COMPILADOS:
        _COMPILADOS[clave] = compilar(clave.encode())
    return _COMPILADOS[clave]


def correr(esquema, documento, **opciones):
    """Valida con el autómata un documento dado como objeto de Python o como bytes."""
    datos = documento if isinstance(documento, bytes) else json.dumps(documento, ensure_ascii=False).encode()
    return validar(compilado(esquema).automata, datos, **opciones)


def instancias(esquema, n=400, semilla=1):
    rng = random.Random(semilla)
    return [json.loads(json.dumps(generar(esquema, esquema, rng))) for _ in range(n)]


# ---------------------------------------------------------------------------
# Contra el oráculo
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('nombre', list(EXACTOS) + ['respaldo:' + n for n in RESPALDO])
def test_mismo_veredicto_que_jsonschema(nombre):
    esquema = RESPALDO[nombre[9:]] if nombre.startswith('respaldo:') else EXACTOS[nombre]
    oraculo = jsonschema.Draft202012Validator(esquema)
    assert compilado(esquema).automata.diferidas == []
    validos = 0
    documentos = instancias(esquema)
    for doc in documentos:
        r = correr(esquema, doc)
        esperado = oraculo.is_valid(doc)
        assert r.estado == ('valido' if esperado else 'invalido'), doc
        assert (r.violaciones == 0) == esperado and len(r.detalle) == r.violaciones
        validos += esperado
    if nombre != 'libre':
        assert 10 < validos < len(documentos) - 10, 'la muestra no tiene bastantes casos de las dos clases'


@pytest.mark.parametrize('nombre', DIFERIDOS)
def test_esquemas_con_verificacion_diferida(nombre):
    esquema = DIFERIDOS[nombre]
    oraculo = jsonschema.Draft202012Validator(esquema)
    assert compilado(esquema).automata.diferidas
    estados = set()
    for doc in instancias(esquema):
        r = correr(esquema, doc)
        estados.add(r.estado)
        if r.estado == 'invalido':      # lo que el autómata sí comprueba no produce falsas alarmas
            assert not oraculo.is_valid(doc), doc
        elif r.estado == 'valido':      # si no cruzó nada diferido, el veredicto es completo
            assert oraculo.is_valid(doc), doc
        else:
            assert r.estado == 'pendiente' and r.pendientes > 0
    assert 'pendiente' in estados


def test_lista_de_diferidas():
    assert compilado(DIFERIDOS['patron_no_regular']).automata.diferidas == [('/properties/a', 'pattern')]
    assert compilado(DIFERIDOS['multiplo_largo']).automata.diferidas == [('', 'multipleOf')]
    assert compilado(DIFERIDOS['anticipacion']).automata.diferidas == [('', 'patternProperties')]
    # las estrategias de respaldo no dejan nada diferido
    assert all(compilado(e).automata.diferidas == [] for e in RESPALDO.values())
    r = correr(DIFERIDOS['patron_no_regular'], {'a': 'aba'})
    assert (r.estado, r.pendientes) == ('pendiente', 1)
    assert correr(DIFERIDOS['patron_no_regular'], {'a': 'abaaaa'}).detalle == [(6, 'long_max')]


def test_ambiguedad_resuelta_con_sub_ejecuciones():
    ambiguo = RESPALDO['ambiguo']
    assert correr(ambiguo, 3).estado == 'valido'                  # cumple la rama de los enteros
    assert correr(ambiguo, 7.5).estado == 'valido'                # cumple la de number ≥ 5
    assert correr(ambiguo, 2.5).detalle == [(0, 'anyOf')]         # no cumple ninguna de las dos
    assert correr(ambiguo, 'x').estado == 'valido'                # una sola rama admite cadenas: despacho
    assert correr(ambiguo, None).detalle == [(0, 'no admitido')]  # ninguna admite null
    una = RESPALDO['una_ambigua']
    assert correr(una, 20).estado == 'valido' and correr(una, -3).estado == 'valido'
    assert correr(una, 5).detalle == [(0, 'oneOf')]               # cumplen dos ramas
    assert correr(una, {'a': 1, 'b': 2}).detalle == [(15, 'oneOf')]   # al cerrar el objeto
    # la ruta de la violación es la del valor
    r = correr({'properties': {'x': ambiguo}}, {'x': 2.5})
    assert (r.ruta, r.detalle) == ('/x', [(6, 'anyOf')])


def test_tres_valores_con_comprobaciones_diferidas():
    # una rama con un patrón no regular da «quizá»: anyOf cumple si otra rama cumple seguro
    quiza = {'anyOf': [{'type': 'string', 'pattern': '^(a)\\1$'}, {'type': 'string', 'maxLength': 1}]}
    assert correr(quiza, 'x').estado == 'valido'
    assert (correr(quiza, 'aa').estado, correr(quiza, 'aa').pendientes) == ('pendiente', 1)
    assert correr({'not': quiza['anyOf'][0]}, 'aa').estado == 'pendiente'
    assert correr({'not': quiza['anyOf'][0]}, 5).estado == 'valido'   # el not de algo que no admite números


def test_huellas_y_multiplos():
    unicos = {'uniqueItems': True}
    assert correr(unicos, [1, 1.0]).detalle == [(7, 'unicos')]        # 1 y 1.0 son el mismo valor
    assert correr(unicos, [{'a': 1, 'b': [2]}, {'b': [2.0], 'a': 1}]).estado == 'invalido'  # el orden no importa
    assert correr(unicos, b'["a", "\\u0061"]').estado == 'invalido'
    assert correr(unicos, [[1, 2], [2, 1], {'a': 1}, {'a': '1'}, 'x']).estado == 'valido'
    # si no caben las huellas, la comprobación queda pendiente
    r = correr(unicos, list(range(20)), max_huellas=8)
    assert (r.estado, r.pendientes) == ('pendiente', 1)
    assert correr(unicos, [5, 5] + list(range(20)), max_huellas=8).estado == 'invalido'
    constante = {'const': {'a': [1, {'b': None}]}}
    assert correr(constante, b'{"a": [1.0, {"b": null}]}').estado == 'valido'
    assert correr(constante, b'{"a": [1, {"b": null}, 2]}').detalle == [(25, 'enum_huella')]
    multiplo = {'multipleOf': 0.01}
    assert correr(multiplo, b'0.07').estado == 'valido'                # exacto: no hay error de redondeo
    assert correr(multiplo, b'0.075').detalle == [(0, 'multiplo')]
    assert correr(multiplo, b'7e300').estado == 'valido' and correr(multiplo, b'7e-300').estado == 'invalido'
    assert correr({'multipleOf': 7}, b'1e100').estado == 'invalido'    # 10^100 no es múltiplo de 7
    assert correr({'multipleOf': 7}, b'7e100').estado == 'valido'


def test_limite_de_sub_ejecuciones():
    # cada nivel de anidamiento abre un anyOf ambiguo de dos ramas que siguen vivas hasta el final
    anidado = {'anyOf': [{'type': 'array', 'items': {'$ref': '#'}}, {'type': 'array', 'maxItems': 5}]}
    assert correr(anidado, b'[[[[[[[[]]]]]]]]').estado == 'valido'
    r = correr(anidado, b'[[[[[[[[[[]]]]]]]]]]', max_hilos=16)
    assert (r.estado, r.error) == ('limite', (8, 'demasiadas sub-ejecuciones'))


# ---------------------------------------------------------------------------
# Pesos: número de violaciones
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('documento, esperado', [
    # cada violación se da como (texto donde ocurre, motivo); la posición es la de ese texto en el documento
    (b'{"edad": 30, "nombre": "Ana"}', []),
    (b'{}', [(b'}', 'required'), (b'}', 'required')]),
    (b'{"edad": -1.5, "nombre": ""}', [(b'-1.5', 'entero'), (b'-1.5', 'min'), (b'""', 'long_min')]),
    (b'{"edad": 200, "nombre": "Ana", "rol": "x", "z": [1, {"y": 2}]}',
     [(b'200', 'max'), (b'"x"', 'enum_cad'), (b'[1,', 'no admitido')]),
    (b'{"edad": "30", "nombre": 7, "activo": null}',
     [(b'"30"', 'no admitido'), (b'7', 'no admitido'), (b'null', 'no admitido')]),
    (b'{"rol": 3, "nombre": "Ana"}', [(b'}', 'required')]),
    (b'{"rol": 3.0, "nombre": "Ana", "edad": 1e1}', []),         # 3.0 es igual a 3 y 1e1 es el entero 10
    (b'[]', [(b'[', 'no admitido')]),
    (b'"x"', [(b'"x"', 'no admitido')]),
])
def test_violaciones_de_persona(documento, esperado):
    r = correr(PERSONA, documento)
    detalle = [(documento.index(texto), motivo) for texto, motivo in esperado]
    assert r.detalle == detalle and r.violaciones == len(detalle)
    assert r.estado == ('invalido' if detalle else 'valido')
    assert jsonschema.Draft202012Validator(PERSONA).is_valid(json.loads(documento)) == (not detalle)


def test_contadores_y_mascaras():
    arreglo = {'type': 'array', 'minItems': 2, 'maxItems': 3}
    assert correr(arreglo, [1]).detalle == [(2, 'minItems')]
    assert correr(arreglo, [1, 2, 3]).violaciones == 0
    assert correr(arreglo, [1, 2, 3, 4, 5, 6]).detalle == [(10, 'maxItems')]   # se cuenta una sola vez, al pasarse
    objeto = {'minProperties': 2, 'maxProperties': 2, 'dependentRequired': {'a': ['b', 'c']}}
    assert [m for _, m in correr(objeto, {'a': 1}).detalle] == ['dependentRequired', 'dependentRequired', 'minProperties']
    assert [m for _, m in correr(objeto, {'a': 1, 'b': 1, 'c': 1}).detalle] == ['maxProperties']
    assert correr(objeto, {'x': 1, 'y': 2}).violaciones == 0
    assert correr(objeto, 5).violaciones == 0   # las restricciones de objeto no afectan a otros tipos


def test_un_valor_no_admitido_se_salta_entero():
    r = correr({'type': 'string'}, b'{"a": [1, 2, {"b": [null]}], "c": {}}')
    assert (r.estado, r.detalle, r.profundidad) == ('invalido', [(0, 'no admitido')], 4)


def test_numeros_enormes_y_precision():
    limites = {'type': 'number', 'minimum': 0.1, 'maximum': 10000000000000000000000}
    assert correr(limites, b'0.1').violaciones == 0
    assert correr(limites, b'0.09999999999999999999999999').detalle == [(0, 'min')]
    assert correr(limites, b'10000000000000000000000.0000000001').detalle == [(0, 'max')]
    assert correr(limites, b'1e22').violaciones == 0
    assert correr(limites, b'1e999999999999999999999999').detalle == [(0, 'max')]
    assert correr(limites, b'1e-999999999999999999999999').detalle == [(0, 'min')]
    assert correr(limites, b'-1e999999999999999999999999').detalle == [(0, 'min')]
    assert correr({'type': 'integer'}, b'1e999999999999999999999999').violaciones == 0
    assert correr({'type': 'integer'}, b'1e-999999999999999999999999').detalle == [(0, 'entero')]
    assert correr({'type': 'integer'}, b'0e-999999999999999999999999').violaciones == 0
    assert correr({'enum': [100]}, b'1.00e2').violaciones == 0


def test_longitud_en_caracteres():
    assert correr({'maxLength': 2}, '😀ñ').violaciones == 0             # 2 caracteres, 6 bytes
    assert correr({'maxLength': 2}, b'"\\ud83d\\ude00\\u00f1"').violaciones == 0
    assert correr({'maxLength': 2}, 'abc').detalle == [(0, 'long_max')]


def test_las_pruebas_van_de_menor_a_mayor_costo():
    esquema = {'type': 'integer', 'minimum': 0, 'enum': [1, 2, 3], 'maximum': 9}
    a = compilado(esquema).automata
    pruebas = a.delta[a.inicial]['NUMERO'].pruebas
    assert [p[0] for p in pruebas] == ['entero', 'max', 'min', 'enum_num']   # a igual costo, el orden del esquema
    assert [costo(p) for p in pruebas] == sorted(costo(p) for p in pruebas)
    # en fallo temprano se detiene en la primera prueba que falla, que es la más barata
    r = correr(esquema, b'-0.5', fallo_temprano=True)
    assert (r.estado, r.detalle) == ('invalido', [(0, 'entero')])
    assert correr(esquema, b'-0.5').violaciones == 3


def test_fallo_temprano():
    documento = b'{"edad": 200, "nombre": "Ana", "rol": "x", "z": [1, {"y": 2}]}'
    completo, corto = correr(PERSONA, documento), correr(PERSONA, documento, fallo_temprano=True)
    assert (corto.estado, corto.violaciones, corto.detalle) == ('invalido', 1, completo.detalle[:1])
    # ni siquiera llega a leer el resto: un error de sintaxis posterior no se ve
    assert correr(PERSONA, b'{"edad": 200, ]]]', fallo_temprano=True).estado == 'invalido'
    assert correr(PERSONA, b'{"edad": 200, ]]]').estado == 'malformado'


# ---------------------------------------------------------------------------
# Documentos mal formados, límites y streaming
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('documento, pos, motivo', [
    (b'', 0, 'sintáctico'), (b'{"edad": 1,}', 11, 'sintáctico'), (b'{"edad" 1}', 8, 'sintáctico'),
    (b'{"edad": 1} {}', 12, 'sintáctico'), (b'[1 2]', 3, 'sintáctico'), (b'{"edad": [1,]}', 12, 'sintáctico'),
    (b'{1: 2}', 1, 'sintáctico'), (b'{"edad": 1', 10, 'sintáctico'), (b']', 0, 'sintáctico'),
    (b'{"edad": 01}', 10, 'léxico'), (b'{"edad": "\xff"}', 10, 'léxico'), (b'{"nombre": "a', 13, 'léxico'),
    (b'{"edad": 1, "nombre": "a", "edad": 2}', 27, 'clave repetida'),
])
def test_documentos_mal_formados(documento, pos, motivo):
    r = correr(PERSONA, documento)
    assert r.estado == 'malformado' and r.error[0] == pos and r.error[1].startswith(motivo)


def test_claves_repetidas_no_declaradas():
    # limitación conocida: sin memoria no acotada solo se detectan las repetidas que el esquema declara
    assert correr({'properties': {'a': True}}, b'{"a": 1, "a": 2}').estado == 'malformado'
    assert correr({}, b'{"a": 1, "a": 2}').estado == 'valido'
    repetida = b'{"b": 1, "b": 2, "a": 3, "a": 4}'
    assert correr({'required': ['a']}, repetida).error == (repetida.rindex(b'"a"'), 'clave repetida')


def test_limite_de_profundidad():
    assert correr({}, b'[[[1]]]', max_profundidad=3).profundidad == 3
    r = correr({}, b'[[[[1]]]]', max_profundidad=3)
    assert (r.estado, r.error) == ('limite', (3, 'profundidad máxima superada'))
    assert correr({}, b'[' * 128 + b']' * 128).estado == 'valido'
    assert correr({}, b'[' * 129 + b']' * 129).estado == 'limite'


@pytest.mark.parametrize('k', [1, 5, 30, 60])
def test_bombeo_del_arbol(k):
    # u·v^k·x·y^k·z: cada vuelta añade un objeto y un arreglo; la pila crece con k y nada más
    documento = b'{"v": 1, "h": [' * k + b'{"v": 0}' + b']}' * k
    r = correr(ARBOL, documento)
    assert (r.estado, r.profundidad) == ('valido', 2 * k + 1)
    roto = documento.replace(b'{"v": 0}', b'{"w": 0}')
    hoja = roto.index(b'{"w": 0}')   # la propiedad «w» no está admitida y falta «v»
    assert correr(ARBOL, roto).detalle == [(hoja + 6, 'no admitido'), (hoja + 7, 'required')]


def test_cualquier_particion_da_el_mismo_resultado():
    rng = random.Random(5)
    documentos = [json.dumps(d, ensure_ascii=False).encode() for d in instancias(PERSONA, 60) + instancias(ARBOL, 60)]
    documentos += [b'{"edad": 1,}', b'{"edad": 01}', b'{"edad": 1, "edad": 2}', '{"nombre": "ñ😀"}'.encode()]
    for i, datos in enumerate(documentos):
        esquema = PERSONA if i < 60 or i >= 120 else ARBOL
        esperado = correr(esquema, datos)
        assert correr(esquema, datos, cortes=[1] * len(datos)) == esperado
        for _ in range(3):
            assert correr(esquema, datos, cortes=particion(rng, len(datos))) == esperado


# ---------------------------------------------------------------------------
# Verificación, minimización y tamaño
# ---------------------------------------------------------------------------
def bruto(esquema):
    return construir(analizar_fuente(json.dumps(esquema).encode()))


@pytest.mark.parametrize('nombre', list(EXACTOS) + ['diferido:' + n for n in DIFERIDOS]
                         + ['respaldo:' + n for n in RESPALDO])
def test_el_automata_es_un_dpda_valido(nombre):
    esquema = (DIFERIDOS[nombre[9:]] if nombre.startswith('diferido:') else RESPALDO[nombre[9:]]
               if nombre.startswith('respaldo:') else EXACTOS[nombre])
    for a in (bruto(esquema), compilado(esquema).automata):
        v = verificar(a)
        assert v.problemas == []
        assert a.pila_inicial in v.cimas[a.inicial]
        assert all(v.cimas[q] == {a.pila_inicial} for q in a.finales)
        assert v.transiciones_expandidas >= sum(len(a.delta[q]) for q in v.alcanzables)
    assert len(verificar(compilado(esquema).automata).alcanzables) == len(compilado(esquema).automata.estados)


def test_la_verificacion_detecta_automatas_incorrectos():
    def estropear(cambio):
        a = bruto(PERSONA)
        cambio(a, {nombre: q for q, nombre in enumerate(a.estados)})
        return ' / '.join(verificar(a).problemas)

    def cadena_y_clave(a, q):
        a.delta[q['OI1']]['CADENA'] = Transicion(destino=q['DP0'])

    def sin_resto(a, q):
        del a.delta[q['OI1']]['CLAVE', None]

    def desapila_el_fondo(a, q):
        a.delta[a.inicial]['NULL'] = Transicion(desapila=True)

    def accion_en_otro_marco(a, q):
        a.delta[q['AS0.0']]['COMA'] = replace(a.delta[q['AS0.0']]['COMA'], accion=('clave', 0, None))

    def acepta_con_pila(a, q):
        a.delta[q['OS1']]['FIN'] = Transicion(destino=q['ACEPTA'])

    assert 'dos transiciones' in estropear(cadena_y_clave)
    assert 'no declaradas' in estropear(sin_resto)
    assert 'fondo de la pila' in estropear(desapila_el_fondo)
    assert 'actúa sobre' in estropear(accion_en_otro_marco)
    assert 'sin vaciar' in estropear(acepta_con_pila)
    assert estropear(lambda a, q: None) == ''


@pytest.mark.parametrize('nombre', list(EXACTOS) + ['respaldo:' + n for n in RESPALDO])
def test_minimizar_no_cambia_el_comportamiento(nombre):
    esquema = RESPALDO[nombre[9:]] if nombre.startswith('respaldo:') else EXACTOS[nombre]
    sin_minimizar, minimo = bruto(esquema), compilado(esquema).automata
    assert len(minimo.estados) <= len(verificar(sin_minimizar).alcanzables)
    for doc in instancias(esquema, 150, semilla=9):
        datos = json.dumps(doc).encode()
        assert validar(minimo, datos) == validar(sin_minimizar, datos)
    assert len(minimizar(minimo).estados) == len(minimo.estados)   # ya es mínimo


def test_minimizar_fusiona_estados_equivalentes():
    iguales = {'type': 'object', 'properties': {f'p{i}': {'type': 'string', 'maxLength': 9} for i in range(40)}}
    c = compilado(iguales)
    # 40 propiedades con el mismo esquema: un solo par de estados para todas
    assert c.sin_minimizar['alcanzables'] > 80 and c.tamano['estados'] < 20
    assert c.tamano['simbolos_de_entrada'] == 12 + 40 + 1   # los tokens, una clave por propiedad y «otra clave»
    distintas = {'type': 'object', 'properties': {f'p{i}': {'type': 'string', 'maxLength': i} for i in range(40)}}
    assert compilado(distintas).tamano['estados'] > 80   # aquí cada propiedad necesita sus estados
    # el estado fusionado se nombra con todos los que reúne: la clave "y" no va a un estado llamado como el de "x"
    punto = compilado({'type': 'object', 'properties': {'x': {'type': 'integer'}, 'y': {'type': 'integer'}}})
    destinos = {f['simbolo']: f['destino'] for f in filas_delta(punto) if f['estado'] == 'OI1'}
    assert destinos['clave "x"'] == destinos['clave "y"'] == 'DP2≡DP3' and 'V2≡V3' in punto.automata.estados
    assert not any('≡' in n for n in compilado(distintas).automata.estados)   # sin fusiones, los nombres de siempre


def test_tamano_y_descripcion():
    c = compilado(PERSONA)
    # 7 nodos (raíz, 4 propiedades, «false» de las adicionales y el libre): 7 estados V y 6 DP; el objeto raíz
    # (OI, OC, OS); el objeto y el arreglo libres (3 + 3) para saltar valores no admitidos; FIN y ACEPTA
    assert c.tamano == {'estados': 24, 'simbolos_de_pila': 4, 'simbolos_de_entrada': 17, 'transiciones': 91,
                        'transiciones_expandidas': 91}
    # si un estado se alcanza con varias cimas, δ(q, a, Z) tiene más entradas que la forma compacta
    compartido = compilado({'properties': {'a': {}, 'b': {'properties': {'c': {}}}}}).tamano
    assert compartido['transiciones_expandidas'] > compartido['transiciones']
    texto = describir(c)
    assert 'M = (Q, Σ, Γ, δ, q0, Z0, F, w)' in texto and 'Verificado.' in texto
    assert 'clave "edad"' in texto and 'es entero; ≤ 150; ≥ 0' in texto and "∈ {'admin', 'user'}" in texto
    assert 'requiere 0b110' in texto   # bits por orden alfabético: activo, edad, nombre, rol
    assert 'VERIFICACIÓN DIFERIDA' in describir(compilado(DIFERIDOS['patron_no_regular']))
    assert 'anyOf' in describir(compilado(RESPALDO['ambiguo']))   # el grupo de sub-ejecuciones
    dot = a_dot(c)
    assert dot.startswith('digraph M {') and 'style=dashed' in dot and 'shape=doublecircle' in dot
    assert json.loads(json.dumps(c.automata.a_dict()))['estados'] == c.automata.estados


def test_esquema_con_errores():
    with pytest.raises(ErrorDeEsquema) as e:
        compilar(b'{"type": "strin", "$ref": "#"}')
    assert [d.codigo for d in e.value.analisis.errores] == ['tipo-desconocido', 'ciclo-de-referencias']


def test_cota_del_producto(monkeypatch):
    monkeypatch.setattr(modulo_nodos, 'MAX_NODOS', 10)
    with pytest.raises(modulo_nodos.ExplosionDeEstados):
        bruto({'properties': {f'p{i}': {'maxLength': i} for i in range(20)}})


def test_esquema_grande():
    grande = {'type': 'object', 'properties': {f'p{i}': {'type': 'integer', 'minimum': i % 7} for i in range(2000)},
              'required': [f'p{i}' for i in range(0, 2000, 3)]}
    c = compilado(grande)
    assert c.tamano['estados'] < 40                     # solo hay 7 esquemas de propiedad distintos
    documento = {f'p{i}': i % 7 for i in range(2000)}
    assert correr(grande, documento).estado == 'valido'
    del documento['p0'], documento['p3']
    documento['p8'] = 0
    assert sorted(m for _, m in correr(grande, documento).detalle) == ['min', 'required', 'required']
