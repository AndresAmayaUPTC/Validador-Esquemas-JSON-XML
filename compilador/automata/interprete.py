"""Intérprete de referencia del autómata (Fases 4 y 6): la semántica ejecutable de M.

Ejecuta el DPDA ponderado sobre los tokens del lexer, por bloques. Es deliberadamente simple: sirve para
probar el autómata contra un validador independiente y para probar el motor de Rust contra él.

Ruta del error. La posición dentro del documento se da como JSON Pointer (RFC 6901), reconstruido desde
la pila: cada marco recuerda la clave o el índice por el que va. Para que la memoria siga acotada, de
cada clave se guardan como mucho CLAVE_MAX bytes y la ruta completa no pasa de RUTA_MAX; lo que no cabe
se sustituye por «…».
"""
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..lexico.lexer import ErrorLexico, Lexer
from .dpda import RETORNO, Automata

MAX_PROFUNDIDAD = 128  # contenedores abiertos a la vez (marcos de pila sin contar el fondo)
CLAVE_MAX = 24         # bytes que se conservan de cada clave para la ruta
RUTA_MAX = 256         # bytes de la ruta completa
PUNTOS = '…'.encode('utf-8')


@dataclass
class Resultado:
    """estado:
        'valido'      cumple el esquema
        'pendiente'   cumple todo lo que el autómata verifica, pero cruzó comprobaciones diferidas
        'invalido'    tiene violaciones (peso > 0)
        'malformado'  no es JSON bien formado (error léxico, sintáctico o clave repetida)
        'limite'      superó un límite: profundidad de la pila, tamaño o tiempo
    """
    estado: str
    violaciones: int = 0                 # peso total de la ejecución
    pendientes: int = 0
    detalle: list = field(default_factory=list)   # (posición en bytes, motivo) de cada violación
    error: tuple | None = None           # (posición, motivo) si es 'malformado' o 'limite'
    profundidad: int = 0                 # máximo de contenedores abiertos a la vez
    ruta: str | None = None              # JSON Pointer del error o, si no lo hay, de la primera violación


def valor_decimal(lexema: str) -> Decimal:
    """Valor exacto del número. Si el exponente no cabe en un Decimal, devuelve un infinito o un valor
    diminuto con su signo: para compararlo con cualquier límite del esquema el resultado es el mismo."""
    try:
        return Decimal(lexema)
    except InvalidOperation:
        mantisa, _, exponente = lexema.lower().partition('e')
        if not mantisa.strip('-0.'):
            return Decimal(0)
        signo = '-' if lexema.startswith('-') else ''
        return Decimal(signo + ('1e-999999999999' if exponente.startswith('-') else 'Infinity'))


def falla(prueba, token) -> bool:
    """True si el valor del token no cumple la prueba."""
    clase = prueba[0]
    if clase == 'long_min':
        return len(token.valor) < prueba[1]   # longitud en caracteres (puntos de código), no en bytes
    if clase == 'long_max':
        return len(token.valor) > prueba[1]
    if clase == 'enum_cad':
        return token.valor not in prueba[1]
    d = valor_decimal(token.valor)
    if clase == 'entero':
        return d != d.to_integral_value()     # 1.0 y 1e2 son enteros; 1.5 y 1e-3 no
    if clase == 'enum_num':
        return d not in prueba[1]             # igualdad numérica: 1 y 1.0 son el mismo valor
    limite, exclusivo = prueba[1], prueba[2]
    if clase == 'min':
        return d <= limite if exclusivo else d < limite
    return d >= limite if exclusivo else d > limite


class _Marco:
    """Marco de pila: símbolo de Γ, los dos registros y la posición actual para la ruta."""
    __slots__ = ('simbolo', 'mascara', 'cuenta', 'es_objeto', 'clave', 'truncada')

    def __init__(self, simbolo, es_objeto=False):
        self.simbolo, self.mascara, self.cuenta = simbolo, 0, 0
        self.es_objeto, self.clave, self.truncada = es_objeto, None, False

    def segmento(self):
        """Tramo de la ruta que aporta este marco, o None si aún no ha leído nada."""
        if self.es_objeto:
            if self.clave is None:
                return None
            return b'/' + self.clave.replace(b'~', b'~0').replace(b'/', b'~1') + (PUNTOS if self.truncada else b'')
        return b'/%d' % (self.cuenta - 1) if self.cuenta else None


def recortar(nombre: str):
    """Los primeros CLAVE_MAX bytes de la clave, sin partir ningún carácter, y si hubo que recortar."""
    datos = nombre.encode('utf-8')
    if len(datos) <= CLAVE_MAX:
        return datos, False
    corte = CLAVE_MAX
    while datos[corte] & 0xC0 == 0x80:  # no cortar en medio de un carácter
        corte -= 1
    return datos[:corte], True


class Validador:
    """Ejecuta el autómata sobre un documento que llega por bloques: feed(bloque)... y finish().

    Límites: max_profundidad (contenedores abiertos), max_bytes (tamaño del documento) y plazo (segundos
    desde que se crea el validador; se comprueba al empezar cada bloque).
    """

    def __init__(self, automata: Automata, max_profundidad=MAX_PROFUNDIDAD, fallo_temprano=False,
                 max_bytes=None, plazo=None):
        self.a, self.max_profundidad, self.fallo_temprano = automata, max_profundidad, fallo_temprano
        self.max_bytes = max_bytes
        self._vence = None if plazo is None else time.monotonic() + plazo
        self._lexer = Lexer()
        self._estado = automata.inicial
        self._pila = [_Marco(automata.pila_inicial)]
        self._bytes = 0
        self._r = Resultado('valido')
        self._terminado = False

    def feed(self, bloque: bytes):
        if self._terminado:
            return
        if self._vence is not None and time.monotonic() >= self._vence:
            return self._detener('limite', self._bytes, 'tiempo máximo superado')
        excede = self.max_bytes is not None and self._bytes + len(bloque) > self.max_bytes
        if excede:
            bloque = bloque[:self.max_bytes - self._bytes]
        self._bytes += len(bloque)
        self._lexico(lambda: self._lexer.feed(bloque))
        if excede:
            self._detener('limite', self.max_bytes, 'tamaño máximo superado')

    def progreso(self) -> dict:
        """Estado parcial de la ejecución, para mostrar la validación mientras llegan los bloques."""
        return {'bytes': self._bytes, 'violaciones': self._r.violaciones, 'pendientes': self._r.pendientes,
                'profundidad': len(self._pila) - 1, 'profundidad_maxima': self._r.profundidad,
                'estado_del_automata': self.a.estados[self._estado], 'ruta': self._ruta(True),
                'pila': [self.a.simbolos_pila[m.simbolo] for m in self._pila], 'terminado': self._terminado}

    def finish(self) -> Resultado:
        if not self._terminado:
            self._lexico(self._lexer.finish)
        r = self._r
        if r.error is None:  # el token FIN llevó al estado de aceptación, o se cortó por fallo temprano
            r.estado = 'invalido' if r.violaciones else 'pendiente' if r.pendientes else 'valido'
        return r

    def _lexico(self, obtener):
        try:
            tokens = obtener()
        except ErrorLexico as e:
            tokens, error = e.tokens, e
        else:
            error = None
        for token in tokens:
            if self._terminado:
                return
            self._paso(token)
        if error is not None:
            self._detener('malformado', error.pos, 'léxico: ' + str(error))

    def _ruta(self, con_la_cima) -> str:
        """JSON Pointer de la posición actual. Sin la cima, es la ruta del contenedor abierto."""
        marcos = self._pila[1:] if con_la_cima else self._pila[1:-1]
        ruta = b''
        for marco in marcos:
            tramo = marco.segmento()
            if tramo is None:
                break
            if len(ruta) + len(tramo) > RUTA_MAX - len(PUNTOS):
                ruta += PUNTOS
                break
            ruta += tramo
        return ruta.decode('utf-8')

    def _detener(self, estado, pos, motivo):
        if not self._terminado:
            r = self._r
            r.estado, r.error, r.ruta, self._terminado = estado, (pos, motivo), self._ruta(False), True

    def _violar(self, token, motivo, cuantas=1, del_contenedor=False):
        """Suma violaciones. del_contenedor: la violación es del objeto o arreglo de la cima (falta una
        propiedad, sobran elementos), no del valor que se está leyendo."""
        if cuantas:
            if not self._r.violaciones:
                self._r.ruta = self._ruta(not del_contenedor)
            self._r.violaciones += cuantas
            self._r.detalle += [(token.inicio, motivo)] * cuantas

    def _paso(self, token):
        a, transiciones = self.a, self.a.delta[self._estado]
        marco = self._pila[-1]
        simbolo = token.tipo
        if token.tipo == 'CADENA' and 'CADENA' not in transiciones:  # posición de clave
            simbolo = ('CLAVE', token.valor) if ('CLAVE', token.valor) in transiciones else ('CLAVE', None)
        t = transiciones.get(simbolo)
        if t is None:
            return self._detener('malformado', token.inicio, f'sintáctico: no se esperaba {token.tipo}')

        if t.accion:
            clase, *p = t.accion
            if clase == 'clave':
                bit, maximo = p
                marco.clave, marco.truncada = recortar(token.valor)
                marco.cuenta += 1
                if maximo is not None and marco.cuenta == maximo + 1:
                    self._violar(token, 'maxProperties', del_contenedor=True)
                if bit is not None:
                    if marco.mascara >> bit & 1:
                        return self._detener('malformado', token.inicio, 'clave repetida')
                    marco.mascara |= 1 << bit
            elif clase == 'cerrar_objeto':
                requeridas, dependencias, minimo = p
                self._violar(token, 'required', bin(requeridas & ~marco.mascara).count('1'), True)
                for bit, exige in dependencias:
                    if marco.mascara >> bit & 1:
                        self._violar(token, 'dependentRequired', bin(exige & ~marco.mascara).count('1'), True)
                if minimo is not None and marco.cuenta < minimo:
                    self._violar(token, 'minProperties', del_contenedor=True)
            elif clase == 'elemento':
                marco.cuenta += 1
                if p[0] is not None and marco.cuenta == p[0] + 1:
                    self._violar(token, 'maxItems', del_contenedor=True)
            elif clase == 'cerrar_arreglo':
                if p[0] is not None and marco.cuenta < p[0]:
                    self._violar(token, 'minItems', del_contenedor=True)

        self._violar(token, 'no admitido', t.peso)
        for prueba in t.pruebas:  # ya vienen ordenadas de menor a mayor costo
            if falla(prueba, token):
                self._violar(token, prueba[0])
                if self.fallo_temprano:
                    break
        self._r.pendientes += t.pendientes

        if t.desapila:
            self._pila.pop()
        if t.cima is not None:
            marco.simbolo = t.cima
        if t.apila is not None:
            if len(self._pila) > self.max_profundidad:
                return self._detener('limite', token.inicio, 'profundidad máxima superada')
            self._pila.append(_Marco(t.apila, es_objeto=token.tipo == 'LLAVE_ABRE'))
            self._r.profundidad = max(self._r.profundidad, len(self._pila) - 1)
        self._estado = a.retorno[self._pila[-1].simbolo] if t.destino is RETORNO else t.destino

        if self._r.violaciones and self.fallo_temprano:
            self._r.estado, self._terminado = 'invalido', True


def validar(automata: Automata, datos: bytes, cortes=(), **opciones) -> Resultado:
    """Valida datos partiéndolos en bloques de los tamaños dados (el resto va en un último bloque)."""
    v, i = Validador(automata, **opciones), 0
    for n in cortes:
        v.feed(datos[i:i + n])
        i += n
    v.feed(datos[i:])
    return v.finish()
