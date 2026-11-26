"""Pruebas de la conversión de esquemas (JSON Schema ↔ XSD), de la inferencia y de la conversión de documentos.

Que un esquema convertido «signifique lo mismo» se comprueba con documentos: los válidos según el esquema
de origen, convertidos con la convención, deben cumplir el esquema convertido (y los inválidos no, cuando
la conversión es exacta). Los oráculos son independientes: jsonschema y xmlschema.
"""
import json
import random
from decimal import Decimal

import jsonschema
import pytest
import xmlschema
from fastapi.testclient import TestClient

from compilador.automata import expresiones as ex
from compilador.conversion import ErrorDeConversion
from compilador.conversion.__main__ import main
from compilador.conversion.convencion import es_nombre_xml, ocurrencias
from compilador.conversion.documentos import a_texto_json, json_a_xml, xml_a_json
from compilador.conversion.inferir_json import inferir_json
from compilador.conversion.inferir_xsd import inferir_xsd, orden_comun, tipo_de_textos
from compilador.conversion.json_a_xsd import json_a_xsd
from compilador.conversion.patrones import NoTraducible, ecma_a_xsd, xsd_a_ecma
from compilador.conversion.xsd_a_json import leer_xsd, xsd_a_json
from compilador.formatos import compilar_esquema, generar_casos, validar_documento
from pruebas.corpus_xsd import ESQUEMAS, PEDIDO, xsd
from pruebas.generador import generar
from web.backend.app import crear_app
from web.backend.repositorio import RepositorioArchivos


def _cadenas(rng, n=1500):
    alfabeto = list('abcxyz019 .-_@\t\nÀé€😀')
    return [''.join(rng.choice(alfabeto) for _ in range(rng.randint(0, 6))) for _ in range(n)]


def _valido(compilado, datos: bytes) -> bool:
    return validar_documento(compilado, datos).estado == 'valido'


# ---------------------------------------------------------------------------
# Expresiones regulares
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('patron', [r'[A-Z]{2}[0-9]+', 'a|b', '(ab)*c?', r'[a-z-[aeiou]]+', r'\i\c*', '.', '[^a]',
                                    r'\s*x', '(a|bc){2,5}', r'\p{L}+', r'x\.y\*', r'[\-+]?[0-9]{1,3}', ''])
def test_xsd_a_ecma_reconoce_lo_mismo(patron):
    ecma, aproximado = xsd_a_ecma((patron,))
    assert not aproximado
    assert ecma.startswith('^') and ecma.endswith('$')
    for s in _cadenas(random.Random(patron)):
        assert ex.coincide_xsd((patron,), s) == ex.coincide(ecma, s), s


def test_xsd_a_ecma_varias_facetas_y_digitos():
    assert xsd_a_ecma(('a', 'b+')) == ('^(?:a|b+)$', False)
    ecma, aproximado = xsd_a_ecma((r'\d{3}-\d{4}',))
    assert (ecma, aproximado) == (r'^\d{3}-\d{4}$', True)     # \d de XSD incluye otros alfabetos
    assert not ex.coincide(ecma, '١٢٣-٤٥٦٧') and ex.coincide_xsd((r'\d{3}-\d{4}',), '١٢٣-٤٥٦٧')
    with pytest.raises(NoTraducible):
        xsd_a_ecma((r'\p{IsBasicLatin}',))


@pytest.mark.parametrize('patron', ['^a+$', 'abc', '^[a-z]+', 'x$', '^(a|b)$', '^a$|^b$', 'a|^b', '', '^$', '$', '^',
                                    r'\d+\.\d*', r'[\w.-]+@[\w-]+', '.', '^a(?:b|c)?$', r'^\S+$', 'a{2}', r'[^ab]c'])
def test_ecma_a_xsd_reconoce_lo_mismo(patron):
    traducido = ecma_a_xsd(patron)
    for s in _cadenas(random.Random(patron)):
        assert ex.coincide(patron, s) == ex.coincide_xsd((traducido,), s), (s, traducido)


def test_ecma_a_xsd_forma():
    assert ecma_a_xsd('^[a-z]+$') == '[a-z]+'
    assert ecma_a_xsd('^(a|b)$') == '(a|b)'
    assert ecma_a_xsd('abc') == r'[\s\S]*abc[\s\S]*'
    for patron in ('a^b', r'(a)\1', '(?=x)', '[a-'):
        with pytest.raises(NoTraducible):
            ecma_a_xsd(patron)


# ---------------------------------------------------------------------------
# Convención
# ---------------------------------------------------------------------------
def test_ocurrencias_del_modelo_de_contenido():
    tipo = leer_xsd(ESQUEMAS['modelos']).elementos['r']
    cuenta = {n: (mn, mx) for n, (mn, mx, _) in ocurrencias(tipo.particula).items()}
    assert cuenta == {'a': (0, None), 'b': (0, 2), 'c': (0, 2), 'd': (0, 8), 'e': (0, None), 'fin': (1, 1)}


def test_nombres_xml():
    assert es_nombre_xml('linea') and es_nombre_xml('año') and es_nombre_xml('_x-1.2')
    assert not any(es_nombre_xml(n) for n in ('', '1a', 'a b', 'a:b', '@id', '$', 'xmlnombre'))


# ---------------------------------------------------------------------------
# XSD → JSON Schema
# ---------------------------------------------------------------------------
def test_xsd_a_json_del_pedido():
    c = xsd_a_json(PEDIDO)
    esquema = json.loads(c.texto)
    assert esquema['title'] == 'pedido' and esquema['additionalProperties'] is False
    assert esquema['properties']['@id'] == {'type': 'integer', 'minimum': 1}
    assert esquema['properties']['linea'] == {'type': 'array', 'items': {'$ref': '#/$defs/Linea'}, 'minItems': 1}
    assert esquema['required'] == ['@id', 'cliente', 'linea']
    precio = esquema['$defs']['Precio']
    assert precio['properties']['$'] == {'type': 'number', 'minimum': 0, 'exclusiveMaximum': 1000000}
    assert precio['required'] == ['@moneda', '$']
    assert len(c.avisos) == 1 and 'orden' in c.avisos[0].mensaje


@pytest.mark.parametrize('nombre', ESQUEMAS)
def test_xsd_a_json_conserva_los_documentos_validos(nombre):
    """Todo documento válido según el XSD, leído como JSON, cumple el JSON Schema convertido; y casi todos los
    inválidos lo incumplen (los que no, cambian solo el orden de los hijos, que en JSON no existe)."""
    fuente = ESQUEMAS[nombre]
    convertido = xsd_a_json(fuente).texto
    oraculo = jsonschema.Draft202012Validator(json.loads(convertido))
    propio, analisis = compilar_esquema(convertido.encode()), leer_xsd(fuente)
    rechazados = invalidos = 0
    for caso in generar_casos(compilar_esquema(fuente), validos=25, invalidos=25, malformados=0, semilla=5):
        try:
            texto = a_texto_json(xml_a_json(caso.payload, analisis))
        except ErrorDeConversion:
            continue    # la raíz no está declarada
        veredicto = oraculo.is_valid(json.loads(texto))
        assert veredicto == _valido(propio, texto.encode())
        if caso.estado == 'valido':
            assert veredicto, (caso.payload, texto)
        elif caso.estado == 'invalido':
            invalidos += 1
            rechazados += not veredicto
    assert rechazados >= 0.85 * invalidos


def test_xsd_a_json_elecciones():
    fuente = xsd('''
  <xs:element name="r"><xs:complexType><xs:sequence>
    <xs:choice><xs:element name="a" type="xs:string"/><xs:element name="b" type="xs:integer"/></xs:choice>
    <xs:element name="c" type="xs:boolean" minOccurs="0"/>
  </xs:sequence></xs:complexType></xs:element>''')
    oraculo = jsonschema.Draft202012Validator(json.loads(xsd_a_json(fuente).texto))
    assert oraculo.is_valid({'a': 'x'}) and oraculo.is_valid({'b': 1, 'c': True})
    assert not oraculo.is_valid({'a': 'x', 'b': 1})      # dos ramas del choice a la vez
    assert not oraculo.is_valid({'c': False})            # ninguna rama


def test_xsd_a_json_avisa_lo_que_omite():
    c = xsd_a_json(ESQUEMAS['patron'])
    mensajes = ' '.join(a.mensaje for a in c.avisos)
    assert 'pattern sobre xs:integer se omite' in mensajes and '[0-9]' in mensajes
    with pytest.raises(ErrorDeConversion) as e:
        xsd_a_json(xsd('<xs:element name="a" type="Nada"/>'))
    assert any('tipo-desconocido' in x for x in e.value.errores)
    with pytest.raises(ErrorDeConversion):
        xsd_a_json(b'<xs:schema')


# ---------------------------------------------------------------------------
# JSON Schema → XSD
# ---------------------------------------------------------------------------
EXACTOS = {
    'persona': {'title': 'persona', 'type': 'object', 'properties': {
        '@id': {'type': 'integer', 'minimum': 1}, 'nombre': {'type': 'string', 'minLength': 1, 'maxLength': 5},
        'edad': {'type': 'integer', 'minimum': 0, 'maximum': 150}, 'activo': {'type': 'boolean'},
        'rol': {'enum': ['admin', 'user']},
        'tel': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[0-9]{3}$'}, 'maxItems': 2}},
        'required': ['@id', 'nombre'], 'additionalProperties': False},
    'arbol': {'$defs': {'nodo': {'type': 'object', 'required': ['v'], 'additionalProperties': False, 'properties': {
        'v': {'type': 'integer'}, 'h': {'type': 'array', 'items': {'$ref': '#/$defs/nodo'}, 'maxItems': 2}}}},
        '$ref': '#/$defs/nodo'},
    'precio': {'type': 'object', 'properties': {'@moneda': {'enum': ['COP', 'USD']},
                                                '$': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 100}},
               'required': ['@moneda', '$'], 'additionalProperties': False},
    'limites': {'type': 'number', 'exclusiveMinimum': 1, 'exclusiveMaximum': 3},
    'texto': {'type': 'string', 'pattern': 'ab+', 'maxLength': 4},
    'constante': {'type': 'object', 'properties': {'ok': {'const': True}, 'n': {'type': 'integer', 'exclusiveMaximum': 2.5}},
                  'additionalProperties': False},
}


def _con_vacios(v, raiz=True):
    """True si hay {} o [] dentro del documento (o [] en la raíz): la convención no los distingue de "", de un
    elemento vacío ni de una ausencia."""
    if isinstance(v, (dict, list)) and not v and (not raiz or isinstance(v, list)):
        return True
    return any(_con_vacios(h, False) for h in (v.values() if isinstance(v, dict) else v if isinstance(v, list) else []))


@pytest.mark.parametrize('nombre', EXACTOS)
def test_json_a_xsd_exacto_da_el_mismo_veredicto(nombre):
    esquema = EXACTOS[nombre]
    c = json_a_xsd(json.dumps(esquema).encode())
    assert all('orden' in a.mensaje for a in c.avisos), c.avisos
    oraculo_json, oraculo_xml = jsonschema.Draft202012Validator(esquema), xmlschema.XMLSchema(c.texto)
    propio, analisis = compilar_esquema(c.texto.encode()), leer_xsd(c.texto.encode())
    rng, vistos = random.Random(2), {True: 0, False: 0}
    for _ in range(300):
        doc = json.loads(json.dumps(generar(esquema, esquema, rng)))
        if _con_vacios(doc):
            continue
        esperado = oraculo_json.is_valid(doc)
        documento = json_a_xml(doc, analisis)
        assert oraculo_xml.is_valid(documento) == esperado, (doc, documento)
        assert _valido(propio, documento.encode()) == esperado
        vistos[esperado] += 1
    assert vistos[True] > 5 and vistos[False] > 5


def test_json_a_xsd_forma():
    c = json_a_xsd(json.dumps(EXACTOS['arbol']).encode())
    assert '<xs:element name="raiz" type="nodo"/>' in c.texto
    assert '<xs:element name="h" type="nodo" minOccurs="0" maxOccurs="2"/>' in c.texto
    c = json_a_xsd(json.dumps(EXACTOS['constante']).encode())
    assert '<xs:pattern value="true|1"/>' in c.texto and '<xs:maxInclusive value="2"/>' in c.texto
    c = json_a_xsd(json.dumps(EXACTOS['precio']).encode())
    assert '<xs:extension base="Texto">' in c.texto and '<xs:simpleType name="Texto">' in c.texto


@pytest.mark.parametrize('esquema, aviso', [
    ({'anyOf': [{'type': 'string'}, {'type': 'integer'}]}, 'anyOf'),
    ({'type': 'object', 'properties': {'a b': {'type': 'string'}}}, 'no es un nombre de elemento'),
    ({'type': 'object', 'patternProperties': {'^x': {}}, 'additionalProperties': False}, 'propiedades con patrón'),
    ({'type': 'object'}, 'propiedades no declaradas'),
    ({'type': 'number', 'multipleOf': 0.5}, 'multipleOf'),
    ({'type': ['string', 'integer']}, 'tipo unión'),
    ({'type': 'array', 'items': {'type': 'integer'}}, '<elemento>'),
    ({'type': 'string', 'pattern': '(a)\\1'}, 'no se pudo traducir'),
    ({'type': 'object', 'properties': {'a': {'type': ['integer', 'null']}}, 'additionalProperties': False}, 'null'),
])
def test_json_a_xsd_avisa_y_sigue_compilando(esquema, aviso):
    c = json_a_xsd(json.dumps(esquema).encode())
    assert any(aviso in a.mensaje for a in c.avisos), c.avisos
    compilar_esquema(c.texto.encode())


def test_json_a_xsd_recursion_y_errores():
    lista = {'type': 'object', 'required': ['sig'], 'properties': {
        'sig': {'anyOf': [{'$ref': '#'}, {'type': 'null'}]}, 'v': {'type': 'number'}}, 'additionalProperties': False}
    c = json_a_xsd(json.dumps(lista).encode())
    assert '<xs:element name="sig" type="Raiz" minOccurs="0"/>' in c.texto
    ciclo = {'$defs': {'l': {'type': 'array', 'items': {'$ref': '#/$defs/l'}}}, 'type': 'object',
             'properties': {'x': {'$ref': '#/$defs/l'}}, 'additionalProperties': False}
    compilar_esquema(json_a_xsd(json.dumps(ciclo).encode()).texto.encode())
    with pytest.raises(ErrorDeConversion) as e:
        json_a_xsd(b'{"type": "entero"}')
    assert e.value.errores


def test_ida_y_vuelta_xsd_json_xsd():
    """XSD → JSON Schema → XSD: los documentos válidos del original siguen siendo válidos."""
    for nombre in ('pedido', 'valores'):
        vuelta = json_a_xsd(xsd_a_json(ESQUEMAS[nombre]).texto.encode()).texto
        oraculo = xmlschema.XMLSchema(vuelta)
        for caso in generar_casos(compilar_esquema(ESQUEMAS[nombre]), validos=15, invalidos=0, malformados=0, semilla=1):
            assert oraculo.is_valid(caso.payload.decode()), (nombre, caso.payload)


# ---------------------------------------------------------------------------
# Inferencia
# ---------------------------------------------------------------------------
def test_inferir_json_con_genson():
    ejemplos = ['{"id": 1, "nombre": "Ana", "tags": ["a"]}', '{"id": 2.5, "nombre": "Luis", "activo": true}']
    c = inferir_json(ejemplos)
    esquema = json.loads(c.texto)
    assert esquema['$schema'] == 'https://json-schema.org/draft/2020-12/schema'
    assert esquema['required'] == ['id', 'nombre']
    assert esquema['properties']['id'] == {'type': 'number'}
    compilado = compilar_esquema(c.texto.encode())
    assert all(_valido(compilado, e.encode()) for e in ejemplos)
    assert not _valido(compilado, b'{"id": "x", "nombre": "y"}')
    assert inferir_json(['[1, "a", null]']).avisos       # con un ejemplo, se avisa
    with pytest.raises(ErrorDeConversion):
        inferir_json(['{"a": }'])


@pytest.mark.parametrize('nombre', ESQUEMAS)
def test_inferir_xsd_acepta_sus_ejemplos(nombre):
    compilado = compilar_esquema(ESQUEMAS[nombre])
    ejemplos = [c.payload for c in generar_casos(compilado, validos=8, invalidos=0, malformados=0, semilla=3)]
    c = inferir_xsd(ejemplos)
    oraculo, propio = xmlschema.XMLSchema(c.texto), compilar_esquema(c.texto.encode())
    for e in ejemplos:
        assert oraculo.is_valid(e.decode()) and _valido(propio, e)


def test_inferir_xsd_tipos_orden_y_ocurrencias():
    ejemplos = ['<p id="1"><n>Ana</n><x>2</x><x>3.5</x><ok>true</ok></p>', '<p id="7" v=""><n>Luis</n><ok>false</ok></p>']
    texto = inferir_xsd(ejemplos).texto
    assert '<xs:element name="x" type="xs:decimal" minOccurs="0" maxOccurs="unbounded"/>' in texto
    assert '<xs:element name="ok" type="xs:boolean"/>' in texto
    assert '<xs:attribute name="id" type="xs:integer" use="required"/>' in texto
    assert '<xs:attribute name="v" type="xs:string"/>' in texto
    assert tipo_de_textos(['1', '-2']) == 'xs:integer' and tipo_de_textos(['1', '']) == 'xs:string'
    assert orden_comun([['a', 'b'], ['b', 'c'], ['a', 'c']]) == ['a', 'b', 'c']
    assert orden_comun([['a', 'b'], ['b', 'a']]) is None and orden_comun([['a', 'b', 'a']]) is None
    c = inferir_xsd(['<r><a/><b/></r>', '<r><b/><a/></r>'])
    assert '<xs:choice minOccurs="1" maxOccurs="unbounded">' in c.texto and c.avisos


def test_inferir_xsd_limites():
    c = inferir_xsd(['<r>texto <b>1</b> más</r>'])
    assert any('mixto' in a.mensaje for a in c.avisos)
    with pytest.raises(ErrorDeConversion):
        inferir_xsd(['<r xmlns="urn:x"/>'])
    with pytest.raises(ErrorDeConversion):
        inferir_xsd(['<r><a></r>'])
    dos = inferir_xsd(['<a>1</a>', '<b>x</b>']).texto
    assert '<xs:element name="a" type="xs:integer"/>' in dos and '<xs:element name="b" type="xs:string"/>' in dos


# ---------------------------------------------------------------------------
# Documentos
# ---------------------------------------------------------------------------
def test_documento_xml_a_json_y_vuelta():
    analisis = leer_xsd(PEDIDO)
    documento = (b'<pedido id="7" estado="nuevo"><cliente>Ana</cliente><linea cantidad="2"><producto>Lapiz</producto>'
                 b'<precio moneda="COP">1.50</precio></linea></pedido>')
    valor = xml_a_json(documento, analisis)
    assert valor == {'@id': 7, '@estado': 'nuevo', 'cliente': 'Ana', 'linea': [
        {'@cantidad': 2, 'producto': 'Lapiz', 'precio': {'@moneda': 'COP', '$': Decimal('1.50')}}]}
    assert '"$": 1.50' in a_texto_json(valor)
    # al volver, los hijos salen en el orden del XSD aunque el JSON los tenga en otro
    reordenado = {'linea': valor['linea'], 'cliente': 'Ana', '@id': 7}
    xml = json_a_xml(reordenado, analisis)
    assert xml.index('<cliente>') < xml.index('<linea')
    assert xmlschema.XMLSchema(PEDIDO.decode()).is_valid(xml)


def test_documento_con_varias_raices():
    analisis = leer_xsd(ESQUEMAS['arbol'])
    assert xml_a_json(b'<lista><dato>1</dato></lista>', analisis) == {'lista': {'dato': [Decimal(1)]}}
    assert '<lista>' in json_a_xml({'lista': {'dato': [1, 2]}}, analisis)
    with pytest.raises(ErrorDeConversion):
        json_a_xml({'a': 1, 'b': 2}, analisis)
    with pytest.raises(ErrorDeConversion):
        xml_a_json(b'<otro/>', analisis)


# ---------------------------------------------------------------------------
# Línea de órdenes y aplicación web
# ---------------------------------------------------------------------------
def test_linea_de_ordenes(tmp_path, capsys):
    esquema, ejemplo = tmp_path / 'e.xsd', tmp_path / 'd.json'
    esquema.write_bytes(PEDIDO)
    ejemplo.write_text('{"@id": 1, "cliente": "Ana", "linea": [{"@cantidad": 1, "producto": "x", '
                       '"precio": {"@moneda": "COP", "$": 2}}]}', encoding='utf-8')
    assert main(['xsd-a-json', str(esquema)]) == 0
    salida = capsys.readouterr()
    assert json.loads(salida.out)['title'] == 'pedido' and 'aviso' in salida.err
    assert main(['json-a-xml', str(esquema), str(ejemplo)]) == 0
    assert '<precio moneda="COP">2</precio>' in capsys.readouterr().out
    assert main(['inferir', str(ejemplo)]) == 0
    assert '"required"' in capsys.readouterr().out
    malo = tmp_path / 'malo.json'
    malo.write_text('{"type": 3}', encoding='utf-8')
    assert main(['json-a-xsd', str(malo)]) == 1


@pytest.fixture()
def cliente(tmp_path):
    return TestClient(crear_app(RepositorioArchivos(tmp_path / 'casos')))


def test_rutas_de_conversion(cliente):
    r = cliente.post('/api/convertir', json={'esquema': PEDIDO.decode()}).json()
    assert r['origen'] == 'xsd' and r['formato'] == 'json' and json.loads(r['texto'])['title'] == 'pedido'
    r = cliente.post('/api/convertir', json={'esquema': json.dumps(EXACTOS['persona'])}).json()
    assert r['formato'] == 'xsd' and '<xs:element name="persona">' in r['texto']
    r = cliente.post('/api/inferir', json={'documentos': ['<a>1</a>', '<a>2</a>']}).json()
    assert r['formato'] == 'xsd' and 'xs:integer' in r['texto']
    r = cliente.post('/api/inferir', json={'documentos': ['{"a": 1}']}).json()
    assert r['formato'] == 'json' and r['avisos']
    r = cliente.post('/api/convertir-documento', json={'esquema': json.dumps(EXACTOS['persona']),
                                                       'documento': '{"nombre": "Ana", "@id": 3}'}).json()
    assert r['formato'] == 'xml' and '<persona id="3">' in r['texto']
    r = cliente.post('/api/convertir-documento', json={'esquema': PEDIDO.decode(),
                                                       'documento': '<pedido id="1"><cliente>A</cliente></pedido>'}).json()
    assert r['formato'] == 'json' and json.loads(r['texto']) == {'@id': 1, 'cliente': 'A'}


def test_errores_de_conversion_en_la_web(cliente):
    r = cliente.post('/api/convertir', json={'esquema': '{"type": "entero"}'})
    assert r.status_code == 422 and r.json()['errores']
    r = cliente.post('/api/inferir', json={'documentos': ['<a/>', '{}']})
    assert r.status_code == 400
    r = cliente.post('/api/inferir', json={'documentos': []})
    assert r.status_code == 422
    r = cliente.post('/api/convertir-documento', json={'esquema': PEDIDO.decode(), 'documento': '{"a": '})
    assert r.status_code == 422
