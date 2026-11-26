"""Traducción de expresiones regulares entre XSD (facetas pattern) y ECMA-262 («pattern» de JSON Schema).

Las dos sintaxis se leen con los analizadores de compilador.automata.expresiones, que producen el mismo
árbol: ('car', rangos), ('cat', hijos), ('alt', hijos), ('rep', hijo, mín, máx), ('ini',) y ('fin',).
Traducir es volver a imprimir ese árbol con la sintaxis del otro lado, así que no hay reglas de
reescritura de texto que puedan fallar en un caso raro: cada conjunto de caracteres se escribe como
rangos explícitos de puntos de código, y \\d, \\w, \\i o \\p{...} quedan resueltos.

Diferencias de significado que hay que cubrir:
  · Anclaje. En XSD la expresión debe casar con el valor completo; en JSON Schema basta con que aparezca
    en algún lugar de la cadena. XSD → ECMA: ^(?:R)$. ECMA → XSD: se quitan los ^ y $ de los extremos de
    cada alternativa y, donde no los hay, se añade [\\s\\S]* (cualquier cosa). Un ^ o $ en medio de la
    expresión no tiene equivalente.
  · Caracteres admitidos. Un valor de XML solo contiene caracteres de XML (XML 1.0 §2.2): al escribir
    para XSD los conjuntos se recortan a ellos, lo que no cambia qué valores casan.
  · \\d de XSD son los dígitos de todos los alfabetos (categoría Nd). Escritos uno a uno ocupan una línea
    entera; se traducen como [0-9] y la conversión lo avisa.
"""
import unicodedata

from ..automata import expresiones as ex

SUSTITUTOS = ((0xD800, 0xDFFF),)
TODO = ((0, ex.MAX_CARACTER),)
UNIVERSO_ECMA = ex._normalizar([(0, 0xD7FF), (0xE000, ex.MAX_CARACTER)])
UNIVERSO_XML = ((0x09, 0x0A), (0x0D, 0x0D), (0x20, 0xD7FF), (0xE000, 0xFFFD), (0x10000, ex.MAX_CARACTER))
ASCII_DIGITOS = ((0x30, 0x39),)

_SINTAXIS_ECMA = set('^$\\.*+?()[]{}|')     # los únicos escapes de identidad válidos fuera de una clase (modo u)
_SINTAXIS_XSD = set('\\|.-^?*+{}()[]')
_CONTROL = {0x09: 't', 0x0A: 'n', 0x0D: 'r', 0x0B: 'v', 0x0C: 'f'}


class NoTraducible(Exception):
    """La expresión no tiene equivalente en el otro lenguaje (o no se puede leer)."""


def _interseccion(a, b):
    return ex._normalizar(ex._interseccion(a, b))


def _diferencia(a, b):
    return ex._diferencia(a, b) if a else ()


def _contiene(a, b):
    """True si el conjunto a incluye todo b."""
    return not _diferencia(b, a)


def _digitos_no_ascii():
    return _diferencia(ex.categoria('Nd'), ASCII_DIGITOS)


# ---------------------------------------------------------------------------
# Impresión de un conjunto de caracteres
# ---------------------------------------------------------------------------
def _visible(c):
    return unicodedata.category(chr(c))[0] not in 'CZ' or c == 0x20


def _caracter(c, dialecto, en_clase):
    """Un carácter escrito para el dialecto ('ecma' o 'xsd')."""
    letra = chr(c)
    sintaxis = _SINTAXIS_ECMA if dialecto == 'ecma' else _SINTAXIS_XSD
    if letra in sintaxis or (en_clase and letra in '[]^-\\'):
        return '\\' + letra
    if c in _CONTROL and (dialecto == 'ecma' or c in (0x09, 0x0A, 0x0D)):
        return '\\' + _CONTROL[c]
    if dialecto == 'ecma' and not _visible(c) and c <= 0xFFFF:
        return f'\\u{c:04X}'
    return letra   # en XSD no hay escapes numéricos: el carácter va tal cual (el XML lo escapa al escribirlo)


def _rangos(rangos, dialecto):
    partes = []
    for lo, hi in rangos:
        if lo == hi:
            partes.append(_caracter(lo, dialecto, True))
        elif hi == lo + 1:
            partes.append(_caracter(lo, dialecto, True) + _caracter(hi, dialecto, True))
        else:
            partes.append(_caracter(lo, dialecto, True) + '-' + _caracter(hi, dialecto, True))
    return ''.join(partes)


class _Impresora:
    def __init__(self, dialecto):
        self.dialecto = dialecto
        self.universo = UNIVERSO_ECMA if dialecto == 'ecma' else UNIVERSO_XML
        self.aproximado = False      # se tradujo \d de XSD como [0-9]

    def conjunto(self, rangos):
        s = _interseccion(rangos, self.universo)
        resto = _diferencia(self.universo, s)
        if not s:
            return '[^\\s\\S]'
        if not resto:
            return '[\\s\\S]'
        if self.dialecto == 'ecma':
            if s == ex.DIGITO:
                return '\\d'
            if s == ex.PALABRA:
                return '\\w'
        candidatos = []
        for positivo, elegido in ((True, s), (False, resto)):
            aproximado = False
            if self.dialecto == 'ecma':
                # \d (o \D) de XSD: los dígitos de otros alfabetos se dejan fuera, y la conversión lo avisa
                no_ascii = _digitos_no_ascii()
                if _contiene(elegido, no_ascii) and _contiene(((0, 0x7F),), _diferencia(elegido, no_ascii)):
                    elegido, aproximado = _diferencia(elegido, no_ascii), True
            if positivo and len(elegido) == 1 and elegido[0][0] == elegido[0][1]:
                texto = _caracter(elegido[0][0], self.dialecto, False)
            elif self.dialecto == 'ecma' and elegido == ex.DIGITO:
                texto = '\\d' if positivo else '\\D'
            else:
                texto = '[' + ('' if positivo else '^') + _rangos(elegido, self.dialecto) + ']'
            candidatos.append((len(texto), not positivo, texto, aproximado))
        _, _, texto, aproximado = min(candidatos)
        self.aproximado |= aproximado
        return texto

    def grupo(self, texto):
        return ('(?:' if self.dialecto == 'ecma' else '(') + texto + ')'

    def nodo(self, n):
        clase = n[0]
        if clase == 'car':
            return self.conjunto(n[1])
        if clase == 'cat':
            return ''.join(self.grupo(self.nodo(h)) if h[0] == 'alt' else self.nodo(h) for h in n[1])
        if clase == 'alt':
            return '|'.join(self.nodo(h) for h in n[1])
        if clase == 'rep':
            _, hijo, minimo, maximo = n
            atomo = self.nodo(hijo) if hijo[0] == 'car' else self.grupo(self.nodo(hijo))
            if (minimo, maximo) == (0, None):
                return atomo + '*'
            if (minimo, maximo) == (1, None):
                return atomo + '+'
            if (minimo, maximo) == (0, 1):
                return atomo + '?'
            if maximo is None:
                return f'{atomo}{{{minimo},}}'
            return f'{atomo}{{{minimo}}}' if minimo == maximo else f'{atomo}{{{minimo},{maximo}}}'
        if self.dialecto != 'ecma':
            raise NoTraducible('ancla ^ o $ en medio de la expresión')
        return '^' if clase == 'ini' else '$'


# ---------------------------------------------------------------------------
# XSD → ECMA
# ---------------------------------------------------------------------------
def xsd_a_ecma(textos) -> tuple:
    """Un grupo de facetas pattern de XSD (se combinan con «o») → (patrón de JSON Schema, aproximado).
    aproximado es True si \\d se tradujo como [0-9]. Lanza NoTraducible si alguna no se puede leer."""
    try:
        arboles = [ex.analizar_xsd(t) for t in textos]
    except (ex.ErrorDeExpresion, ex.NoRegular) as e:
        raise NoTraducible(str(e)) from e
    imp = _Impresora('ecma')
    cuerpo = imp.nodo(arboles[0] if len(arboles) == 1 else ('alt', arboles))
    simple = len(arboles) == 1 and arboles[0][0] != 'alt'
    return '^' + (cuerpo if simple else imp.grupo(cuerpo)) + '$', imp.aproximado


# ---------------------------------------------------------------------------
# ECMA → XSD
# ---------------------------------------------------------------------------
def _alternativas(n):
    """Las ramas de nivel superior: se abren las alternativas y los grupos de un solo elemento."""
    if n[0] == 'alt':
        return [r for h in n[1] for r in _alternativas(h)]
    if n[0] == 'cat' and len(n[1]) == 1 and n[1][0][0] in ('alt', 'cat'):
        return _alternativas(n[1][0])
    return [n]


def _con_anclas(n):
    if n[0] in ('ini', 'fin'):
        return True
    if n[0] in ('cat', 'alt'):
        return any(_con_anclas(h) for h in n[1])
    return n[0] == 'rep' and _con_anclas(n[1])


def ecma_a_xsd(texto: str) -> str:
    """Patrón de JSON Schema → expresión de XSD con el mismo conjunto de cadenas que casan.
    Lanza NoTraducible si usa algo que no es regular o un ancla en medio."""
    try:
        arbol = ex.analizar(texto)
    except (ex.ErrorDeExpresion, ex.NoRegular) as e:
        raise NoTraducible(str(e)) from e
    imp = _Impresora('xsd')
    cualquiera = imp.conjunto(TODO) + '*'
    ramas = []
    for rama in _alternativas(arbol):
        partes = list(rama[1]) if rama[0] == 'cat' else [rama]
        izquierda = derecha = False
        while partes and partes[0] == ('ini',):
            izquierda, partes = True, partes[1:]
        while partes and partes[-1] == ('fin',):
            derecha, partes = True, partes[:-1]
        if any(_con_anclas(p) for p in partes):
            raise NoTraducible('ancla ^ o $ en medio de la expresión')
        cuerpo = imp.nodo(('cat', partes))
        if not cuerpo and not izquierda and not derecha:
            ramas.append(cualquiera)
            continue
        ramas.append(('' if izquierda else cualquiera) + cuerpo + ('' if derecha else cualquiera))
    if len(ramas) == 1:
        return ramas[0]
    return '|'.join(ramas)
