"""Pruebas de la parte frontal de XML: lexer por bloques con valores, gramática LL(1) y árbol."""
import random
import xml.etree.ElementTree as ET

import pytest

from compilador.lexico.afd import construir_xml
from compilador.lexico.lexer import ErrorLexico, Token
from compilador.lexico.lexer_xml import LexerXML, solo_espacios, tokenizar_xml
from compilador.sintactico.arbol import a_texto, derivar, hojas
from compilador.sintactico.gramaticas import XML
from compilador.sintactico.parser import ErrorSintactico
from compilador.sintactico.xml_dom import Elemento, ErrorXML, analizar_xml
from pruebas.corpus import particion
from pruebas.corpus_xml import ENTRADAS_XML, documento_aleatorio, mutantes_xml

AFD = construir_xml()
TODAS = ENTRADAS_XML + mutantes_xml()


def resumen(resultado):
    tokens, error = resultado
    return tokens, error and (error.pos, error.motivo)


def valores(datos):
    tokens, error = tokenizar_xml(datos)
    assert error is None, error
    return [(t.tipo, t.valor) for t in tokens if t.valor is not None]


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('datos', ENTRADAS_XML, ids=repr)
def test_coincide_con_el_afd(datos):
    """Mismos tokens que el AFD de la sección 4, salvo que los tramos de TEXTO seguidos salen fusionados."""
    tokens, error = tokenizar_xml(datos)
    esperados, pos = AFD.lex(datos)
    if error is not None and error.motivo in ('nombre', 'referencia'):
        return   # comprobaciones sobre el valor: el AFD solo valida la forma
    assert (error and error.pos) == pos
    sin_texto = lambda ts: [t for t in ts if t[0] != 'TEXTO']
    assert sin_texto([(t.tipo, (t.inicio, t.fin)) for t in tokens]) == sin_texto(esperados)[:len(sin_texto([(t.tipo, 0) for t in tokens]))]


def test_valores_de_los_tokens():
    assert valores('<p id="7">Hola &amp; adiós</p>'.encode()) == [
        ('NOMBRE', 'p'), ('NOMBRE', 'id'), ('VALOR_ATRIB', '7'), ('TEXTO', 'Hola & adiós'), ('NOMBRE', 'p')]
    # un solo TEXTO aunque haya comentario, CDATA y referencia en medio
    tokens, _ = tokenizar_xml(b'<r>a<!--c-->b<![CDATA[x]]y]]>&#65;</r>')
    assert [t for t in tokens if t.tipo == 'TEXTO'] == [Token('TEXTO', 3, 34, 'abx]]yA')]
    assert valores(b'<r><![CDATA[a<b&c]]></r>')[1] == ('TEXTO', 'a<b&c')
    assert valores(b'<r><![CDATA[]]]]></r>')[1] == ('TEXTO', ']]')
    assert [t for t, _ in valores(b'<r><![CDATA[]]></r>')] == ['NOMBRE', 'NOMBRE']       # nunca un TEXTO vacío
    assert valores(b'<r>&lt;&gt;&amp;&quot;&apos;&#x41;&#65;&#x1F600;</r>')[1] == ('TEXTO', '<>&"\'AA😀')


def test_normalizacion_de_fines_de_linea_y_atributos():
    assert valores(b'<r>l1\r\nl2\rl3\n</r>')[1] == ('TEXTO', 'l1\nl2\nl3\n')
    assert valores(b'<r><![CDATA[a\r\nb]]></r>')[1] == ('TEXTO', 'a\nb')
    assert valores(b'<r>a&#13;\nb</r>')[1] == ('TEXTO', 'a\r\nb')             # un CR por referencia no se normaliza
    assert valores(b'<r a="x\ty\r\nz\rw\n"/>')[2] == ('VALOR_ATRIB', 'x y z w ')
    assert valores(b'<r a="&#9;&#10;&lt;"/>')[2] == ('VALOR_ATRIB', '\t\n<')    # por referencia quedan como son
    tokens, _ = tokenizar_xml(b'<r> \n\t</r>')
    assert solo_espacios(tokens[3]) and not solo_espacios(tokenizar_xml(b'<r> a </r>')[0][3])


@pytest.mark.parametrize('datos, pos, motivo', [
    (b'<!DOCTYPE r><r/>', 2, 'byte'),
    (b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>', 2, 'byte'),            # XXE
    (b'<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><l>&b;</l>', 2, 'byte'),      # expansión de entidades
    (b'<r><!DOCTYPE r></r>', 5, 'byte'),
    (b'<r>&nbsp;</r>', 4, 'byte'), (b'<r>a & b</r>', 6, 'byte'), (b'<r>a]]>b</r>', 6, 'byte'),
    (b'<r>&#0;</r>', 6, 'referencia'), (b'<r>&#xFFFE;</r>', 10, 'referencia'), (b'<r>&#1114112;</r>', 12, 'referencia'),
    (b'<r>&#xD800;</r>', 10, 'referencia'),
    ('<a×/>'.encode(), 3, 'nombre'), ('<×a/>'.encode(), 2, 'nombre'), ('<a ·b="1"/>'.encode(), 4, 'nombre'),
    (b'<r a="<"/>', 6, 'byte'), (b'<r>\x01</r>', 3, 'byte'), (b'<r>\xef\xbf\xbe</r>', 5, 'byte'),
    (b'<r', 2, 'fin'), (b'<r><![CDATA[x', 13, 'fin'), (b'<r>&amp', 7, 'fin'),
    (b'<' + b'n' * 1025 + b'/>', 1025, 'nombre-largo'),
])
def test_errores_lexicos(datos, pos, motivo):
    assert resumen(tokenizar_xml(datos))[1] == (pos, motivo)


def test_nombres_no_ascii_validos():
    assert valores('<año·1 é="1"/>'.encode())[:2] == [('NOMBRE', 'año·1'), ('NOMBRE', 'é')]   # «·» vale dentro, no al inicio
    assert valores(('<' + 'n' * 1024 + '/>').encode())[0][1] == 'n' * 1024


@pytest.mark.parametrize('tam', [1, 2, 3, 7])
def test_bloques_de_tamano_fijo(tam):
    for datos in TODAS:
        assert resumen(tokenizar_xml(datos, [tam] * (len(datos) // tam + 1))) == resumen(tokenizar_xml(datos)), datos


def test_particiones_aleatorias():
    rng = random.Random(11)
    for datos in TODAS + [documento_aleatorio(rng) for _ in range(200)]:
        for _ in range(2):
            cortes = particion(rng, len(datos))
            assert resumen(tokenizar_xml(datos, cortes)) == resumen(tokenizar_xml(datos)), (datos, cortes)


def test_el_error_se_conserva():
    lexer = LexerXML()
    with pytest.raises(ErrorLexico) as primero:
        lexer.feed(b'<r a="1"><!DOCTYPE')
    assert (primero.value.pos, [t.tipo for t in primero.value.tokens]) == (11, ['ETIQ_ABRE', 'NOMBRE', 'NOMBRE', 'IGUAL', 'VALOR_ATRIB', 'ETIQ_FIN'])
    with pytest.raises(ErrorLexico) as segundo:
        lexer.finish()
    assert segundo.value is primero.value


# ---------------------------------------------------------------------------
# Gramática y árbol
# ---------------------------------------------------------------------------
def test_la_gramatica_de_xml_es_ll1():
    assert XML.conflictos() == []
    assert XML.terminales == sorted(['DECL_XML', 'ETIQ_ABRE', 'ETIQ_CIERRE_ABRE', 'NOMBRE', 'IGUAL', 'VALOR_ATRIB',
                                     'ETIQ_FIN', 'ETIQ_AUTOCIERRE', 'TEXTO', 'FIN'])   # Σ_T de la Tabla 10
    assert XML.primeros['elemento'] == {'ETIQ_ABRE'} and XML.siguientes['atributos'] == {'ETIQ_AUTOCIERRE', 'ETIQ_FIN'}


def test_arbol_de_un_documento():
    fuente = '<pedido id="7">\n  <linea n="1">lápiz</linea>\n  <linea n="2"/>\n</pedido>'.encode()
    r = analizar_xml(fuente)
    assert (r.nombre, r.inicio, r.fin, r.atributo('id')) == ('pedido', 0, len(fuente), '7')
    assert [e.nombre for e in r.elementos()] == ['linea', 'linea'] and r.texto().strip() == ''
    primera, segunda = r.elementos()
    assert (primera.texto(), primera.atributo('n'), segunda.hijos, segunda.atributo('falta', 'x')) == ('lápiz', '1', [], 'x')
    assert fuente[primera.inicio:primera.fin] == '<linea n="1">lápiz</linea>'.encode()
    assert fuente[r.atributos['id'].inicio:r.atributos['id'].fin] == b'id="7"'


@pytest.mark.parametrize('datos, pos, motivo', [
    (b'<a></b>', 5, 'cierre'), (b'<a><b></a></b>', 8, 'cierre'), (b'<r a="1" a="2"/>', 9, 'atributo-repetido'),
    (b'<r/>texto', 4, 'texto-fuera'), (b'texto<r/>', 0, 'texto-fuera'),
    (b'<a>' * 65 + b'</a>' * 65, 192, 'anidamiento'),
])
def test_errores_de_buena_formacion(datos, pos, motivo):
    with pytest.raises(ErrorXML) as e:
        analizar_xml(datos)
    assert (e.value.pos, e.value.motivo) == (pos, motivo)


@pytest.mark.parametrize('datos, pos', [(b'', 0), (b'<r/><r/>', 4), (b'</r>', 0), (b'<r a/>', 4), (b' ', 1)])
def test_errores_sintacticos(datos, pos):
    with pytest.raises((ErrorSintactico, ErrorLexico)) as e:
        analizar_xml(datos)
    assert e.value.pos == pos and not isinstance(e.value, ErrorXML)


def test_arbol_de_derivacion():
    datos = '<año mes="ene">x</año>'.encode()
    arbol = derivar(datos, xml=True)
    assert hojas(arbol) == tokenizar_xml(datos)[0]
    texto = a_texto(arbol, datos).splitlines()
    assert texto[0] == 'documento' and any('NOMBRE  «año»' in linea for linea in texto) and any('prologo' in linea for linea in texto)


# ---------------------------------------------------------------------------
# Oráculo independiente: xml.etree.ElementTree
# ---------------------------------------------------------------------------
def nuestro(e: Elemento):
    hijos = [nuestro(h) if isinstance(h, Elemento) else h.valor for h in e.hijos]
    return e.nombre, {n: a.valor for n, a in e.atributos.items()}, hijos


def de_etree(e):
    hijos = [e.text] if e.text else []
    for h in e:
        hijos.append(de_etree(h))
        if h.tail:
            hijos.append(h.tail)
    return e.tag, dict(e.attrib), hijos


def test_mismo_arbol_que_elementtree():
    rng = random.Random(3)
    for _ in range(600):
        datos = documento_aleatorio(rng)
        assert nuestro(analizar_xml(datos)) == de_etree(ET.fromstring(datos)), datos


def test_acepta_lo_mismo_que_elementtree():
    """Sobre el corpus y sus mutantes (sin DOCTYPE, que aquí se rechaza a propósito), los dos aceptan lo mismo."""
    comparados = aceptados = 0
    for datos in TODAS:
        if b'<!DOCTYPE' in datos or b'<?' in datos or b':' in datos:
            continue   # la declaración y los espacios de nombres tienen reglas propias de cada analizador
        try:
            nuestro_arbol = nuestro(analizar_xml(datos))
        except (ErrorLexico, ErrorSintactico):
            nuestro_arbol = None
        try:
            suyo = de_etree(ET.fromstring(datos))
        except ET.ParseError:
            suyo = None
        comparados += 1
        aceptados += nuestro_arbol is not None
        assert nuestro_arbol == suyo, datos
    assert comparados > 800 and 50 < aceptados < comparados - 300
