"""Lexer de JSON en streaming (Fase 1, paso 5).

Es el AFD de afd.py más las *acciones de valor*: lo que hay que hacer en cada transición para obtener
el valor del token (el texto decodificado de una CADENA o el lexema de un NUMERO).

Este lexer es el que usa el compilador para leer esquemas, y también la referencia contra la que se
prueba el lexer de Rust: los dos ejecutan el mismo autómata con las mismas acciones.
"""
from dataclasses import dataclass

from .afd import EOF, construir_json

# ---------------------------------------------------------------------------
# Acciones de valor. Dependen solo del estado de origen y del de destino, de modo
# que no dividen las clases de bytes del AFD.
# ---------------------------------------------------------------------------
NADA, INICIO_CADENA, LITERAL, ESCAPE, HEX, HEX_ALTO, HEX_FIN, NUMERO = range(8)
NOMBRES_OP = ('Nada', 'InicioCadena', 'Literal', 'Escape', 'Hex', 'HexAlto', 'HexFin', 'Numero')

_ESTADOS_NUMERO = {f'N{i}' for i in range(1, 9)}
# transiciones que leen uno de los tres primeros dígitos de \uXXXX (el cuarto es HEX_ALTO o HEX_FIN)
_DIGITOS_HEX = {('C3', 'H3'), ('C3', 'C4'), ('C4', 'H2'), ('C4', 'A2'), ('H3', 'H2'), ('H2', 'H1'),
                ('A2', 'A1'), ('B2', 'B3'), ('B3', 'H2')}
ESCAPES = {ord('"'): 0x22, ord('\\'): 0x5C, ord('/'): 0x2F, ord('b'): 0x08, ord('f'): 0x0C,
           ord('n'): 0x0A, ord('r'): 0x0D, ord('t'): 0x09}


def acciones_json(afd):
    """Devuelve op(estado, símbolo): la acción de valor de esa transición del AFD de JSON (None si no existe)."""
    en_cadena = lambda q: q == 'C1' or q.startswith('C·')  # contenido de la cadena y sus estados UTF-8

    def op(s, b):
        t = afd.delta[s][b]
        if t is None:
            return None
        d = t[0]
        if d in _ESTADOS_NUMERO:
            return NUMERO          # el byte pertenece al lexema del número
        if en_cadena(s) and en_cadena(d):
            return LITERAL         # el byte pasa tal cual al valor de la cadena
        if d == 'C1' and s in ('S0', 'S'):
            return INICIO_CADENA   # comilla de apertura
        if (s, d) == ('C2', 'C1'):
            return ESCAPE          # \n, \t, \", ...
        if (s, d) == ('A1', 'B0'):
            return HEX_ALTO        # cuarto dígito de un sustituto alto: se guarda y se espera el bajo
        if (s, d) == ('H1', 'C1'):
            return HEX_FIN         # cuarto dígito: se emite el punto de código
        if (s, d) in _DIGITOS_HEX:
            return HEX
        return NADA
    return op


_AFD = construir_json()
_op = acciones_json(_AFD)
_OPS = {s: [_op(s, b) for b in range(257)] for s in _AFD.delta}


# ---------------------------------------------------------------------------
# Tokens y errores
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Token:
    """Token (tipo, valor, i, f) de la Definición 5. [inicio, fin) es su intervalo en bytes.

    valor: texto decodificado de una CADENA, lexema exacto de un NUMERO y None en los demás tokens.
    """
    tipo: str
    inicio: int
    fin: int
    valor: str | None = None

    @property
    def es_entero(self):
        """Indicador léxico entero/real de un NUMERO: no tiene parte decimal ni exponente."""
        return self.tipo == 'NUMERO' and not set(self.valor) & set('.eE')


MOTIVOS = {'byte': 'byte inesperado', 'fin': 'fin de entrada inesperado', 'largo': 'número demasiado largo'}


class ErrorLexico(Exception):
    """Error léxico en la posición pos (en bytes). motivo ∈ MOTIVOS.
    tokens: los tokens completos que la misma llamada produjo antes del error."""

    def __init__(self, pos, motivo, tokens=()):
        super().__init__(f'{MOTIVOS[motivo]} en el byte {pos}')
        self.pos, self.motivo, self.tokens = pos, motivo, list(tokens)


def _hex(b):
    return b - 0x30 if b <= 0x39 else (b | 0x20) - 0x57


class Lexer:
    """Lexer de JSON por bloques: feed(bloque) las veces que haga falta y finish() al cerrar el flujo.

    Entre bloques solo se conserva el estado del autómata, el inicio del token en curso y su valor
    parcial, así que un token o un carácter UTF-8 pueden quedar partidos entre dos bloques.
    El análisis se detiene en el primer error; las llamadas posteriores vuelven a lanzarlo.
    """

    def __init__(self, max_numero=1024):
        self._estado, self._pos, self._inicio = _AFD.inicial, 0, 0
        self._cadena, self._numero = bytearray(), bytearray()
        self._acumulado = self._alto = 0
        self._max_numero, self._error = max_numero, None

    def feed(self, bloque: bytes) -> list:
        """Consume un bloque y devuelve los tokens que se completaron con él."""
        return self._consumir(bloque)

    def finish(self) -> list:
        """Entrega ⊣: devuelve los tokens pendientes y el token FIN."""
        return self._consumir((EOF,))

    def _consumir(self, simbolos):
        if self._error:
            raise self._error
        salida = []
        for sym in simbolos:
            t = _AFD.delta[self._estado][sym]
            if t is None:
                self._fallar('fin' if sym == EOF else 'byte', salida)
            siguiente, emitidos, marca = t
            for tipo, modo in emitidos:
                salida.append(self._token(tipo, modo))
            if marca:
                self._inicio = self._pos
            op = _OPS[self._estado][sym]
            if op == LITERAL:
                self._cadena.append(sym)
            elif op == NUMERO:
                if marca:
                    self._numero.clear()
                if len(self._numero) == self._max_numero:
                    self._fallar('largo', salida)
                self._numero.append(sym)
            elif op == INICIO_CADENA:
                self._cadena.clear()
            elif op == ESCAPE:
                self._cadena.append(ESCAPES[sym])
            elif op == HEX:
                self._acumulado = self._acumulado << 4 | _hex(sym)
            elif op == HEX_ALTO:
                self._alto, self._acumulado = self._acumulado << 4 | _hex(sym), 0
            elif op == HEX_FIN:
                punto = self._acumulado << 4 | _hex(sym)
                if self._alto:  # par de sustitutos: el AFD garantiza que aquí llega el bajo
                    punto = 0x10000 + ((self._alto - 0xD800) << 10) + (punto - 0xDC00)
                self._cadena += chr(punto).encode('utf-8')
                self._acumulado = self._alto = 0
            self._estado = siguiente
            if sym != EOF:
                self._pos += 1
        return salida

    def _token(self, tipo, modo):
        i, f = {'prev': (self._inicio, self._pos), 'close': (self._inicio, self._pos + 1),
                'single': (self._pos, self._pos + 1), 'eof': (self._pos, self._pos)}[modo]
        valor = None
        if tipo == 'CADENA':
            valor = self._cadena.decode('utf-8')
        elif tipo == 'NUMERO':
            valor = self._numero.decode('ascii')
        return Token(tipo, i, f, valor)

    def _fallar(self, motivo, tokens):
        self._error = ErrorLexico(self._pos, motivo, tokens)
        raise self._error


def tokenizar(datos: bytes, cortes=(), max_numero=1024):
    """Tokeniza datos partiéndolos en bloques de los tamaños dados (el resto va en un último bloque).
    Devuelve (tokens, error): error es None o el ErrorLexico que detuvo el análisis."""
    lexer, tokens, i = Lexer(max_numero), [], 0
    try:
        for n in cortes:
            tokens += lexer.feed(datos[i:i + n])
            i += n
        tokens += lexer.feed(datos[i:])
        tokens += lexer.finish()
    except ErrorLexico as e:
        return tokens + e.tokens, e
    return tokens, None
