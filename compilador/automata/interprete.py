"""Intérprete de referencia del autómata (Fases 4 y 6): la semántica ejecutable de M.

Ejecuta el DPDA ponderado sobre los tokens del lexer, por bloques. Es deliberadamente simple: sirve para
probar el autómata contra un validador independiente y para probar el motor de Rust contra él.

Ruta del error. La posición dentro del documento se da como JSON Pointer (RFC 6901), reconstruido desde
la pila: cada marco recuerda la clave o el índice por el que va. Para que la memoria siga acotada, de
cada clave se guardan como mucho CLAVE_MAX bytes y la ruta completa no pasa de RUTA_MAX; lo que no cabe
se sustituye por «…».

Sub-ejecuciones (ver nodos.py). Junto a la ejecución principal puede haber hasta MAX_HILOS
sub-ejecuciones del mismo autómata, cada una con su pila y su peso. Por cada token:

  1. nacen los grupos que piden las transiciones de todas las ejecuciones (también las recién nacidas,
     que leen ese mismo token);
  2. se actualizan las huellas de los valores (si el autómata las usa);
  3. da su paso cada ejecución, en orden de nacimiento (la principal primero);
  4. se combinan los grupos cuyas ramas terminaron, del más reciente al más antiguo (así un grupo
     anidado entrega su resultado a su madre antes de que se combine el grupo de la madre).

El resultado de una rama es «cumple» (peso 0), «falla» (peso > 0) o «duda» (peso 0 pero con
comprobaciones diferidas): la combinación sigue una lógica de tres valores, y una «duda» que importa
para el veredicto cuenta como comprobación pendiente.
"""
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..lexico.lexer import ErrorLexico, Lexer
from . import expresiones, valores
from .dpda import RETORNO, Automata

MAX_PROFUNDIDAD = 128  # contenedores abiertos a la vez (marcos de pila sin contar el fondo)
MAX_HILOS = 16         # sub-ejecuciones vivas a la vez
MAX_HUELLAS = 1024     # huellas de elementos guardadas a la vez (uniqueItems)
CLAVE_MAX = 24         # bytes que se conservan de cada clave para la ruta
RUTA_MAX = 256         # bytes de la ruta completa
PUNTOS = '…'.encode('utf-8')

# motivo de la violación que produce cada grupo de sub-ejecuciones
MOTIVO_GRUPO = {'alguna': 'anyOf', 'una': 'oneOf', 'no': 'not', 'si': 'if', 'dependiente': 'dependentSchemas',
                'nombres': 'propertyNames', 'cumple': 'contiene'}
CUMPLE, FALLA, DUDA = 'cumple', 'falla', 'duda'
CIERRES = ('LLAVE_CIERRA', 'CORCHETE_CIERRA')


@dataclass
class Resultado:
    """estado:
        'valido'      cumple el esquema
        'pendiente'   cumple todo lo que el autómata verifica, pero cruzó comprobaciones diferidas
        'invalido'    tiene violaciones (peso > 0)
        'malformado'  no es JSON bien formado (error léxico, sintáctico o clave repetida)
        'limite'      superó un límite: profundidad de la pila, tamaño, tiempo o sub-ejecuciones
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
    if clase == 'patron':
        return not expresiones.coincide(prueba[1], token.valor)
    if clase == 'multiplo':
        return not valores.es_multiplo(valores.canonico(token.valor), valores.canonico_decimal(prueba[1]))
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
    """Marco de pila: símbolo de Γ, los registros y la posición actual para la ruta."""
    __slots__ = ('simbolo', 'mascara', 'cuenta', 'coincidencias', 'dudosas', 'es_objeto', 'clave', 'truncada')

    def __init__(self, simbolo, es_objeto=False):
        self.simbolo, self.mascara, self.cuenta = simbolo, 0, 0
        self.coincidencias = self.dudosas = 0   # elementos que cumplen contains (seguro / quizá)
        self.es_objeto, self.clave, self.truncada = es_objeto, None, False

    def segmento(self):
        """Tramo de la ruta que aporta este marco, o None si aún no ha leído nada."""
        if self.es_objeto:
            if self.clave is None:
                return None
            return b'/' + self.clave.replace(b'~', b'~0').replace(b'/', b'~1') + (PUNTOS if self.truncada else b'')
        return b'/%d' % (self.cuenta - 1) if self.cuenta else None


class _Hilo:
    """Una ejecución del autómata: la principal o una sub-ejecución de un grupo."""
    __slots__ = ('estado', 'pila', 'peso', 'pendientes', 'grupo', 'terminado')

    def __init__(self, estado, fondo, grupo=None):
        self.estado, self.pila, self.grupo = estado, [_Marco(fondo)], grupo
        self.peso = self.pendientes = 0
        self.terminado = False

    def resultado(self):
        return FALLA if self.peso else DUDA if self.pendientes else CUMPLE


class _Grupo:
    __slots__ = ('clase', 'madre', 'ramas', 'marco')

    def __init__(self, clase, madre, marco):
        self.clase, self.madre, self.marco, self.ramas = clase, madre, marco, []   # ramas: _Hilo o None


class _Contenedor:
    """Objeto o arreglo abierto, para calcular huellas: lo mismo para todas las ejecuciones."""
    __slots__ = ('objeto', 'acumulado', 'clave', 'espera_clave', 'recoger', 'huellas', 'base', 'repetido',
                 'desbordado')

    def __init__(self, objeto, base):
        self.objeto, self.base = objeto, base
        self.acumulado = valores.INICIO_OBJETO if objeto else valores.INICIO_ARREGLO
        self.clave, self.espera_clave = 0, objeto
        self.recoger = self.repetido = self.desbordado = False
        self.huellas = set()


def recortar(nombre: str):
    """Los primeros CLAVE_MAX bytes de la clave, sin partir ningún carácter, y si hubo que recortar."""
    datos = nombre.encode('utf-8')
    if len(datos) <= CLAVE_MAX:
        return datos, False
    corte = CLAVE_MAX
    while datos[corte] & 0xC0 == 0x80:  # no cortar en medio de un carácter
        corte -= 1
    return datos[:corte], True


def combinar(clase, resultados):
    """Resultado de un grupo a partir de los de sus ramas (None = rama ausente, cuenta como cumple).
    Devuelve FALLA (una violación para la madre), DUDA (una comprobación pendiente) o CUMPLE."""
    r = [CUMPLE if x is None else x for x in resultados]
    if clase == 'alguna':
        return CUMPLE if CUMPLE in r else FALLA if all(x == FALLA for x in r) else DUDA
    if clase == 'una':
        cumplen, dudan = r.count(CUMPLE), r.count(DUDA)
        if cumplen > 1 or cumplen + dudan == 0:
            return FALLA
        return CUMPLE if cumplen == 1 and dudan == 0 else DUDA
    if clase == 'no':
        return {CUMPLE: FALLA, FALLA: CUMPLE, DUDA: DUDA}[r[0]]
    if clase in ('si', 'dependiente'):
        condicion, entonces, sino = r
        if condicion == CUMPLE:
            return entonces
        if condicion == FALLA:
            return sino
        return entonces if entonces == sino else DUDA
    return r[0]   # nombres y cumple


def _usa_huellas(a: Automata) -> bool:
    if not hasattr(a, '_usa_huellas'):
        a._usa_huellas = any(t.recoger or any(p[0] in ('enum_huella', 'unicos') for p in t.pruebas)
                             for d in a.delta for t in d.values())
    return a._usa_huellas


def _patrones(a: Automata, estado):
    """Expresiones con las que se clasifican las claves no declaradas en un estado que lee claves."""
    cache = a.__dict__.setdefault('_patrones', {})
    if estado not in cache:
        cache[estado] = sorted({p for s in a.delta[estado] if isinstance(s, tuple) and isinstance(s[1], frozenset)
                                for p in s[1]})
    return cache[estado]


class Validador:
    """Ejecuta el autómata sobre un documento que llega por bloques: feed(bloque)... y finish().

    Límites: max_profundidad (contenedores abiertos), max_bytes (tamaño del documento), plazo (segundos
    desde que se crea el validador; se comprueba al empezar cada bloque), max_hilos (sub-ejecuciones
    vivas a la vez) y max_huellas (huellas de elementos guardadas a la vez para uniqueItems; si no caben,
    esa comprobación queda pendiente).
    """

    def __init__(self, automata: Automata, max_profundidad=MAX_PROFUNDIDAD, fallo_temprano=False,
                 max_bytes=None, plazo=None, max_hilos=MAX_HILOS, max_huellas=MAX_HUELLAS):
        self.a, self.max_profundidad, self.fallo_temprano = automata, max_profundidad, fallo_temprano
        self.max_bytes, self.max_hilos, self.max_huellas = max_bytes, max_hilos, max_huellas
        self._vence = None if plazo is None else time.monotonic() + plazo
        self._lexer = Lexer()
        self._principal = _Hilo(automata.inicial, automata.pila_inicial)
        self._principal.pila[0].es_objeto = False
        self._hilos = [self._principal]   # en orden de nacimiento
        self._grupos = []                 # en orden de nacimiento
        self._huellas = _usa_huellas(automata)
        self._contenedores, self._guardadas = [], 0
        self._ultima, self._cerrado = 0, None
        # si es una lista, cada transición de la ejecución principal se anota como (estado antes, estado después):
        # la interfaz la usa para dibujar el recorrido, porque un bloque puede provocar varias transiciones
        self.recorrido = None
        self._bytes = 0
        self._r = Resultado('valido')
        self._terminado = False

    # compatibilidad: el estado y la pila de la ejecución principal
    @property
    def _estado(self):
        return self._principal.estado

    @property
    def _pila(self):
        return self._principal.pila

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
                'pila': [self.a.simbolos_pila[m.simbolo] for m in self._pila], 'terminado': self._terminado,
                'subejecuciones': len(self._hilos) - 1}

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
            antes = self._estado
            self._token(token)
            if self.recorrido is not None and (not self._terminado or self._r.estado == 'invalido'):
                # la transición ocurrió (no la cortó un error): estados, el token que la provocó y la pila después
                self.recorrido.append((antes, self._estado, token, [m.simbolo for m in self._pila]))
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

    def _violar(self, hilo, token, motivo, cuantas=1, del_contenedor=False):
        """Suma violaciones a una ejecución. del_contenedor: la violación es del objeto o arreglo de la
        cima (falta una propiedad, sobran elementos), no del valor que se está leyendo."""
        if not cuantas:
            return
        if hilo is not self._principal:
            hilo.peso += cuantas
            return
        if not self._r.violaciones:
            self._r.ruta = self._ruta(not del_contenedor)
        self._r.violaciones += cuantas
        self._r.detalle += [(token.inicio, motivo)] * cuantas

    def _pendiente(self, hilo, cuantas=1):
        if hilo is self._principal:
            self._r.pendientes += cuantas
        else:
            hilo.pendientes += cuantas

    def _transicion(self, estado, token):
        transiciones = self.a.delta[estado]
        simbolo = token.tipo
        if token.tipo == 'CADENA' and 'CADENA' not in transiciones:  # posición de clave
            if ('CLAVE', token.valor) in transiciones:
                simbolo = ('CLAVE', token.valor)
            else:
                casa = frozenset(p for p in _patrones(self.a, estado) if expresiones.coincide(p, token.valor))
                simbolo = ('CLAVE', casa) if casa and ('CLAVE', casa) in transiciones else ('CLAVE', None)
        return transiciones.get(simbolo)

    # ---- un token: nacer, huellas, pasos, combinar ----
    def _token(self, token):
        self._nacer(token)
        if self._terminado:
            return
        if self._huellas:
            self._huella(token)
        for hilo in list(self._hilos):
            self._paso(hilo, token)
            if self._terminado:
                return
        if len(self._hilos) > 1:
            self._combinar(token)

    def _nacer(self, token):
        i = 0
        while i < len(self._hilos):  # la lista crece: las recién nacidas también pueden hacer nacer
            madre = self._hilos[i]
            i += 1
            t = self._transicion(madre.estado, token)
            if t is None or not t.hilos:
                continue
            for clase, inicios in t.hilos:
                grupo = _Grupo(clase, madre, madre.pila[-1] if clase == 'contiene' else None)
                for inicio in inicios:
                    if inicio is None:
                        grupo.ramas.append(None)
                        continue
                    if len(self._hilos) - 1 >= self.max_hilos:
                        return self._detener('limite', token.inicio, 'demasiadas sub-ejecuciones')
                    hilo = _Hilo(inicio, self.a.pila_hilo, grupo)
                    grupo.ramas.append(hilo)
                    self._hilos.append(hilo)
                self._grupos.append(grupo)

    def _combinar(self, token):
        for grupo in reversed(list(self._grupos)):
            vivas = [h for h in grupo.ramas if h is not None]
            if not vivas[0].terminado:   # todas las ramas leen el mismo valor: terminan a la vez
                continue
            self._grupos.remove(grupo)
            r = combinar(grupo.clase, [None if h is None else h.resultado() for h in grupo.ramas])
            if grupo.clase == 'contiene':
                grupo.marco.coincidencias += r == CUMPLE
                grupo.marco.dudosas += r == DUDA
            elif r == FALLA:
                self._violar(grupo.madre, token, MOTIVO_GRUPO[grupo.clase])
            elif r == DUDA:
                self._pendiente(grupo.madre)
        self._hilos = [h for h in self._hilos if not h.terminado]
        if self._r.violaciones and self.fallo_temprano:
            self._r.estado, self._terminado = 'invalido', True

    def _huella(self, token):
        """Huellas de los valores (las mismas para todas las ejecuciones: dependen solo del documento)."""
        tipo, pila = token.tipo, self._contenedores
        if tipo in ('LLAVE_ABRE', 'CORCHETE_ABRE'):
            pila.append(_Contenedor(tipo == 'LLAVE_ABRE', self._guardadas))
        elif tipo in CIERRES:
            if not pila:   # documento mal formado: lo detecta el paso de la ejecución principal
                return
            c = self._cerrado = pila.pop()
            self._guardadas = c.base
            self._terminar_valor(valores.cerrar_objeto(c.acumulado) if c.objeto else valores.cerrar_arreglo(c.acumulado))
        elif tipo == 'CADENA':
            h = valores.de_cadena(token.valor.encode('utf-8'))
            if pila and pila[-1].espera_clave:
                pila[-1].clave, pila[-1].espera_clave = h, False
            else:
                self._terminar_valor(h)
        elif tipo == 'NUMERO':
            self._terminar_valor(valores.de_numero(token.valor))
        elif tipo in ('TRUE', 'FALSE', 'NULL'):
            self._terminar_valor(valores.de_literal({'TRUE': valores.VERDADERO, 'FALSE': valores.FALSO,
                                                     'NULL': valores.NULO}[tipo]))
        elif tipo == 'COMA' and pila and pila[-1].objeto:
            pila[-1].espera_clave = True

    def _terminar_valor(self, h):
        self._ultima = h
        if not self._contenedores:
            return
        c = self._contenedores[-1]
        if c.objeto:
            c.acumulado = valores.miembro(c.acumulado, c.clave, h)
            return
        c.acumulado = valores.elemento(c.acumulado, h)
        if c.recoger and not c.repetido:
            if h in c.huellas:
                c.repetido = True
            elif self._guardadas >= self.max_huellas:
                c.desbordado = True
            else:
                c.huellas.add(h)
                self._guardadas += 1

    def _cierre(self, hilo, prueba, token):
        """Prueba de una transición que cierra un contenedor: FALLA, DUDA o CUMPLE."""
        clase = prueba[0]
        if clase == 'enum_huella':
            return FALLA if self._ultima not in prueba[1] else CUMPLE
        if clase == 'unicos':
            c = self._cerrado
            return FALLA if c.repetido else DUDA if c.desbordado else CUMPLE
        _, minimo, maximo = prueba   # contiene: el número de elementos que cumplen está en [c, c + d]
        marco = hilo.pila[-1]
        c, d = marco.coincidencias, marco.dudosas
        if c >= minimo and (maximo is None or c + d <= maximo):
            return CUMPLE
        if c + d < minimo or (maximo is not None and c > maximo):
            return FALLA
        return DUDA

    def _paso(self, hilo, token):
        a = self.a
        principal = hilo is self._principal
        marco = hilo.pila[-1]
        t = self._transicion(hilo.estado, token)
        if t is None:
            if principal:
                self._detener('malformado', token.inicio, f'sintáctico: no se esperaba {token.tipo}')
            return

        if t.accion:
            clase, *p = t.accion
            if clase == 'clave':
                bit, maximo = p
                if principal:
                    marco.clave, marco.truncada = recortar(token.valor)
                marco.cuenta += 1
                if maximo is not None and marco.cuenta == maximo + 1:
                    self._violar(hilo, token, 'maxProperties', del_contenedor=True)
                if bit is not None:
                    if marco.mascara >> bit & 1:  # la vea la ejecución que la vea, el documento está mal formado
                        return self._detener('malformado', token.inicio, 'clave repetida')
                    marco.mascara |= 1 << bit
            elif clase == 'cerrar_objeto':
                requeridas, dependencias, minimo = p
                self._violar(hilo, token, 'required', bin(requeridas & ~marco.mascara).count('1'), True)
                for bit, exige in dependencias:
                    if marco.mascara >> bit & 1:
                        self._violar(hilo, token, 'dependentRequired', bin(exige & ~marco.mascara).count('1'), True)
                if minimo is not None and marco.cuenta < minimo:
                    self._violar(hilo, token, 'minProperties', del_contenedor=True)
            elif clase == 'elemento':
                marco.cuenta += 1
                if p[0] is not None and marco.cuenta == p[0] + 1:
                    self._violar(hilo, token, 'maxItems', del_contenedor=True)
            elif clase == 'cerrar_arreglo':
                if p[0] is not None and marco.cuenta < p[0]:
                    self._violar(hilo, token, 'minItems', del_contenedor=True)

        self._violar(hilo, token, 'no admitido', t.peso)
        cierre = token.tipo in CIERRES
        for prueba in t.pruebas:  # ya vienen ordenadas de menor a mayor costo
            if cierre:
                r = self._cierre(hilo, prueba, token)
                if r == DUDA:
                    self._pendiente(hilo)
                if r != FALLA:
                    continue
                self._violar(hilo, token, prueba[0], del_contenedor=True)
            elif falla(prueba, token):
                self._violar(hilo, token, prueba[0])
            else:
                continue
            if self.fallo_temprano:
                break
        self._pendiente(hilo, t.pendientes)

        if t.desapila:
            hilo.pila.pop()
        if t.cima is not None:
            marco.simbolo = t.cima
        if t.apila is not None:
            if principal and len(hilo.pila) > self.max_profundidad:
                return self._detener('limite', token.inicio, 'profundidad máxima superada')
            hilo.pila.append(_Marco(t.apila, es_objeto=token.tipo == 'LLAVE_ABRE'))
            if principal:
                self._r.profundidad = max(self._r.profundidad, len(hilo.pila) - 1)
        if t.recoger and self._huellas:
            self._contenedores[-1].recoger = True
        hilo.estado = a.retorno[hilo.pila[-1].simbolo] if t.destino is RETORNO else t.destino
        if not principal:
            hilo.terminado = hilo.estado == a.fin_hilo
        elif self._r.violaciones and self.fallo_temprano:
            self._r.estado, self._terminado = 'invalido', True


def validar(automata: Automata, datos: bytes, cortes=(), **opciones) -> Resultado:
    """Valida datos partiéndolos en bloques de los tamaños dados (el resto va en un último bloque)."""
    v, i = Validador(automata, **opciones), 0
    for n in cortes:
        v.feed(datos[i:i + n])
        i += n
    v.feed(datos[i:])
    return v.finish()
