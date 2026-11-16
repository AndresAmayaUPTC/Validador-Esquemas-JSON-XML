"""Árbol de un documento XML (Fase 2, XML): elementos, atributos y texto, con sus posiciones en bytes.

Primero el parser LL(1) comprueba la gramática XML de gramaticas.py; después un recorrido de los tokens
con una pila construye el árbol y comprueba lo que una gramática libre de contexto no puede:

  - que el nombre de cada etiqueta de cierre sea el de su apertura,
  - que un elemento no repita atributos,
  - que fuera del elemento raíz solo haya espacios.

Este árbol existe solo en tiempo de compilación: sirve para leer los esquemas XSD. El validador de
documentos no construye ningún árbol.
"""
from dataclasses import dataclass, field

from ..lexico.lexer_xml import solo_espacios, tokenizar_xml
from .gramaticas import XML
from .parser import AnalizadorLL1, ErrorSintactico

MAX_ANIDAMIENTO = 64   # elementos abiertos a la vez


class ErrorXML(ErrorSintactico):
    """El documento cumple la gramática pero no está bien formado. motivo: 'cierre', 'atributo-repetido',
    'texto-fuera' o 'anidamiento'."""

    def __init__(self, pos, motivo, mensaje):
        Exception.__init__(self, f'{mensaje} en el byte {pos}')
        self.pos, self.motivo, self.encontrado, self.esperados = pos, motivo, None, []


@dataclass
class Atributo:
    valor: str
    inicio: int     # posición del nombre
    fin: int        # final del valor


@dataclass
class Texto:
    valor: str
    inicio: int
    fin: int


@dataclass
class Elemento:
    nombre: str
    inicio: int
    fin: int = 0
    atributos: dict = field(default_factory=dict)   # nombre → Atributo, en el orden del texto
    hijos: list = field(default_factory=list)       # Elemento y Texto, en el orden del texto

    def elementos(self):
        return [h for h in self.hijos if isinstance(h, Elemento)]

    def texto(self):
        """El texto directo del elemento, concatenado."""
        return ''.join(h.valor for h in self.hijos if isinstance(h, Texto))

    def atributo(self, nombre, defecto=None):
        a = self.atributos.get(nombre)
        return a.valor if a else defecto


_analizador = AnalizadorLL1(XML)


def construir(tokens) -> Elemento:
    """Árbol a partir de una secuencia de tokens que ya cumple la gramática."""
    raiz, pila, i = None, [], 0
    while i < len(tokens):
        t = tokens[i]
        if t.tipo == 'TEXTO':
            if pila:
                pila[-1].hijos.append(Texto(t.valor, t.inicio, t.fin))
            elif not solo_espacios(t):
                raise ErrorXML(t.inicio, 'texto-fuera', 'hay texto fuera del elemento raíz')
        elif t.tipo == 'ETIQ_ABRE':
            if len(pila) >= MAX_ANIDAMIENTO:
                raise ErrorXML(t.inicio, 'anidamiento', 'anidamiento demasiado profundo')
            e = Elemento(tokens[i + 1].valor, t.inicio)
            i += 2
            while tokens[i].tipo == 'NOMBRE':   # NOMBRE IGUAL VALOR_ATRIB
                nombre, valor = tokens[i], tokens[i + 2]
                if nombre.valor in e.atributos:
                    raise ErrorXML(nombre.inicio, 'atributo-repetido', f'el atributo «{nombre.valor}» está repetido')
                e.atributos[nombre.valor] = Atributo(valor.valor, nombre.inicio, valor.fin)
                i += 3
            (pila[-1].hijos if pila else []).append(e)
            raiz = raiz or e
            if tokens[i].tipo == 'ETIQ_AUTOCIERRE':
                e.fin = tokens[i].fin
            else:
                pila.append(e)
        elif t.tipo == 'ETIQ_CIERRE_ABRE':
            nombre, e = tokens[i + 1], pila.pop()
            if nombre.valor != e.nombre:
                raise ErrorXML(nombre.inicio, 'cierre', f'se cierra «{nombre.valor}» pero el elemento abierto es «{e.nombre}»')
            e.fin = tokens[i + 2].fin
            i += 2
        i += 1
    return raiz


def analizar_xml(datos: bytes) -> Elemento:
    """Lexer + parser LL(1) + árbol. Lanza ErrorLexico, ErrorSintactico o ErrorXML, con la posición en bytes."""
    tokens, error = tokenizar_xml(datos)
    if error:
        raise error
    _analizador.analizar(tokens)
    return construir(tokens)
