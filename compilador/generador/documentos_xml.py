"""Generador de documentos XML de prueba a partir de un esquema XSD compilado (Fase 5, XML).

Igual que payloads.py para JSON: produce documentos válidos recorriendo el modelo (un camino de
aceptación por el autómata de cada modelo de contenido, valores que cumplen las facetas), documentos
inválidos alterando uno válido (quitar un atributo obligatorio, cambiar el orden de los hijos, poner un
valor fuera de rango...) y documentos mal formados alterando los bytes (cortar el documento, cerrar con
otro nombre, añadir un DOCTYPE...). El veredicto de cada caso lo da el intérprete de referencia.
"""
import random
from collections import deque
from decimal import Decimal

from ..automata import expresiones
from ..automata.compilar import Compilado
from ..xsd.interprete import fallos, validar_xml
from ..xsd.modelo import pruebas_lexicas
from .payloads import Caso

MAX_PROFUNDIDAD = 6     # a partir de aquí cada elemento se cierra por el camino más corto
MAX_ELEMENTOS = 150     # por documento
MAX_HIJOS = 8           # por elemento, antes de buscar la salida
INFINITO = 10**9
_LETRAS = 'abcdexyz ñéü0159-_.'
_ESPECIALES = ['&', '<', '>', '"', "'", ']]>', '€', '😀']


class _Imposible(Exception):
    """No se encontró un valor que cumpla el tipo."""


class Nodo:
    """Elemento de un documento en construcción. tipo es None en los elementos que el esquema no conoce."""

    def __init__(self, nombre, tipo=None):
        self.nombre, self.tipo, self.atributos, self.hijos = nombre, tipo, {}, []   # hijos: Nodo o str

    def todos(self):
        yield self
        for h in self.hijos:
            if isinstance(h, Nodo):
                yield from h.todos()


def _texto(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _valor(s):
    for original, escrito in (('&', '&amp;'), ('<', '&lt;'), ('"', '&quot;'), ('\t', '&#9;'), ('\n', '&#10;'), ('\r', '&#13;')):
        s = s.replace(original, escrito)
    return s


def serializar(nodo, rng=None, sangria=None) -> str:
    """El documento como texto. Con sangria, los hijos de los elementos que solo contienen elementos van
    en líneas separadas (ahí los espacios no cuentan)."""
    atributos = ''.join(f' {n}="{_valor(v)}"' for n, v in nodo.atributos.items())
    if not nodo.hijos:
        corto = rng is None or rng.random() < 0.6
        return f'<{nodo.nombre}{atributos}/>' if corto else f'<{nodo.nombre}{atributos}></{nodo.nombre}>'
    separable = sangria is not None and nodo.tipo is not None and nodo.tipo.contenido == 'elementos' \
        and not nodo.tipo.afd.vacio() and all(isinstance(h, Nodo) for h in nodo.hijos)
    dentro = sangria + '  ' if separable else None
    partes = [serializar(h, rng, dentro) if isinstance(h, Nodo) else _texto(h) for h in nodo.hijos]
    if separable:
        return f'<{nodo.nombre}{atributos}>' + ''.join(f'\n{dentro}{p}' for p in partes) + f'\n{sangria}</{nodo.nombre}>'
    return f'<{nodo.nombre}{atributos}>{"".join(partes)}</{nodo.nombre}>'


class GeneradorXML:
    def __init__(self, compilado: Compilado, semilla=0):
        self.c, self.an, self.rng = compilado, compilado.analisis, random.Random(semilla)
        self._elementos = 0
        # altura mínima de un árbol de cada tipo: sirve para cerrar las recursiones
        self.altura, ronda, cambio = {}, 1, True
        while cambio:
            nuevos = [t for t in self.an.tipos if id(t) not in self.altura
                      and (t.contenido != 'elementos' or self._camino(t, 0, ronda - 1) is not None)]
            self.altura.update((id(t), ronda) for t in nuevos)
            cambio, ronda = bool(nuevos), ronda + 1

    def _altura(self, tipo):
        return self.altura.get(id(tipo), 1 if tipo.contenido != 'elementos' else INFINITO)

    def _camino(self, tipo, desde, limite):
        """Nombres de los hijos del camino más corto hasta un estado final, usando solo hijos de altura ≤ limite."""
        afd, previo, cola = tipo.afd, {desde: None}, deque([desde])
        while cola:
            q = cola.popleft()
            if afd.acepta[q]:
                camino = []
                while previo[q] is not None:
                    q, nombre = previo[q]
                    camino.append(nombre)
                return camino[::-1]
            for nombre, d in sorted(afd.delta[q].items()):
                if d not in previo and self._altura(afd.tipo_de[nombre]) <= limite:
                    previo[d] = (q, nombre)
                    cola.append(d)
        return None

    # ---- valores ----
    def valor(self, simple) -> str:
        """Un valor que cumple el tipo simple."""
        pruebas = pruebas_lexicas(simple)
        for _ in range(60):
            v = self._candidato(simple)
            if v is not None and not fallos(pruebas, v):
                return v
        raise _Imposible(simple.nombre or simple.base)

    def _candidato(self, simple):
        rng = self.rng
        grupos = [p[1] for p in simple.pruebas if p[0] == 'patron']
        if grupos and rng.random() < 0.8:   # un texto sacado del propio AFD de los patrones
            return expresiones.ejemplo(grupos, rng, rng.randint(1, 8))
        if simple.base == 'boolean':
            return rng.choice(['true', 'false', '1', '0'])
        enumerados = [set(p[1]) for p in simple.pruebas if p[0] in ('enum_cad', 'enum_num')]
        if simple.base == 'string':
            if enumerados:
                return rng.choice(sorted(set.intersection(*enumerados)) or [None])
            minimo = max([p[1] for p in simple.pruebas if p[0] == 'long_min'], default=0)
            maximo = min([p[1] for p in simple.pruebas if p[0] == 'long_max'], default=minimo + 12)
            if minimo > maximo:
                return None
            largo = rng.randint(minimo, min(maximo, minimo + 30))
            letras = [rng.choice(_ESPECIALES) if rng.random() < 0.08 else rng.choice(_LETRAS) for _ in range(largo)]
            return ''.join(letras)[:largo]
        if enumerados:
            valores = sorted(set.intersection(*enumerados))
            return self._escribir(rng.choice(valores), simple) if valores else None
        minimos = [(p[1], p[2]) for p in simple.pruebas if p[0] == 'min']
        maximos = [(p[1], p[2]) for p in simple.pruebas if p[0] == 'max']
        abajo = max(minimos, default=None)
        arriba = min(maximos, key=lambda x: (x[0], not x[1]), default=None)
        if abajo is None and arriba is None:
            abajo, arriba = (Decimal(-50), False), (Decimal(50), False)
        elif abajo is None:
            abajo = (arriba[0] - 100, False)
        elif arriba is None:
            arriba = (abajo[0] + 100, False)
        if abajo[0] > arriba[0]:
            return None
        if simple.base == 'decimal' and rng.random() < 0.5:
            d = abajo[0] + (arriba[0] - abajo[0]) * Decimal(rng.randint(0, 1000)) / 1000
        else:
            primero, ultimo = int(abajo[0].to_integral_value(rounding='ROUND_CEILING')), int(arriba[0].to_integral_value(rounding='ROUND_FLOOR'))
            if primero > ultimo:
                d = (abajo[0] + arriba[0]) / 2
            else:
                d = Decimal(rng.randint(primero, min(ultimo, primero + 10**6)))
        return self._escribir(d, simple)

    def _escribir(self, d, simple):
        """Una de las formas léxicas del número."""
        texto = format(d, 'f')
        forma = self.rng.randrange(8)
        if forma == 0 and not texto.startswith('-'):
            texto = '+' + texto
        elif forma == 1:
            texto = ('-00' + texto[1:]) if texto.startswith('-') else '00' + texto
        elif forma == 2:
            texto = f' {texto}\n'
        elif forma == 3 and simple.base == 'decimal' and '.' not in texto:
            texto += self.rng.choice(['.', '.0', '.000'])
        return texto

    # ---- documentos ----
    def elemento(self, nombre, tipo, prof=0) -> Nodo:
        rng, n = self.rng, Nodo(nombre, tipo)
        self._elementos += 1
        for a in tipo.atributos.values():
            if a.requerido or rng.random() < 0.6:
                n.atributos[a.nombre] = self.valor(a.simple)
        if tipo.contenido == 'simple':
            v = self.valor(tipo.simple)
            n.hijos = [v] if v else []
        elif tipo.contenido == 'libre':
            if rng.random() < 0.5:
                n.hijos = [rng.choice(['texto libre', '7', ' '])] if rng.random() < 0.5 else [Nodo('cualquiera')]
        else:
            afd, d = tipo.afd, 0
            while True:
                justo = prof >= MAX_PROFUNDIDAD or self._elementos >= MAX_ELEMENTOS
                if afd.acepta[d] and (justo or len(n.hijos) >= MAX_HIJOS or not afd.delta[d] or rng.random() < 0.35):
                    break
                if justo or len(n.hijos) >= MAX_HIJOS:
                    camino = (self._camino(tipo, d, self._altura(tipo) - 1) if justo else None) or self._camino(tipo, d, INFINITO - 1)
                    hijo = camino[0]
                else:
                    hijo = rng.choice(sorted(afd.delta[d]))
                n.hijos.append(self.elemento(hijo, afd.tipo_de[hijo], prof + 1))
                d = afd.delta[d][hijo]
        return n

    def arbol(self) -> Nodo:
        """Árbol de un documento válido, con cualquiera de los elementos globales como raíz."""
        self._elementos = 0
        nombre = self.rng.choice(sorted(self.an.elementos))
        return self.elemento(nombre, self.an.elementos[nombre])

    def escribir(self, arbol) -> bytes:
        rng = self.rng
        prologo = rng.choice(['', '', '<?xml version="1.0" encoding="UTF-8"?>\n', '<?xml version="1.0"?>', '<!-- generado -->\n'])
        cuerpo = serializar(arbol, rng, '' if rng.random() < 0.5 else None)
        return (prologo + cuerpo + rng.choice(['', '\n'])).encode('utf-8')

    def documento(self) -> bytes:
        return self.escribir(self.arbol())

    # ---- documentos inválidos: una alteración del árbol ----
    def alterar(self, arbol):
        """Aplica una alteración al azar al árbol (que modifica) y devuelve su descripción, o None si no pudo."""
        rng = self.rng
        nodos = list(arbol.todos())
        n = rng.choice(nodos)
        hijos = [i for i, h in enumerate(n.hijos) if isinstance(h, Nodo)]
        clase = rng.randrange(11)
        if clase == 0 and n.atributos:
            quitado = rng.choice(sorted(n.atributos))
            del n.atributos[quitado]
            return f'sin el atributo «{quitado}» de <{n.nombre}>'
        if clase == 1:
            n.atributos['noDeclarado'] = '1'
            return f'atributo no declarado en <{n.nombre}>'
        if clase == 2 and hijos:
            i = rng.choice(hijos)
            return f'sin el hijo <{n.hijos.pop(i).nombre}> de <{n.nombre}>'
        if clase == 3 and hijos:
            i = rng.choice(hijos)
            n.hijos.insert(i, n.hijos[i])
            return f'hijo <{n.hijos[i].nombre}> repetido en <{n.nombre}>'
        if clase == 4 and len(hijos) >= 2:
            i, j = rng.sample(hijos, 2)
            n.hijos[i], n.hijos[j] = n.hijos[j], n.hijos[i]
            return f'hijos de <{n.nombre}> en otro orden'
        if clase == 5:
            n.hijos.insert(rng.randint(0, len(n.hijos)), Nodo('desconocido'))
            return f'hijo no declarado en <{n.nombre}>'
        if clase == 6:
            malo = rng.choice(['abc', '', '1e3', '99999999999999999999', '-99999999999999999999', '0.5', 'x' * 300, 'TRUE', ' ', '--1'])
            n.hijos = [malo] if malo else []
            return f'contenido de <{n.nombre}> sustituido por «{malo[:20]}»'
        if clase == 7 and n.atributos:
            nombre = rng.choice(sorted(n.atributos))
            n.atributos[nombre] = rng.choice(['abc', '', '-1', '99999999999999999999', '1.5', 'x' * 300, '0'])
            return f'valor del atributo «{nombre}» de <{n.nombre}> sustituido'
        if clase == 8:
            n.hijos.insert(rng.randint(0, len(n.hijos)), 'texto suelto')
            return f'texto dentro de <{n.nombre}>'
        if clase == 9:
            n.nombre += 'X'
            return f'elemento <{n.nombre[:-1]}> con otro nombre'
        if clase == 10 and n.hijos:
            n.hijos = []
            return f'<{n.nombre}> vacío'
        return None

    def mutante(self):
        """(documento, descripción): un documento válido con una alteración. Puede seguir siendo válido."""
        while True:
            arbol = self.arbol()
            descripcion = self.alterar(arbol)
            if descripcion is not None:
                return self.escribir(arbol), descripcion

    # ---- documentos mal formados: una alteración de los bytes ----
    def _deformar(self, datos: bytes):
        rng = self.rng
        cierre = datos.rfind(b'</')
        clase = rng.randrange(9)
        if clase == 0:
            return datos[:rng.randint(1, max(1, len(datos.rstrip()) - 1))], 'documento cortado'
        if clase == 1 and cierre >= 0:
            return datos[:cierre + 2] + b'otro' + datos[cierre + 2:], 'la etiqueta de cierre no coincide con la de apertura'
        if clase == 2:
            i = datos.find(b'<', datos.find(b'?>') + 1)
            return datos[:i] + b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + datos[i:], 'DOCTYPE con una entidad externa (XXE)'
        if clase == 3 and cierre >= 0:
            return datos[:cierre] + b'&nbsp;' + datos[cierre:], 'entidad no definida'
        if clase == 4 and cierre >= 0:
            return datos[:cierre] + b'\x01' + datos[cierre:], 'carácter de control'
        if clase == 5 and cierre >= 0:
            return datos[:cierre] + b'\xff\xfe' + datos[cierre:], 'bytes que no son UTF-8'
        if clase == 6:
            return datos.rstrip() + b'<otra/>', 'dos elementos raíz'
        if clase == 7 and b'="' in datos:
            i = datos.find(b'"', datos.find(b'="') + 2)
            return datos[:i] + datos[i + 1:], 'valor de atributo sin cerrar'
        if clase == 8:
            i = datos.find(b'<', datos.find(b'?>') + 1)
            return datos[:i] + b'<!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>' + datos[i:], \
                'DOCTYPE con expansión de entidades'
        return None, None

    def casos(self, validos=5, invalidos=5, malformados=3):
        """Casos de prueba; el veredicto de cada uno es el del intérprete de referencia."""
        a, salida = self.c.automata, []

        def caso(clase, datos, descripcion):
            r = validar_xml(a, datos)
            return Caso(clase, datos, r.estado, r.violaciones, descripcion)

        for i in range(validos):
            try:
                salida.append(caso('valido', self.documento(), f'documento válido {i + 1}'))
            except _Imposible:
                break
        if not salida and validos:
            return salida   # el esquema no tiene documentos válidos que sepamos construir
        intentos = 0
        while sum(c.clase == 'invalido' for c in salida) < invalidos and intentos < 40 * invalidos:
            intentos += 1
            try:
                c = caso('invalido', *self.mutante())
            except _Imposible:
                break
            if c.estado == 'invalido' and all(c.payload != otro.payload for otro in salida):
                salida.append(c)
        intentos = 0
        while sum(c.clase == 'malformado' for c in salida) < malformados and intentos < 40 * malformados:
            intentos += 1
            try:
                datos, descripcion = self._deformar(self.documento())
            except _Imposible:
                break
            if datos is not None:
                c = caso('malformado', datos, descripcion)
                if c.estado == 'malformado' and all(c.payload != otro.payload for otro in salida):
                    salida.append(c)
        return salida


def generar_casos_xml(compilado: Compilado, validos=5, invalidos=5, malformados=3, semilla=0):
    return GeneradorXML(compilado, semilla).casos(validos, invalidos, malformados)
