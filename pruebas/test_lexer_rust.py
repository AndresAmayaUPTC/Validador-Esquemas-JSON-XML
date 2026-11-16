"""Pruebas diferenciales: el lexer de Rust debe dar exactamente lo mismo que el de Python.

Se compila validador/examples/lexjson.rs y se le envían todos los casos en un solo proceso.
Si cargo no está instalado, estas pruebas se omiten.
"""
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from compilador.lexico.lexer import tokenizar
from compilador.lexico.tablas import camel
from pruebas.corpus import ENTRADAS_JSON, mutantes, particion

VALIDADOR = Path(__file__).resolve().parents[1] / 'validador'


@pytest.fixture(scope='module')
def lexjson():
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(Path.home() / '.cargo' / 'bin'))
    if cargo is None:
        pytest.skip('cargo no está instalado')
    orden = [cargo, 'run', '--quiet', '--example', 'lexjson']
    subprocess.run([cargo, 'build', '--quiet', '--example', 'lexjson'], cwd=VALIDADOR, check=True)

    def ejecutar(casos):
        """casos: lista de (datos, cortes). Devuelve una línea de resultado por caso."""
        entrada = ''.join(f"{d.hex()}:{','.join(map(str, c))}\n" for d, c in casos)
        r = subprocess.run(orden, cwd=VALIDADOR, input=entrada.encode('ascii'), capture_output=True, check=True)
        lineas = r.stdout.decode('ascii').splitlines()
        assert len(lineas) == len(casos)
        return lineas
    return ejecutar


def esperado(datos, cortes):
    """La misma línea que escribe lexjson, calculada con el lexer de Python."""
    tokens, error = tokenizar(datos, cortes)
    partes = []
    for t in tokens:
        parte = f'{camel(t.tipo)},{t.inicio},{t.fin}'
        if t.tipo == 'CADENA':
            parte += ',' + t.valor.encode('utf-8').hex()
        elif t.tipo == 'NUMERO':
            parte += f",{t.valor.encode('ascii').hex()},{'e' if t.es_entero else 'r'}"
        partes.append(parte)
    partes.append('OK' if error is None else f'ERR,{error.pos},{error.motivo}')
    return ' '.join(partes)


def comparar(lexjson, casos):
    for (datos, cortes), obtenido in zip(casos, lexjson(casos)):
        assert obtenido == esperado(datos, cortes), (datos, cortes)


def test_corpus_en_un_solo_bloque(lexjson):
    comparar(lexjson, [(d, []) for d in ENTRADAS_JSON])


def test_corpus_byte_a_byte(lexjson):
    comparar(lexjson, [(d, [1] * len(d)) for d in ENTRADAS_JSON])


def test_mutantes_con_particiones_aleatorias(lexjson):
    rng = random.Random(23)
    casos = [(d, c) for d in ENTRADAS_JSON + mutantes()
             for c in ([], [1] * len(d), particion(rng, len(d)), particion(rng, len(d)))]
    assert len(casos) > 8000
    comparar(lexjson, casos)


def test_la_particion_no_cambia_el_resultado(lexjson):
    rng = random.Random(31)
    for d in ENTRADAS_JSON:
        casos = [(d, [])] + [(d, particion(rng, len(d))) for _ in range(5)]
        assert len(set(lexjson(casos))) == 1, d
