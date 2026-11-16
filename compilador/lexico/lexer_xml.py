"""Lexer de XML en streaming (Fase 1, XML): el AFD de afd.construir_xml() más las acciones de valor.

Tokens (Tabla 10 del documento): DECL_XML, ETIQ_ABRE, ETIQ_CIERRE_ABRE, NOMBRE, IGUAL, VALOR_ATRIB,
ETIQ_FIN, ETIQ_AUTOCIERRE, TEXTO y FIN. Reglas de valor:

  NOMBRE       el nombre; cada carácter no ASCII se comprueba contra NameStartChar/NameChar al completarse.
  VALOR_ATRIB  referencias resueltas y normalización de XML 1.0 §3.3.3: tab, CR y LF literales pasan a
               espacio (CRLF es un solo espacio). Los caracteres que llegan por referencia no se tocan.
  TEXTO        todo el texto que hay entre dos etiquetas es UN token, aunque lo formen datos, secciones
               CDATA y referencias, y aunque haya comentarios o instrucciones de procesamiento en medio.
               Se emite justo antes del token de la etiqueta siguiente (o de FIN). Su intervalo va del
               primer byte del primer tramo al final del último. CRLF y CR literales pasan a LF (§2.11).
               Nunca se emite vacío.

Posición de los errores: la del byte en el que el autómata no puede continuar; para un carácter no
permitido en un nombre, la de su último byte; para una referencia numérica inválida, la de su «;».

Las acciones dependen solo del estado de origen y del de destino, igual que en el lexer de JSON, para
poder exportarlas como tabla al lexer de Rust.
"""
from .afd import EOF, construir_xml
from .lexer import MOTIVOS, ErrorLexico, Token

MOTIVOS.update({
    'nombre': 'carácter no permitido en un nombre',
    'referencia': 'referencia a un carácter no permitido',
    'nombre-largo': 'nombre demasiado largo',
})

MAX_NOMBRE = 1024
ENTIDADES = {'lt': 0x3C, 'gt': 0x3E, 'amp': 0x26, 'quot': 0x22, 'apos': 0x27}
_NOMBRES = ('TN', 'AN', 'CN')            # nombre de elemento, de atributo y de etiqueta de cierre

# NameStartChar y lo que añade NameChar, fuera de ASCII (XML 1.0, quinta edición)
_INICIO = ((0xC0, 0xD6), (0xD8, 0xF6), (0xF8, 0x2FF), (0x370, 0x37D), (0x37F, 0x1FFF), (0x200C, 0x200D),
           (0x2070, 0x218F), (0x2C00, 0x2FEF), (0x3001, 0xD7FF), (0xF900, 0xFDCF), (0xFDF0, 0xFFFD), (0x10000, 0xEFFFF))
_RESTO = ((0xB7, 0xB7), (0x300, 0x36F), (0x203F, 0x2040))


def es_inicio_de_nombre(c):
    return any(a <= c <= b for a, b in _INICIO)


def es_caracter_de_nombre(c):
    return es_inicio_de_nombre(c) or any(a <= c <= b for a, b in _RESTO)


def es_char(c):
    """Producción Char de XML: lo que puede aparecer en un documento."""
    return c in (0x9, 0xA, 0xD) or 0x20 <= c <= 0xD7FF or 0xE000 <= c <= 0xFFFD or 0x10000 <= c <= 0x10FFFF


def _clase(q):
    """A qué valor contribuye el estado q (incluidos sus estados UTF-8 «·» y de referencia «&»)."""
    base = q.split('·')[0].split('&')[0]
    if base in ('TX', 'TX]', 'TX]]'):
        return 'texto'
    if base in ('AV"', "AV'"):
        return 'valor'
    if base in _NOMBRES:
        return 'nombre'
    if base in ('D', 'D1', 'D2', 'D3'):
        return 'cdata'
    return None


def acciones_xml(afd):
    """Devuelve op(estado, símbolo): la acción de valor de esa transición, como tupla (nombre, argumentos...).

      ('texto', k)            el byte es texto; k = bytes de un BOM incompleto que van delante (0, 1 o 2)
      ('cdata',)              byte de una sección CDATA (antes van los «]» que estuvieran pendientes)
      ('cdata_corchete',)     «]» dentro de CDATA: puede ser el principio de «]]>»
      ('cdata_fin',)          «>» que cierra la sección: los «]]» pendientes no son texto
      ('nombre', k)           byte de un nombre: k = 0 si es ASCII, 1 si empieza o continúa un carácter de
                              varios bytes y 2 si lo completa (entonces se comprueba el carácter)
      ('valor_ini',)          comilla que abre un valor de atributo
      ('valor',)              byte de un valor de atributo
      ('ref_ini',) ('ref_dec',) ('ref_hex',)   comienzo y dígitos de una referencia
      ('ref_num', destino)    «;» de una referencia numérica; destino es 'texto' o 'valor'
      ('entidad', destino, c) «;» de una de las cinco entidades predefinidas, que vale el carácter c
      ('nada',)
    """
    def op(s, b):
        t = afd.delta[s][b]
        if t is None:
            return None
        d = t[0]
        cs, cd = _clase(s), _clase(d)
        if '&' in d:
            if '&' not in s:
                return ('ref_ini',)
            return ('ref_dec',) if d.endswith('#d') else ('ref_hex',) if d.endswith('#xh') else ('nada',)
        if '&' in s:  # el «;» que cierra la referencia
            destino = 'texto' if cs == 'texto' else 'valor'
            if s.endswith('#d') or s.endswith('#xh'):
                return ('ref_num', destino)
            return ('entidad', destino, ENTIDADES[s.rsplit('/', 1)[1]])
        if cd == 'texto':
            return ('texto', {'BOM1': 1, 'BOM2': 2}.get(s, 0))
        if cd == 'cdata':
            if cs != 'cdata':
                return ('nada',)   # el «[» que abre la sección
            return ('cdata_corchete',) if d in ('D2', 'D3') and b == 0x5D else ('cdata',)
        if cs == 'cdata':
            return ('cdata_fin',)
        if cd == 'nombre':
            return ('nombre', 1 if '·' in d else 2 if '·' in s else 0)
        if cd == 'valor':
            return ('valor_ini',) if s == 'AQ' else ('valor',)
        return ('nada',)
    return op


_AFD = construir_xml()
_op = acciones_xml(_AFD)
_OPS = {s: [_op(s, b) for b in range(257)] for s in _AFD.delta}


def solo_espacios(token) -> bool:
    """Indicador «solo espacios» de un TEXTO: no tiene nada más que espacio, tab, CR y LF."""
    return token.tipo == 'TEXTO' and not token.valor.strip(' \t\r\n')


class LexerXML:
    """Lexer de XML por bloques: feed(bloque) las veces que haga falta y finish() al cerrar el flujo.
    El análisis se detiene en el primer error; las llamadas posteriores vuelven a lanzarlo."""

    def __init__(self):
        self._estado, self._pos, self._inicio = _AFD.inicial, 0, 0
        self._nombre, self._valor, self._texto = bytearray(), bytearray(), bytearray()
        self._tramo = None          # [inicio, fin) del texto pendiente de emitir
        self._cr = False            # el byte anterior fue un CR literal
        self._corchetes = 0         # «]» de CDATA que aún no se sabe si son texto
        self._ref = 0               # valor de la referencia numérica en curso
        self._error = None

    def feed(self, bloque: bytes) -> list:
        return self._consumir(bloque)

    def finish(self) -> list:
        return self._consumir((EOF,))

    # ---- valores ----
    def _a_texto(self, b):
        if b == 0x0D:
            self._texto.append(0x0A)
        elif not (b == 0x0A and self._cr):   # el LF de un CRLF ya se contó
            self._texto.append(b)
        self._cr = b == 0x0D

    def _a_valor(self, b):
        if b in (0x09, 0x0A, 0x0D):
            if not (b == 0x0A and self._cr):
                self._valor.append(0x20)
        else:
            self._valor.append(b)
        self._cr = b == 0x0D

    def _caracter(self, destino, c):
        """Añade un carácter que llega por referencia: no se normaliza."""
        (self._texto if destino == 'texto' else self._valor).extend(chr(c).encode('utf-8'))
        self._cr = False

    def _emitir(self, tipo, i, f, salida):
        if tipo == 'TEXTO':         # se acumula: puede seguir tras un comentario o una sección CDATA
            self._tramo = (self._tramo[0], f) if self._tramo else (i, f)
            return
        if tipo in ('ETIQ_ABRE', 'ETIQ_CIERRE_ABRE', 'FIN'):
            if self._tramo and self._texto:
                salida.append(Token('TEXTO', *self._tramo, self._texto.decode('utf-8')))
            self._tramo, self._cr = None, False
            self._texto.clear()
        valor = None
        if tipo == 'NOMBRE':
            valor = self._nombre.decode('utf-8')
        elif tipo == 'VALOR_ATRIB':
            valor = self._valor.decode('utf-8')
        salida.append(Token(tipo, i, f, valor))

    def _consumir(self, simbolos):
        if self._error:
            raise self._error
        salida = []
        for sym in simbolos:
            t = _AFD.delta[self._estado][sym]
            if t is None:
                self._fallar('fin' if sym == EOF else 'byte', salida)
            siguiente, emitidos, marca = t
            pos = self._pos
            for tipo, modo in emitidos:
                i, f = {'prev': (self._inicio, pos), 'close': (self._inicio, pos + 1),
                        'single': (pos, pos + 1), 'eof': (pos, pos)}[modo]
                self._emitir(tipo, i, f, salida)
            if marca:
                self._inicio = pos
            op = _OPS[self._estado][sym]
            clase = op[0]
            if clase == 'texto':
                for b in (0xEF, 0xBB)[:op[1]]:
                    self._a_texto(b)
                self._a_texto(sym)
            elif clase == 'nombre':
                if marca:
                    self._nombre.clear()
                if len(self._nombre) == MAX_NOMBRE:
                    self._fallar('nombre-largo', salida)
                self._nombre.append(sym)
                if op[1] == 2:   # se completó un carácter de varios bytes
                    i = len(self._nombre) - 1
                    while self._nombre[i] & 0xC0 == 0x80:
                        i -= 1
                    c = ord(self._nombre[i:].decode('utf-8'))
                    if not (es_inicio_de_nombre(c) if i == 0 else es_caracter_de_nombre(c)):
                        self._fallar('nombre', salida)
            elif clase == 'valor':
                self._a_valor(sym)
            elif clase == 'valor_ini':
                self._valor.clear()
                self._cr = False
            elif clase == 'cdata':
                for _ in range(self._corchetes):
                    self._a_texto(0x5D)
                self._corchetes = 0
                self._a_texto(sym)
            elif clase == 'cdata_corchete':
                if self._corchetes == 2:
                    self._a_texto(0x5D)     # con tres seguidos, el primero ya es texto seguro
                else:
                    self._corchetes += 1
            elif clase == 'cdata_fin':
                self._corchetes = 0
            elif clase == 'ref_ini':
                self._ref = 0
            elif clase == 'ref_dec':
                self._ref = min(self._ref * 10 + sym - 0x30, 0x110000)
            elif clase == 'ref_hex':
                self._ref = min(self._ref * 16 + int(chr(sym), 16), 0x110000)
            elif clase == 'ref_num':
                if not es_char(self._ref):
                    self._fallar('referencia', salida)
                self._caracter(op[1], self._ref)
            elif clase == 'entidad':
                self._caracter(op[1], op[2])
            self._estado = siguiente
            if sym != EOF:
                self._pos += 1
        return salida

    def _fallar(self, motivo, tokens):
        self._error = ErrorLexico(self._pos, motivo, tokens)
        raise self._error


def tokenizar_xml(datos: bytes, cortes=()):
    """Tokeniza datos partiéndolos en bloques de los tamaños dados (el resto va en un último bloque).
    Devuelve (tokens, error): error es None o el ErrorLexico que detuvo el análisis."""
    lexer, tokens, i = LexerXML(), [], 0
    try:
        for n in cortes:
            tokens += lexer.feed(datos[i:i + n])
            i += n
        tokens += lexer.feed(datos[i:])
        tokens += lexer.finish()
    except ErrorLexico as e:
        return tokens + e.tokens, e
    return tokens, None
