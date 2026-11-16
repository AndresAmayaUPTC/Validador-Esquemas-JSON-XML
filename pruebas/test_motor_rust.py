"""Pruebas diferenciales del motor de Rust contra el intérprete de Python (Fase 5).

Los dos ejecutan el mismo autómata: uno desde los objetos de Python y el otro desde las tablas planas.
Deben coincidir en todo: estado, número de violaciones, comprobaciones diferidas, profundidad, posición y
motivo de la primera violación, y posición y causa del error.
Si cargo no está instalado, estas pruebas se omiten.
"""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from compilador.automata.interprete import validar
from compilador.generador.tablas import aplanar, serializar
from pruebas.corpus import ENTRADAS_JSON, particion
from pruebas.test_automata import ARBOL, DIFERIDOS, EXACTOS, PERSONA, compilado, instancias

VALIDADOR = Path(__file__).resolve().parents[1] / 'validador'
FALLOS = {'léxico: byte': 'lexico-byte', 'léxico: fin': 'lexico-fin', 'léxico: número': 'lexico-largo',
          'sintáctico': 'sintactico', 'clave repetida': 'clave-repetida', 'profundidad': 'profundidad',
          'tamaño': 'tamano', 'tiempo': 'tiempo'}


@pytest.fixture(scope='module')
def motor():
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(Path.home() / '.cargo' / 'bin'))
    if cargo is None:
        pytest.skip('cargo no está instalado')
    subprocess.run([cargo, 'build', '--quiet', '--example', 'dpda'], cwd=VALIDADOR, check=True)

    def ejecutar(lotes):
        """lotes: lista de (autómata, casos); cada caso es (datos, cortes, profundidad, fallo_temprano).
        Devuelve, por lote, la lista de líneas de resultado."""
        entrada = []
        for automata, casos in lotes:
            entrada.append('automata\n' + serializar(aplanar(automata)) + 'fin\n')
            for d, c, p, f, *extra in casos:
                limite = '-' if not extra or extra[0] is None else extra[0]
                vencido = ' T' if len(extra) > 1 and extra[1] else ''
                entrada.append(f"caso .{d.hex()} {','.join(map(str, c)) or '-'} {p} {int(f)} {limite}{vencido}\n")
        r = subprocess.run([cargo, 'run', '--quiet', '--example', 'dpda'], cwd=VALIDADOR,
                           input=''.join(entrada).encode('ascii'), capture_output=True, check=True)
        lineas = iter(r.stdout.decode('ascii').splitlines())
        salida = [[next(lineas) for _ in casos] for _, casos in lotes]
        assert next(lineas, None) is None
        return salida
    return ejecutar


def esperado(automata, caso):
    """La línea que debe escribir el motor, calculada con el intérprete de referencia."""
    # un caso es (datos, cortes, profundidad, fallo_temprano) y, opcionalmente, (..., max_bytes, plazo_vencido)
    datos, cortes, profundidad, temprano, *extra = caso
    r = validar(automata, datos, cortes, max_profundidad=profundidad, fallo_temprano=temprano,
                max_bytes=extra[0] if extra else None, plazo=0 if len(extra) > 1 and extra[1] else None)
    primera = f'{r.detalle[0][0]} {r.detalle[0][1].replace(" ", "_")}' if r.detalle else '- -'
    error = '- -'
    if r.error:
        error = f'{r.error[0]} ' + next(codigo for texto, codigo in FALLOS.items() if r.error[1].startswith(texto))
    ruta = '-' if r.ruta is None else '.' + r.ruta.encode('utf-8').hex()
    return f'{r.estado} {r.violaciones} {r.pendientes} {r.profundidad} {primera} {error} {ruta}'


def comparar(motor, lotes):
    total = 0
    for (automata, casos), lineas in zip(lotes, motor(lotes)):
        for caso, linea in zip(casos, lineas):
            assert linea == esperado(automata, caso), caso
            total += 1
    return total


def estropear(rng, datos):
    """Un cambio de un byte: casi siempre deja el documento mal formado."""
    m = bytearray(datos)
    i = rng.randint(0, len(m))
    operacion = rng.randrange(3)
    if operacion == 0 and i < len(m):
        del m[i]
    elif operacion == 1:
        m.insert(i, rng.choice(b'{}[],:"\\0x \xff\xc3'))
    else:
        del m[i:]
    return bytes(m)


def casos_de(esquema, rng, n=120):
    casos = []
    for doc in instancias(esquema, n, semilla=rng.randrange(10 ** 6)):
        datos = json.dumps(doc, ensure_ascii=rng.random() < 0.5).encode()
        casos.append((datos, [], 128, False))
        casos.append((datos, particion(rng, len(datos)), 128, False))
        casos.append((datos, [], 128, True))                          # fallo temprano
        casos.append((estropear(rng, datos), particion(rng, len(datos)), 128, rng.random() < 0.3))
        casos.append((datos, [], rng.randint(0, 3), False))           # con la pila muy limitada
    return casos


def test_esquemas_exactos_y_diferidos(motor):
    rng = random.Random(41)
    lotes = [(compilado(esquema).automata, casos_de(esquema, rng)) for esquema in list(EXACTOS.values()) + list(DIFERIDOS.values())]
    assert comparar(motor, lotes) == 30 * 120 * 5


def test_todos_los_estados_aparecen(motor):
    rng = random.Random(43)
    lotes = [(compilado(e).automata, casos_de(e, rng, 60)) for e in (PERSONA, ARBOL, DIFERIDOS['patron'])]
    estados = {linea.split()[0] for lineas in motor(lotes) for linea in lineas}
    assert estados == {'valido', 'pendiente', 'invalido', 'malformado', 'limite'}


def test_documentos_del_corpus_lexico(motor):
    # entradas pensadas para el lexer: UTF-8 inválido, escapes, números límite... con el esquema libre
    libre = compilado({}).automata
    rng = random.Random(47)
    casos = [(d, c, 128, False) for d in ENTRADAS_JSON for c in ([], [1] * len(d), particion(rng, len(d)))]
    assert comparar(motor, [(libre, casos)]) == 3 * len(ENTRADAS_JSON)


def numero_aleatorio(rng):
    """Lexema de un número de JSON con ceros de relleno, decimales y exponentes de todos los tamaños."""
    entero = rng.choice(['0', '1', '5', '10', '99', '100', '123456789012345678901234567890', str(rng.randint(0, 2000))])
    texto = rng.choice(['', '-']) + entero
    if rng.random() < 0.5:
        texto += '.' + rng.choice(['0', '00', '5', '05', '500', '000001', '999999999999999999999', str(rng.randint(0, 999))])
    if rng.random() < 0.5:
        texto += rng.choice('eE') + rng.choice(['', '+', '-']) + rng.choice(
            ['0', '1', '2', '3', '5', '10', '22', '100', '400', '0007', '999999999999', '99999999999999999999999999'])
    return texto.encode()


def test_comparacion_decimal_exacta(motor):
    rng = random.Random(53)
    esquemas = [
        {'type': 'number', 'minimum': 0.1, 'maximum': 100},
        {'type': 'integer', 'exclusiveMinimum': -5, 'exclusiveMaximum': 1000},
        {'enum': [0, 1, 10, 100, 0.5, -99, 1e22]},
        {'type': 'number', 'minimum': -1000000000000000000000000, 'maximum': 123456789012345678901234567890},
        {'allOf': [{'minimum': 5}, {'maximum': 5}], 'type': 'integer'},
        {'type': 'number', 'exclusiveMinimum': 0.000001, 'maximum': 99.5},
        {'const': 0}, {'const': 500}, {'const': -0.05},
    ]
    lotes = []
    for esquema in esquemas:
        casos = [(numero_aleatorio(rng), [], 128, rng.random() < 0.2) for _ in range(600)]
        lotes.append((compilado(esquema).automata, casos))
    lineas = motor(lotes)
    assert comparar(motor, lotes) == 600 * len(esquemas)
    for resultado in lineas:  # cada esquema ve números que cumplen y números que no
        assert {l.split()[0] for l in resultado} == {'valido', 'invalido'}


def test_cadenas_y_tries(motor):
    rng = random.Random(59)
    esquema = {'type': 'object', 'additionalProperties': {'type': 'null'}, 'properties': {
        'a': {'enum': ['', 'a', 'ab', 'abc', 'ñ', 'año', '😀', 'x' * 40]},
        'ab': {'allOf': [{'enum': ['a', 'b', 'c']}, {'enum': ['b', 'c', 'd']}, {'enum': ['c', 'd']}], 'minLength': 1},
        'abc': {'type': 'string', 'minLength': 2, 'maxLength': 3},
        '': {'const': 'vacía'}, 'año': {'const': 'ñ'}, '😀': {'maxLength': 1},
    }}
    alfabeto = ['', 'a', 'b', 'c', 'd', 'ab', 'abc', 'abcd', 'ñ', 'año', 'añ', '😀', '😀😀', 'x' * 40, 'x' * 39, 'vacía']
    casos = []
    for _ in range(1500):
        doc = {rng.choice(alfabeto): rng.choice(alfabeto + [None]) for _ in range(rng.randint(0, 4))}
        datos = json.dumps(doc, ensure_ascii=rng.random() < 0.5).encode()
        casos.append((datos, particion(rng, len(datos)), 128, rng.random() < 0.2))
    assert comparar(motor, [(compilado(esquema).automata, casos)]) == 1500


def test_mascaras_de_varias_palabras(motor):
    # 300 propiedades: la máscara ocupa 5 palabras de 64 bits
    rng = random.Random(61)
    nombres = [f'p{i}' for i in range(300)]
    esquema = {'type': 'object', 'properties': {n: {'type': 'integer'} for n in nombres},
               'required': nombres[::7], 'dependentRequired': {'p1': ['p299', 'p150'], 'p64': ['p63', 'p65']},
               'minProperties': 40, 'maxProperties': 200}
    automata = compilado(esquema).automata
    assert aplanar(automata).palabras == 5
    casos = []
    for _ in range(300):
        elegidas = rng.sample(nombres, rng.randint(0, 300)) + rng.sample(nombres[::7], rng.randint(0, 43))
        doc = {n: rng.choice([1, 1, 1, 'x']) for n in elegidas}
        datos = json.dumps(doc).encode()
        casos.append((datos, particion(rng, min(len(datos), 40)), 128, False))
    casos.append((b'{"p5": 1, "p299": 2, "p5": 3}', [], 128, False))   # clave repetida
    lineas = motor([(automata, casos)])[0]
    assert comparar(motor, [(automata, casos)]) == len(casos)
    assert lineas[-1].startswith('malformado') and lineas[-1].split()[7] == 'clave-repetida'
    assert len({l.split()[1] for l in lineas}) > 50   # muchas cuentas de violaciones distintas


def test_bombeo_y_profundidad(motor):
    automata = compilado(ARBOL).automata
    casos = []
    for k in (1, 10, 63, 64, 100, 127):
        documento = b'{"v": 1, "h": [' * k + b'{"v": 0}' + b']}' * k
        casos += [(documento, [], 128, False), (documento, [7] * 50, 2 * k, False), (documento, [], 2 * k + 1, False)]
    lineas = motor([(automata, casos)])[0]
    assert comparar(motor, [(automata, casos)]) == len(casos)
    assert lineas[0] == 'valido 0 0 3 - - - - -'
    assert lineas[1].startswith('limite') and lineas[2].startswith('valido')
    assert lineas[9].startswith('limite')   # k = 64 pide 129 niveles y el máximo es 128
