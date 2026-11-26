"""Pruebas del backend de la aplicación web (Fase 7): REST, WebSocket, almacén de casos y WebAssembly."""
import base64
import io
import json
import struct
import zipfile

import mongomock
import pytest
from fastapi.testclient import TestClient

from compilador.automata.compilar import compilar
from compilador.automata.interprete import validar
from compilador.formatos import compilar_esquema
from web.backend import nativo, sembrar
from web.backend.app import crear_app
from web.backend.repositorio import CATEGORIAS, RepositorioArchivos, RepositorioMongo

PERSONA = json.dumps(sembrar.PERSONA, indent=2)
ARBOL = json.dumps(sembrar.ARBOL)
b64 = lambda datos: base64.b64encode(datos).decode('ascii')


@pytest.fixture()
def cliente(tmp_path):
    return TestClient(crear_app(RepositorioArchivos(tmp_path / 'casos')))


# ---------------------------------------------------------------------------
# Compilación
# ---------------------------------------------------------------------------
def test_salud_y_ejemplos(cliente):
    salud = cliente.get('/api/salud').json()
    assert salud['ok'] and salud['almacen'] == 'archivos' and salud['casos'] == 0
    assert {'cargo', 'medicion', 'wasm'} <= set(salud)
    ejemplos = cliente.get('/api/ejemplos').json()
    assert [e['nombre'] for e in ejemplos] == list(sembrar.EJEMPLOS) + list(sembrar.EJEMPLOS_XML)
    assert [e['lenguaje'] for e in ejemplos] == ['json'] * 4 + ['xml'] * 3
    for e in ejemplos:   # cada ejemplo compila y su documento no tiene violaciones
        r = cliente.post('/api/validar', json={'esquema': e['esquema'], 'payload': e['payload']}).json()
        assert r['estado'] in ('valido', 'pendiente'), e['nombre']


def test_compilar_un_esquema(cliente):
    r = cliente.post('/api/compilar', json={'esquema': PERSONA}).json()
    assert r['ok'] and r['diagnosticos'] == [] and r['nivel'] == 'b'
    assert r['nivel_texto'].startswith('DPDA con registros acotados')
    a = r['automata']
    assert a['tamano']['estados'] < a['sin_minimizar']['estados'] and a['tamano']['transiciones'] == len(a['delta'])
    assert a['dot'].startswith('digraph M {') and 'M = (Q, Σ, Γ, δ, q0, Z0, F, w)' in a['descripcion']
    assert a['diferidas'] == [] and a['bytes_de_tablas'] > 1000 and not a['delta_truncada']
    assert {'estado', 'simbolo', 'destino', 'pila', 'efecto'} == set(a['delta'][0])
    assert r['simbolos'][0] == {'ruta': '', 'recursivo': False, 'byte': 0, 'linea': 1, 'columna': 1, 'tipos': ['object']}
    assert {'palabra': 'required', 'categoria': 'b', 'estrategia': 'máscara de bits en el marco de pila', 'usos': 1} in r['clasificacion']
    recursivo = cliente.post('/api/compilar', json={'esquema': ARBOL}).json()
    assert recursivo['grupos_recursivos'] == 1 and any(x['recursiva'] for x in recursivo['referencias'])
    diferido = cliente.post('/api/compilar', json={'esquema': json.dumps(sembrar.FUERA_DEL_DPDA)}).json()
    # solo el patrón no regular queda diferido: los demás tienen su estrategia de respaldo
    assert diferido['automata']['diferidas'] == [{'ruta': '/properties/firma', 'palabra': 'pattern'}]
    estrategias = {c['palabra']: c['estrategia'] for c in diferido['clasificacion']}
    assert estrategias['uniqueItems'].startswith('huellas') and 'sub-ejecuciones' in estrategias['anyOf']


@pytest.mark.parametrize('esquema, fase, linea, columna', [
    ('{\n  "type": 01\n}', 'léxico', 2, 12),
    ('{\n  "type": 5\n}', 'sintáctico', 2, 11),
    ('{"minimum": "x"}', 'sintáctico', 1, 13),
    ('', 'sintáctico', 1, 1),
])
def test_esquemas_que_no_compilan(cliente, esquema, fase, linea, columna):
    r = cliente.post('/api/compilar', json={'esquema': esquema})
    assert r.status_code == 200   # un esquema con errores es un resultado, no un fallo de la petición
    assert (r.json()['ok'], r.json()['fase'], r.json()['linea'], r.json()['columna']) == (False, fase, linea, columna)


def test_errores_semanticos_con_posicion(cliente):
    r = cliente.post('/api/compilar', json={'esquema': '{\n  "type": "strin",\n  "$ref": "#"\n}'}).json()
    assert (r['ok'], r['fase']) == (False, 'semántico')
    assert [(d['codigo'], d['linea'], d['columna']) for d in r['diagnosticos']] == [
        ('tipo-desconocido', 2, 11), ('ciclo-de-referencias', 3, 3)]
    # validar con ese esquema devuelve el mismo detalle, con estado 422
    v = cliente.post('/api/validar', json={'esquema': '{"type": "strin"}', 'payload': '1'})
    assert v.status_code == 422 and v.json()['esquema']['fase'] == 'semántico'


def test_limites_de_tamano(cliente):
    enorme = '{"title": "' + 'x' * 300_000 + '"}'
    assert cliente.post('/api/compilar', json={'esquema': enorme}).status_code == 413
    assert cliente.post('/api/validar', json={'esquema': '{}', 'max_profundidad': 0, 'payload': '1'}).status_code == 400
    assert cliente.post('/api/validar', json={'esquema': '{}'}).status_code == 400            # sin documento
    assert cliente.post('/api/validar', json={'esquema': '{}', 'payload_b64': '***'}).status_code == 400


# ---------------------------------------------------------------------------
# Validación, árbol y generación
# ---------------------------------------------------------------------------
def test_validar_documentos(cliente):
    pedir = lambda **campos: cliente.post('/api/validar', json={'esquema': PERSONA, **campos}).json()
    assert pedir(payload='{"nombre": "Ana", "edad": 30}')['estado'] == 'valido'
    r = pedir(payload='{\n  "nombre": "Ana",\n  "edad": 200,\n  "x": 1\n}')
    assert (r['estado'], r['violaciones'], r['ruta']) == ('invalido', 2, '/edad')
    assert r['detalle'][0] == {'byte': 31, 'linea': 3, 'columna': 11, 'motivo': 'max', 'mensaje': 'el número es mayor que el máximo'}
    assert r['detalle'][1]['linea'] == 4
    mal = pedir(payload_b64=b64(b'{"nombre": "A\xffna"}'))                # no es UTF-8: solo cabe en base64
    assert (mal['estado'], mal['error']['byte'], mal['error']['columna']) == ('malformado', 13, 14)
    assert pedir(payload='{"nombre": "", "edad": -1}', fallo_temprano=True)['violaciones'] == 1
    assert pedir(payload='{"etiquetas": [[[1]]]}', max_profundidad=2)['estado'] == 'limite'
    assert pedir(payload='{"nombre": "Ana", "edad": 30}', max_bytes=10)['error']['motivo'] == 'tamaño máximo superado'


def test_arbol_de_derivacion(cliente):
    r = cliente.post('/api/arbol', json={'texto': '{"a": [1, true]}'}).json()
    assert r['tokens'] == 10 and r['arbol']['simbolo'] == 'json' and r['dot'].startswith('digraph derivacion {')
    assert r['texto'].splitlines()[0] == 'json'
    esquema = cliente.post('/api/arbol', json={'texto': '{"type": "string"}', 'es_esquema': True}).json()
    assert esquema['arbol']['simbolo'] == 'documento' and '"type"' in esquema['texto']
    roto = cliente.post('/api/arbol', json={'texto': '{"a": }'})
    assert roto.status_code == 422 and (roto.json()['fase'], roto.json()['columna']) == ('sintáctico', 7)
    largo = cliente.post('/api/arbol', json={'texto': json.dumps(list(range(300)))})
    assert largo.status_code == 413 and '602 tokens' in largo.json()['mensaje']   # 300 números, 299 comas, 2 corchetes y FIN


def test_generar_casos(cliente):
    casos = cliente.post('/api/generar', json={'esquema': PERSONA, 'validos': 3, 'invalidos': 3, 'malformados': 2}).json()
    assert [c['clase'] for c in casos] == ['valido'] * 3 + ['invalido'] * 3 + ['malformado'] * 2
    automata = compilar(PERSONA.encode()).automata
    for c in casos:
        assert validar(automata, base64.b64decode(c['payload_b64'])).estado == c['estado']
    assert cliente.post('/api/generar', json={'esquema': PERSONA, 'validos': 500}).status_code == 400


def test_descargar_el_validador_generado(cliente):
    r = cliente.post('/api/crate', json={'esquema': PERSONA, 'nombre': 'validador_persona'})
    assert r.status_code == 200 and r.headers['content-type'] == 'application/zip'
    assert 'validador_persona.zip' in r.headers['content-disposition']
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        nombres = set(z.namelist())
        assert {'validador_persona/Cargo.toml', 'validador_persona/src/lib.rs', 'validador_persona/src/tablas.rs',
                'validador_persona/include/validador.h', 'validador_persona/tests/casos.rs',
                'validador_persona/motor/src/validador.rs'} <= nombres
        assert 'validador = { path = "motor" }' in z.read('validador_persona/Cargo.toml').decode()
    for nombre in ('../x', 'Con Mayusculas', 'año', ''):
        assert cliente.post('/api/crate', json={'esquema': PERSONA, 'nombre': nombre}).status_code == 400


# ---------------------------------------------------------------------------
# Casos de prueba en la base de datos
# ---------------------------------------------------------------------------
@pytest.fixture(params=['archivos', 'mongodb'])
def repositorio(request, tmp_path):
    if request.param == 'archivos':
        return RepositorioArchivos(tmp_path / 'casos')
    return RepositorioMongo(mongomock.MongoClient()['validador']['casos'])


def test_almacen_de_casos(repositorio):
    assert repositorio.listar() == []
    id_ = repositorio.guardar({'nombre': 'b', 'categoria': 'invalido', 'esquema': '{}', 'payload_b64': ''})
    repositorio.guardar({'_id': 'fijo1', 'nombre': 'a', 'categoria': 'valido', 'esquema': '{}', 'payload_b64': ''})
    assert [c['nombre'] for c in repositorio.listar()] == ['a', 'b']           # por categoría y luego por nombre
    assert [c['_id'] for c in repositorio.listar('invalido')] == [id_]
    repositorio.guardar(repositorio.obtener('fijo1') | {'notas': 'cambiada'})   # mismo _id: reemplaza
    assert repositorio.obtener('fijo1')['notas'] == 'cambiada' and len(repositorio.listar()) == 2
    assert repositorio.borrar('fijo1') and not repositorio.borrar('fijo1')
    assert repositorio.obtener('fijo1') is None and repositorio.obtener('../../etc') is None


def test_sembrar_los_casos_de_la_propuesta(repositorio):
    assert len(sembrar.CASOS) == 26 and len(sembrar.CASOS_XML) == 23 and len(sembrar.CASOS_SUSTENTACION) == 19
    # un caso sembrado con un nombre que ya no existe y otro creado a mano por la API
    repositorio.guardar({'_id': sembrar.identificador('Patrón sin verificar'), 'nombre': 'Patrón sin verificar'})
    repositorio.guardar({'_id': 'amano', 'nombre': 'Creado por la API', 'categoria': 'valido'})
    assert sembrar.sembrar(repositorio) == (68, 1)
    assert repositorio.obtener('amano') is not None   # lo que no es de la siembra no se toca
    repositorio.borrar('amano')
    casos = repositorio.listar()
    assert {c['categoria'] for c in casos} == set(CATEGORIAS)
    estados = {c['nombre']: (c['esperado']['estado'], c['esperado']['violaciones']) for c in casos}
    for c in casos:
        if c['categoria'] in ('valido', 'invalido', 'malformado'):
            assert c['esperado']['estado'] == c['categoria'], c['nombre']
    assert estados['Falta la edad'] == ('invalido', 1) and estados['Varias violaciones'] == ('invalido', 5)
    assert estados['Patrón que no casa'] == estados['anyOf ambiguo'] == estados['uniqueItems con repetidos'] == ('invalido', 1)
    assert estados['Patrón no regular'] == ('pendiente', 0)
    assert estados['Árbol con k = 60'] == ('valido', 0) and estados['Arreglo de 20 000 números'] == ('valido', 0)
    assert estados['Profundidad sobre el límite'][0] == 'limite' and estados['Números enormes'] == ('invalido', 1)
    repositorio.guardar(casos[0] | {'ultimo': {'estado': 'valido'}})
    assert sembrar.sembrar(repositorio) == (68, 0)                 # sembrar otra vez no duplica ni borra métricas
    assert len(repositorio.listar()) == 68 and repositorio.obtener(casos[0]['_id'])['ultimo'] == {'estado': 'valido'}
    # los de XML: las mismas categorías, y los ataques con DOCTYPE se rechazan como mal formados en el byte 2
    de_xml = [c for c in casos if c['lenguaje'] == 'xml']
    assert len(de_xml) == 29 and {c['categoria'] for c in de_xml} == set(CATEGORIAS)
    assert estados['XML: DOCTYPE con entidad externa (XXE)'] == estados['XML: expansión de entidades'] == ('malformado', 0)
    assert estados['XML: varias violaciones'] == ('invalido', 8) and estados['XML: pattern que no casa'] == ('invalido', 1) \
        and estados['XML: pattern no regular'] == ('pendiente', 0)
    assert estados['XML: profundidad sobre el límite'][0] == 'limite' and estados['XML: decimales enormes'] == ('valido', 0)
    # los de la sustentación: autómatas pequeños y un veredicto de cada clase
    sustentacion = {c['nombre']: c for c in casos if c['categoria'] == 'sustentacion'}
    assert len(sustentacion) == 19 and casos[0]['categoria'] == 'sustentacion'   # se listan primero
    assert {estados[n][0] for n in sustentacion} == {'valido', 'invalido', 'malformado'}
    assert all(compilar_esquema(c['esquema'].encode()).tamano['estados'] <= 22 for c in sustentacion.values())
    assert estados['Lista sin cerrar'] == estados['Matriz con un ] de más'] == estados['XML: cierre que no coincide'] == ('malformado', 0)
    assert estados['Punto sin y'] == estados['XML: edad 300'] == ('invalido', 1) and estados['Matriz 2 × 2'] == ('valido', 0)


def test_casos_por_la_api(cliente):
    nuevo = {'nombre': 'Edad negativa', 'categoria': 'invalido', 'esquema': PERSONA,
             'payload_b64': b64(b'{"nombre": "Ana", "edad": -1}'), 'esperado': {'estado': 'invalido', 'violaciones': 1}}
    creado = cliente.post('/api/casos', json=nuevo)
    assert creado.status_code == 201
    id_ = creado.json()['_id']
    (resumen,) = cliente.get('/api/casos').json()
    assert resumen['nombre'] == 'Edad negativa' and resumen['bytes'] == 29 and 'esquema' not in resumen
    assert cliente.get(f'/api/casos/{id_}').json()['esquema'] == PERSONA
    assert cliente.get('/api/casos', params={'categoria': 'valido'}).json() == []

    r = cliente.post(f'/api/casos/{id_}/ejecutar').json()
    assert (r['resultado']['estado'], r['ultimo']['coincide'], r['ultimo']['ruta']) == ('invalido', True, '/edad')
    guardado = cliente.get(f'/api/casos/{id_}').json()['ultimo']
    assert guardado['estado'] == 'invalido' and 'metricas' in guardado and guardado['fecha'].endswith('+00:00')

    assert cliente.put(f'/api/casos/{id_}', json=nuevo | {'notas': 'revisado'}).status_code == 200
    assert cliente.get(f'/api/casos/{id_}').json()['notas'] == 'revisado'
    assert cliente.post('/api/casos', json=nuevo | {'categoria': 'otra'}).status_code == 400
    assert cliente.post('/api/casos', json=nuevo | {'payload_b64': '***'}).status_code == 400
    borrado = cliente.delete(f'/api/casos/{id_}')
    # sin cuerpo y sin tipo de contenido: el navegador no debe intentar leer un JSON vacío
    assert borrado.status_code == 204 and borrado.content == b'' and 'content-type' not in borrado.headers
    for respuesta in (cliente.get(f'/api/casos/{id_}'), cliente.delete(f'/api/casos/{id_}'),
                      cliente.put(f'/api/casos/{id_}', json=nuevo), cliente.post(f'/api/casos/{id_}/ejecutar')):
        assert respuesta.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
def test_validacion_en_streaming(cliente):
    documento = '{"nombre": "Ana", "edad": 200, "etiquetas": ["uno", "demasiado largo para caber"]}'.encode()
    esperado = cliente.post('/api/validar', json={'esquema': PERSONA, 'payload': documento.decode()}).json()
    with cliente.websocket_connect('/ws/validar') as ws:
        ws.send_json({'tipo': 'inicio', 'esquema': PERSONA})
        assert ws.receive_json() == {'tipo': 'listo'}
        progresos = []
        for i in range(0, len(documento), 7):
            ws.send_bytes(documento[i:i + 7])
            progresos.append(ws.receive_json())
        ws.send_json({'tipo': 'fin'})
        final = ws.receive_json()
    pasos_finales = final.pop('pasos')
    assert final == {'tipo': 'resultado', **esperado}
    # el recorrido: cada transición sale de donde terminó la anterior, termina en ACEPTA, y el estado que informa
    # cada progreso es el de su última transición (un bloque puede traer varias, o ninguna)
    pasos = [x for p in progresos for x in p['pasos']] + pasos_finales
    assert pasos[0][0] == 'V1' and pasos[-1] == ['FIN', 'ACEPTA'] and pasos_finales[-1] == ['FIN', 'ACEPTA']
    assert all(a[1] == b[0] for a, b in zip(pasos, pasos[1:]))
    assert all(p['pasos'][-1][1] == p['estado_del_automata'] for p in progresos if p['pasos'])
    assert any(len(p['pasos']) > 1 for p in progresos)
    assert [p['bytes'] for p in progresos] == [min(i + 7, len(documento)) for i in range(0, len(documento), 7)]
    assert [p['violaciones'] for p in progresos] == sorted(p['violaciones'] for p in progresos)   # solo crecen
    assert sum(len(p['nuevas']) for p in progresos) == esperado['violaciones'] == 2
    en_el_arreglo = next(p for p in progresos if p['profundidad'] == 2)
    assert en_el_arreglo['ruta'].startswith('/etiquetas') and len(en_el_arreglo['pila']) == 3
    assert progresos[-1]['profundidad'] == 0 and max(p['profundidad_maxima'] for p in progresos) == 2


def test_streaming_con_un_esquema_que_no_compila(cliente):
    with cliente.websocket_connect('/ws/validar') as ws:
        ws.send_json({'tipo': 'inicio', 'esquema': '{"type": 5}'})
        error = ws.receive_json()
    assert error['tipo'] == 'error' and error['esquema']['fase'] == 'sintáctico'


# ---------------------------------------------------------------------------
# Código nativo: medición y WebAssembly
# ---------------------------------------------------------------------------
def test_comparar_con_la_linea_base(cliente):
    documento = json.dumps([{'nombre': f'P{i}', 'edad': i % 90} for i in range(200)])
    esquema = json.dumps({'type': 'array', 'items': sembrar.PERSONA})
    r = cliente.post('/api/comparar', json={'esquema': esquema, 'payload': documento})
    if r.status_code == 503:
        pytest.skip(r.json()['mensaje'])
    m = r.json()
    assert m['bytes'] == len(documento) and m['streaming']['estado'] == m['dom']['estado'] == 'valido'
    assert m['streaming']['asignaciones'] == 0 and m['dom']['asignaciones'] > 0 and m['streaming']['mb_s'] > 0

    with cliente.websocket_connect('/ws/comparar') as ws:
        ws.send_json({'esquema': esquema, 'payload': documento, 'rondas': 3})
        mensajes = [ws.receive_json() for _ in range(4)]
    assert [x['tipo'] for x in mensajes] == ['medicion', 'medicion', 'medicion', 'fin']
    assert [x['ronda'] for x in mensajes[:3]] == [1, 2, 3] and all(x['streaming']['asignaciones'] == 0 for x in mensajes[:3])

    ancho = json.dumps({'properties': {f'p{i}': {'type': 'null'} for i in range(300)}})
    assert cliente.post('/api/comparar', json={'esquema': ancho, 'payload': '{}'}).status_code == 422


def test_webassembly_en_un_sandbox(cliente):
    """El módulo se ejecuta aquí con wasmtime, igual que en el navegador: sin acceso a nada del servidor."""
    wasmtime = pytest.importorskip('wasmtime')
    r = cliente.post('/api/wasm', json={'esquema': PERSONA})
    if r.status_code == 503:
        pytest.skip(r.json()['mensaje'])
    assert r.headers['content-type'] == 'application/wasm' and r.content[:4] == b'\0asm'
    assert cliente.post('/api/wasm', json={'esquema': PERSONA}).content == r.content        # sale de la caché

    almacen = wasmtime.Store()
    instancia = wasmtime.Instance(almacen, wasmtime.Module(almacen.engine, r.content), [])   # sin importaciones
    pdv = instancia.exports(almacen)
    memoria = pdv['memory']
    tamano = pdv['pdv_tamano'](almacen)
    base = memoria.data_len(almacen)
    memoria.grow(almacen, 2)
    validador, resultado, datos = base, base + (tamano + 7) // 8 * 8, base + (tamano + 7) // 8 * 8 + 64
    automata = compilar(PERSONA.encode()).automata
    for documento in (b'{"nombre": "Ana", "edad": 30}', b'{"nombre": "", "edad": 200, "x": []}', b'{"nombre": "Ana",}', b'[1]'):
        pdv['pdv_iniciar'](almacen, validador, 128, 0, 0)
        memoria.write(almacen, documento, datos)
        pdv['pdv_feed'](almacen, validador, datos, len(documento))
        pdv['pdv_finish'](almacen, validador, resultado)
        estado, violaciones, _, profundidad = struct.unpack('<4I', memoria.read(almacen, resultado, resultado + 16))
        esperado = validar(automata, documento)
        assert (('valido', 'pendiente', 'invalido', 'malformado', 'limite')[estado], violaciones, profundidad) == \
            (esperado.estado, esperado.violaciones, esperado.profundidad), documento


def test_el_frontend_se_sirve_desde_la_misma_direccion(cliente):
    r = cliente.get('/')
    assert r.status_code == 200 and '<html' in r.text.lower()
    assert cliente.get('/api/docs').status_code == 200
