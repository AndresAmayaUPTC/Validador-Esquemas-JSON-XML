"""AST del esquema (Fase 2): se obtiene del árbol de derivación de la gramática ESQUEMA.

El árbol de derivación tiene un nodo por cada símbolo de la gramática; el AST se queda solo con lo que
significa: esquemas, sus palabras clave y los valores JSON literales, cada uno con su posición en bytes
para que las fases siguientes puedan señalar los errores en el texto original.

El AST existe solo en tiempo de compilación: el validador generado no construye ningún árbol.
"""
import json
from dataclasses import dataclass

from ..lexico.esquema import tokenizar_esquema
from .gramaticas import ESQUEMA
from .parser import AnalizadorLL1

MAX_ANIDAMIENTO = 64  # llaves y corchetes abiertos a la vez; acota la recursión del compilador


@dataclass
class Valor:
    """Valor JSON literal. clase: 'cadena', 'numero', 'booleano', 'nulo', 'arreglo' u 'objeto'.

    dato: el texto de una cadena; el lexema exacto de un número (no se convierte, para no perder
    precisión); True/False; None; una lista de Valor; o una lista de pares (clave, Valor).
    """
    clase: str
    dato: object
    inicio: int
    fin: int

    def a_python(self):
        if self.clase == 'numero':
            return json.loads(self.dato)
        if self.clase == 'arreglo':
            return [v.a_python() for v in self.dato]
        if self.clase == 'objeto':
            return {k: v.a_python() for k, v in self.dato}
        return self.dato


@dataclass
class Entrada:
    """Par nombre → esquema de "properties", "$defs", etc. [inicio, fin) es la posición del nombre."""
    nombre: str
    esquema: 'Esquema'
    inicio: int
    fin: int


@dataclass
class Clave:
    """Miembro de un esquema. [inicio, fin) es la posición de su nombre.

    forma indica el tipo de valor:
      'esquema' → Esquema            ("items", "not", ...)
      'mapa'    → lista de Entrada   ("properties", "$defs", ...)
      'lista'   → lista de Esquema   ("allOf", "prefixItems", ...)
      'valor'   → Valor              (todo lo demás, incluidas las claves que no son del vocabulario)
    conocida es False cuando el nombre no es una palabra clave de JSON Schema.
    """
    nombre: str
    conocida: bool
    forma: str
    valor: object
    inicio: int
    fin: int

    def a_python(self):
        if self.forma == 'mapa':
            return {e.nombre: e.esquema.a_python() for e in self.valor}
        if self.forma == 'lista':
            return [e.a_python() for e in self.valor]
        return self.valor.a_python()


@dataclass
class Esquema:
    """Esquema: true, false (booleano) o un objeto con sus claves en el orden del texto."""
    booleano: bool | None
    claves: list
    inicio: int
    fin: int

    def clave(self, nombre):
        """La primera clave con ese nombre, o None."""
        return next((c for c in self.claves if c.nombre == nombre), None)

    def a_python(self):
        """El documento JSON equivalente (si hay claves repetidas, gana la última)."""
        return self.booleano if self.booleano is not None else {c.nombre: c.a_python() for c in self.claves}


# ---------------------------------------------------------------------------
# Del árbol de derivación al AST. Hay una función por no terminal; la recursión solo baja por el
# anidamiento real del esquema, porque las listas (mas_claves, mas_valores, ...) se recorren con un bucle.
# ---------------------------------------------------------------------------
def _elementos(nodo, simbolo):
    """Nodos «simbolo» de una lista X → s mas_X | ε,  mas_X → COMA s mas_X | ε."""
    salida = []
    while nodo.hijos:
        salida += [h for h in nodo.hijos if h.simbolo == simbolo]
        nodo = nodo.hijos[-1]  # el último hijo es la continuación de la lista
    return salida


def _cadena(nodo):
    token = nodo.hijos[0].token
    return Valor('cadena', token.valor, token.inicio, token.fin)


def _cadenas(nodo):
    abre, lista, cierra = nodo.hijos
    return Valor('arreglo', [_cadena(c) for c in _elementos(lista, 'cadena')], abre.token.inicio, cierra.token.fin)


def _valor(nodo):
    """Construye el Valor de un nodo valor, cadena, booleano, tipo, objeto, arreglo, cadenas o mapa_cadenas,
    o de un terminal NUMERO."""
    s = nodo.simbolo
    if s in ('valor', 'booleano', 'tipo'):
        return _valor(nodo.hijos[0])
    if s == 'cadena':
        return _cadena(nodo)
    if s == 'cadenas':
        return _cadenas(nodo)
    if s in ('objeto', 'arreglo', 'mapa_cadenas'):
        abre, lista, cierra = nodo.hijos
        if s == 'arreglo':
            dato = [_valor(v) for v in _elementos(lista, 'valor')]
        elif s == 'objeto':
            dato = [(p.hijos[0].hijos[0].token.valor, _valor(p.hijos[2])) for p in _elementos(lista, 'par')]
        else:
            dato = [(d.hijos[0].hijos[0].token.valor, _cadenas(d.hijos[2])) for d in _elementos(lista, 'dependencia')]
        return Valor('arreglo' if s == 'arreglo' else 'objeto', dato, abre.token.inicio, cierra.token.fin)
    token = nodo.token
    if s == 'NUMERO':
        return Valor('numero', token.valor, token.inicio, token.fin)
    if s == 'NULL':
        return Valor('nulo', None, token.inicio, token.fin)
    return Valor('booleano', s == 'TRUE', token.inicio, token.fin)


def _esquema(nodo):
    h = nodo.hijos
    if len(h) == 1:  # true | false
        return Esquema(h[0].simbolo == 'TRUE', [], h[0].token.inicio, h[0].token.fin)
    return Esquema(None, [_clave(c) for c in _elementos(h[1], 'clave')], h[0].token.inicio, h[2].token.fin)


def _clave(nodo):
    cabeza, _, cuerpo = nodo.hijos
    token = cabeza.token or cabeza.hijos[0].token  # terminal directo o kw_x → "palabra"
    if cuerpo.simbolo == 'esquema':
        forma, valor = 'esquema', _esquema(cuerpo)
    elif cuerpo.simbolo == 'mapa':
        entradas = _elementos(cuerpo.hijos[1], 'entrada')
        forma, valor = 'mapa', [Entrada(e.hijos[0].hijos[0].token.valor, _esquema(e.hijos[2]),
                                        e.hijos[0].hijos[0].token.inicio, e.hijos[0].hijos[0].token.fin)
                                for e in entradas]
    elif cuerpo.simbolo == 'lista':
        forma, valor = 'lista', [_esquema(cuerpo.hijos[1])] + [_esquema(e) for e in _elementos(cuerpo.hijos[2], 'esquema')]
    else:
        forma, valor = 'valor', _valor(cuerpo)
    return Clave(token.valor, token.tipo == 'PALABRA_CLAVE', forma, valor, token.inicio, token.fin)


_analizador = AnalizadorLL1(ESQUEMA)


def construir(raiz) -> Esquema:
    """AST a partir del árbol de derivación de un documento de esquema."""
    return _esquema(raiz.hijos[0])


def analizar_esquema(datos: bytes) -> Esquema:
    """Lexer + parser LL(1) + AST. Lanza ErrorLexico o ErrorSintactico con la posición en bytes."""
    return construir(_analizador.analizar(tokenizar_esquema(datos), MAX_ANIDAMIENTO))
