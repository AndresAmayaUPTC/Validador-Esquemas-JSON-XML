"""Pruebas diferenciales del motor de Rust de XML contra el intérprete de Python.

Los dos ejecutan el mismo autómata: uno desde los objetos de Python y el otro desde las tablas planas.
Deben coincidir en todo: estado, violaciones, comprobaciones diferidas, profundidad, posición y motivo de
la primera violación, posición y causa del error, y ruta. Si cargo no está instalado, se omiten.
"""
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from compilador.generador.documentos_xml import GeneradorXML
from compilador.generador.tablas import aplanar, serializar
from compilador.xsd.compilar import compilar_xsd
from compilador.xsd.interprete import NUMERO_MAX, validar_xml
from pruebas.corpus import particion
from pruebas.corpus_xml import ENTRADAS_XML, documento_aleatorio, mutantes_xml
from pruebas.corpus_xsd import ESQUEMAS, xsd
from pruebas.test_xsd import BUENO, compilado

VALIDADOR = Path(__file__).resolve().parents[1] / 'validador'
FALLOS = {'léxico: byte': 'lexico-byte', 'léxico: fin': 'lexico-fin', 'léxico: carácter': 'lexico-nombre',
          'léxico: referencia': 'lexico-referencia', 'léxico: nombre': 'lexico-nombre-largo',
          'sintáctico': 'sintactico', 'atributo repetido': 'atributo-repetido', 'número': 'numero-largo',
          'profundidad': 'profundidad', 'tamaño': 'tamano', 'tiempo': 'tiempo'}


@pytest.fixture(scope='module')
def motor():
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(Path.home() / '.cargo' / 'bin'))
    if cargo is None:
        pytest.skip('cargo no está instalado')
    subprocess.run([cargo, 'build', '--quiet', '--example', 'dpda'], cwd=VALIDADOR, check=True)

    def ejecutar(lotes):
        """lotes: lista de (autómata, casos); un caso es (datos, cortes, profundidad, fallo_temprano[, max_bytes[, vencido]])."""
        entrada = []
        for automata, casos in lotes:
            entrada.append('automata xml\n' + serializar(aplanar(automata)) + 'fin\n')
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
    datos, cortes, profundidad, temprano, *extra = caso
    r = validar_xml(automata, datos, cortes, max_profundidad=profundidad, fallo_temprano=temprano,
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
    m = bytearray(datos)
    i = rng.randint(0, len(m))
    operacion = rng.randrange(3)
    if operacion == 0 and i < len(m):
        del m[i]
    elif operacion == 1:
        m.insert(i, rng.choice(b'<>/="\'&;]x \xff\xc3'))
    else:
        del m[i:]
    return bytes(m)


def casos_de(nombre, rng, n=60):
    g, casos = GeneradorXML(compilado(nombre), semilla=rng.randrange(10 ** 6)), []
    for i in range(n):
        datos = g.documento() if i % 3 == 0 else g.mutante()[0]
        casos.append((datos, [], 128, False))
        casos.append((datos, particion(rng, len(datos)), 128, False))
        casos.append((datos, [1] * len(datos), 128, True))               # fallo temprano, byte a byte
        casos.append((estropear(rng, datos), particion(rng, len(datos)), 128, rng.random() < 0.3))
        casos.append((datos, [], rng.randint(0, 3), False))              # con la pila muy limitada
        casos.append((datos, [], 128, False, rng.randint(0, len(datos))))  # con el tamaño limitado
    return casos


def test_todos_los_esquemas(motor):
    rng = random.Random(41)
    lotes = [(compilado(nombre).automata, casos_de(nombre, rng)) for nombre in sorted(ESQUEMAS)]
    assert comparar(motor, lotes) == len(ESQUEMAS) * 60 * 6
    estados = {linea.split()[0] for lineas in motor(lotes) for linea in lineas}
    assert estados == {'valido', 'pendiente', 'invalido', 'malformado', 'limite'}


def test_documentos_del_corpus_lexico(motor):
    # entradas pensadas para el lexer, con un esquema que admite cualquier contenido en <r> y con el de pedidos
    libre = compilar_xsd(xsd('<xs:element name="r"/><xs:element name="a" type="xs:string"/>')).automata
    rng = random.Random(47)
    entradas = ENTRADAS_XML + mutantes_xml(por_entrada=6) + [documento_aleatorio(rng) for _ in range(150)]
    casos = [(d, c, 128, False) for d in entradas for c in ([], [1] * len(d), particion(rng, len(d)))]
    assert comparar(motor, [(libre, casos), (compilado('pedido').automata, casos[::3])]) == len(casos) + len(casos[::3])


def numero_aleatorio(rng):
    """Texto que casi siempre es un número de XSD: signos, ceros de relleno, punto sin dígitos, espacios..."""
    entero = rng.choice(['', '0', '1', '5', '10', '99', '100', '000', '123456789012345678901234567890', str(rng.randint(0, 2000))])
    texto = rng.choice(['', '', '-', '+']) + entero
    if rng.random() < 0.5:
        texto += '.' + rng.choice(['', '0', '00', '5', '05', '500', '000001', '999999999999999999999', str(rng.randint(0, 999))])
    if rng.random() < 0.08:
        texto += rng.choice(['e3', ' 1', 'x', ',5', '..', '-'])
    return rng.choice(['', '', ' ', '\n\t ']) + texto + rng.choice(['', '', ' ', '\r\n'])


def test_comparacion_decimal_y_formas_lexicas(motor):
    rng = random.Random(53)
    tipo = lambda base, facetas: (f'<xs:simpleType name="T"><xs:restriction base="xs:{base}">{facetas}</xs:restriction></xs:simpleType>'
                                  '<xs:element name="n"><xs:complexType><xs:simpleContent><xs:extension base="T">'
                                  '<xs:attribute name="a" type="T"/></xs:extension></xs:simpleContent></xs:complexType></xs:element>')
    esquemas = [
        tipo('decimal', '<xs:minInclusive value="0.1"/><xs:maxInclusive value="100"/>'),
        tipo('integer', '<xs:minExclusive value="-5"/><xs:maxExclusive value="1000"/>'),
        tipo('decimal', ''.join(f'<xs:enumeration value="{v}"/>' for v in ('0', '1', '10', '100', '0.5', '-99', '+007.50'))),
        tipo('decimal', '<xs:minExclusive value="-0.000001"/><xs:maxExclusive value="123456789012345678901234567890.5"/>'),
        tipo('integer', ''), tipo('decimal', ''), tipo('boolean', ''), tipo('unsignedLong', ''),
    ]
    lotes = []
    for cuerpo in esquemas:
        casos = []
        for _ in range(250):
            n, a = numero_aleatorio(rng), numero_aleatorio(rng)
            if rng.random() < 0.1:
                n = rng.choice(['true', 'false', ' 1 ', '0', 'True', 'truee'])
            datos = f'<n a="{a}">{n}</n>'.encode()
            casos.append((datos, particion(rng, len(datos)), 128, rng.random() < 0.2))
        lotes.append((compilar_xsd(xsd(cuerpo)).automata, casos))
    assert comparar(motor, lotes) == 8 * 250
    estados = {linea.split()[0] for lineas in motor(lotes) for linea in lineas}
    assert estados == {'valido', 'invalido'}


def test_limites_y_casos_borde(motor):
    pedido = compilado('pedido').automata
    arbol = compilado('arbol').automata
    hondo = lambda n: ('<arbol>' + '<valor>1</valor><hijo>' * n + '<valor>x</valor>' + '</hijo>' * n + '</arbol>').encode()
    largo = b'9' * (NUMERO_MAX + 1)
    nombre = 'ñ' * 30
    casos_arbol = [
        (hondo(100), [], 128, False), (hondo(200), [], 128, False), (hondo(200), [], 256, False), (hondo(300), [], 256, False),
        (hondo(60), [7] * 200, 128, False),                                   # ruta recortada
        (hondo(5), [], 128, False, None, True),                               # plazo vencido
        (b'<arbol><valor>' + largo + b'</valor></arbol>', [], 128, False),
        (b'<arbol><valor>' + largo[:-1] + b'</valor></arbol>', [], 128, False),
        (b'<arbol><valor>x' + largo + b'</valor></arbol>', [], 128, False),
        (b'<arbol><valor> ' + largo + b' 1</valor></arbol>', [], 128, False),
        (b'<lista><dato>' + largo + b'.' + largo + b'</dato></lista>', [], 128, False),
        (f'<{nombre} {nombre}="1"><{nombre}/></{nombre}>'.encode(), [3] * 40, 128, False),
        (f'<arbol {nombre}="1" etiqueta="a" etiqueta="b"/>'.encode(), [], 128, False),
    ]
    casos_pedido = [(d.encode(), c, 128, t) for d in [
        BUENO, BUENO.replace('</cliente>', '</client>'), BUENO.replace('<linea', '<x><y></x></y><linea'),
        BUENO.replace('<linea', '<x><yy></yy></x><linea'), BUENO.replace('id="7"', 'id="7" id="8"'),
        BUENO.replace('<pedido', '<pedido xmlns:xsi="u" xsi:noNamespaceSchemaLocation="p.xsd" xmlns="v"'),
        BUENO.replace('Ana', 'l1\r\nl2&#13;&#10;<![CDATA[ ]]> <!-- c --> fin'), '<!DOCTYPE pedido>' + BUENO, '', ' ', '<pedido/>',
        BUENO + BUENO, BUENO[:40], '﻿' + BUENO, '<?xml version="1.0"?>\n' + BUENO + '\n<!-- fin -->\n',
    ] for c in ([], [1] * len(d.encode()), [5] * (len(d.encode()) // 5)) for t in (False, True)]
    assert comparar(motor, [(arbol, casos_arbol), (pedido, casos_pedido)]) == len(casos_arbol) + len(casos_pedido)
