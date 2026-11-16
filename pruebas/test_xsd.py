"""Pruebas de XSD: análisis semántico, modelos de contenido, autómata de XML e intérprete de referencia.
El oráculo independiente es la biblioteca xmlschema."""
import io
import json
import random
import re

import pytest
import xmlschema

from compilador.automata.compilar import ErrorDeEsquema, a_dot, describir, filas_delta
from compilador.automata.verificacion import verificar
from compilador.generador.documentos_xml import GeneradorXML, generar_casos_xml
from compilador.lexico.lexer import ErrorLexico
from compilador.sintactico.parser import ErrorSintactico
from compilador.xsd.automata import construir_xml
from compilador.xsd.compilar import compilar_xsd
from compilador.xsd.contenido import ModeloDemasiadoGrande, construir_afd
from compilador.xsd.interprete import NUMERO_MAX, ValidadorXML, validar_xml
from compilador.xsd.modelo import Particula, Tipo, analizar_xsd
from pruebas.corpus import particion
from pruebas.corpus_xsd import ESQUEMAS, PEDIDO, xsd

COMPILADOS = {}


def compilado(nombre):
    if nombre not in COMPILADOS:
        COMPILADOS[nombre] = compilar_xsd(ESQUEMAS[nombre])
    return COMPILADOS[nombre]


def correr(esquema: bytes, documento: str, **opciones):
    return validar_xml(compilar_xsd(esquema).automata, documento.encode('utf-8'), **opciones)


def pedido(documento, **opciones):
    return validar_xml(compilado('pedido').automata, documento.encode('utf-8'), **opciones)


BUENO = ('<pedido id="7" estado="pagado"><cliente>Ana</cliente>'
         '<linea cantidad="2"><producto>Lápiz</producto><precio moneda="COP">1500.50</precio></linea></pedido>')


# ---------------------------------------------------------------------------
# Modelos de contenido: Glushkov → subconjuntos → Moore
# ---------------------------------------------------------------------------
def _acepta(afd, palabra):
    q = 0
    for nombre in palabra:
        if nombre not in afd.delta[q]:
            return False
        q = afd.delta[q][nombre]
    return afd.acepta[q]


def _regex(p):
    cuerpo = p.nombre if p.clase == 'elemento' else \
        ('' if p.clase == 'secuencia' else '|').join(_regex(h) for h in p.hijos)
    return f'(?:{cuerpo}){{{p.minimo},{"" if p.maximo is None else p.maximo}}}'


def _particula_al_azar(rng, tipo, prof=0):
    minimo = rng.choice([0, 1, 1, 2])
    maximo = rng.choice([None, minimo, minimo + 1, minimo + 2]) if rng.random() < 0.8 else max(minimo, 1)
    if prof == 3 or rng.random() < 0.4:
        return Particula('elemento', minimo, maximo, nombre=rng.choice('abc'), tipo=tipo)
    return Particula(rng.choice(['secuencia', 'opcion']), minimo, maximo,
                     [_particula_al_azar(rng, tipo, prof + 1) for _ in range(rng.randint(0, 3))])


def test_el_afd_del_modelo_de_contenido_reconoce_el_mismo_lenguaje_que_la_expresion():
    rng, tipo = random.Random(5), Tipo('t', contenido='simple')
    for _ in range(150):
        p = _particula_al_azar(rng, tipo)
        afd, patron = construir_afd(p), re.compile(_regex(p) + r'\Z')
        for _ in range(60):
            palabra = ''.join(rng.choice('abc') for _ in range(rng.randint(0, 7)))
            assert _acepta(afd, palabra) == bool(patron.match(palabra)), (_regex(p), palabra)


def test_el_afd_es_minimo_y_esta_acotado():
    tipo = Tipo('t', contenido='simple')
    a = lambda minimo, maximo: Particula('elemento', minimo, maximo, nombre='a', tipo=tipo)
    assert len(construir_afd(a(0, None)).acepta) == 1                     # a*
    assert len(construir_afd(a(3, 5)).acepta) == 6                        # a{3,5}
    assert len(construir_afd(Particula('opcion', 1, 1, [a(1, 1), a(1, 1)])).acepta) == 2   # a|a = a
    assert construir_afd(None).vacio() and construir_afd(Particula('secuencia')).vacio()
    with pytest.raises(ModeloDemasiadoGrande):
        construir_afd(a(0, 5000))


# ---------------------------------------------------------------------------
# Análisis semántico
# ---------------------------------------------------------------------------
def test_analisis_del_esquema_de_pedidos():
    an = analizar_xsd(PEDIDO)
    assert an.ok and an.nivel == 'b' and list(an.elementos) == ['pedido'] and an.grupos == 0
    linea = next(t for t in an.tipos if t.nombre == 'Linea')
    assert [(n, a.requerido) for n, a in linea.atributos.items()] == [('cantidad', True)]
    precio = next(t for t in an.tipos if t.nombre == 'Precio')
    assert precio.contenido == 'simple' and precio.simple.base == 'decimal' and len(precio.simple.pruebas) == 2
    assert {u.categoria for u in an.usos} == {'a', 'b'}
    assert 'NIVEL: DPDA con registros acotados' in an.informe(PEDIDO)


def test_recursion():
    an = analizar_xsd(ESQUEMAS['arbol'])
    assert an.ok and an.grupos == 2 and len(an.recursivos) == 2
    assert sorted(destino for _, destino, recursiva in an.referencias if recursiva) == ['Nodo', 'Nodo', 'lista']


@pytest.mark.parametrize('cuerpo, codigo', [
    ('<xs:element name="a" type="Falta"/>', 'tipo-desconocido'),
    ('<xs:element name="a" type="xs:date"/>', 'no-soportada'),
    ('<xs:element name="a"><xs:complexType><xs:all><xs:element name="b"/></xs:all></xs:complexType></xs:element>', 'no-soportada'),
    ('<xs:element name="a"><xs:complexType mixed="true"><xs:sequence/></xs:complexType></xs:element>', 'no-soportada'),
    ('<xs:element name="a" default="x" type="xs:string"/>', 'no-soportada'),
    ('<xs:element name="a"><xs:complexType><xs:sequence><xs:any/></xs:sequence></xs:complexType></xs:element>', 'no-soportada'),
    ('<xs:import namespace="urn:x"/><xs:element name="a"/>', 'no-soportada'),
    ('<xs:element name="a"/><xs:element name="a"/>', 'nombre-repetido'),
    ('<xs:complexType name="T"/>', 'sin-elementos'),
    ('<xs:element name="a" type="T"/><xs:complexType name="T"><xs:sequence><xs:element name="b" type="T"/></xs:sequence></xs:complexType>',
     'recursion-sin-caso-base'),
    ('<xs:element name="a"><xs:complexType><xs:sequence><xs:element name="b" type="xs:int" minOccurs="3" maxOccurs="2"/>'
     '</xs:sequence></xs:complexType></xs:element>', 'valor-invalido'),
    ('<xs:element name="a"><xs:complexType><xs:sequence><xs:element name="b" type="xs:int"/><xs:element name="b" type="xs:string"/>'
     '</xs:sequence></xs:complexType></xs:element>', 'elemento-inconsistente'),
    ('<xs:element name="a"><xs:complexType><xs:sequence><xs:element name="b" type="xs:int" maxOccurs="5000"/>'
     '</xs:sequence></xs:complexType></xs:element>', 'modelo-demasiado-grande'),
    ('<xs:element name="a"><xs:simpleType><xs:restriction base="xs:string"><xs:minInclusive value="1"/></xs:restriction>'
     '</xs:simpleType></xs:element>', 'faceta-no-aplicable'),
    ('<xs:element name="a"><xs:simpleType><xs:restriction base="xs:integer"><xs:maxInclusive value="1.5"/></xs:restriction>'
     '</xs:simpleType></xs:element>', 'valor-invalido'),
    ('<xs:element name="a" type="A"/><xs:simpleType name="A"><xs:restriction base="B"/></xs:simpleType>'
     '<xs:simpleType name="B"><xs:restriction base="A"/></xs:simpleType>', 'ciclo-de-tipos'),
    ('<xs:element name="a"><xs:complexType><xs:attribute name="x"/><xs:attribute name="x"/></xs:complexType></xs:element>', 'nombre-repetido'),
    ('<xs:element name="a"><xs:complexType><xs:sequence><xs:element ref="nada"/></xs:sequence></xs:complexType></xs:element>',
     'elemento-desconocido'),
    ('<xs:element name="a"><xs:complexType><xs:attribute name="x" type="p:t"/></xs:complexType></xs:element>', 'prefijo-desconocido'),
])
def test_errores_del_esquema(cuerpo, codigo):
    an = analizar_xsd(xsd(cuerpo))
    assert codigo in {d.codigo for d in an.errores}, [(d.codigo, d.mensaje) for d in an.diagnosticos]
    assert all(0 <= d.inicio <= d.fin for d in an.diagnosticos)
    with pytest.raises(ErrorDeEsquema):
        compilar_xsd(xsd(cuerpo))


def test_esquemas_que_no_son_xsd():
    assert [d.codigo for d in analizar_xsd(b'<esquema/>').errores] == ['no-es-xsd']
    con_espacio = b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:x"><xs:element name="a"/></xs:schema>'
    assert [d.codigo for d in analizar_xsd(con_espacio).errores] == ['no-soportada']
    with pytest.raises(ErrorLexico):
        compilar_xsd(b'<!DOCTYPE x><xs:schema/>')
    with pytest.raises(ErrorSintactico):
        compilar_xsd(b'<a></b>')


def test_esquema_sin_prefijo():
    fuente = b'<schema xmlns="http://www.w3.org/2001/XMLSchema"><element name="n" type="int"/></schema>'
    a = compilar_xsd(fuente).automata
    assert validar_xml(a, b'<n>5</n>').estado == 'valido' and validar_xml(a, b'<n>x</n>').estado == 'invalido'


# ---------------------------------------------------------------------------
# Autómata
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('nombre', sorted(ESQUEMAS))
def test_el_automata_es_un_dpda_valido(nombre):
    c = compilado(nombre)
    bruto = construir_xml(c.analisis)
    assert verificar(bruto).ok and c.verificacion.ok
    assert c.tamano['estados'] <= c.sin_minimizar['estados']
    assert describir(c).startswith('M = (Q, Σ, Γ, δ, q0, Z0, F, w)') and a_dot(c).startswith('digraph M {')
    assert len(filas_delta(c)) == c.tamano['transiciones']
    json.dumps(c.automata.a_dict())


def test_la_minimizacion_fusiona_estados():
    c = compilar_xsd(xsd('<xs:element name="r"><xs:complexType><xs:sequence>'
                         + ''.join(f'<xs:element name="e{i}" type="xs:int"/>' for i in range(8))
                         + '</xs:sequence></xs:complexType></xs:element>'))
    # los estados que esperan «=» y el valor de un atributo no declarado son iguales en todos los elementos;
    # los demás se distinguen por el nombre de la etiqueta de cierre
    assert c.tamano['estados'] < c.sin_minimizar['estados']


# ---------------------------------------------------------------------------
# Intérprete: veredictos, pesos y rutas
# ---------------------------------------------------------------------------
def test_documento_valido():
    r = pedido(BUENO)
    assert (r.estado, r.violaciones, r.pendientes, r.profundidad, r.ruta) == ('valido', 0, 0, 3, None)
    assert pedido('<?xml version="1.0"?>\n<!-- c -->' + BUENO.replace('><', '>\n  <') + '\n').estado == 'valido'


@pytest.mark.parametrize('documento, violaciones, motivo, ruta', [
    (BUENO.replace(' id="7"', ''), 1, 'atributo requerido', '/pedido'),
    (BUENO.replace('id="7"', 'id="0"'), 1, 'min', '/pedido/@id'),
    (BUENO.replace('id="7"', 'id="x"'), 1, 'xs_entero', '/pedido/@id'),
    (BUENO.replace('estado="pagado"', 'estado="roto"'), 1, 'enum_cad', '/pedido/@estado'),
    (BUENO.replace('estado=', 'otro="1" estado='), 1, 'atributo no declarado', '/pedido/@otro'),
    (BUENO.replace('<cliente>Ana</cliente>', ''), 2, 'elemento no permitido', '/pedido/linea[1]'),
    (BUENO.replace('</pedido>', '<nota>n</nota><nota>m</nota></pedido>'), 1, 'elemento no permitido', '/pedido/nota[4]'),
    (BUENO.replace('<linea', 'texto<linea'), 1, 'texto no permitido', '/pedido'),
    (BUENO.replace('1500.50', '-1'), 1, 'min', '/pedido/linea[2]/precio[2]'),
    (BUENO.replace('1500.50', '1e3'), 1, 'xs_decimal', '/pedido/linea[2]/precio[2]'),
    (BUENO.replace('1500.50', '1000000'), 1, 'max', '/pedido/linea[2]/precio[2]'),
    (BUENO.replace('Lápiz', ''), 1, 'contenido incompleto', '/pedido/linea[2]/producto[1]'),
    (BUENO.replace('Lápiz', 'x' * 41), 1, 'long_max', '/pedido/linea[2]/producto[1]'),
    (BUENO.replace('Lápiz', 'a<b/>c'), 1, 'elemento no permitido', '/pedido/linea[2]/producto[1]/b[1]'),
    (BUENO.replace('<precio moneda="COP">1500.50</precio>', ''), 1, 'contenido incompleto', '/pedido/linea[2]'),
    (BUENO.replace('pedido', 'otro'), 1, 'elemento no permitido', '/otro'),
    ('<pedido/>', 2, 'atributo requerido', '/pedido'),
])
def test_violaciones(documento, violaciones, motivo, ruta):
    r = pedido(documento)
    assert (r.estado, r.violaciones, r.detalle[0][1], r.ruta) == ('invalido', violaciones, motivo, ruta), r


def test_el_peso_es_la_suma_de_las_violaciones():
    malo = ('<pedido estado="x" sobra="1"><cliente>c</cliente><linea cantidad="-2" otro="3"><producto></producto><precio>abc</precio><z/></linea>'
            'texto</pedido>')
    r = pedido(malo)
    assert r.violaciones == len(r.detalle) == 10 and r.estado == 'invalido'
    assert pedido(malo, fallo_temprano=True).violaciones == 1


@pytest.mark.parametrize('documento, motivo', [
    ('<!DOCTYPE pedido>' + BUENO, 'léxico'),
    ('<!DOCTYPE pedido [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + BUENO.replace('Ana', '&x;'), 'léxico'),
    ('<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>' + BUENO.replace('Ana', '&b;'), 'léxico'),
    (BUENO.replace('Ana', '&nbsp;'), 'léxico'),
    (BUENO.replace('</cliente>', '</client>'), 'cierre'),
    (BUENO.replace('</pedido>', '</Pedido>'), 'cierre'),
    (BUENO.replace('<linea', '<x><y></x></y><linea'), 'cierre'),          # en elementos que el esquema no conoce
    (BUENO.replace('id="7"', 'id="7" id="8"'), 'atributo repetido'),
    (BUENO + '<otro/>', 'no se esperaba'),
    (BUENO[:-9], 'no se esperaba'),
    ('texto' + BUENO, 'no se esperaba'),
    ('', 'no se esperaba'),
    (BUENO.replace('Ana', '\x01'), 'léxico'),
])
def test_documentos_mal_formados(documento, motivo):
    r = pedido(documento)
    assert r.estado == 'malformado' and motivo in r.error[1], r


def test_elementos_desconocidos_se_saltan_enteros():
    r = pedido(BUENO.replace('<linea', '<x a="1" a2="2"><y>t<z/></y></x><linea', 1))
    assert (r.estado, r.violaciones, r.ruta) == ('invalido', 1, '/pedido/x[2]')


def test_atributos_de_espacios_de_nombres_no_cuentan():
    con = BUENO.replace('<pedido', '<pedido xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="p.xsd"')
    assert pedido(con).estado == 'valido'
    assert pedido(BUENO.replace('<pedido', '<pedido xmlns="urn:otro"')).estado == 'invalido'


def test_tipos_simples():
    a = compilado('valores').automata
    ficha = lambda **cambios: ('<ficha id="{id}" color="{color}"><codigo>{codigo}</codigo><nota>{nota}</nota><dado>{dado}</dado>'
                               '<color>{c2}</color><activo>{activo}</activo><octeto>{octeto}</octeto><grande>{grande}</grande>'
                               '<resumen>{resumen}</resumen></ficha>').format(**{
                                   'id': '1', 'color': 'rojo', 'codigo': 'ABCD', 'nota': '4.5', 'dado': '6', 'c2': '', 'activo': 'true',
                                   'octeto': '255', 'grande': '-9223372036854775808', 'resumen': 'ok', **cambios}).encode()
    violaciones = lambda **cambios: validar_xml(a, ficha(**cambios)).violaciones
    assert validar_xml(a, ficha()).estado == 'valido'
    for cambio, esperado in [
        ({'codigo': 'ABC'}, 1), ({'codigo': 'ñ€😀x'}, 0), ({'codigo': 'ABCDE'}, 1),             # length cuenta caracteres
        ({'nota': '0'}, 1), ({'nota': '0.0001'}, 0), ({'nota': '5.0'}, 0), ({'nota': '5.00001'}, 1), ({'nota': ' +05. '}, 0),
        ({'nota': '.5'}, 0), ({'nota': '5,0'}, 1), ({'nota': ''}, 1),
        ({'dado': '06'}, 0), ({'dado': '+6'}, 0), ({'dado': '7'}, 1), ({'dado': '6.0'}, 1), ({'dado': ' 3\n'}, 0),
        ({'c2': 'azul claro'}, 0), ({'c2': 'azul  claro'}, 1), ({'c2': ' rojo'}, 1), ({'color': ''}, 0), ({'color': 'gris'}, 1),
        ({'activo': '1'}, 0), ({'activo': ' false '}, 0), ({'activo': 'TRUE'}, 1), ({'activo': ''}, 1),
        ({'octeto': '256'}, 1), ({'octeto': '-1'}, 1), ({'octeto': '-0'}, 0),
        ({'grande': '9223372036854775807'}, 0), ({'grande': '9223372036854775808'}, 1),
        ({'resumen': 'a'}, 1), ({'resumen': 'a' * 11}, 1), ({'resumen': '&lt;&amp;'}, 0),
        ({'id': '-1'}, 1), ({'id': '1.0'}, 1),
    ]:
        assert violaciones(**cambio) == esperado, cambio


def test_contenido_vacio_libre_y_simple_con_atributos():
    a = compilado('mixtos').automata
    base = '<registro><medida unidad="m">1.5</medida>{}<vacio{}</registro>'
    estado = lambda medio='', vacio='/>': validar_xml(a, base.format(medio, vacio).encode()).estado
    assert estado() == 'valido'
    assert estado(vacio='></vacio>') == 'valido' and estado(vacio=' marca="1"/>') == 'valido'
    assert estado(vacio='> </vacio>') == 'invalido'          # el contenido vacío no admite ni espacios
    assert estado('<extra a="1"><lo><que/>sea</lo>texto</extra>') == 'valido'            # sin tipo: anyType
    assert estado('<cualquiera><x></y></cualquiera>') == 'malformado'
    assert estado('<fechada unidad="s" anio="2026">3</fechada>') == 'valido'
    assert estado('<fechada anio="2026">3</fechada>') == 'invalido'                       # el atributo heredado es obligatorio
    assert validar_xml(a, b'<nota>cualquier texto</nota>').estado == 'valido'             # otro elemento global como raíz
    assert validar_xml(a, b'<nota><b/></nota>').estado == 'invalido'


def test_pattern_queda_pendiente():
    c = compilado('patron')
    assert c.automata.diferidas == [('/element[usuario]/complexType/sequence/element[correo]/simpleType/restriction/pattern', 'pattern')]
    assert [d.codigo for d in c.analisis.avisos] == ['diferida'] and c.analisis.nivel == 'c'
    r = validar_xml(c.automata, b'<usuario><correo>a@b</correo><edad>30</edad></usuario>')
    assert (r.estado, r.pendientes, r.violaciones) == ('pendiente', 1, 0)
    assert validar_xml(c.automata, b'<usuario><correo>a@b</correo><edad>300</edad></usuario>').estado == 'invalido'


def test_limites():
    c = compilado('arbol')
    hondo = lambda n: ('<arbol>' + '<valor>1</valor><hijo>' * n + '<valor>1</valor>' + '</hijo>' * n + '</arbol>').encode()
    assert validar_xml(c.automata, hondo(100)).profundidad == 102   # la raíz, 100 hijos y el último <valor>
    r = validar_xml(c.automata, hondo(200))
    assert (r.estado, r.error[1]) == ('limite', 'profundidad máxima superada')
    assert validar_xml(c.automata, hondo(200), max_profundidad=300).estado == 'valido'
    r = validar_xml(c.automata, hondo(50), max_bytes=100)
    assert (r.estado, r.error) == ('limite', (100, 'tamaño máximo superado'))
    assert validar_xml(c.automata, hondo(5), plazo=0).error[1] == 'tiempo máximo superado'
    # un número larguísimo solo detiene la validación si hay que compararlo con algo
    largo = b'9' * (NUMERO_MAX + 1)
    assert validar_xml(c.automata, b'<arbol><valor>' + largo + b'</valor></arbol>').estado == 'limite'
    assert validar_xml(c.automata, b'<arbol><valor>' + largo[:-1] + b'</valor></arbol>').estado == 'invalido'
    assert validar_xml(c.automata, b'<arbol><valor>x' + largo + b'</valor></arbol>').estado == 'invalido'
    assert validar_xml(c.automata, b'<lista><dato>' + largo + b'</dato></lista>').estado == 'valido'


def test_la_ruta_esta_acotada():
    c = compilado('arbol')
    n = 60
    documento = ('<arbol>' + '<valor>1</valor><hijo>' * n + '<valor>x</valor>' + '</hijo>' * n + '</arbol>').encode()
    r = validar_xml(c.automata, documento)
    assert r.estado == 'invalido' and r.ruta.startswith('/arbol/hijo[2]/hijo[2]') and r.ruta.endswith('…')
    assert len(r.ruta.encode()) <= 256
    largo = 'n' * 40
    r = correr(xsd('<xs:element name="r"/>'), f'<{largo} {largo}="1"/>')
    assert r.ruta == '/' + 'n' * 24 + '…'


def test_progreso_en_streaming():
    v = ValidadorXML(compilado('pedido').automata)
    v.feed(BUENO[:60].encode())
    p = v.progreso()
    assert (p['bytes'], p['profundidad'], p['ruta'], p['terminado']) == (60, 2, '/pedido/linea[2]', False)
    assert p['pila'][0] == 'Z0' and len(p['pila']) == 3
    v.feed(BUENO[60:].encode())
    assert v.finish().estado == 'valido' and v.progreso()['profundidad'] == 0


# ---------------------------------------------------------------------------
# Generador de documentos y oráculo independiente
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('nombre', sorted(ESQUEMAS))
def test_casos_generados(nombre):
    casos = generar_casos_xml(compilado(nombre), 6, 6, 4, semilla=3)
    por_clase = {clase: [c for c in casos if c.clase == clase] for clase in ('valido', 'invalido', 'malformado')}
    assert [len(v) for v in por_clase.values()] == [6, 6, 4]
    esperado = 'pendiente' if nombre == 'patron' else 'valido'
    assert all(c.estado == esperado and c.violaciones == 0 for c in por_clase['valido'])
    assert all(c.estado == 'invalido' and c.violaciones > 0 for c in por_clase['invalido'])
    assert all(c.estado == 'malformado' for c in por_clase['malformado'])
    assert casos == generar_casos_xml(compilado(nombre), 6, 6, 4, semilla=3)   # determinista


@pytest.mark.parametrize('nombre', sorted(set(ESQUEMAS) - {'patron'}))
def test_mismo_veredicto_que_xmlschema(nombre):
    c, oraculo = compilado(nombre), xmlschema.XMLSchema(io.BytesIO(ESQUEMAS[nombre]))
    g, validos, invalidos = GeneradorXML(c, semilla=11), 0, 0
    for i in range(240):
        datos, que = (g.documento(), 'válido') if i % 3 == 0 else g.mutante()
        r = validar_xml(c.automata, datos)
        assert r.estado in ('valido', 'invalido'), (que, datos, r)
        assert (r.estado == 'valido') == oraculo.is_valid(io.BytesIO(datos)), (que, datos, r)
        assert i % 3 or r.estado == 'valido', datos
        validos += r.estado == 'valido'
        invalidos += r.estado == 'invalido'
    assert validos >= 80 and invalidos >= 60


@pytest.mark.parametrize('nombre', sorted(ESQUEMAS))
def test_el_resultado_no_depende_de_los_bloques(nombre):
    c, rng = compilado(nombre), random.Random(2)
    for caso in generar_casos_xml(c, 5, 5, 4, semilla=8):
        entero = validar_xml(c.automata, caso.payload)
        for _ in range(4):
            assert validar_xml(c.automata, caso.payload, particion(rng, len(caso.payload))) == entero
        assert validar_xml(c.automata, caso.payload, [1] * len(caso.payload)) == entero
