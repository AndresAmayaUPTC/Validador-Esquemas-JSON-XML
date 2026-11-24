"""Pruebas del validador nativo generado a partir de un XSD: crate de Rust, API en C y WebAssembly."""
import ctypes
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from compilador.formatos import compilar_esquema, es_xml, generar_casos, validar_documento
from compilador.generador.documentos_xml import GeneradorXML
from compilador.generador.rust import ESTADOS, FALLOS, MOTIVOS, generar_crate
from compilador.generador.tablas import SIN_OTRA, TOKENS_XML, aplanar, emitir_rust
from compilador.xsd.interprete import validar_xml
from pruebas.corpus_xsd import ESQUEMAS, PEDIDO
from pruebas.test_generador import CODIGO, DESTINO_DE_COMPILACION, PdvResultado, cargo  # noqa: F401 (cargo es un fixture)
from pruebas.test_motor_xml_rust import FALLOS as TEXTO_DE_FALLO
from pruebas.test_xsd import BUENO, compilado


def test_formato_por_el_primer_caracter():
    assert es_xml(PEDIDO) and es_xml(b'\xef\xbb\xbf \n<a/>') and not es_xml(b' {"type": "object"}') and not es_xml(b'true')
    c = compilar_esquema(PEDIDO)
    assert c.xml and not compilar_esquema(b'{"type": "integer"}').xml
    assert validar_documento(c, BUENO.encode()).estado == 'valido'
    assert validar_documento(compilar_esquema(b'{"type": "integer"}'), b'5', [1]).estado == 'valido'
    assert [x.clase for x in generar_casos(c, 1, 1, 1)] == ['valido', 'invalido', 'malformado']


def test_tablas_planas_de_xml():
    c = compilado('pedido')
    t = aplanar(c.automata)
    assert t.xml and len(t.delta) == t.n_estados * len(TOKENS_XML) and t.palabras == 1
    nombre = TOKENS_XML.index('NOMBRE')
    leen_nombres = [q for q in range(t.n_estados) if t.delta[q * 12 + nombre] != 0xFFFFFFFF]
    # los estados que esperan el nombre de cierre no tienen «cualquier otro nombre»
    assert any(t.delta[q * 12 + nombre] == SIN_OTRA for q in leen_nombres)
    assert all(t.trie[q] != 0xFFFFFFFF for q in leen_nombres)
    codigo = emitir_rust(t, 'pedido.xsd')
    assert 'a partir de pedido.xsd' in codigo and codigo.count('Transicion {') == len(t.transiciones)
    assert not aplanar(compilar_esquema(b'{"type": "integer"}').automata).xml


@pytest.fixture(scope='module')
def crates(tmp_path_factory, cargo):  # noqa: F811
    raiz = tmp_path_factory.mktemp('generados_xml')
    resultado = {}
    for nombre in ('pedido', 'valores', 'patron'):
        c = compilado(nombre)
        casos = generar_casos(c, 10, 10, 6, semilla=4)
        carpeta = generar_crate(c, raiz / nombre, f'validador_xml_{nombre}', casos=casos, origen=f'{nombre}.xsd')
        cargo(carpeta, 'build', '--quiet')
        resultado[nombre] = (carpeta, c, casos)
    return resultado


def test_archivos_del_crate(crates):
    carpeta, _, casos = crates['pedido']
    lib = (carpeta / 'src' / 'lib.rs').read_text(encoding='utf-8')
    assert 'esquema XSD' in lib and "validador::ValidadorXml<'static, PROFUNDIDAD" in lib
    assert not any(palabra in lib for palabra in ('Vec', 'Box', 'String', 'alloc', 'format!'))
    cabecera = (carpeta / 'include' / 'validador.h').read_text(encoding='utf-8')
    assert 'PDV_MOTIVO_ELEMENTO_NO_PERMITIDO = 18' in cabecera and 'PDV_FALLO_ATRIBUTO_REPETIDO = 112' in cabecera
    assert (carpeta / 'tests' / 'casos.rs').read_text(encoding='utf-8').count('Estado::') >= len(casos) == 26


def test_el_validador_nativo_pasa_sus_casos(crates, cargo):  # noqa: F811
    for carpeta, _, _ in crates.values():
        assert 'test result: ok. 2 passed' in cargo(carpeta, 'test', '--quiet')


def esperado_c(automata, datos, profundidad=128, temprano=False, max_bytes=None):
    r = validar_xml(automata, datos, max_profundidad=profundidad, fallo_temprano=temprano, max_bytes=max_bytes)
    if r.error:
        fallo = next(codigo for texto, codigo in TEXTO_DE_FALLO.items() if r.error[1].startswith(texto))
        posicion, motivo = r.error[0], 101 + FALLOS.index(fallo)
    elif r.detalle:
        posicion, motivo = r.detalle[0][0], 1 + MOTIVOS.index(r.detalle[0][1].replace(' ', '_'))
    else:
        posicion, motivo = -1, 0
    return [ESTADOS.index(r.estado), r.violaciones, r.pendientes, r.profundidad, posicion, motivo, r.ruta or '']


def documentos_de(c, casos, n, semilla):
    g = GeneradorXML(c, semilla)
    return [x.payload for x in casos] + [g.documento() if i % 3 == 0 else g.mutante()[0] for i in range(n)]


@pytest.mark.skipif(sys.platform != 'win32', reason='la ruta de la biblioteca está escrita para Windows')
def test_api_en_c_desde_python(crates):
    rng = random.Random(7)
    for nombre, (carpeta, c, casos) in crates.items():
        pdv = ctypes.CDLL(str(DESTINO_DE_COMPILACION / 'debug' / f'validador_xml_{nombre}.dll'))
        pdv.pdv_tamano.restype = pdv.pdv_alineacion.restype = ctypes.c_size_t
        pdv.pdv_iniciar.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint64]
        pdv.pdv_feed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        pdv.pdv_agotar_tiempo.argtypes = [ctypes.c_void_p]
        pdv.pdv_finish.argtypes = [ctypes.c_void_p, ctypes.POINTER(PdvResultado)]
        pdv.pdv_ruta.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        pdv.pdv_ruta.restype = ctypes.c_size_t
        tamano = pdv.pdv_tamano()
        # 128 marcos de 64 bytes, el búfer de un número (256 bytes), el resultado con su ruta y el lexer
        assert 4096 < tamano < 12288 and pdv.pdv_alineacion() == 8
        memoria = (ctypes.c_uint64 * (tamano // 8 + 1))()
        for datos in documentos_de(c, casos, 150, semilla=8):
            profundidad, temprano = rng.choice([128, 128, 2]), rng.random() < 0.2
            max_bytes = rng.choice([None, None, None, rng.randint(1, len(datos))])
            pdv.pdv_iniciar(memoria, profundidad, int(temprano), max_bytes or 0)
            corte = rng.randint(0, len(datos))
            pdv.pdv_feed(memoria, datos[:corte], corte)
            pdv.pdv_feed(memoria, datos[corte:], len(datos) - corte)
            r = PdvResultado()
            pdv.pdv_finish(memoria, ctypes.byref(r))
            posicion = -1 if r.posicion == 2 ** 64 - 1 else r.posicion
            bufer = ctypes.create_string_buffer(256)
            largo = pdv.pdv_ruta(memoria, bufer, 256)
            obtenido = [r.estado, r.violaciones, r.pendientes, r.profundidad, posicion, r.motivo, bufer.raw[:largo].decode('utf-8')]
            assert obtenido == esperado_c(c.automata, datos, profundidad, temprano, max_bytes), datos


def test_webassembly(crates, cargo, tmp_path):  # noqa: F811
    node = shutil.which('node')
    instalados = subprocess.run(['rustup', 'target', 'list', '--installed'], capture_output=True, text=True,
                                env=dict(os.environ, PATH=str(Path.home() / '.cargo' / 'bin') + os.pathsep + os.environ['PATH']))
    if node is None or 'wasm32-unknown-unknown' not in instalados.stdout:
        pytest.skip('hace falta node y el destino wasm32-unknown-unknown de Rust')
    carpeta, c, casos = crates['pedido']
    cargo(carpeta, 'build', '--quiet', '--release', '--target', 'wasm32-unknown-unknown')
    modulo = DESTINO_DE_COMPILACION / 'wasm32-unknown-unknown' / 'release' / 'validador_xml_pedido.wasm'
    assert modulo.stat().st_size < 200_000
    documentos = documentos_de(c, casos, 60, semilla=5)
    (tmp_path / 'casos.json').write_text(json.dumps([d.hex() for d in documentos]))
    r = subprocess.run([node, str(CODIGO / 'pruebas' / 'wasm_runner.mjs'), str(modulo), str(tmp_path / 'casos.json')],
                       capture_output=True, text=True, check=True)
    assert json.loads(r.stdout) == [esperado_c(c.automata, d) for d in documentos]


def test_orden_de_linea(tmp_path):
    esquema = tmp_path / 'e.xsd'
    esquema.write_bytes(ESQUEMAS['arbol'])
    orden = [sys.executable, '-m', 'compilador.generador', str(esquema), '--validos', '2', '--invalidos', '2', '--malformados', '1']
    r = subprocess.run(orden, cwd=CODIGO, capture_output=True, check=True)
    lineas = [json.loads(linea) for linea in r.stdout.decode('utf-8').splitlines()]
    assert [linea['clase'] for linea in lineas] == ['valido', 'valido', 'invalido', 'invalido', 'malformado']
