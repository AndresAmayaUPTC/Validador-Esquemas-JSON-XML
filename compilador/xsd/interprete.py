"""Intérprete de referencia del autómata de XML (Fases 4 y 6, XML): la semántica ejecutable de M.

Mismo papel que compilador/automata/interprete.py para JSON: ejecuta el DPDA ponderado sobre los tokens
del lexer de XML, por bloques, y sirve de referencia para el motor de Rust.

Orden de cada paso: acción sobre el marco de la cima; reescritura de la cima y apilado; peso y pruebas
(así la ruta de una violación ya incluye el elemento que se acaba de abrir); desapilado; destino.

Ruta del error: /raiz/hijo[k]/@atributo, donde k es la posición del elemento entre los hijos elemento de
su padre (desde 1). Como en JSON, de cada nombre se guardan como mucho CLAVE_MAX bytes y la ruta no pasa
de RUTA_MAX; lo que no cabe se sustituye por «…».

Límites del modelo, que el motor de Rust comparte:
  - Un valor numérico (sin los espacios de los lados) de más de NUMERO_MAX bytes que haya que comparar
    con un límite o una enumeración detiene la validación con el estado «limite».
  - Los atributos repetidos se detectan en los atributos declarados (con la máscara). En los que el
    esquema no declara no hay dónde recordarlos con memoria acotada.
  - En los elementos que el esquema no conoce, el nombre de cierre se compara por longitud y huella.
"""
import time
from decimal import Decimal

from ..automata.dpda import RETORNO, Automata
from ..automata.interprete import CLAVE_MAX, MAX_PROFUNDIDAD, PUNTOS, RUTA_MAX, Resultado, recortar
from ..lexico.lexer import ErrorLexico
from ..lexico.lexer_xml import LexerXML
from .modelo import BLANCOS, BOOLEANOS, DECIMAL, ENTERO

NUMERO_MAX = 256
PREFIJOS_NS = ('xmlns:', 'xsi:')
_FORMA = {'xs_entero': ENTERO.match, 'xs_decimal': DECIMAL.match, 'xs_booleano': BOOLEANOS.__contains__}
_COMPARAN = ('min', 'max', 'enum_num')


def huella(nombre: str):
    """Longitud en bytes y FNV-1a de 64 bits del nombre."""
    datos, h = nombre.encode('utf-8'), 0xCBF29CE484222325
    for b in datos:
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return len(datos), h


def falla(prueba, valor: str) -> bool:
    """True si el valor (texto de un elemento o valor de un atributo) no cumple la prueba."""
    clase = prueba[0]
    if clase == 'long_min':
        return len(valor) < prueba[1]
    if clase == 'long_max':
        return len(valor) > prueba[1]
    if clase == 'enum_cad':
        return valor not in prueba[1]
    recortado = valor.strip(BLANCOS)   # whiteSpace = collapse en números y booleanos
    if clase in _FORMA:
        return not _FORMA[clase](recortado)
    d = Decimal(recortado)
    if clase == 'enum_num':
        return d not in prueba[1]
    limite, exclusivo = prueba[1], prueba[2]
    if clase == 'min':
        return d <= limite if exclusivo else d < limite
    return d >= limite if exclusivo else d > limite


def fallos(pruebas, valor: str, temprano=False) -> list:
    """Motivos de las pruebas que el valor no cumple. Si no tiene la forma léxica de su tipo, las demás
    pruebas no tienen sentido y no se evalúan."""
    salida = []
    for prueba in pruebas:
        if falla(prueba, valor):
            salida.append(prueba[0])
            if temprano or prueba[0] in _FORMA:
                break
    return salida


class _Marco:
    __slots__ = ('simbolo', 'mascara', 'cuenta', 'nombre', 'truncado', 'huella')

    def __init__(self, simbolo, nombre=None):
        self.simbolo, self.mascara, self.cuenta = simbolo, 0, 0
        self.nombre, self.truncado = recortar(nombre) if nombre is not None else (b'', False)
        self.huella = huella(nombre) if nombre is not None else None


class ValidadorXML:
    """Ejecuta el autómata sobre un documento XML que llega por bloques: feed(bloque)... y finish()."""

    def __init__(self, automata: Automata, max_profundidad=MAX_PROFUNDIDAD, fallo_temprano=False,
                 max_bytes=None, plazo=None):
        self.a, self.max_profundidad, self.fallo_temprano = automata, max_profundidad, fallo_temprano
        self.max_bytes = max_bytes
        self._vence = None if plazo is None else time.monotonic() + plazo
        self._lexer = LexerXML()
        self._estado = automata.inicial
        self._pila = [_Marco(automata.pila_inicial)]
        self._atributo = None       # nombre (recortado) del atributo que se está leyendo
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
        return {'bytes': self._bytes, 'violaciones': self._r.violaciones, 'pendientes': self._r.pendientes,
                'profundidad': len(self._pila) - 1, 'profundidad_maxima': self._r.profundidad,
                'estado_del_automata': self.a.estados[self._estado], 'ruta': self._ruta(),
                'pila': [self.a.simbolos_pila[m.simbolo] for m in self._pila], 'terminado': self._terminado}

    def finish(self) -> Resultado:
        if not self._terminado:
            self._lexico(self._lexer.finish)
        r = self._r
        if r.error is None:
            r.estado = 'invalido' if r.violaciones else 'pendiente' if r.pendientes else 'valido'
        return r

    def _lexico(self, obtener):
        try:
            tokens, error = obtener(), None
        except ErrorLexico as e:
            tokens, error = e.tokens, e
        for token in tokens:
            if self._terminado:
                return
            self._paso(token)
        if error is not None:
            self._detener('malformado', error.pos, 'léxico: ' + str(error))

    def _ruta(self, con_atributo=False) -> str:
        ruta, completa = b'', True
        for padre, marco in zip(self._pila, self._pila[1:]):
            tramo = b'/' + marco.nombre + (PUNTOS if marco.truncado else b'')
            if padre is not self._pila[0]:
                tramo += b'[%d]' % padre.cuenta
            if len(ruta) + len(tramo) > RUTA_MAX - len(PUNTOS):
                ruta, completa = ruta + PUNTOS, False
                break
            ruta += tramo
        if con_atributo and completa and self._atributo is not None:
            nombre, truncado = self._atributo
            tramo = b'/@' + nombre + (PUNTOS if truncado else b'')
            ruta += tramo if len(ruta) + len(tramo) <= RUTA_MAX - len(PUNTOS) else PUNTOS
        return ruta.decode('utf-8')

    def _detener(self, estado, pos, motivo, con_atributo=False):
        if not self._terminado:
            r = self._r
            r.estado, r.error, r.ruta, self._terminado = estado, (pos, motivo), self._ruta(con_atributo), True

    def _violar(self, token, motivo, cuantas=1, con_atributo=False):
        if cuantas:
            if not self._r.violaciones:
                self._r.ruta = self._ruta(con_atributo)
            self._r.violaciones += cuantas
            self._r.detalle += [(token.inicio, motivo)] * cuantas

    def _paso(self, token):
        a, transiciones = self.a, self.a.delta[self._estado]
        marco = self._pila[-1]
        simbolo = token.tipo
        if simbolo == 'TEXTO':
            simbolo = 'TEXTO' if token.valor.strip(BLANCOS) else 'ESPACIOS'
        elif simbolo == 'NOMBRE':
            clase = next((s[0] for s in transiciones if isinstance(s, tuple)), None)
            if clase == 'ATRIB' and token.valor.startswith(PREFIJOS_NS):
                simbolo = 'ATRIB_NS'
            else:
                simbolo = (clase, token.valor) if (clase, token.valor) in transiciones else (clase, None)
        t = transiciones.get(simbolo)
        if t is None:
            if isinstance(simbolo, tuple) and simbolo[0] == 'CIERRE':
                return self._detener('malformado', token.inicio, 'sintáctico: la etiqueta de cierre no coincide con la de apertura')
            return self._detener('malformado', token.inicio, f'sintáctico: no se esperaba {token.tipo}')
        en_atributo = token.tipo == 'VALOR_ATRIB' or simbolo == 'ATRIB_NS' or (isinstance(simbolo, tuple) and simbolo[0] == 'ATRIB')
        if en_atributo and token.tipo == 'NOMBRE':
            self._atributo = recortar(token.valor)

        # ---- acción sobre los registros del marco de la cima ----
        if t.accion:
            clase, *p = t.accion
            if clase == 'atributo':
                if marco.mascara >> p[0] & 1:
                    return self._detener('malformado', token.inicio, 'atributo repetido', True)
                marco.mascara |= 1 << p[0]
            elif clase == 'cerrar_atributos':
                self._violar(token, 'atributo requerido', bin(p[0] & ~marco.mascara).count('1'))
            elif clase == 'hijo':
                marco.cuenta += 1
            elif clase == 'cierre_libre' and huella(token.valor) != marco.huella:
                return self._detener('malformado', token.inicio, 'sintáctico: la etiqueta de cierre no coincide con la de apertura')

        # ---- pila: reescribir la cima y apilar ----
        if t.cima is not None:
            marco.simbolo = t.cima
        if t.apila is not None:
            if len(self._pila) > self.max_profundidad:
                return self._detener('limite', token.inicio, 'profundidad máxima superada')
            self._pila.append(_Marco(t.apila, token.valor))
            self._r.profundidad = max(self._r.profundidad, len(self._pila) - 1)

        # ---- peso fijo y pruebas sobre el valor ----
        if t.peso:
            motivo = ('elemento no permitido' if token.tipo == 'NOMBRE' and not en_atributo else
                      'atributo no declarado' if en_atributo else
                      'texto no permitido' if token.tipo == 'TEXTO' else 'contenido incompleto')
            self._violar(token, motivo, t.peso, en_atributo)
        if t.pruebas:
            if any(p[0] in _COMPARAN for p in t.pruebas) and len(token.valor.strip(BLANCOS).encode('utf-8')) > NUMERO_MAX \
                    and not falla(t.pruebas[0], token.valor):
                return self._detener('limite', token.inicio, 'número demasiado largo', en_atributo)
            for motivo in fallos(t.pruebas, token.valor, self.fallo_temprano):
                self._violar(token, motivo, 1, en_atributo)
        self._r.pendientes += t.pendientes

        # ---- desapilar y destino ----
        if t.desapila:
            self._pila.pop()
        self._estado = a.retorno[self._pila[-1].simbolo] if t.destino is RETORNO else t.destino
        if self._r.violaciones and self.fallo_temprano:
            self._r.estado, self._terminado = 'invalido', True


def validar_xml(automata: Automata, datos: bytes, cortes=(), **opciones) -> Resultado:
    """Valida datos partiéndolos en bloques de los tamaños dados (el resto va en un último bloque)."""
    v, i = ValidadorXML(automata, **opciones), 0
    for n in cortes:
        v.feed(datos[i:i + n])
        i += n
    v.feed(datos[i:])
    return v.finish()
