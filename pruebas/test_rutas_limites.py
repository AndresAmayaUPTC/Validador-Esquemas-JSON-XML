"""Pruebas de la Fase 6: ruta del error como JSON Pointer y límites de tamaño y tiempo."""
import json
import random

import pytest

from pruebas.corpus import particion
from pruebas.test_automata import EXACTOS, PERSONA, compilado, correr
from pruebas.test_motor_rust import comparar, estropear, motor  # noqa: F401  (motor es un fixture)

ANIDADO = EXACTOS['anidado']   # arreglo de objetos con "a": [1] obligatorio


@pytest.mark.parametrize('esquema, documento, ruta', [
    (PERSONA, b'{"edad": "x", "nombre": "Ana"}', '/edad'),            # el valor de una propiedad
    (PERSONA, b'{"nombre": "Ana"}', ''),                              # falta una propiedad: la ruta es la del objeto
    (PERSONA, b'7', ''),
    (PERSONA, b'{"edad": 1, "nombre": "Ana", "zz": {"a": [1]}}', '/zz'),
    (ANIDADO, b'[{"a": [1]}, {"a": [1, 5]}]', '/1/a/1'),              # elemento de un arreglo dentro de un objeto
    (ANIDADO, b'[{"a": [1]}, {}]', '/1'),
    (ANIDADO, b'[{"a": [1]}, {"a": 3}]', '/1/a'),
    ({'maxItems': 1}, b'[1, 2, 3]', ''),                              # sobran elementos: la ruta es la del arreglo
    ({'properties': {'l': {'minItems': 2}}}, b'{"l": [1]}', '/l'),
    ({'additionalProperties': {'type': 'null'}}, b'{"a/b~c": 1}', '/a~1b~0c'),   # escapes de RFC 6901
    ({'additionalProperties': {'type': 'null'}}, b'{"": 1}', '/'),
    ({'items': {'items': {'type': 'null'}}}, b'[[], [null, null], [null, 0]]', '/2/1'),
])
def test_ruta_de_la_primera_violacion(esquema, documento, ruta):
    r = correr(esquema, documento)
    assert (r.estado, r.ruta) == ('invalido', ruta)


def test_la_ruta_es_none_si_no_hay_nada_que_senalar():
    assert correr(PERSONA, b'{"edad": 1, "nombre": "Ana"}').ruta is None


@pytest.mark.parametrize('documento, ruta', [
    (b'{"a": {"b": [1, 2,]}}', '/a/b'),      # el error está dentro del arreglo /a/b
    (b'{"a": {"b": 1,}}', '/a'),
    (b'[1, 2', ''),
    (b'{"a": [{"x": 1}, {"y" 2}]}', '/a/1'),
    (b'{"a": 01}', ''),                      # error léxico leyendo el valor de "a": se señala el objeto que lo contiene
])
def test_ruta_de_un_documento_mal_formado(documento, ruta):
    r = correr({}, documento)
    assert (r.estado, r.ruta) == ('malformado', ruta)


def test_claves_largas_se_recortan_en_la_ruta():
    nulos = {'additionalProperties': {'type': 'null'}}
    corta, larga = 'k' * 24, 'k' * 25
    assert correr(nulos, {corta: 1}).ruta == '/' + corta                       # 24 bytes caben enteros
    assert correr(nulos, {larga: 1}).ruta == '/' + 'k' * 24 + '…'
    assert correr(nulos, {'ñ' * 13: 1}).ruta == '/' + 'ñ' * 12 + '…'           # 26 bytes: se queda en 12 caracteres
    assert correr(nulos, {'a' + 'ñ' * 12: 1}).ruta == '/a' + 'ñ' * 11 + '…'    # no se parte un carácter
    assert correr(nulos, {'😀' * 7: 1}).ruta == '/' + '😀' * 6 + '…'
    # las propiedades declaradas se recortan igual: la ruta no depende del esquema
    declarada = {'properties': {larga: {'type': 'null'}}}
    assert correr(declarada, {larga: 1}).ruta == '/' + 'k' * 24 + '…'


def test_rutas_muy_profundas_se_recortan():
    profundo = {'items': {'$ref': '#'}, 'type': 'array'}
    r = correr(profundo, b'[' * 127 + b'1' + b']' * 127)
    assert r.estado == 'invalido' and r.ruta == '/0' * 126 + '…'    # 252 bytes de ruta y la marca
    assert len(r.ruta.encode()) <= 256
    assert correr(profundo, b'[' * 126 + b'1' + b']' * 126).ruta == '/0' * 126   # justo lo que cabe


def test_limite_de_tamano():
    documento = b'{"edad": 30, "nombre": "Ana"}'
    assert correr(PERSONA, documento, max_bytes=len(documento)).estado == 'valido'
    r = correr(PERSONA, documento, max_bytes=len(documento) - 1)
    assert (r.estado, r.error, r.ruta) == ('limite', (len(documento) - 1, 'tamaño máximo superado'), '')
    r = correr(PERSONA, documento, max_bytes=10, cortes=[4, 4, 4])     # el límite cae en medio de un bloque
    assert (r.estado, r.error) == ('limite', (10, 'tamaño máximo superado'))
    assert correr(PERSONA, documento, max_bytes=0).error == (0, 'tamaño máximo superado')
    # si antes del límite ya había un error, gana el error
    assert correr(PERSONA, b'{"edad": 01, "nombre": "Ana"}', max_bytes=20).estado == 'malformado'
    # las violaciones anteriores al corte se conservan
    r = correr(PERSONA, b'{"edad": "x", "nombre": "Ana"}', max_bytes=15)
    assert (r.estado, r.violaciones, r.detalle) == ('limite', 1, [(9, 'no admitido')])


def test_limite_de_tiempo():
    documento = b'{"edad": 30, "nombre": "Ana"}'
    r = correr(PERSONA, documento, plazo=0)
    assert (r.estado, r.error) == ('limite', (0, 'tiempo máximo superado'))
    assert correr(PERSONA, documento, plazo=60).estado == 'valido'


def test_rutas_y_limites_en_el_motor_de_rust(motor):  # noqa: F811
    # claves con caracteres especiales, largas y de varios bytes; rutas profundas; tamaño y plazo
    rng = random.Random(67)
    nulos = compilado({'additionalProperties': {'$ref': '#'}, 'items': {'$ref': '#'},
                       'type': ['object', 'array', 'null']}).automata
    nombres = ['a', '', 'a/b', '~', '~~//', 'k' * 23, 'k' * 24, 'k' * 25, 'k' * 60, 'ñ' * 12, 'ñ' * 13, 'a' + 'ñ' * 12,
               '😀' * 6, '😀' * 7, 'ab' + '😀' * 6, '€' * 8, '€' * 9, 'x€' * 6 + 'y', 'con espacio', '"comillas"']

    def documento(prof):
        if prof == 0 or rng.random() < 0.15:
            return rng.choice([None, None, 1, 'x'])      # 1 y 'x' son violaciones
        if rng.random() < 0.5:
            return [documento(prof - 1) for _ in range(rng.randint(0, 3))]
        return {rng.choice(nombres): documento(prof - 1) for _ in range(rng.randint(0, 3))}

    casos = []
    for _ in range(1500):
        datos = json.dumps(documento(rng.randint(1, 12)), ensure_ascii=rng.random() < 0.5).encode()
        casos.append((datos, particion(rng, len(datos)), 128, False))
        casos.append((estropear(rng, datos), particion(rng, len(datos)), rng.choice([128, 3]), False))
        casos.append((datos, particion(rng, len(datos)), 128, rng.random() < 0.3, rng.randint(0, len(datos) + 2)))
    profundo = b'{"k": [' * 60 + b'1' + b']}' * 60
    casos += [(profundo, [], 128, False), (profundo[:300], [], 128, False), (profundo, [], 100, False),
              (profundo, [], 128, False, 200), (profundo, [], 128, False, None, True), (b'[1]', [1], 128, False, None, True)]
    lineas = motor([(nulos, casos)])[0]
    assert comparar(motor, [(nulos, casos)]) == len(casos)
    rutas = {bytes.fromhex(linea.split()[8][1:]).decode() for linea in lineas if linea.split()[8] != '-'}
    assert any(r.endswith('…') for r in rutas) and any('~1' in r for r in rutas) and any('ñ' in r for r in rutas)
    assert max(len(r.encode()) for r in rutas) <= 256 and len(rutas) > 300
    assert {'tamano', 'tiempo', 'profundidad', 'sintactico'} <= {linea.split()[7] for linea in lineas}
