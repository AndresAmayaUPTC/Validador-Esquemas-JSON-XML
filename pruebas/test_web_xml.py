"""Pruebas del backend de la aplicación web con esquemas XSD y documentos XML."""
import base64
import io
import struct
import zipfile

import pytest
from fastapi.testclient import TestClient

from web.backend import nativo, sembrar
from web.backend.app import crear_app
from web.backend.repositorio import RepositorioArchivos

XSD, XML = sembrar.PEDIDO_XSD, sembrar.PEDIDO_XML
b64 = lambda datos: base64.b64encode(datos).decode('ascii')


@pytest.fixture()
def cliente(tmp_path):
    return TestClient(crear_app(RepositorioArchivos(tmp_path / 'casos')))


def test_compilar_un_xsd(cliente):
    r = cliente.post('/api/compilar', json={'esquema': XSD}).json()
    assert r['ok'] and r['formato'] == 'xml' and r['diagnosticos'] == [] and r['nivel'] == 'b'
    a = r['automata']
    assert a['tamano']['estados'] <= a['sin_minimizar']['estados'] and a['dot'].startswith('digraph M {')
    assert any(f['simbolo'] == 'elemento "pedido"' for f in a['delta']) and a['palabras_de_mascara'] == 1
    primero = r['simbolos'][0]
    assert (primero['ruta'], primero['recursivo'], primero['linea'], primero['columna'], primero['tipos']) == (
        'elemento pedido', False, 4, 5, ['tipo anónimo'])
    precio = next(s for s in r['simbolos'] if s['ruta'] == 'Precio')
    assert (precio['linea'], precio['columna'], precio['tipos']) == (25, 3, ['texto Importe; @moneda'])
    assert XSD.encode('utf-8')[precio['byte']:].startswith(b'<xs:complexType name="Precio">')
    assert {s['palabra'] for s in r['clasificacion']} >= {'element', 'sequence', 'attribute', 'enumeration'}
    assert cliente.post('/api/compilar', json={'esquema': '{"type": "integer"}'}).json()['formato'] == 'json'
    recursivo = cliente.post('/api/compilar', json={'esquema': sembrar.NODO_XSD}).json()
    assert recursivo['grupos_recursivos'] == 1 and any(x['recursiva'] for x in recursivo['referencias'])
    diferido = cliente.post('/api/compilar', json={'esquema': sembrar.USUARIO_XSD}).json()
    assert [d['palabra'] for d in diferido['automata']['diferidas']] == ['pattern'] and diferido['nivel'] == 'c'


def test_xsd_que_no_compilan(cliente):
    r = cliente.post('/api/compilar', json={'esquema': '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n<xs:element name="a"'}).json()
    assert (r['ok'], r['fase']) == (False, 'léxico') and r['linea'] == 2
    r = cliente.post('/api/compilar', json={'esquema': '<a></b>'}).json()
    assert (r['ok'], r['fase'], r['linea'], r['columna']) == (False, 'sintáctico', 1, 6)
    malo = XSD.replace('type="Estado"', 'type="NoExiste"').replace('<xs:sequence>', '<xs:all>', 1).replace('</xs:sequence>', '</xs:all>', 1)
    r = cliente.post('/api/compilar', json={'esquema': malo}).json()
    assert (r['ok'], r['fase']) == (False, 'semántico')
    assert {d['codigo'] for d in r['diagnosticos']} == {'tipo-desconocido', 'no-soportada'}
    assert all(d['linea'] > 1 for d in r['diagnosticos'])
    assert cliente.post('/api/validar', json={'esquema': malo, 'payload': XML}).status_code == 422


def test_validar_documentos_xml(cliente):
    validar = lambda documento, **extra: cliente.post('/api/validar', json={'esquema': XSD, 'payload': documento, **extra}).json()
    r = validar(XML)
    assert (r['estado'], r['violaciones'], r['profundidad'], r['error']) == ('valido', 0, 3, None)
    r = validar(XML.replace('id="7"', 'id="cero"').replace('<nota>', '<extra/><nota>'))
    assert (r['estado'], r['violaciones'], r['ruta']) == ('invalido', 2, '/pedido/@id')
    assert [(d['linea'], d['motivo']) for d in r['detalle']] == [(2, 'xs_entero'), (12, 'elemento no permitido')]
    assert r['detalle'][0]['mensaje'] == 'el valor no es un entero (xs:integer)'
    assert validar(XML.replace('id="7"', 'id="cero"').replace('1500.50', 'x'), fallo_temprano=True)['violaciones'] == 1
    r = validar('<!DOCTYPE pedido [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + XML.replace('Ana', '&x;'))
    assert r['estado'] == 'malformado' and r['error']['motivo'].startswith('léxico') and r['error']['byte'] == 2
    binario = cliente.post('/api/validar', json={'esquema': XSD, 'payload_b64': b64(XML.encode().replace(b'Ana', b'A\xffna'))}).json()
    assert binario['estado'] == 'malformado'


def test_arbol_de_derivacion_de_xml(cliente):
    r = cliente.post('/api/arbol', json={'texto': '<a x="1">hola<b/></a>'}).json()
    assert r['tokens'] == 14 and r['arbol']['simbolo'] == 'documento' and 'NOMBRE  «a»' in r['texto']
    assert r['dot'].startswith('digraph derivacion {')
    # un XSD es un documento XML: se deriva con la misma gramática, se pida como esquema o no
    corto = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="a"/></xs:schema>'
    assert cliente.post('/api/arbol', json={'texto': corto, 'es_esquema': True}).json()['arbol']['simbolo'] == 'documento'
    r = cliente.post('/api/arbol', json={'texto': '<a></b>'})
    assert r.status_code == 422 and r.json()['fase'] == 'sintáctico'


def test_generar_casos_y_descargar_el_validador(cliente):
    casos = cliente.post('/api/generar', json={'esquema': XSD, 'validos': 3, 'invalidos': 3, 'malformados': 2, 'semilla': 5}).json()
    assert [c['clase'] for c in casos] == ['valido'] * 3 + ['invalido'] * 3 + ['malformado'] * 2
    for c in casos:
        r = cliente.post('/api/validar', json={'esquema': XSD, 'payload_b64': c['payload_b64']}).json()
        assert (r['estado'], r['violaciones']) == (c['estado'], c['violaciones'])
    r = cliente.post('/api/crate', json={'esquema': XSD, 'nombre': 'validador_pedidos'})
    assert r.status_code == 200
    archivos = zipfile.ZipFile(io.BytesIO(r.content))
    nombres = set(archivos.namelist())
    assert {'validador_pedidos/src/lib.rs', 'validador_pedidos/src/tablas.rs', 'validador_pedidos/include/validador.h',
            'validador_pedidos/tests/casos.rs', 'validador_pedidos/motor/src/validador_xml.rs',
            'validador_pedidos/motor/src/xml.rs'} <= nombres
    assert 'ValidadorXml' in archivos.read('validador_pedidos/src/lib.rs').decode('utf-8')


def test_casos_de_xml_por_la_api(cliente):
    cuerpo = {'nombre': 'XXE', 'categoria': 'malformado', 'esquema': XSD,
              'payload_b64': b64(b'<!DOCTYPE p [<!ENTITY x SYSTEM "file:///etc/passwd">]><pedido>&x;</pedido>')}
    id_ = cliente.post('/api/casos', json=cuerpo).json()['_id']
    r = cliente.post(f'/api/casos/{id_}/ejecutar').json()
    assert (r['resultado']['estado'], r['ultimo']['estado']) == ('malformado', 'malformado')
    assert cliente.get(f'/api/casos/{id_}').json()['ultimo']['estado'] == 'malformado'


def test_validacion_en_streaming_de_xml(cliente):
    datos = XML.encode('utf-8')
    with cliente.websocket_connect('/ws/validar') as ws:
        ws.send_json({'tipo': 'inicio', 'esquema': XSD})
        assert ws.receive_json() == {'tipo': 'listo'}
        profundidades = []
        for i in range(0, len(datos), 40):
            ws.send_bytes(datos[i:i + 40])
            progreso = ws.receive_json()
            assert progreso['tipo'] == 'progreso' and progreso['pila'][0] == 'Z0'
            profundidades.append(progreso['profundidad'])
        ws.send_json({'tipo': 'fin'})
        r = ws.receive_json()
    assert (r['tipo'], r['estado'], r['violaciones']) == ('resultado', 'valido', 0) and max(profundidades) == 3
    with cliente.websocket_connect('/ws/validar') as ws:
        ws.send_json({'tipo': 'inicio', 'esquema': XSD})
        ws.receive_json()
        ws.send_bytes(XML.replace('estado="pagado"', 'estado="roto"').encode('utf-8')[:120])
        progreso = ws.receive_json()
        assert progreso['violaciones'] == 1 and progreso['nuevas'][0]['motivo'] == 'enum_cad' and progreso['ruta'].startswith('/pedido')


def test_comparar_xml_con_la_linea_base(cliente):
    if not nativo.disponibilidad()['medicion']:
        pytest.skip('el programa de medición no está disponible')
    documento = '<pedido id="1">' + '<cliente>c</cliente>' + '<linea cantidad="1"><producto>p</producto><precio moneda="COP">1</precio></linea>' * 300 + '</pedido>'
    m = cliente.post('/api/comparar', json={'esquema': XSD, 'payload': documento}).json()
    assert m['bytes'] == len(documento) and m['streaming']['estado'] == 'valido' and m['streaming']['asignaciones'] == 0
    # en XML la línea base solo construye el árbol: no valida contra el XSD
    assert m['dom']['solo_parseo'] is True and m['dom']['estado'] == 'bien formado' and m['dom']['asignaciones'] > 0
    assert m['streaming']['mb_s'] > 0 and m['dom']['mb_s'] > 0
    malo = cliente.post('/api/comparar', json={'esquema': XSD, 'payload': documento.replace('cantidad="1"', 'cantidad="0"', 5)}).json()
    assert (malo['streaming']['estado'], malo['streaming']['violaciones'], malo['dom']['estado']) == ('invalido', 5, 'bien formado')


def test_webassembly_de_un_xsd(cliente):
    if not nativo.disponibilidad()['wasm']:
        pytest.skip('falta cargo o el destino wasm32-unknown-unknown')
    r = cliente.post('/api/wasm', json={'esquema': XSD})
    assert r.status_code == 200 and r.headers['content-type'] == 'application/wasm'
    assert r.content[:4] == b'\0asm' and struct.unpack('<I', r.content[4:8])[0] == 1 and len(r.content) < 200_000
    # el módulo exporta la misma API en C que el de JSON
    assert all(nombre in r.content for nombre in (b'pdv_iniciar', b'pdv_feed', b'pdv_finish', b'pdv_ruta', b'memory'))
