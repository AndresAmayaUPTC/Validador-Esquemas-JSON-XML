"""Pruebas de las tablas del lexer de XML y pruebas diferenciales del lexer de Rust contra el de Python.

Se compila validador/examples/lexxml.rs y se le envían todos los casos en un solo proceso.
Si cargo no está instalado, las pruebas diferenciales se omiten.
"""
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from compilador.lexico.afd import construir_xml
from compilador.lexico.lexer_xml import tokenizar_xml
from compilador.lexico.tablas import camel, simular
from compilador.lexico.tablas_xml import CORRIENTES, DESTINO, caminos_rapidos, emitir_rust, tablas_xml
from pruebas.corpus import particion
from pruebas.corpus_xml import ENTRADAS_XML, documento_aleatorio, mutantes_xml

VALIDADOR = Path(__file__).resolve().parents[1] / 'validador'
T = tablas_xml()


def test_dimensiones_de_las_tablas():
    # el AFD de XML tiene 242 estados; con las acciones de valor, el transductor mínimo tiene 260
    assert (len(T.estados), T.n_clases, len(T.acciones)) == (260, 66, 47)
    assert all(len(fila) == 67 for fila in T.delta + T.accion)
    assert T.acciones[0] == (('nada',), False, ())
    puro, estado = caminos_rapidos(T)
    assert all(T.delta[estado][T.clase[b]] == estado and T.accion[estado][T.clase[b]] == puro for b in CORRIENTES)


def test_las_tablas_equivalen_al_afd():
    afd = construir_xml()
    for datos in ENTRADAS_XML + mutantes_xml(por_entrada=5):
        assert simular(T, datos) == afd.lex(datos), datos


def test_el_archivo_de_rust_esta_al_dia():
    assert DESTINO.read_bytes().decode('utf-8') == emitir_rust(T), 'ejecuta: python -m compilador.lexico.tablas_xml'


@pytest.fixture(scope='module')
def lexxml():
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(Path.home() / '.cargo' / 'bin'))
    if cargo is None:
        pytest.skip('cargo no está instalado')
    subprocess.run([cargo, 'build', '--quiet', '--example', 'lexxml'], cwd=VALIDADOR, check=True)

    def ejecutar(casos):
        entrada = ''.join(f"{d.hex()}:{','.join(map(str, c))}\n" for d, c in casos)
        r = subprocess.run([cargo, 'run', '--quiet', '--example', 'lexxml'], cwd=VALIDADOR,
                           input=entrada.encode('ascii'), capture_output=True, check=True)
        lineas = r.stdout.decode('ascii').splitlines()
        assert len(lineas) == len(casos)
        return lineas
    return ejecutar


def esperado(datos, cortes):
    """La misma línea que escribe lexxml, calculada con el lexer de Python."""
    tokens, error = tokenizar_xml(datos, cortes)
    partes = [f'{camel(t.tipo)},{t.inicio},{t.fin}' + ('' if t.valor is None else ',' + t.valor.encode('utf-8').hex())
              for t in tokens]
    partes.append('OK' if error is None else f'ERR,{error.pos},{error.motivo}')
    return ' '.join(partes)


def comparar(lexxml, casos):
    for (datos, cortes), obtenido in zip(casos, lexxml(casos)):
        assert obtenido == esperado(datos, cortes), (datos, cortes)


def test_corpus_en_un_solo_bloque_y_byte_a_byte(lexxml):
    comparar(lexxml, [(d, c) for d in ENTRADAS_XML for c in ([], [1] * len(d))])


def test_mutantes_con_particiones_aleatorias(lexxml):
    rng = random.Random(23)
    casos = [(d, c) for d in ENTRADAS_XML + mutantes_xml()
             for c in ([], [1] * len(d), particion(rng, len(d)), particion(rng, len(d)))]
    assert len(casos) > 5000
    comparar(lexxml, casos)


def test_documentos_aleatorios(lexxml):
    rng = random.Random(41)
    documentos = [documento_aleatorio(rng, 5) for _ in range(400)]
    largo = b'<r a="' + b'valor con espacios y \t tab ' * 40 + b'">' + (b'texto corriente bastante largo ] con & no: &amp; \r\n' * 60) \
        + b'<![CDATA[' + b'a]b]]c' * 50 + b']]></r>'
    comparar(lexxml, [(d, c) for d in documentos + [largo] for c in ([], particion(rng, len(d)), [7] * (len(d) // 7))])
