"""Documentos XML de ejemplo → XSD.

    python -m compilador.conversion inferir ejemplo1.xml ejemplo2.xml ...

Se agrupan las apariciones de cada elemento por su ruta desde la raíz (/pedido/linea/producto) y de cada
grupo se infiere un tipo anónimo:

  · Atributos: los que aparecen; use="required" si están en todas las apariciones.
  · Contenido: si alguna aparición tiene hijos, es un modelo de contenido; si no, texto.
  · Modelo de contenido: si en todos los ejemplos los hijos siguen un mismo orden (cada nombre en un solo
    tramo seguido y sin contradicciones entre ejemplos), una sequence con las veces que aparece cada uno
    (minOccurs = el mínimo visto, maxOccurs = 1 o unbounded). Si no hay un orden común, un choice
    repetido con todos los nombres: se pierde el orden, pero lo cumplen todos los ejemplos.
  · Texto: xs:boolean si todos los valores son true o false, xs:integer si todos son enteros, xs:decimal
    si son números, y xs:string en otro caso (también si alguno está vacío).

Todos los ejemplos cumplen el XSD inferido, salvo los que tienen contenido mixto (texto y elementos a la
vez), que el subconjunto de XSD no admite. Es una generalización a partir de ejemplos positivos: el XSD
acepta también documentos que no se parecen a ninguno de ellos (Gold, 1967: con solo ejemplos positivos
no se puede identificar un lenguaje regular en general).
"""
import re

from ..lexico.lexer import ErrorLexico
from ..sintactico.parser import ErrorSintactico
from ..sintactico.xml_dom import analizar_xml
from ..xsd.modelo import XS, analizar_xsd
from . import Avisos, Conversion, ErrorDeConversion
from .json_a_xsd import NodoXsd

_BOOLEANO = re.compile(r'(true|false)\Z')
_ENTERO = re.compile(r'[+-]?[0-9]+\Z')
_DECIMAL = re.compile(r'[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)\Z')
_BLANCOS = ' \t\n\r'


def tipo_de_textos(valores) -> str:
    """El tipo predefinido más estrecho que admite todos los valores."""
    limpios = [v.strip(_BLANCOS) for v in valores]
    if not limpios or any(not v for v in limpios):
        return 'xs:string'
    for expresion, tipo in ((_BOOLEANO, 'xs:boolean'), (_ENTERO, 'xs:integer'), (_DECIMAL, 'xs:decimal')):
        if all(expresion.match(v) for v in limpios):
            return tipo
    return 'xs:string'


def _tramos(nombres):
    """[(nombre, veces seguidas)] de una secuencia de nombres."""
    tramos = []
    for n in nombres:
        if tramos and tramos[-1][0] == n:
            tramos[-1][1] += 1
        else:
            tramos.append([n, 1])
    return tramos


def orden_comun(secuencias):
    """El orden de los nombres si todas las secuencias lo respetan (cada nombre en un solo tramo), o None."""
    primero, despues = {}, {}
    for s in secuencias:
        tramos = _tramos(s)
        nombres = [n for n, _ in tramos]
        if len(set(nombres)) != len(nombres):
            return None
        for i, a in enumerate(nombres):
            primero.setdefault(a, len(primero))
            despues.setdefault(a, set()).update(nombres[i + 1:])
    # orden topológico; entre los que están libres, el que apareció antes
    pendientes, orden = dict(primero), []
    while pendientes:
        libres = [n for n in pendientes if not any(n in despues[m] for m in pendientes if m != n)]
        if not libres:
            return None     # un ciclo: a antes que b en un ejemplo y b antes que a en otro
        elegido = min(libres, key=primero.get)
        orden.append(elegido)
        del pendientes[elegido]
    return orden


class _Inferencia:
    def __init__(self):
        self.avisos = Avisos()

    def elemento(self, nombre, apariciones, ruta):
        nodo = NodoXsd('element', {'name': nombre})
        tipo = self.tipo(apariciones, ruta)
        if isinstance(tipo, str):
            nodo.atributos['type'] = tipo
        else:
            nodo.hijos.append(tipo)
        return nodo

    def atributos(self, apariciones, ruta):
        valores = {}
        for e in apariciones:
            for nombre, a in e.atributos.items():
                if nombre == 'xmlns' or nombre.startswith('xmlns:'):
                    continue
                if ':' in nombre:
                    self.avisos.agregar(f'{ruta}/@{nombre}', 'los atributos con prefijo (de otro espacio de nombres) '
                                                             'no están en el subconjunto: se omiten')
                    continue
                valores.setdefault(nombre, []).append(a.valor)
        nodos = []
        for nombre, vistos in valores.items():
            nodo = NodoXsd('attribute', {'name': nombre, 'type': tipo_de_textos(vistos)})
            if len(vistos) == len(apariciones):
                nodo.atributos['use'] = 'required'
            nodos.append(nodo)
        return nodos

    def tipo(self, apariciones, ruta):
        atributos = self.atributos(apariciones, ruta)
        con_hijos = any(e.elementos() for e in apariciones)
        if not con_hijos:
            simple = tipo_de_textos([e.texto() for e in apariciones])
            if not atributos:
                return simple
            return NodoXsd('complexType', hijos=[NodoXsd('simpleContent', hijos=[
                NodoXsd('extension', {'base': simple}, atributos)])])
        if any(e.texto().strip(_BLANCOS) for e in apariciones):
            self.avisos.agregar(ruta, 'contenido mixto (texto entre los elementos hijos): el subconjunto de XSD no lo '
                                      'admite, así que los ejemplos con ese texto no cumplirán el esquema')
        secuencias = [[h.nombre for h in e.elementos()] for e in apariciones]
        hijos = {}
        for e in apariciones:
            for h in e.elementos():
                hijos.setdefault(h.nombre, []).append(h)
        orden = orden_comun(secuencias)
        particulas = []
        if orden is not None:
            for nombre in orden:
                cuentas = [sum(1 for n in s if n == nombre) for s in secuencias]
                nodo = self.elemento(nombre, hijos[nombre], f'{ruta}/{nombre}')
                if min(cuentas) != 1:
                    nodo.atributos['minOccurs'] = min(cuentas)
                if max(cuentas) > 1:
                    nodo.atributos['maxOccurs'] = 'unbounded'
                particulas.append(nodo)
            modelo = NodoXsd('sequence', hijos=particulas)
        else:
            self.avisos.agregar(ruta, 'los hijos no siguen un orden común en los ejemplos: se admiten en cualquier '
                                      'orden y cantidad (choice repetido)')
            modelo = NodoXsd('choice', {'minOccurs': 0 if any(not s for s in secuencias) else 1,
                                        'maxOccurs': 'unbounded'},
                             [self.elemento(n, hijos[n], f'{ruta}/{n}') for n in hijos])
        return NodoXsd('complexType', hijos=[modelo, *atributos])


def inferir_xsd(documentos) -> Conversion:
    """documentos: textos (bytes o str) de uno o más documentos XML."""
    if not documentos:
        raise ErrorDeConversion('hace falta al menos un documento de ejemplo')
    raices = {}
    for i, doc in enumerate(documentos, 1):
        datos = doc.encode('utf-8') if isinstance(doc, str) else doc
        try:
            raiz = analizar_xml(datos)
        except (ErrorLexico, ErrorSintactico) as e:
            raise ErrorDeConversion(f'el ejemplo {i} no es XML bien formado: {e}', [str(e)]) from e
        espacios = [n for n in _todos(raiz) if ':' in n.nombre or n.atributo('xmlns') is not None]
        if espacios:
            raise ErrorDeConversion(f'el ejemplo {i} usa espacios de nombres (<{espacios[0].nombre}>), que el '
                                    'subconjunto de XSD no admite')
        raices.setdefault(raiz.nombre, []).append(raiz)
    inferencia = _Inferencia()
    globales = [inferencia.elemento(nombre, apariciones, f'/{nombre}') for nombre, apariciones in raices.items()]
    if len(documentos) == 1:
        inferencia.avisos.agregar('/', 'con un solo ejemplo, cada elemento y atributo que aparece queda como obligatorio')
    lineas = ['<?xml version="1.0" encoding="UTF-8"?>', f'<xs:schema xmlns:xs="{XS}">']
    for g in globales:
        g.escribir(lineas, '  ')
    lineas.append('</xs:schema>')
    texto = '\n'.join(lineas) + '\n'
    analisis = analizar_xsd(texto.encode('utf-8'))
    if not analisis.ok:   # sería un defecto de la inferencia
        raise AssertionError('el XSD inferido no compila: ' + '; '.join(d.mensaje for d in analisis.errores))
    return Conversion(texto, 'xsd', list(inferencia.avisos))


def _todos(e):
    yield e
    for h in e.elementos():
        yield from _todos(h)
