"""Análisis semántico de un esquema XSD (Fase 3, XML): del árbol del documento al modelo de tipos.

    python -m compilador.xsd.modelo esquema.xsd      # informe: diagnósticos, símbolos y clasificación

Tabla de símbolos: los elementos globales y los tipos con nombre (complexType y simpleType comparten un
solo espacio de nombres). Con ella se resuelven type="...", base="..." y ref="...". No se detiene en el
primer problema: recoge todos los diagnósticos, y lo que no entiende lo sustituye por «cualquier cosa»
para poder seguir.

Subconjunto soportado: ver la sección 8 de PLAN.md. Lo demás es un error «no-soportada».
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from ..automata import expresiones
from ..semantico.clasificacion import NIVELES, Uso, nivel
from ..semantico.simbolos import Diagnostico, Diagnosticos
from ..sintactico.xml_dom import analizar_xml
from .contenido import AFD, ModeloDemasiadoGrande, TiposInconsistentes, construir_afd

XS = 'http://www.w3.org/2001/XMLSchema'
ENTEROS = {  # tipos predefinidos derivados de integer: (mínimo, máximo)
    'integer': (None, None), 'long': (-2**63, 2**63 - 1), 'int': (-2**31, 2**31 - 1), 'short': (-2**15, 2**15 - 1),
    'byte': (-128, 127), 'nonNegativeInteger': (0, None), 'positiveInteger': (1, None),
    'nonPositiveInteger': (None, 0), 'negativeInteger': (None, -1), 'unsignedLong': (0, 2**64 - 1),
    'unsignedInt': (0, 2**32 - 1), 'unsignedShort': (0, 2**16 - 1), 'unsignedByte': (0, 255),
}
ENTERO = re.compile(r'[+-]?[0-9]+\Z')
DECIMAL = re.compile(r'[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)\Z')
BOOLEANOS = ('true', 'false', '1', '0')
BLANCOS = ' \t\n\r'
MAX_CONTADOR = 2**32 - 2

# atributos del esquema que no cambian la validación en este subconjunto
_SIN_EFECTO = {'id', 'form', 'final', 'block', 'finalDefault', 'blockDefault', 'version', 'elementFormDefault',
               'attributeFormDefault'}
_VALOR_POR_DEFECTO = {'nillable': 'false', 'abstract': 'false', 'mixed': 'false'}
_ADMITIDOS = {
    'schema': set(), 'element': {'name', 'type', 'ref', 'minOccurs', 'maxOccurs'}, 'complexType': {'name'},
    'simpleType': {'name'}, 'sequence': {'minOccurs', 'maxOccurs'}, 'choice': {'minOccurs', 'maxOccurs'},
    'attribute': {'name', 'type', 'use'}, 'simpleContent': set(), 'extension': {'base'}, 'restriction': {'base'},
}
_FACETAS_DE_LONGITUD = ('length', 'minLength', 'maxLength')
_FACETAS_DE_RANGO = {'minInclusive': ('min', False), 'minExclusive': ('min', True),
                     'maxInclusive': ('max', False), 'maxExclusive': ('max', True)}


@dataclass(eq=False)
class Simple:
    """Tipo simple resuelto: el tipo primitivo y las pruebas que acumulan sus restricciones."""
    base: str                 # 'string', 'integer', 'decimal' o 'boolean'
    pruebas: tuple = ()       # ('min'|'max', Decimal, exclusivo), ('long_min'|'long_max', n), ('enum_num'|'enum_cad', valores),
                              # ('patron', expresiones, colapsar): el valor completo casa con alguna de las expresiones
    patrones: tuple = ()      # ruta de cada grupo de facetas pattern que queda en verificación diferida
    nombre: str = None


@dataclass(eq=False)
class Atributo:
    nombre: str
    simple: Simple
    requerido: bool


@dataclass(eq=False)
class Particula:
    clase: str                # 'elemento', 'secuencia' u 'opcion'
    minimo: int = 1
    maximo: int = 1           # None = unbounded
    hijos: list = field(default_factory=list)
    nombre: str = None        # solo en 'elemento'
    tipo: 'Tipo' = None


@dataclass(eq=False)
class Tipo:
    """Tipo de un elemento. contenido: 'elementos' (modelo de contenido), 'simple' (texto) o 'libre' (anyType)."""
    ruta: str
    nombre: str = None
    contenido: str = 'elementos'
    simple: Simple = None
    particula: Particula = None
    atributos: dict = field(default_factory=dict)   # nombre → Atributo
    inicio: int = 0
    listo: bool = False
    afd: AFD = None           # solo si contenido == 'elementos'

    def hijos(self):
        """Tipos de los elementos hijos."""
        salida, pila = [], [self.particula] if self.particula else []
        while pila:
            p = pila.pop()
            if p.clase == 'elemento':
                salida.append(p.tipo)
            pila.extend(p.hijos)
        return salida

    def descripcion(self):
        if self.contenido == 'libre':
            return 'cualquier contenido'
        if self.contenido == 'simple':
            return 'texto ' + (self.simple.nombre or f'xs:{self.simple.base}')
        return 'sin hijos' if self.afd is None or self.afd.vacio() else 'elementos'


@dataclass
class AnalisisXSD:
    diagnosticos: list
    elementos: dict           # elementos globales: nombre → Tipo
    tipos: list               # todos los tipos, en el orden en que se leyeron
    usos: list                # clasificación (Uso)
    referencias: list         # (ruta de origen, nombre del destino, es recursiva)
    recursivos: set           # id de los tipos que pueden contenerse a sí mismos
    grupos: int               # grupos de tipos mutuamente recursivos
    libre: Tipo = None

    @property
    def errores(self):
        return [d for d in self.diagnosticos if d.severidad == 'error']

    @property
    def avisos(self):
        return [d for d in self.diagnosticos if d.severidad == 'aviso']

    @property
    def ok(self):
        return not self.errores

    @property
    def nivel(self):
        return nivel(self.usos)

    def informe(self, fuente: bytes = None) -> str:
        def donde(pos):
            if fuente is None:
                return f'byte {pos}'
            return f'{fuente.count(bytes([10]), 0, pos) + 1}:{pos - fuente.rfind(bytes([10]), 0, pos)}'

        lineas = [f'DIAGNÓSTICOS: {len(self.errores)} errores, {len(self.avisos)} avisos']
        lineas += [f'  {d.severidad:5}  {donde(d.inicio)}  {d.codigo}: {d.mensaje}  [{d.ruta}]' for d in self.diagnosticos]
        lineas += ['', f'ELEMENTOS GLOBALES: {len(self.elementos)}']
        lineas += [f'  {nombre:24}  {t.nombre or t.ruta}' for nombre, t in self.elementos.items()]
        lineas += ['', f'TIPOS: {len(self.tipos)}']
        for t in self.tipos:
            atributos = ', '.join(('' if a.requerido else '?') + n for n, a in t.atributos.items())
            lineas.append(f'  {t.nombre or t.ruta:44}  {"recursivo" if id(t) in self.recursivos else "":9}  '
                          f'{t.descripcion()}' + (f'; atributos: {atributos}' if atributos else ''))
        lineas += ['', f'REFERENCIAS: {len(self.referencias)}']
        lineas += [f'  {origen:44}  →  {destino:20}  {"llamada recursiva" if rec else "expansión"}'
                   for origen, destino, rec in self.referencias]
        lineas += ['', 'CLASIFICACIÓN']
        resumen = {}
        for u in self.usos:
            resumen[u.palabra, u.categoria, u.estrategia] = resumen.get((u.palabra, u.categoria, u.estrategia), 0) + 1
        orden = ('a', 'b', 'c', 'anotacion', 'desconocida', 'no-soportada')
        for (palabra, categoria, estrategia), n in sorted(resumen.items(), key=lambda x: (orden.index(x[0][1]), x[0][0])):
            etiqueta = f'({categoria})' if len(categoria) == 1 else categoria
            lineas.append(f'  {etiqueta:12}  {palabra:22}  ×{n:<3}  {estrategia}')
        lineas += ['', f'NIVEL: {NIVELES[self.nivel]}',
                   f'RECURSIÓN: {self.grupos} grupo(s) de tipos recursivos' if self.grupos
                   else 'RECURSIÓN: ninguna; la profundidad del documento la acota el propio esquema']
        return '\n'.join(lineas)


def pruebas_lexicas(simple: Simple) -> tuple:
    """Las pruebas completas de un tipo simple: primero la forma léxica, después las facetas."""
    forma = {'string': (), 'integer': (('xs_entero',),), 'decimal': (('xs_decimal',),), 'boolean': (('xs_booleano',),)}
    return forma[simple.base] + simple.pruebas


class _Lector:
    def __init__(self, raiz):
        self.diag, self.usos, self.tipos, self.referencias = Diagnosticos(), [], [], []
        self.libre = Tipo('(anyType)', 'xs:anyType', 'libre', listo=True)
        self.g_elementos, self.g_tipos = {}, {}          # nombre → (nodo, ruta)
        self.tipo_global, self.complejos, self.simples = {}, {}, {}
        self.predefinidos, self.de_simple, self.en_curso = {}, {}, set()
        self.raiz = raiz
        self.prefijo = raiz.nombre.rpartition(':')[0]
        declarado = raiz.atributo('xmlns:' + self.prefijo if self.prefijo else 'xmlns')
        if raiz.nombre.rpartition(':')[2] != 'schema' or declarado != XS:
            self.diag.error('no-es-xsd', f'la raíz debe ser el elemento «schema» del espacio de nombres {XS}', raiz, '')
            return
        self._atributos(raiz, 'schema', '/schema')
        if raiz.atributo('targetNamespace') is not None:
            self.diag.error('no-soportada', 'los esquemas con targetNamespace no están soportados', raiz, '/schema')
        self._indexar()
        for nombre in self.g_tipos:
            self._tipo_de_nombre(('', nombre), self.g_tipos[nombre][0], self.g_tipos[nombre][1])
        for nombre in self.g_elementos:
            self._global(nombre)
        if not self.g_elementos:
            self.diag.error('sin-elementos', 'el esquema no declara ningún elemento global: ningún documento puede cumplirlo',
                            raiz, '/schema')

    # ---- utilidades ----
    def _local(self, nodo):
        """Nombre local del elemento si es del espacio de nombres de XSD; si no, None."""
        prefijo, _, local = nodo.nombre.rpartition(':')
        return local if prefijo == self.prefijo else None

    def _hijos(self, nodo, ruta):
        """(nombre local, hijo, ruta) de cada hijo de XSD; las anotaciones se saltan."""
        cuenta = {}
        for h in nodo.elementos():
            local = self._local(h)
            if local is None:
                self.diag.error('elemento-desconocido', f'«{h.nombre}» no es un elemento de XSD', h, ruta)
                continue
            nombre = h.atributo('name') or h.atributo('ref')
            cuenta[local] = cuenta.get(local, 0) + 1
            sub = f'{ruta}/{local}' + (f'[{nombre}]' if nombre else f'[{cuenta[local]}]' if cuenta[local] > 1 else '')
            if local == 'annotation':
                self._uso('annotation', 'anotacion', 'documentación: no afecta a la validación', h, sub)
                continue
            yield local, h, sub

    def _uso(self, palabra, categoria, estrategia, nodo, ruta):
        self.usos.append(Uso(palabra, categoria, estrategia, ruta, nodo.inicio, nodo.fin))

    def _no_soportada(self, que, nodo, ruta):
        self._uso(que, 'no-soportada', 'error de compilación', nodo, ruta)
        self.diag.error('no-soportada', f'«{que}» no está soportado', nodo, ruta)

    def _atributos(self, nodo, local, ruta):
        """Rechaza los atributos de XSD que este subconjunto no implementa."""
        for nombre, a in nodo.atributos.items():
            if ':' in nombre or nombre == 'xmlns' or nombre in _SIN_EFECTO or nombre in _ADMITIDOS.get(local, ()):
                continue
            if nombre == 'targetNamespace' and local == 'schema':
                continue   # se diagnostica aparte
            if _VALOR_POR_DEFECTO.get(nombre) == a.valor.strip() or (nombre, a.valor.strip()) == ('use', 'optional'):
                continue
            self._no_soportada(f'{nombre}="{a.valor}" en {local}', nodo, ruta)

    def _qname(self, valor, nodo, ruta):
        """('xs', local) para un nombre de XSD, ('', local) para uno del esquema, o None."""
        prefijo, _, local = valor.strip().rpartition(':')
        if prefijo == self.prefijo and (prefijo or local in ENTEROS or local in
                                        ('string', 'decimal', 'boolean', 'anyType', 'anySimpleType')):
            return 'xs', local
        if prefijo:
            self.diag.error('prefijo-desconocido', f'el prefijo de «{valor}» no está declarado', nodo, ruta)
            return None
        return '', local

    def _entero(self, nodo, atributo, defecto, ruta, ilimitado=False):
        valor = nodo.atributo(atributo)
        if valor is None:
            return defecto
        valor = valor.strip()
        if ilimitado and valor == 'unbounded':
            return None
        if not valor.isascii() or not valor.isdigit() or int(valor) > MAX_CONTADOR:
            self.diag.error('valor-invalido', f'{atributo}="{valor}" debe ser un entero no negativo', nodo, ruta)
            return defecto
        return int(valor)

    # ---- tabla de símbolos ----
    def _indexar(self):
        for local, h, ruta in self._hijos(self.raiz, ''):
            nombre = h.atributo('name')
            if local not in ('element', 'complexType', 'simpleType'):
                self._no_soportada(local, h, ruta)
            elif not nombre or ':' in nombre:
                self.diag.error('sin-nombre', f'«{local}» global necesita un atributo name válido', h, ruta)
            else:
                tabla = self.g_elementos if local == 'element' else self.g_tipos
                if nombre in tabla:
                    self.diag.error('nombre-repetido', f'«{nombre}» ya está declarado', h, ruta)
                else:
                    tabla[nombre] = (h, ruta)

    def _global(self, nombre) -> Tipo:
        """Tipo del elemento global."""
        if nombre not in self.tipo_global:
            nodo, ruta = self.g_elementos[nombre]
            for sobra in ('ref', 'minOccurs', 'maxOccurs'):
                if nodo.atributo(sobra) is not None:
                    self.diag.error('valor-invalido', f'un elemento global no puede llevar «{sobra}»', nodo, ruta)
            tipo = self._tipo_de_elemento(nodo, ruta, lambda t: self.tipo_global.setdefault(nombre, t))
            self.tipo_global.setdefault(nombre, tipo)
        return self.tipo_global[nombre]

    def _tipo_simple(self, simple) -> Tipo:
        """El tipo de un elemento cuyo contenido es solo texto de ese tipo simple."""
        if id(simple) not in self.de_simple:
            self.de_simple[id(simple)] = Tipo(simple.nombre or f'xs:{simple.base}', simple.nombre or f'xs:{simple.base}',
                                              'simple', simple, listo=True)
        return self.de_simple[id(simple)]

    def _predefinido(self, local, nodo, ruta):
        """Tipo simple predefinido, o None si no está soportado."""
        if local not in self.predefinidos:
            if local in ENTEROS:
                minimo, maximo = ENTEROS[local]
                pruebas = tuple(p for p in ((('min', Decimal(minimo), False) if minimo is not None else None),
                                            (('max', Decimal(maximo), False) if maximo is not None else None)) if p)
                self.predefinidos[local] = Simple('integer', pruebas, nombre=f'xs:{local}')
            elif local in ('string', 'anySimpleType', 'decimal', 'boolean'):
                self.predefinidos[local] = Simple('string' if local == 'anySimpleType' else local, nombre=f'xs:{local}')
            else:
                self._no_soportada(f'el tipo predefinido xs:{local}', nodo, ruta)
                return None
        return self.predefinidos[local]

    def _simple_de_nombre(self, q, nodo, ruta) -> Simple:
        """Tipo simple al que se refiere un nombre calificado (para atributos y para base=...)."""
        if q is None:
            return Simple('string')
        espacio, local = q
        if espacio == 'xs':
            return self._predefinido(local, nodo, ruta) or Simple('string')
        if local not in self.g_tipos:
            self.diag.error('tipo-desconocido', f'no existe el tipo «{local}»', nodo, ruta)
            return Simple('string')
        declaracion, su_ruta = self.g_tipos[local]
        if self._local(declaracion) != 'simpleType':
            self.diag.error('tipo-no-simple', f'«{local}» es un tipo complejo y aquí hace falta uno simple', nodo, ruta)
            return Simple('string')
        if local not in self.simples:
            if local in self.en_curso:
                self.diag.error('ciclo-de-tipos', f'el tipo «{local}» se define a partir de sí mismo', nodo, ruta)
                return Simple('string')
            self.en_curso.add(local)
            self._atributos(declaracion, 'simpleType', su_ruta)
            simple = self._simple(declaracion, su_ruta)
            simple.nombre = local
            self.simples[local] = simple
            self.en_curso.discard(local)
        return self.simples[local]

    def _tipo_de_nombre(self, q, nodo, ruta) -> Tipo:
        """Tipo de elemento al que se refiere type="..."."""
        if q is None:
            return self.libre
        espacio, local = q
        if espacio == 'xs':
            if local == 'anyType':
                return self.libre
            simple = self._predefinido(local, nodo, ruta)
            return self._tipo_simple(simple) if simple else self.libre
        if local not in self.g_tipos:
            self.diag.error('tipo-desconocido', f'no existe el tipo «{local}»', nodo, ruta)
            return self.libre
        declaracion, su_ruta = self.g_tipos[local]
        if self._local(declaracion) == 'simpleType':
            return self._tipo_simple(self._simple_de_nombre(q, nodo, ruta))
        if local not in self.complejos:
            tipo = self.complejos[local] = self._nuevo(su_ruta, declaracion, local)
            self._llenar(tipo, declaracion, su_ruta)
        return self.complejos[local]

    def _nuevo(self, ruta, nodo, nombre=None) -> Tipo:
        tipo = Tipo(ruta, nombre, inicio=nodo.inicio)
        self.tipos.append(tipo)
        return tipo

    # ---- declaraciones ----
    def _tipo_de_elemento(self, nodo, ruta, registrar=lambda t: None) -> Tipo:
        """Tipo de una declaración de elemento: type="...", un tipo anónimo dentro o, si no hay nada, anyType."""
        self._atributos(nodo, 'element', ruta)
        nombre_de_tipo, dentro = nodo.atributo('type'), []
        for local, h, sub in self._hijos(nodo, ruta):
            if local in ('complexType', 'simpleType'):
                dentro.append((local, h, sub))
            else:
                self._no_soportada(local, h, sub)
        if nombre_de_tipo is not None and dentro or len(dentro) > 1:
            self.diag.error('tipo-duplicado', 'un elemento solo puede tener un tipo: type="..." o un tipo anónimo', nodo, ruta)
        if nombre_de_tipo is not None:
            tipo = self._tipo_de_nombre(self._qname(nombre_de_tipo, nodo, ruta), nodo, ruta)
            self.referencias.append((ruta, nombre_de_tipo.strip(), tipo))
            return tipo
        if not dentro:
            return self.libre
        local, h, sub = dentro[0]
        if h.atributo('name') is not None:
            self.diag.error('valor-invalido', 'un tipo anónimo no lleva name', h, sub)
        if local == 'simpleType':
            self._atributos(h, 'simpleType', sub)
            return self._tipo_simple(self._simple(h, sub))
        tipo = self._nuevo(sub, h)
        registrar(tipo)   # antes de leerlo: dentro puede haber una referencia al propio elemento
        self._llenar(tipo, h, sub)
        return tipo

    def _llenar(self, tipo, nodo, ruta):
        """Lee un complexType."""
        self._atributos(nodo, 'complexType', ruta)
        modelos = 0
        for local, h, sub in self._hijos(nodo, ruta):
            if local in ('sequence', 'choice'):
                modelos += 1
                if modelos > 1 or tipo.contenido != 'elementos':
                    self.diag.error('contenido-duplicado', 'un tipo complejo solo puede tener un modelo de contenido', h, sub)
                else:
                    tipo.particula = self._particula(local, h, sub, tipo)
            elif local == 'attribute':
                self._atributo(tipo, h, sub)
            elif local == 'simpleContent':
                self._atributos(h, 'simpleContent', sub)
                if modelos:
                    self.diag.error('contenido-duplicado', 'un tipo complejo solo puede tener un modelo de contenido', h, sub)
                self._contenido_simple(tipo, h, sub)
                modelos += 1
            else:
                self._no_soportada(local, h, sub)
        tipo.listo = True

    def _contenido_simple(self, tipo, nodo, ruta):
        tipo.contenido, tipo.simple = 'simple', Simple('string')
        for local, h, sub in self._hijos(nodo, ruta):
            if local != 'extension':
                self._no_soportada(f'{local} dentro de simpleContent', h, sub)
                continue
            self._atributos(h, 'extension', sub)
            q = self._qname(h.atributo('base') or '', h, sub)
            if h.atributo('base') is None:
                self.diag.error('valor-invalido', 'falta el atributo base', h, sub)
            elif q is not None and q[0] == '' and q[1] in self.g_tipos and self._local(self.g_tipos[q[1]][0]) == 'complexType':
                base = self._tipo_de_nombre(q, h, sub)
                if not base.listo:
                    self.diag.error('ciclo-de-tipos', f'el tipo «{q[1]}» se extiende a sí mismo', h, sub)
                elif base.contenido != 'simple':
                    self._no_soportada('extender un tipo complejo que no es de contenido simple', h, sub)
                else:
                    tipo.simple, tipo.atributos = base.simple, dict(base.atributos)
            else:
                tipo.simple = self._simple_de_nombre(q, h, sub)
            self._uso('simpleContent', 'a', 'texto del elemento con pruebas sobre su valor', h, sub)
            for local2, h2, sub2 in self._hijos(h, sub):
                if local2 == 'attribute':
                    self._atributo(tipo, h2, sub2)
                else:
                    self._no_soportada(local2, h2, sub2)

    def _atributo(self, tipo, nodo, ruta):
        self._atributos(nodo, 'attribute', ruta)
        nombre, uso = nodo.atributo('name'), (nodo.atributo('use') or 'optional').strip()
        if nodo.atributo('ref') is not None:
            return self._no_soportada('attribute con ref', nodo, ruta)
        if not nombre or ':' in nombre or nombre.startswith('xmlns'):
            return self.diag.error('sin-nombre', 'el atributo necesita un name válido', nodo, ruta)
        if uso not in ('optional', 'required'):
            return self._no_soportada(f'use="{uso}"', nodo, ruta)
        simple, anonimos = None, []
        for local, h, sub in self._hijos(nodo, ruta):
            if local == 'simpleType':
                anonimos.append((h, sub))
            else:
                self._no_soportada(local, h, sub)
        if nodo.atributo('type') is not None:
            simple = self._simple_de_nombre(self._qname(nodo.atributo('type'), nodo, ruta), nodo, ruta)
        if anonimos:
            if simple is not None or len(anonimos) > 1:
                self.diag.error('tipo-duplicado', 'un atributo solo puede tener un tipo', nodo, ruta)
            self._atributos(anonimos[0][0], 'simpleType', anonimos[0][1])
            simple = self._simple(*anonimos[0])
        if nombre in tipo.atributos:
            self.diag.error('nombre-repetido', f'el atributo «{nombre}» ya está declarado en este tipo', nodo, ruta)
        tipo.atributos[nombre] = Atributo(nombre, simple or Simple('string'), uso == 'required')
        self._uso('attribute', 'b', 'un bit en la máscara del marco; los obligatorios se comprueban al cerrar la etiqueta'
                  if uso == 'required' else 'un bit en la máscara del marco (detecta el atributo repetido)', nodo, ruta)

    def _particula(self, local, nodo, ruta, dueno) -> Particula:
        """sequence o choice, con sus hijos."""
        self._atributos(nodo, local, ruta)
        p = Particula('secuencia' if local == 'sequence' else 'opcion', *self._ocurrencias(nodo, ruta))
        self._uso(local, 'a', 'autómata finito del modelo de contenido (Glushkov, subconjuntos y minimización)', nodo, ruta)
        for local2, h, sub in self._hijos(nodo, ruta):
            if local2 in ('sequence', 'choice'):
                p.hijos.append(self._particula(local2, h, sub, dueno))
            elif local2 == 'element':
                p.hijos.append(self._elemento_local(h, sub, dueno))
            else:
                self._no_soportada(local2, h, sub)
        return p

    def _ocurrencias(self, nodo, ruta):
        minimo = self._entero(nodo, 'minOccurs', 1, ruta)
        maximo = self._entero(nodo, 'maxOccurs', 1, ruta, ilimitado=True)
        if maximo is not None and minimo > maximo:
            self.diag.error('valor-invalido', f'minOccurs ({minimo}) es mayor que maxOccurs ({maximo})', nodo, ruta)
            maximo = minimo
        if (minimo, maximo) != (1, 1):
            self._uso('minOccurs/maxOccurs', 'a', 'copias y ciclos en el autómata del modelo de contenido', nodo, ruta)
        return minimo, maximo

    def _elemento_local(self, nodo, ruta, dueno) -> Particula:
        minimo, maximo = self._ocurrencias(nodo, ruta)
        ref, nombre = nodo.atributo('ref'), nodo.atributo('name')
        self._uso('element', 'a', 'estados del elemento y una transición en el modelo de contenido de su padre', nodo, ruta)
        if ref is not None:
            self._atributos(nodo, 'element', ruta)
            q = self._qname(ref, nodo, ruta)
            if nombre is not None or nodo.atributo('type') is not None or nodo.elementos():
                self.diag.error('valor-invalido', 'un elemento con ref no lleva name, type ni tipo anónimo', nodo, ruta)
            if q is None or q[0] != '' or q[1] not in self.g_elementos:
                self.diag.error('elemento-desconocido', f'no existe el elemento global «{ref}»', nodo, ruta)
                return Particula('elemento', minimo, maximo, nombre=ref.strip().rpartition(':')[2] or '?', tipo=self.libre)
            tipo = self._global(q[1])
            self.referencias.append((ruta, q[1], tipo))
            return Particula('elemento', minimo, maximo, nombre=q[1], tipo=tipo)
        if not nombre or ':' in nombre:
            self.diag.error('sin-nombre', 'el elemento necesita un name válido (o un ref)', nodo, ruta)
            nombre = '?'
        return Particula('elemento', minimo, maximo, nombre=nombre, tipo=self._tipo_de_elemento(nodo, ruta))

    # ---- tipos simples ----
    def _simple(self, nodo, ruta) -> Simple:
        """Lee un simpleType: una restricción de otro tipo simple."""
        resultado = None
        for local, h, sub in self._hijos(nodo, ruta):
            if local != 'restriction' or resultado is not None:
                self._no_soportada(local, h, sub)
                continue
            self._atributos(h, 'restriction', sub)
            anonimos = [(h2, sub2) for local2, h2, sub2 in self._hijos(h, sub) if local2 == 'simpleType']
            if h.atributo('base') is not None:
                base = self._simple_de_nombre(self._qname(h.atributo('base'), h, sub), h, sub)
            elif anonimos:
                base = self._simple(*anonimos[0])
            else:
                self.diag.error('valor-invalido', 'la restricción necesita base="..." o un simpleType dentro', h, sub)
                base = Simple('string')
            resultado = self._facetas(base, h, sub)
        if resultado is None:
            self.diag.error('valor-invalido', 'el simpleType no tiene ninguna restricción', nodo, ruta)
        return resultado or Simple('string')

    def _facetas(self, base, nodo, ruta) -> Simple:
        pruebas, patrones, enumerados, propios = list(base.pruebas), list(base.patrones), [], []
        numerico = base.base in ('integer', 'decimal')
        forma = ENTERO if base.base == 'integer' else DECIMAL
        for local, h, sub in self._hijos(nodo, ruta):
            if local == 'simpleType':
                continue
            valor = h.atributo('value')
            if valor is None:
                self.diag.error('valor-invalido', f'la faceta «{local}» necesita un atributo value', h, sub)
            elif local == 'pattern':
                propios.append((valor, h, sub))
            elif local == 'enumeration':
                enumerados.append((valor, h, sub))
            elif local in _FACETAS_DE_LONGITUD:
                n = self._entero(h, 'value', None, sub)
                if base.base != 'string':
                    self.diag.error('faceta-no-aplicable', f'«{local}» solo se aplica a cadenas', h, sub)
                elif n is not None:
                    pruebas += [(c, n) for c in (('long_min', 'long_max') if local == 'length' else
                                                 ('long_min',) if local == 'minLength' else ('long_max',))]
                    self._uso(local, 'a', 'prueba sobre el valor al leer el texto o el atributo', h, sub)
            elif local in _FACETAS_DE_RANGO:
                if not numerico:
                    self.diag.error('faceta-no-aplicable', f'«{local}» solo se aplica a números', h, sub)
                elif not forma.match(valor.strip(BLANCOS)):
                    self.diag.error('valor-invalido', f'«{valor}» no es un valor de xs:{base.base}', h, sub)
                else:
                    clase, exclusivo = _FACETAS_DE_RANGO[local]
                    pruebas.append((clase, Decimal(valor.strip(BLANCOS)), exclusivo))
                    self._uso(local, 'a', 'prueba sobre el valor al leer el texto o el atributo', h, sub)
            else:
                self._no_soportada(f'la faceta {local}', h, sub)
        if enumerados:
            _, h, sub = enumerados[0]
            self._uso('enumeration', 'a', 'prueba sobre el valor: un trie para cadenas, comparación decimal para números', h, sub)
            if base.base == 'boolean':
                self.diag.error('faceta-no-aplicable', '«enumeration» no se aplica a xs:boolean', h, sub)
            elif numerico:
                validos = [v.strip(BLANCOS) for v, _, _ in enumerados if forma.match(v.strip(BLANCOS))]
                for v, h, sub in enumerados:
                    if not forma.match(v.strip(BLANCOS)):
                        self.diag.error('valor-invalido', f'«{v}» no es un valor de xs:{base.base}', h, sub)
                pruebas.append(('enum_num', tuple(sorted({Decimal(v) for v in validos}))))
            else:
                pruebas.append(('enum_cad', tuple(sorted({v for v, _, _ in enumerados}))))
        if propios:
            self._patrones(propios, base, pruebas, patrones)
        return Simple(base.base, tuple(pruebas), tuple(patrones))

    def _patrones(self, propios, base, pruebas, patrones):
        """Las facetas pattern de un mismo paso de restricción se combinan con «o» (XSD Parte 2, 4.3.4):
        forman una sola prueba, un AFD anclado de la unión. Las de pasos distintos se acumulan (y)."""
        motivo, errores = None, False
        for valor, h, sub in propios:
            try:
                expresiones.analizar_xsd(valor)
            except expresiones.ErrorDeExpresion as e:
                self.diag.error('expresion-invalida', f'la expresión regular «{valor}» no es válida: {e}', h, sub)
                errores = True
            except expresiones.NoRegular as e:
                motivo = motivo or str(e)
        if errores:
            return
        textos = tuple(valor for valor, _, _ in propios)
        if motivo is None:
            try:
                expresiones.de_xsd(textos)
            except expresiones.NoRegular as e:
                motivo = str(e)
        _, h, sub = propios[0]
        if motivo is None:
            # en xs:string el valor va tal cual; en los demás tipos, sin los espacios de los lados (collapse)
            pruebas.append(('patron', textos, base.base != 'string'))
            for _, h2, sub2 in propios:
                self._uso('pattern', 'c', 'AFD de la expresión regular (anclada), en la misma pasada', h2, sub2)
        else:
            patrones.append(sub)
            for _, h2, sub2 in propios:
                self._uso('pattern', 'c', f'verificación diferida: {motivo}', h2, sub2)
            self.diag.aviso('diferida', f'pattern queda en verificación diferida ({motivo}): el resultado será '
                                        '«pendiente»', h, sub)


def analizar_xsd(fuente: bytes) -> AnalisisXSD:
    """Lexer + parser de XML + análisis semántico del esquema. Lanza ErrorLexico o ErrorSintactico si el
    texto no es XML bien formado."""
    lector = _Lector(analizar_xml(fuente))
    diag = lector.diag

    # autómata del modelo de contenido de cada tipo
    for t in lector.tipos:
        if t.contenido == 'elementos':
            try:
                t.afd = construir_afd(t.particula)
            except ModeloDemasiadoGrande as e:
                diag.append(_diagnostico('modelo-demasiado-grande', str(e), t))
            except TiposInconsistentes as e:
                diag.append(_diagnostico('elemento-inconsistente',
                                         f'el elemento «{e.nombre}» aparece con dos tipos distintos en el mismo contenido', t))
            if t.afd is None:
                t.afd = construir_afd(None)

    # recursión: qué tipos pueden contenerse a sí mismos
    alcanza = {id(t): {id(h): h for h in t.hijos()} for t in lector.tipos}
    cambio = True
    while cambio:
        cambio = False
        for desde in alcanza.values():
            for h in list(desde.values()):
                for k, v in alcanza.get(id(h), {}).items():
                    if k not in desde:
                        desde[k], cambio = v, True
    recursivos = {i for i, desde in alcanza.items() if i in desde}
    grupos = {frozenset(k for k in alcanza[i] if k in recursivos and i in alcanza[k]) for i in recursivos}

    # un tipo recursivo necesita un caso base: algún contenido finito que lo cumpla
    productivo = {id(t) for t in lector.tipos if t.contenido != 'elementos'} | {id(lector.libre)}
    productivo |= {id(t) for t in lector.de_simple.values()}
    cambio = True
    while cambio:
        cambio = False
        for t in lector.tipos:
            if id(t) in productivo:
                continue
            vistos, pila = {0}, [0]
            while pila:   # ¿se llega a un estado final usando solo hijos que ya son productivos?
                q = pila.pop()
                for nombre, d in t.afd.delta[q].items():
                    if d not in vistos and id(t.afd.tipo_de[nombre]) in productivo:
                        vistos.add(d)
                        pila.append(d)
            if any(t.afd.acepta[q] for q in vistos):
                productivo.add(id(t))
                cambio = True
    for t in lector.tipos:
        if id(t) not in productivo:
            diag.append(_diagnostico('recursion-sin-caso-base', 'ningún documento finito puede cumplir este tipo recursivo', t))

    referencias = [(origen, destino, id(tipo) in recursivos) for origen, destino, tipo in lector.referencias]
    for origen, destino, recursiva in referencias:
        if recursiva:
            lector.usos.append(Uso('type/ref recursivo', 'a', 'llamada recursiva: la pila recuerda dónde seguir en el padre',
                                   origen, 0, 0))
    diag.sort(key=lambda d: (d.inicio, d.severidad))
    return AnalisisXSD(list(diag), {n: lector.tipo_global[n] for n in lector.g_elementos if n in lector.tipo_global},
                       lector.tipos, lector.usos, referencias, recursivos, len(grupos), lector.libre)


def _diagnostico(codigo, mensaje, tipo):
    return Diagnostico('error', codigo, mensaje, tipo.inicio, tipo.inicio, tipo.ruta)


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    with open(sys.argv[1], 'rb') as f:
        contenido = f.read()
    resultado = analizar_xsd(contenido)
    print(resultado.informe(contenido))
    sys.exit(0 if resultado.ok else 1)
