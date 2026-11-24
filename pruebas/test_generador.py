"""Pruebas de la generación de código (Fase 5): payloads, crate de Rust, API en C y WebAssembly."""
import base64
import ctypes
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from compilador.automata.interprete import validar
from compilador.generador.payloads import SIN_DOCUMENTO, Generador, a_json, generar_casos
from compilador.generador.rust import ESTADOS, FALLOS, MOTIVOS, generar_crate
from compilador.generador.tablas import NINGUNO, TOKENS, aplanar, emitir_rust, serializar
from pruebas.test_automata import ARBOL, DIFERIDOS, EXACTOS, PERSONA, RESPALDO, compilado, instancias
from pruebas.test_motor_rust import FALLOS as TEXTO_DE_FALLO

CODIGO = Path(__file__).resolve().parents[1]
DESTINO_DE_COMPILACION = CODIGO.parent / '.cargo-target'   # el mismo del motor: ruta sin tildes


# ---------------------------------------------------------------------------
# Tablas planas
# ---------------------------------------------------------------------------
def test_tablas_planas():
    t = aplanar(compilado(PERSONA).automata)
    assert len(t.delta) == t.n_estados * len(TOKENS) and len(t.trie) == len(t.otra) == t.n_estados
    assert t.palabras == 1 and len(t.mascaras) % t.palabras == 0
    # las transiciones repetidas se guardan una sola vez
    usadas = {x for x in t.delta + t.otra if x != NINGUNO} | {v - 1 for _, _, v in t.trie_nodos if v and v - 1 < len(t.transiciones)}
    assert len(t.transiciones) < sum(len(d) for d in compilado(PERSONA).automata.delta)
    assert usadas <= set(range(len(t.transiciones)))
    # los estados que leen claves tienen trie y «otra clave»; los nombres declarados están en el trie
    claves = [q for q in range(t.n_estados) if t.otra[q] != NINGUNO]
    assert claves and all(t.trie[q] != NINGUNO for q in claves)
    assert t.delta[t.inicial * 12 + TOKENS.index('LLAVE_ABRE')] != NINGUNO
    # sin sub-ejecuciones ni huellas: FIN_HILO y Zh no existen
    assert serializar(t).startswith(f'cabecera {t.n_estados} {t.inicial} {t.acepta} 0 1 {NINGUNO} {NINGUNO} 0\ndelta ')


def test_decimales_normalizados():
    t = aplanar(compilado({'enum': [0, 100, 0.05, -12.5, 1e22, 120.0]}).automata)
    digitos = lambda d: ''.join(map(str, t.digitos[d[2]:d[2] + d[3]]))
    assert [(neg, exp, digitos(d)) for d in t.decimales for neg, exp in [d[:2]]] == [
        (0, 0, ''), (0, 3, '1'), (0, -1, '5'), (1, 2, '125'), (0, 23, '1'), (0, 3, '12')]


def test_codigo_rust_de_las_tablas():
    fuente = emitir_rust(aplanar(compilado(PERSONA).automata), 'persona.json')
    assert fuente.startswith('// Generado por el compilador de esquemas a partir de persona.json.')
    assert 'pub static AUTOMATA: Automata<\'static> = Automata {' in fuente and 'static DELTA: [u32; ' in fuente
    assert 'Transicion { destino: N, desapila: true,' in fuente
    # un esquema sin objetos ni números deja tablas vacías, que también deben ser Rust válido
    assert 'static DEPENDENCIAS: [Dependencia; 0] = [];' in emitir_rust(aplanar(compilado({'type': 'null'}).automata))


# ---------------------------------------------------------------------------
# Generador de payloads
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('nombre', EXACTOS)
def test_casos_generados_contra_el_oraculo(nombre):
    esquema = EXACTOS[nombre]
    oraculo = jsonschema.Draft202012Validator(esquema)
    casos = generar_casos(compilado(esquema), validos=8, invalidos=8, malformados=5, semilla=3)
    por_clase = {clase: [c for c in casos if c.clase == clase] for clase in ('valido', 'invalido', 'malformado')}
    assert len(por_clase['valido']) >= 1 and len(por_clase['malformado']) >= 3
    assert len(por_clase['invalido']) >= (0 if nombre == 'libre' else 3)
    assert len({c.payload for c in casos}) == len(casos)
    for c in por_clase['valido']:
        assert c.estado == 'valido' and c.violaciones == 0
        assert oraculo.is_valid(json.loads(c.payload)), c.payload
    for c in por_clase['invalido']:
        assert c.estado == 'invalido' and c.violaciones >= 1 and ' en ' in c.descripcion
        assert not oraculo.is_valid(json.loads(c.payload)), c.payload
    for c in por_clase['malformado']:
        assert c.estado == 'malformado'
        with pytest.raises(ValueError):   # el módulo json tampoco lo acepta
            json.loads(c.payload.decode('utf-8'))


def test_los_validos_recorren_el_esquema():
    # con bastantes casos, los documentos válidos usan todas las propiedades y los tipos alternativos
    casos = [json.loads(c.payload) for c in generar_casos(compilado(PERSONA), 40, 0, 0, semilla=1)]
    assert len(casos) >= 20
    assert {k for doc in casos for k in doc} == {'edad', 'nombre', 'activo', 'rol'}
    assert {type(doc['rol']).__name__ for doc in casos if 'rol' in doc} == {'str', 'int', 'NoneType'}
    arboles = [json.loads(c.payload) for c in generar_casos(compilado(ARBOL), 40, 0, 0, semilla=1)]
    profundidad = lambda n: 1 + max((profundidad(h) for h in n.get('h', [])), default=0)
    assert max(map(profundidad, arboles)) >= 3   # la recursión se ejercita, y termina


def test_generacion_determinista_y_precisa():
    a = generar_casos(compilado(PERSONA), 5, 5, 3, semilla=9)
    assert a == generar_casos(compilado(PERSONA), 5, 5, 3, semilla=9)
    assert a != generar_casos(compilado(PERSONA), 5, 5, 3, semilla=10)
    # los números se generan como decimales exactos, sin pasar por float
    limites = {'type': 'number', 'minimum': 0.1, 'maximum': 123456789012345678901234567890.5}
    for c in generar_casos(compilado(limites), 10, 0, 0, semilla=2):
        assert validar(compilado(limites).automata, c.payload).estado == 'valido'
    assert a_json({'a': [1, None, True, 'ñ']}) == '{"a": [1, null, true, "ñ"]}'


def test_esquemas_dificiles_para_el_generador():
    imposible = compilado({'type': 'string', 'enum': [1, 2]})        # no admite ningún valor
    casos = generar_casos(imposible, 3, 3, 2, semilla=0)
    assert not [c for c in casos if c.clase == 'valido'] and [c for c in casos if c.clase == 'invalido']
    diferido = generar_casos(compilado(DIFERIDOS['patron_no_regular']), 5, 3, 0, semilla=0)
    assert {c.estado for c in diferido if c.clase == 'valido'} == {'pendiente'}   # el patrón no regular no se comprueba
    # un patrón regular sí: las cadenas válidas salen del propio AFD
    con_patron = generar_casos(compilado(RESPALDO['patron']), 5, 3, 0, semilla=0)
    assert [c.estado for c in con_patron if c.clase == 'valido'] == ['valido'] * 5
    assert Generador(compilado({'minItems': 5000, 'type': 'array'})).documento() is SIN_DOCUMENTO
    assert Generador(compilado({'type': 'null'})).documento() is None   # null es un documento, no una ausencia


# ---------------------------------------------------------------------------
# Crate generado: pruebas nativas, API en C y WebAssembly
# ---------------------------------------------------------------------------
@pytest.fixture(scope='module')
def cargo():
    ejecutable = shutil.which('cargo') or shutil.which('cargo', path=str(Path.home() / '.cargo' / 'bin'))
    if ejecutable is None:
        pytest.skip('cargo no está instalado')
    entorno = dict(os.environ, CARGO_TARGET_DIR=str(DESTINO_DE_COMPILACION))

    def ejecutar(carpeta, *argumentos):
        r = subprocess.run([ejecutable, *argumentos], cwd=carpeta, env=entorno, capture_output=True)
        assert r.returncode == 0, r.stderr.decode('utf-8', 'replace')[-3000:]
        return r.stdout.decode('utf-8', 'replace')
    return ejecutar


# Con sub-ejecuciones (anyOf ambiguo, not, contains, propertyNames), huellas (uniqueItems), patrones y múltiplos.
COMBINADO = {'type': 'object', 'required': ['id'], 'propertyNames': {'maxLength': 5}, 'properties': {
    'lista': RESPALDO['anidados'], 'id': {'type': 'string', 'pattern': '^[a-z]+-[0-9]{2}$'}, 'n': {'multipleOf': 0.25}}}


@pytest.fixture(scope='module')
def crates(tmp_path_factory, cargo):
    """Crates generados para tres esquemas, ya compilados como biblioteca dinámica."""
    raiz = tmp_path_factory.mktemp('generados')
    resultado = {}
    for nombre, esquema in (('persona', PERSONA), ('arbol', ARBOL), ('combinado', COMBINADO)):
        c = compilado(esquema)
        casos = generar_casos(c, 10, 10, 6, semilla=4)
        carpeta = generar_crate(c, raiz / nombre, f'validador_{nombre}', casos=casos, origen=f'{nombre}.json')
        cargo(carpeta, 'build', '--quiet')
        resultado[nombre] = (carpeta, esquema, casos)
    return resultado


def test_archivos_del_crate(crates):
    carpeta, _, casos = crates['persona']
    assert {p.relative_to(carpeta).as_posix() for p in carpeta.rglob('*') if p.is_file()} >= {
        'Cargo.toml', 'src/lib.rs', 'src/tablas.rs', 'include/validador.h', 'tests/casos.rs'}
    cabecera = (carpeta / 'include' / 'validador.h').read_text(encoding='utf-8')
    assert 'PDV_VALIDO = 0, PDV_PENDIENTE = 1, PDV_INVALIDO = 2, PDV_MALFORMADO = 3, PDV_LIMITE = 4' in cabecera
    assert 'PDV_MOTIVO_NO_ADMITIDO = 1' in cabecera and 'PDV_FALLO_LEXICO_BYTE = 101' in cabecera
    assert (carpeta / 'tests' / 'casos.rs').read_text(encoding='utf-8').count('Estado::') >= len(casos)
    # el envoltorio generado no usa nada que reserve memoria
    lib = (carpeta / 'src' / 'lib.rs').read_text(encoding='utf-8')
    assert not any(palabra in lib for palabra in ('Vec', 'Box', 'String', 'alloc', 'format!'))


def test_el_validador_nativo_pasa_sus_casos(crates, cargo):
    for carpeta, _, casos in crates.values():
        salida = cargo(carpeta, 'test', '--quiet')
        assert 'test result: ok. 2 passed' in salida and len(casos) >= 20


class PdvResultado(ctypes.Structure):
    _fields_ = [('estado', ctypes.c_uint32), ('violaciones', ctypes.c_uint32), ('pendientes', ctypes.c_uint32),
                ('profundidad', ctypes.c_uint32), ('posicion', ctypes.c_uint64), ('motivo', ctypes.c_uint32)]


def esperado_c(automata, datos, profundidad=128, temprano=False, max_bytes=None):
    """Lo que debe devolver la API en C, calculado con el intérprete: los campos del resultado y la ruta."""
    r = validar(automata, datos, max_profundidad=profundidad, fallo_temprano=temprano, max_bytes=max_bytes)
    if r.error:
        fallo = next(codigo for texto, codigo in TEXTO_DE_FALLO.items() if r.error[1].startswith(texto))
        posicion, motivo = r.error[0], 101 + FALLOS.index(fallo)
    elif r.detalle:
        posicion, motivo = r.detalle[0][0], 1 + MOTIVOS.index(r.detalle[0][1].replace(' ', '_'))
    else:
        posicion, motivo = -1, 0
    return [ESTADOS.index(r.estado), r.violaciones, r.pendientes, r.profundidad, posicion, motivo, r.ruta or '']


@pytest.mark.skipif(sys.platform != 'win32', reason='la ruta de la biblioteca está escrita para Windows')
def test_api_en_c_desde_python(crates):
    rng = random.Random(7)
    for nombre, (carpeta, esquema, casos) in crates.items():
        pdv = ctypes.CDLL(str(DESTINO_DE_COMPILACION / 'debug' / f'validador_{nombre}.dll'))
        pdv.pdv_tamano.restype = pdv.pdv_alineacion.restype = ctypes.c_size_t
        pdv.pdv_iniciar.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint64]
        pdv.pdv_feed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        pdv.pdv_agotar_tiempo.argtypes = [ctypes.c_void_p]
        pdv.pdv_finish.argtypes = [ctypes.c_void_p, ctypes.POINTER(PdvResultado)]
        pdv.pdv_ruta.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]   # el destino se escribe
        pdv.pdv_ruta.restype = ctypes.c_size_t
        tamano = pdv.pdv_tamano()
        if nombre == 'combinado':
            # 17 ejecuciones × 128 marcos de 24 bytes, 1024 huellas de 8 bytes y una pila de huellas de 128 niveles
            assert 17 * 128 * 24 + 1024 * 8 < tamano < 80_000
        else:
            # el lexer (1 KB), 128 marcos de 24 bytes, el comienzo de la clave de cada nivel y el resultado
            assert 4096 < tamano < 12288 and pdv.pdv_alineacion() == 8
        memoria = (ctypes.c_uint64 * (tamano // 8 + 1))()            # alineada a 8, aportada por el llamador
        automata = compilado(esquema).automata
        documentos = [c.payload for c in casos] + [json.dumps(d).encode() for d in instancias(esquema, 150, semilla=8)]
        for datos in documentos:
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
            largo = pdv.pdv_ruta(memoria, bufer, 256)   # primero se escribe; después se lee el búfer
            ruta = bufer.raw[:largo].decode('utf-8')
            obtenido = [r.estado, r.violaciones, r.pendientes, r.profundidad, posicion, r.motivo, ruta]
            assert obtenido == esperado_c(automata, datos, profundidad, temprano, max_bytes), datos
        # el plazo lo mide quien llama: si se agota entre dos bloques, avisa al validador
        pdv.pdv_iniciar(memoria, 128, 0, 0)
        pdv.pdv_feed(memoria, b'{"v"', 4)
        pdv.pdv_agotar_tiempo(memoria)
        pdv.pdv_feed(memoria, b': 1}', 4)
        r = PdvResultado()
        pdv.pdv_finish(memoria, ctypes.byref(r))
        assert (r.estado, r.posicion, r.motivo) == (ESTADOS.index('limite'), 4, 101 + FALLOS.index('tiempo'))


@pytest.mark.parametrize('nombre', ['arbol', 'combinado'])
def test_webassembly(crates, cargo, tmp_path, nombre):
    node = shutil.which('node')
    instalados = subprocess.run(['rustup', 'target', 'list', '--installed'], capture_output=True, text=True,
                                env=dict(os.environ, PATH=str(Path.home() / '.cargo' / 'bin') + os.pathsep + os.environ['PATH']))
    if node is None or 'wasm32-unknown-unknown' not in instalados.stdout:
        pytest.skip('hace falta node y el destino wasm32-unknown-unknown de Rust')
    carpeta, esquema, casos = crates[nombre]
    cargo(carpeta, 'build', '--quiet', '--release', '--target', 'wasm32-unknown-unknown')
    modulo = DESTINO_DE_COMPILACION / 'wasm32-unknown-unknown' / 'release' / f'validador_{nombre}.wasm'
    assert modulo.stat().st_size < 200_000
    documentos = [c.payload for c in casos] + [json.dumps(d).encode() for d in instancias(esquema, 60, semilla=5)]
    (tmp_path / 'casos.json').write_text(json.dumps([d.hex() for d in documentos]))
    r = subprocess.run([node, str(CODIGO / 'pruebas' / 'wasm_runner.mjs'), str(modulo), str(tmp_path / 'casos.json')],
                       capture_output=True, text=True, check=True)
    automata = compilado(esquema).automata
    assert json.loads(r.stdout) == [esperado_c(automata, d) for d in documentos]


def test_crate_autocontenido(tmp_path):
    carpeta = generar_crate(compilado(PERSONA), tmp_path / 'suelto', 'validador_suelto', autocontenido=True)
    assert 'validador = { path = "motor" }' in (carpeta / 'Cargo.toml').read_text(encoding='utf-8')
    copiados = {p.relative_to(carpeta / 'motor').as_posix() for p in (carpeta / 'motor').rglob('*') if p.is_file()}
    assert copiados == {'Cargo.toml', 'src/lib.rs', 'src/lexer.rs', 'src/tablas_json.rs', 'src/automata.rs',
                        'src/decimal.rs', 'src/huellas.rs', 'src/validador.rs', 'src/xml.rs', 'src/tablas_xml.rs', 'src/validador_xml.rs'}
    assert not (carpeta / 'tests').exists()   # sin casos no se escribe el archivo de pruebas


def test_orden_de_linea(tmp_path):
    esquema = tmp_path / 'e.json'
    esquema.write_bytes(json.dumps(PERSONA).encode())
    orden = [sys.executable, '-m', 'compilador.generador', str(esquema), '--validos', '2', '--invalidos', '2', '--malformados', '1']
    r = subprocess.run(orden, cwd=CODIGO, capture_output=True, check=True)
    lineas = [json.loads(l) for l in r.stdout.decode('utf-8').splitlines()]
    assert [l['clase'] for l in lineas] == ['valido', 'valido', 'invalido', 'invalido', 'malformado']
    assert json.loads(base64.b64decode(lineas[0]['payload_b64']))['edad'] >= 0
    r = subprocess.run(orden + ['--crate', str(tmp_path / 'c'), '--autocontenido'], cwd=CODIGO, capture_output=True, check=True)
    assert 'con 5 casos de prueba' in r.stdout.decode('utf-8') and (tmp_path / 'c' / 'motor' / 'src' / 'validador.rs').exists()
