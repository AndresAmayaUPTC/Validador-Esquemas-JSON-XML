"""Expresiones regulares de JSON Schema → AFD sobre bytes UTF-8 (Fase 4, estrategia de respaldo).

«pattern» y «patternProperties» usan expresiones de ECMA-262. Casi todas describen un lenguaje regular,
así que se compilan a un AFD y el validador las comprueba mientras lee la cadena, byte a byte y sin
retroceso: el tiempo es lineal en la longitud de la cadena y no hay ReDoS posible.

Tubería:

    texto ─analizar→ árbol ─Thompson→ AFN sobre bytes ─subconjuntos→ AFD ─Moore→ AFD mínimo

Los conjuntos de caracteres ([a-z], \\d, el punto...) se traducen a secuencias de rangos de bytes UTF-8,
de modo que el autómata lee directamente los bytes de la cadena ya decodificada.

Búsqueda, no coincidencia completa. En JSON Schema un patrón se cumple si aparece en cualquier lugar de
la cadena. El autómata lee la cadena marcada ⊢w⊣ (INICIO, los bytes y FIN) y reconoce Σ*·R·Σ*, donde en
R el ancla ^ lee ⊢ y $ lee ⊣. Como ⊢ y ⊣ solo aparecen en los extremos, ^ y $ quedan anclados.

Varios patrones a la vez. afd() une los AFN de varios componentes (patrones o nombres literales), cada uno
con su etiqueta, y determiniza la unión: es el producto de los autómatas. Cada estado del AFD lleva el
conjunto de etiquetas que aceptarían si la cadena terminara ahí (es decir, tras leer ⊣).

Lo que no es regular (referencias hacia atrás) o no se implementa (aserciones de anticipación o
retroceso, \\b, propiedades Unicode) se rechaza con NoRegular: esa palabra clave queda en verificación
diferida. Un AFD con más de MAX_ESTADOS estados también (explosión al determinizar).
"""
from dataclasses import dataclass
from functools import lru_cache

MAX_ESTADOS = 4096        # estados del AFD (por subconjuntos, antes de minimizar)
MAX_AFN = 20_000          # estados del AFN: acota la expansión de las repeticiones {n,m}
INICIO, FIN = 256, 257    # símbolos ⊢ y ⊣; los bytes son 0..255
MAX_CARACTER = 0x10FFFF


class ErrorDeExpresion(ValueError):
    """La expresión no es válida en ECMA-262: el esquema tiene un error."""


class NoRegular(Exception):
    """La expresión es válida, pero no se puede compilar a un AFD (o el AFD sería demasiado grande)."""


# ---------------------------------------------------------------------------
# Conjuntos de caracteres: listas ordenadas de rangos [lo, hi] de puntos de código, disjuntos
# ---------------------------------------------------------------------------
def _normalizar(rangos):
    salida = []
    for lo, hi in sorted(rangos):
        if salida and lo <= salida[-1][1] + 1:
            salida[-1] = (salida[-1][0], max(salida[-1][1], hi))
        else:
            salida.append((lo, hi))
    return tuple(salida)


def _complemento(rangos):
    salida, siguiente = [], 0
    for lo, hi in _normalizar(rangos):
        if lo > siguiente:
            salida.append((siguiente, lo - 1))
        siguiente = hi + 1
    if siguiente <= MAX_CARACTER:
        salida.append((siguiente, MAX_CARACTER))
    return tuple(salida)


DIGITO = ((0x30, 0x39),)
PALABRA = _normalizar([(0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A)])
ESPACIO = _normalizar([(0x09, 0x0D), (0x20, 0x20), (0xA0, 0xA0), (0x1680, 0x1680), (0x2000, 0x200A),
                       (0x2028, 0x2029), (0x202F, 0x202F), (0x205F, 0x205F), (0x3000, 0x3000), (0xFEFF, 0xFEFF)])
FIN_DE_LINEA = _normalizar([(0x0A, 0x0A), (0x0D, 0x0D), (0x2028, 0x2029)])
PUNTO = _complemento(FIN_DE_LINEA)
CLASES = {'d': DIGITO, 'D': _complemento(DIGITO), 'w': PALABRA, 'W': _complemento(PALABRA),
          's': ESPACIO, 'S': _complemento(ESPACIO)}
CONTROL = {'t': 0x09, 'n': 0x0A, 'v': 0x0B, 'f': 0x0C, 'r': 0x0D}


def _utf8(c):
    return list(chr(c).encode('utf-8'))


def secuencias_utf8(lo, hi):
    """Rangos de bytes que codifican en UTF-8 los caracteres de [lo, hi]: una lista de secuencias, cada
    una una lista de rangos (b1, b2) por byte. Es el algoritmo de utf8-ranges: partir el intervalo hasta
    que cada trozo tenga la misma longitud de codificación y bytes de continuación completos."""
    salida, pila = [], [(lo, hi)]
    while pila:
        lo, hi = pila.pop()
        if lo > hi:
            continue
        if lo <= 0xDFFF and hi >= 0xD800:  # los sustitutos no se codifican en UTF-8
            pila += [(0xE000, hi), (lo, 0xD7FF)]
            continue
        partido = False
        for limite in (0x7F, 0x7FF, 0xFFFF):
            if lo <= limite < hi:
                pila += [(limite + 1, hi), (lo, limite)]
                partido = True
                break
        if partido:
            continue
        if hi <= 0x7F:
            salida.append([(lo, hi)])
            continue
        for i in (1, 2, 3):
            m = (1 << (6 * i)) - 1
            if lo & ~m != hi & ~m:
                if lo & m != 0:
                    pila += [((lo | m) + 1, hi), (lo, lo | m)]
                    partido = True
                    break
                if hi & m != m:
                    pila += [(hi & ~m, hi), (lo, (hi & ~m) - 1)]
                    partido = True
                    break
        if not partido:
            salida.append(list(zip(_utf8(lo), _utf8(hi))))
    return salida


# ---------------------------------------------------------------------------
# Análisis sintáctico: texto → árbol
#   ('car', rangos)  ('cat', [hijos])  ('alt', [hijos])  ('rep', hijo, mínimo, máximo o None)
#   ('ini',) para ^    ('fin',) para $
# ---------------------------------------------------------------------------
_SINTAXIS = set('^$\\.*+?()[]{}|/-')


class _Analizador:
    def __init__(self, texto):
        self.t, self.i = texto, 0

    def ver(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def comer(self):
        c = self.t[self.i]
        self.i += 1
        return c

    def error(self, mensaje):
        raise ErrorDeExpresion(f'{mensaje} (posición {self.i})')

    def analizar(self):
        arbol = self.alternativa()
        if self.i < len(self.t):
            self.error('paréntesis de cierre sin abrir')
        return arbol

    def alternativa(self):
        ramas = [self.secuencia()]
        while self.ver() == '|':
            self.comer()
            ramas.append(self.secuencia())
        return ramas[0] if len(ramas) == 1 else ('alt', ramas)

    def secuencia(self):
        partes = []
        while self.ver() not in (None, '|', ')'):
            partes.append(self.cuantificar(self.atomo()))
        return ('cat', partes)

    def _limites(self):
        """Si en la posición actual hay un cuantificador {n}, {n,} o {n,m}, lo consume y lo devuelve."""
        j, t = self.i + 1, self.t
        def numero(j):
            k = j
            while k < len(t) and t[k].isdigit() and t[k].isascii():
                k += 1
            return (int(t[j:k]), k) if k > j else (None, j)
        n, j = numero(j)
        if n is None:
            return None
        m = n
        if j < len(t) and t[j] == ',':
            m, j = numero(j + 1)
        if j < len(t) and t[j] == '}':
            self.i = j + 1
            return n, m
        return None

    def cuantificar(self, atomo):
        c = self.ver()
        if c in ('*', '+', '?'):
            self.comer()
            minimo, maximo = {'*': (0, None), '+': (1, None), '?': (0, 1)}[c]
        elif c == '{' and (limites := self._limites()) is not None:
            minimo, maximo = limites
            if maximo is not None and maximo < minimo:
                self.error('el máximo de la repetición es menor que el mínimo')
        else:
            return atomo
        if atomo[0] in ('ini', 'fin'):
            self.error('no se puede repetir una aserción')
        if self.ver() == '?':  # versión perezosa: el mismo lenguaje
            self.comer()
        if self.ver() in ('*', '+', '?') or (self.ver() == '{' and self._limites_sin_consumir()):
            self.error('dos cuantificadores seguidos')
        return ('rep', atomo, minimo, maximo)

    def _limites_sin_consumir(self):
        i = self.i
        hay = self._limites() is not None
        self.i = i
        return hay

    def atomo(self):
        c = self.comer()
        if c == '(':
            if self.t.startswith('?:', self.i):
                self.i += 2
            elif self.t.startswith(('?=', '?!', '?<=', '?<!'), self.i):
                raise NoRegular('aserción de anticipación o retroceso')
            elif self.t.startswith('?<', self.i):
                fin = self.t.find('>', self.i)
                if fin < 0:
                    self.error('nombre de grupo sin cerrar')
                self.i = fin + 1
            elif self.ver() == '?':
                self.error('grupo desconocido')
            dentro = self.alternativa()
            if self.ver() != ')':
                self.error('falta el paréntesis de cierre')
            self.comer()
            return dentro
        if c == '[':
            return ('car', self.clase())
        if c == '.':
            return ('car', PUNTO)
        if c == '^':
            return ('ini',)
        if c == '$':
            return ('fin',)
        if c in ('*', '+', '?'):
            self.error('cuantificador sin nada que repetir')
        if c == '{':
            self.i -= 1
            if self._limites_sin_consumir():
                self.error('cuantificador sin nada que repetir')
            self.i += 1
        if c == '\\':
            return ('car', self.escape(en_clase=False))
        return ('car', ((ord(c), ord(c)),))

    def _hex(self, n):
        texto = self.t[self.i:self.i + n]
        if len(texto) < n or any(x not in '0123456789abcdefABCDEF' for x in texto):
            self.error('escape hexadecimal incompleto')
        self.i += n
        return int(texto, 16)

    def escape(self, en_clase):
        """Escape después de la barra invertida: devuelve un conjunto de rangos."""
        if self.ver() is None:
            self.error('barra invertida al final')
        c = self.comer()
        if c in CLASES:
            return CLASES[c]
        if c in CONTROL:
            return ((CONTROL[c],) * 2,)
        if c == 'b':
            if en_clase:
                return ((0x08, 0x08),)
            raise NoRegular('límite de palabra (\\b)')
        if c == 'B':
            raise NoRegular('límite de palabra (\\B)')
        if c.isdigit() and c != '0':
            raise NoRegular('referencia hacia atrás')
        if c == 'k' and self.ver() == '<':
            raise NoRegular('referencia hacia atrás')
        if c in ('p', 'P'):
            raise NoRegular('propiedad Unicode (\\p)')
        if c == '0':
            if self.ver() is not None and self.ver().isdigit():
                self.error('escape octal')
            return ((0, 0),)
        if c == 'x':
            v = self._hex(2)
            return ((v, v),)
        if c == 'u':
            if self.ver() == '{':
                fin = self.t.find('}', self.i)
                if fin < 0:
                    self.error('escape \\u{...} sin cerrar')
                texto = self.t[self.i + 1:fin]
                if not texto or any(x not in '0123456789abcdefABCDEF' for x in texto) or int(texto, 16) > MAX_CARACTER:
                    self.error('escape \\u{...} inválido')
                self.i = fin + 1
                v = int(texto, 16)
                return ((v, v),)
            v = self._hex(4)
            if 0xD800 <= v <= 0xDBFF and self.t.startswith('\\u', self.i):
                guardado = self.i
                self.i += 2
                bajo = self._hex(4)
                if 0xDC00 <= bajo <= 0xDFFF:
                    v = 0x10000 + ((v - 0xD800) << 10) + (bajo - 0xDC00)
                else:
                    self.i = guardado
            return ((v, v),)
        if c == 'c' and self.ver() is not None and self.ver().isascii() and self.ver().isalpha():
            v = ord(self.comer()) % 32
            return ((v, v),)
        if c in _SINTAXIS or not (c.isascii() and c.isalnum()):
            return ((ord(c), ord(c)),)  # escape de identidad
        self.error(f'escape desconocido \\{c}')

    def clase(self):
        negada = self.ver() == '^'
        if negada:
            self.comer()
        rangos = []
        while True:
            c = self.ver()
            if c is None:
                self.error('clase de caracteres sin cerrar')
            if c == ']':
                self.comer()
                break
            izquierda = self._elemento_de_clase()
            if self.ver() == '-' and self.ver(1) not in (None, ']'):
                self.comer()
                derecha = self._elemento_de_clase()
                if len(izquierda) != 1 or len(derecha) != 1 or izquierda[0][0] != izquierda[0][1] \
                        or derecha[0][0] != derecha[0][1]:
                    self.error('rango de caracteres con una clase en un extremo')
                if izquierda[0][0] > derecha[0][0]:
                    self.error('rango de caracteres al revés')
                rangos.append((izquierda[0][0], derecha[0][0]))
            else:
                rangos += izquierda
        rangos = _normalizar(rangos)
        return _complemento(rangos) if negada else rangos

    def _elemento_de_clase(self):
        c = self.comer()
        if c == '\\':
            if self.ver() == '-':
                self.comer()
                return ((0x2D, 0x2D),)
            return self.escape(en_clase=True)
        return ((ord(c), ord(c)),)


def analizar(texto: str):
    """Árbol de la expresión. Lanza ErrorDeExpresion si no es válida y NoRegular si no se puede compilar."""
    return _Analizador(texto).analizar()


# ---------------------------------------------------------------------------
# Expresiones de XML Schema (Parte 2, apéndice F): otra sintaxis, el mismo árbol
# ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _por_categoria():
    """Categoría general de Unicode (Lu, Nd...) → rangos, con la tabla de unicodedata."""
    import unicodedata
    tabla, actual, inicio = {}, None, 0
    for c in range(MAX_CARACTER + 2):
        cat = unicodedata.category(chr(c)) if c <= MAX_CARACTER else None
        if cat != actual:
            if actual is not None and actual != 'Cs':
                tabla.setdefault(actual, []).append((inicio, c - 1))
            actual, inicio = cat, c
    return {cat: _normalizar(rangos) for cat, rangos in tabla.items()}


@lru_cache(maxsize=None)
def categoria(nombre: str):
    """Rangos de una categoría de \\p{...}: de una letra (L, N, P...) o de dos (Lu, Nd...)."""
    tabla = _por_categoria()
    if len(nombre) == 1:
        rangos = [r for cat, rs in tabla.items() if cat[0] == nombre for r in rs]
    else:
        rangos = list(tabla.get(nombre, ()))
    if not rangos and nombre not in ('Cn', 'Co', 'Cs'):
        raise KeyError(nombre)
    return _normalizar(rangos)


def _inicio_de_nombre():
    from ..lexico.lexer_xml import _INICIO
    return _normalizar([(0x3A, 0x3A), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A), *_INICIO])


def _caracter_de_nombre():
    from ..lexico.lexer_xml import _RESTO
    return _normalizar([*_inicio_de_nombre(), (0x2D, 0x2E), (0x30, 0x39), *_RESTO])


_CONTROL_XSD = {'n': 0x0A, 'r': 0x0D, 't': 0x09}
_ESCAPABLES_XSD = set('\\|.-^?*+{}()[]')
PUNTO_XSD = _complemento(((0x0A, 0x0A), (0x0D, 0x0D)))


def _clase_xsd(letra):
    """Escapes de varios caracteres de XSD: \\s \\i \\c \\d \\w y sus negaciones en mayúscula."""
    base = {'s': lambda: _normalizar([(0x09, 0x0A), (0x0D, 0x0D), (0x20, 0x20)]),
            'i': _inicio_de_nombre, 'c': _caracter_de_nombre, 'd': lambda: categoria('Nd'),
            # \w: todo menos puntuación, separadores y «otros» (P, Z y C)
            'w': lambda: _complemento(categoria('P') + categoria('Z') + categoria('C'))}[letra.lower()]()
    return base if letra.islower() else _complemento(base)


class _AnalizadorXSD(_Analizador):
    """La gramática de XSD: anclada implícitamente (lo hace afd()), sin ^ ni $ especiales, sin grupos
    (?...), sin cuantificadores perezosos, con \\i, \\c, \\p{...} y resta de clases [a-z-[aeiou]]."""

    def cuantificar(self, atomo):
        c = self.ver()
        if c in ('*', '+', '?'):
            self.comer()
            minimo, maximo = {'*': (0, None), '+': (1, None), '?': (0, 1)}[c]
        elif c == '{':
            limites = self._limites()
            if limites is None:
                self.error('cuantificador {n,m} mal escrito')
            minimo, maximo = limites
            if maximo is not None and maximo < minimo:
                self.error('el máximo de la repetición es menor que el mínimo')
        else:
            return atomo
        if self.ver() in ('*', '+', '?', '{'):
            self.error('dos cuantificadores seguidos')
        return ('rep', atomo, minimo, maximo)

    def atomo(self):
        c = self.comer()
        if c == '(':
            dentro = self.alternativa()
            if self.ver() != ')':
                self.error('falta el paréntesis de cierre')
            self.comer()
            return dentro
        if c == '[':
            return ('car', self.clase())
        if c == '.':
            return ('car', PUNTO_XSD)
        if c in '*+?{':
            self.error('cuantificador sin nada que repetir')
        if c in ']}':
            self.error(f'«{c}» debe escribirse con barra invertida')
        if c == '\\':
            return ('car', self.escape(en_clase=False))
        return ('car', ((ord(c), ord(c)),))

    def escape(self, en_clase):
        if self.ver() is None:
            self.error('barra invertida al final')
        c = self.comer()
        if c in _CONTROL_XSD:
            return ((_CONTROL_XSD[c],) * 2,)
        if c in _ESCAPABLES_XSD:
            return ((ord(c), ord(c)),)
        if c in 'sSiIcCdDwW':
            return _clase_xsd(c)
        if c in 'pP':
            if self.ver() != '{' or '}' not in self.t[self.i:]:
                self.error(f'\\{c} necesita una categoría entre llaves')
            fin = self.t.index('}', self.i)
            nombre = self.t[self.i + 1:fin]
            self.i = fin + 1
            if nombre.startswith('Is'):
                raise NoRegular('bloque Unicode (\\p{Is...})')
            try:
                rangos = categoria(nombre)
            except KeyError:
                self.error(f'categoría Unicode desconocida «{nombre}»')
            return rangos if c == 'p' else _complemento(rangos)
        self.error(f'escape desconocido \\{c}')

    def clase(self):
        negada = self.ver() == '^'
        if negada:
            self.comer()
        rangos, primero = [], True
        while True:
            c = self.ver()
            if c is None:
                self.error('clase de caracteres sin cerrar')
            if c == ']' and not primero:
                self.comer()
                break
            if c == '-' and self.ver(1) == '[' and not primero:   # resta de clases
                self.i += 2
                resta = self.clase()
                if self.ver() != ']':
                    self.error('después de la resta de clases debe cerrarse la clase')
                self.comer()
                base = _complemento(rangos) if negada else _normalizar(rangos)
                return _normalizar(_diferencia(base, resta))
            if c == '[':
                self.error('«[» dentro de una clase debe escribirse con barra invertida')
            izquierda = self._elemento_de_clase()
            primero = False
            if self.ver() == '-' and self.ver(1) not in (None, ']', '['):
                self.comer()
                derecha = self._elemento_de_clase()
                if len(izquierda) != 1 or len(derecha) != 1 or izquierda[0][0] != izquierda[0][1] \
                        or derecha[0][0] != derecha[0][1]:
                    self.error('rango de caracteres con una clase en un extremo')
                if izquierda[0][0] > derecha[0][0]:
                    self.error('rango de caracteres al revés')
                rangos.append((izquierda[0][0], derecha[0][0]))
            else:
                rangos += izquierda
        rangos = _normalizar(rangos)
        return _complemento(rangos) if negada else rangos

    def _elemento_de_clase(self):
        c = self.comer()
        if c == '\\':
            return self.escape(en_clase=True)
        return ((ord(c), ord(c)),)


def _diferencia(base, resta):
    """Los rangos de base menos los de resta."""
    return _normalizar([r for r in _interseccion(base, _complemento(resta))])


def _interseccion(a, b):
    salida = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if lo <= hi:
                salida.append((lo, hi))
    return salida


def analizar_xsd(texto: str):
    """Árbol de una expresión de XSD (sin el anclaje, que añade afd())."""
    return _AnalizadorXSD(texto).analizar()


# ---------------------------------------------------------------------------
# AFN de Thompson sobre bytes (0..255) más ⊢ y ⊣
# ---------------------------------------------------------------------------
class _AFN:
    def __init__(self):
        self.aristas, self.vacias = [], []   # por estado: [(lo, hi, destino)] y [destino]

    def estado(self):
        if len(self.aristas) >= MAX_AFN:
            raise NoRegular('la expresión es demasiado grande')
        self.aristas.append([])
        self.vacias.append([])
        return len(self.aristas) - 1

    def fragmento(self, nodo):
        """(entrada, salida) del fragmento que reconoce el nodo."""
        clase = nodo[0]
        e, s = self.estado(), self.estado()
        if clase == 'car':
            for lo, hi in nodo[1]:
                for secuencia in secuencias_utf8(lo, hi):
                    q = e
                    for k, (b1, b2) in enumerate(secuencia):
                        r = s if k == len(secuencia) - 1 else self.estado()
                        self.aristas[q].append((b1, b2, r))
                        q = r
        elif clase == 'cat':
            q = e
            for hijo in nodo[1]:
                he, hs = self.fragmento(hijo)
                self.vacias[q].append(he)
                q = hs
            self.vacias[q].append(s)
        elif clase == 'alt':
            for hijo in nodo[1]:
                he, hs = self.fragmento(hijo)
                self.vacias[e].append(he)
                self.vacias[hs].append(s)
        elif clase == 'rep':
            _, hijo, minimo, maximo = nodo
            q = e
            for _ in range(minimo):
                he, hs = self.fragmento(hijo)
                self.vacias[q].append(he)
                q = hs
            if maximo is None:
                he, hs = self.fragmento(hijo)
                self.vacias[q] += [he, s]
                self.vacias[hs] += [he, s]
            else:
                for _ in range(maximo - minimo):
                    he, hs = self.fragmento(hijo)
                    self.vacias[q] += [he, s]
                    q = hs
                self.vacias[q].append(s)
        else:  # ancla
            simbolo = INICIO if clase == 'ini' else FIN
            self.aristas[e].append((simbolo, simbolo, s))
        return e, s

    def bucle(self, q):
        self.aristas[q].append((0, FIN, q))   # Σ' = todos los bytes, ⊢ y ⊣


@dataclass
class AFD:
    """AFD sobre bytes. Falta de arista = estado muerto (ninguna etiqueta). inicio None: siempre muerto.

    aristas[q]: [(lo, hi, destino)] ordenadas y disjuntas.  etiquetas[q]: etiquetas que aceptan si la
    cadena termina en q.
    """
    inicio: int | None
    aristas: list
    etiquetas: list

    def correr(self, datos: bytes) -> frozenset:
        q = self.inicio
        for b in datos:
            if q is None:
                break
            q = next((d for lo, hi, d in self.aristas[q] if lo <= b <= hi), None)
        return frozenset() if q is None else self.etiquetas[q]

    def alcanzables(self):
        """Conjuntos de etiquetas de todos los estados alcanzables (con el muerto, si se puede llegar a él)."""
        if self.inicio is None:
            return {frozenset()}
        vistos, pila, salida = {self.inicio}, [self.inicio], set()
        while pila:
            q = pila.pop()
            salida.add(self.etiquetas[q])
            cubiertos = 0
            for lo, hi, d in self.aristas[q]:
                cubiertos += hi - lo + 1
                if d not in vistos:
                    vistos.add(d)
                    pila.append(d)
            if cubiertos < 256:
                salida.add(frozenset())
        return salida


def _determinizar(afn, inicio, aceptacion):
    """Subconjuntos. Devuelve el AFD que empieza después de leer ⊢; las etiquetas se miden tras ⊣."""
    def cierre(estados):
        pila, visto = list(estados), set(estados)
        while pila:
            for d in afn.vacias[pila.pop()]:
                if d not in visto:
                    visto.add(d)
                    pila.append(d)
        return frozenset(visto)

    def mover(conjunto, simbolo):
        return cierre({d for q in conjunto for lo, hi, d in afn.aristas[q] if lo <= simbolo <= hi})

    def etiquetas(conjunto):
        return frozenset(aceptacion[q] for q in mover(conjunto, FIN) if q in aceptacion)

    primero = mover(cierre({inicio}), INICIO)
    if not primero:
        return AFD(None, [], [])
    numero, lista, aristas = {primero: 0}, [primero], [[]]
    i = 0
    while i < len(lista):
        conjunto = lista[i]
        cortes = {0, 256}
        for q in conjunto:
            for lo, hi, _ in afn.aristas[q]:
                if lo < 256:
                    cortes.update((lo, min(hi, 255) + 1))
        cortes = sorted(cortes)
        for a, b in zip(cortes, cortes[1:]):
            destino = cierre({d for q in conjunto for lo, hi, d in afn.aristas[q] if lo <= a <= hi})
            if not destino:
                continue
            if destino not in numero:
                if len(lista) >= MAX_ESTADOS:
                    raise NoRegular(f'el AFD pasaría de {MAX_ESTADOS} estados')
                numero[destino] = len(lista)
                lista.append(destino)
                aristas.append([])
            d = numero[destino]
            if aristas[i] and aristas[i][-1][2] == d and aristas[i][-1][1] == a - 1:
                aristas[i][-1] = (aristas[i][-1][0], b - 1, d)
            else:
                aristas[i].append((a, b - 1, d))
        i += 1
    return AFD(0, aristas, [etiquetas(c) for c in lista])


def minimizar(afd: AFD) -> AFD:
    """Refinamiento de Moore con el estado muerto explícito; el bloque del muerto vuelve a ser «sin arista»."""
    if afd.inicio is None:
        return afd
    n = len(afd.aristas)
    muerto = n
    etiquetas = afd.etiquetas + [frozenset()]

    def completas(q):
        """Las aristas de q cubriendo 0..255, con el muerto en los huecos."""
        if q == muerto:
            return [(0, 255, muerto)]
        salida, siguiente = [], 0
        for lo, hi, d in afd.aristas[q]:
            if lo > siguiente:
                salida.append((siguiente, lo - 1, muerto))
            salida.append((lo, hi, d))
            siguiente = hi + 1
        if siguiente <= 255:
            salida.append((siguiente, 255, muerto))
        return salida

    todas = [completas(q) for q in range(n + 1)]
    numeros = {}
    bloque = [numeros.setdefault(etiquetas[q], len(numeros)) for q in range(n + 1)]
    cuantos = len(numeros)
    while True:
        firmas = {}
        nuevo = []
        for q in range(n + 1):
            unidas = []
            for lo, hi, d in todas[q]:
                if unidas and unidas[-1][2] == bloque[d] and unidas[-1][1] == lo - 1:
                    unidas[-1] = (unidas[-1][0], hi, bloque[d])
                else:
                    unidas.append((lo, hi, bloque[d]))
            nuevo.append(firmas.setdefault((bloque[q], tuple(unidas)), len(firmas)))
        bloque = nuevo
        if len(firmas) == cuantos:
            break
        cuantos = len(firmas)

    if bloque[afd.inicio] == bloque[muerto]:
        return AFD(None, [], [])
    orden = {}  # bloque → nuevo número, en anchura desde el inicio
    pila = [afd.inicio]
    representante = {}
    while pila:
        q = pila.pop(0)
        if bloque[q] in orden:
            continue
        orden[bloque[q]] = len(orden)
        representante[bloque[q]] = q
        for _, _, d in todas[q]:
            if bloque[d] != bloque[muerto] and bloque[d] not in orden:
                pila.append(d)
    aristas, etiquetas_nuevas = [], []
    for b, _ in sorted(orden.items(), key=lambda x: x[1]):
        q = representante[b]
        unidas = []
        for lo, hi, d in todas[q]:
            if bloque[d] == bloque[muerto]:
                continue
            nd = orden[bloque[d]]
            if unidas and unidas[-1][2] == nd and unidas[-1][1] == lo - 1:
                unidas[-1] = (unidas[-1][0], hi, nd)
            else:
                unidas.append((lo, hi, nd))
        aristas.append(unidas)
        etiquetas_nuevas.append(etiquetas[q])
    return AFD(0, aristas, etiquetas_nuevas)


def afd(componentes) -> AFD:
    """AFD mínimo del producto de varios componentes, cada uno (etiqueta, clase, dato):
        ('patron', texto)    búsqueda con la semántica de «pattern» de JSON Schema (ECMA-262);
        ('xsd', textos)      la cadena completa casa con alguna de las expresiones de XSD (las facetas
                             pattern de un mismo paso de restricción se combinan con «o»);
        ('literal', bytes)   la cadena completa es exactamente esos bytes.
    Lanza NoRegular o ErrorDeExpresion."""
    a = _AFN()
    inicio = a.estado()
    aceptacion = {}
    for etiqueta, clase, dato in componentes:
        e = a.estado()
        a.vacias[inicio].append(e)
        if clase == 'patron':
            a.bucle(e)
            fe, fs = a.fragmento(analizar(dato))
            a.vacias[e].append(fe)
            fin = a.estado()
            a.vacias[fs].append(fin)
            a.bucle(fin)
        elif clase == 'xsd':   # anclada: ⊢ R ⊣
            q = a.estado()
            a.aristas[e].append((INICIO, INICIO, q))
            fe, fs = a.fragmento(('alt', [analizar_xsd(texto) for texto in dato]))
            a.vacias[q].append(fe)
            fin = a.estado()
            a.aristas[fs].append((FIN, FIN, fin))
        else:
            q = a.estado()
            a.aristas[e].append((INICIO, INICIO, q))
            for b in dato:
                r = a.estado()
                a.aristas[q].append((b, b, r))
                q = r
            fin = a.estado()
            a.aristas[q].append((FIN, FIN, fin))
        aceptacion[fin] = etiqueta
    return minimizar(_determinizar(a, inicio, aceptacion))


@lru_cache(maxsize=4096)
def de_patron(texto: str) -> AFD:
    """AFD de un patrón solo, con la etiqueta 0."""
    return afd([(0, 'patron', texto)])


def regular(texto: str):
    """None si el patrón se compila a un AFD; si no, el motivo. Lanza ErrorDeExpresion si es inválido."""
    try:
        de_patron(texto)
    except NoRegular as e:
        return str(e)
    return None


def coincide(texto: str, cadena: str) -> bool:
    """True si el patrón aparece en la cadena (semántica de «pattern»)."""
    return bool(de_patron(texto).correr(cadena.encode('utf-8')))


@lru_cache(maxsize=4096)
def de_xsd(textos: tuple) -> AFD:
    """AFD de un grupo de facetas pattern de XSD (alternativas), con la etiqueta 0."""
    return afd([(0, 'xsd', textos)])


def regular_xsd(texto: str):
    """Como regular(), para una expresión de XSD."""
    try:
        de_xsd((texto,))
    except NoRegular as e:
        return str(e)
    return None


def coincide_xsd(textos, cadena: str) -> bool:
    """True si la cadena completa casa con alguna de las expresiones de XSD."""
    return bool(de_xsd(tuple(textos)).correr(cadena.encode('utf-8')))


# ---------------------------------------------------------------------------
# Ejemplos: cadenas que casan con unos patrones (para generar datos de prueba)
# ---------------------------------------------------------------------------
_CARACTERES = [chr(c) for c in range(0x20, 0x7F)] + list('ñé€😀\t')


@lru_cache(maxsize=256)
def _grafo_de_ejemplos(patrones: tuple):
    """El AFD del producto de los patrones recorrido por caracteres de una muestra: para cada estado,
    sus sucesores por carácter y la distancia (en caracteres) a un estado que casa con todos. Cada patrón
    es un texto (de JSON Schema) o una tupla de textos (un grupo de XSD)."""
    d = afd([(i, 'xsd' if isinstance(p, tuple) else 'patron', p) for i, p in enumerate(patrones)])
    if d.inicio is None:
        return None
    literales = {c for p in patrones for texto in (p if isinstance(p, tuple) else (p,)) for c in texto}
    caracteres = sorted(set(_CARACTERES) | {c for c in literales if not c.isascii()})
    sucesores, pila = {}, [d.inicio]
    while pila:
        q = pila.pop()
        if q in sucesores:
            continue
        sucesores[q] = []
        for c in caracteres:
            r = q
            for b in c.encode('utf-8'):
                r = next((destino for lo, hi, destino in d.aristas[r] if lo <= b <= hi), None)
                if r is None:
                    break
            if r is not None:
                sucesores[q].append((c, r))
                pila.append(r)
    distancia = {q: 0 for q in sucesores if len(d.etiquetas[q]) == len(patrones)}
    cambio = True
    while cambio:   # Bellman-Ford hacia atrás: pocas decenas de estados
        cambio = False
        for q, salidas in sucesores.items():
            mejor = min((distancia[r] + 1 for _, r in salidas if r in distancia), default=None)
            if mejor is not None and mejor < distancia.get(q, mejor + 1):
                distancia[q], cambio = mejor, True
    return d.inicio, sucesores, distancia


def ejemplo(patrones, rng, largo=6):
    """Una cadena que casa con todos los patrones, elegida al azar, o None si no se encuentra (con los
    caracteres de la muestra). Lanza NoRegular si algún patrón no se puede compilar."""
    grafo = _grafo_de_ejemplos(tuple(patrones))
    if grafo is None:
        return None
    q, sucesores, distancia = grafo
    if q not in distancia:
        return None
    salida = []
    for paso in range(4 * largo + 50):
        if distancia[q] == 0 and (paso >= largo or rng.random() < 0.25):
            break
        utiles = [(c, r) for c, r in sucesores[q] if r in distancia]
        if paso >= largo:   # ya es largo: se va derecho a un estado que casa
            utiles = [(c, r) for c, r in utiles if distancia[r] < distancia[q]] or utiles
        if not utiles:      # ya casa y no se puede alargar
            break
        c, q = rng.choice(utiles)
        salida.append(c)
    return ''.join(salida) if distancia[q] == 0 else None
