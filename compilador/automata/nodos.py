"""Nodos de validación (Fase 4, segundo paso): qué se exige de un valor en cada posición del documento.

Sobre un mismo valor pueden aplicarse varios subesquemas a la vez: el propio, el destino de su $ref,
las ramas de su allOf... Un Nodo es ese conjunto de formas, y validar el valor es validarlo contra todas
(una conjunción). Es el producto de autómatas hecho sobre el esquema: como todos los miembros leen el
mismo valor, apilan y desapilan a la vez, y el estado del producto es el conjunto de miembros.

El conjunto se cierra siguiendo los enlaces que no consumen entrada, igual que una clausura-ε. En un
anyOf o un oneOf se entra por la única rama que admite la clase del token que llega (despacho por el
primer token).

Grupos de sub-ejecuciones. Lo que no es una conjunción (un anyOf o un oneOf en que dos ramas admiten
el mismo tipo de valor, not, if/then/else, dependentSchemas, contains, propertyNames) se resuelve con el
producto de autómatas en tiempo de ejecución: al llegar el primer token del valor nacen sub-ejecuciones
del mismo DPDA, una por rama, que leen ese valor en paralelo con la principal. Cada una lleva su propio
peso; cuando el valor termina, los pesos se combinan según la palabra clave:

    alguna (anyOf)         falla si todas las ramas tienen peso > 0  (el mínimo del semianillo tropical)
    una (oneOf)            falla si no hay exactamente una rama con peso 0
    no (not)               falla si la rama tiene peso 0
    si (if/then/else)      falla si el if pesa 0 y el then no, o si el if pesa > 0 y el else no
    dependiente            como si, con «required: [nombre]» de condición (dependentSchemas)
    nombres                falla si la clave, leída como cadena, no cumple propertyNames
    contiene               cuenta en el marco del arreglo los elementos que cumplen contains
    cumple                 falla si la rama tiene peso > 0 (un segundo contains del mismo arreglo)

Un grupo se describe como (clase, (nodo, nodo, ...)); en «si» y «dependiente» un None es una rama
ausente (then o else que no está).
"""
from dataclasses import dataclass

from ..semantico.metaesquema import decimal
from . import expresiones, valores
from .formas import CLASES, Forma, costo

MAX_NODOS = 50_000  # cota del producto: por encima, el esquema se rechaza

CLASE_DE_TOKEN = {'NULL': 'null', 'TRUE': 'boolean', 'FALSE': 'boolean', 'NUMERO': 'number', 'CADENA': 'string',
                  'LLAVE_ABRE': 'object', 'CORCHETE_ABRE': 'array'}
ESCALARES = ('CADENA', 'NUMERO', 'TRUE', 'FALSE', 'NULL')
_CLASE_AST = {'null': 'nulo', 'boolean': 'booleano', 'number': 'numero', 'string': 'cadena',
              'object': 'objeto', 'array': 'arreglo'}


class ExplosionDeEstados(Exception):
    pass


@dataclass(frozen=True)
class Escalar:
    """Qué pasa al leer un token escalar: violaciones fijas, pruebas sobre su valor, comprobaciones
    diferidas y grupos de sub-ejecuciones que nacen con él."""
    peso: int = 0
    pruebas: tuple = ()
    pendientes: int = 0
    hilos: tuple = ()


@dataclass(frozen=True)
class Objeto:
    prohibido: bool = False     # el nodo no admite objetos: una violación y se salta con el nodo libre
    pendientes: int = 0
    nombres: tuple = ()         # propiedades con bit propio, ordenadas; el bit i es de nombres[i]
    hijos: tuple = ()           # nodo del valor de cada una
    otra: int = 0               # nodo del valor de cualquier otra propiedad que no case con ningún patrón
    requeridas: int = 0         # máscara de bits
    dependencias: tuple = ()    # ((bit que la activa, máscara que exige), ...)
    min_props: int | None = None
    max_props: int | None = None
    por_patrones: tuple = ()    # ((expresiones con las que casa la clave, nodo), ...) para las no declaradas
    hilos: tuple = ()           # grupos que nacen al abrir el objeto
    hilos_clave: tuple = ()     # grupos que nacen en cada clave (propertyNames)
    cierre: tuple = ()          # pruebas al cerrar el objeto: ('enum_huella', huellas)


@dataclass(frozen=True)
class Arreglo:
    prohibido: bool = False
    pendientes: int = 0
    prefijo: tuple = ()         # nodo de cada posición inicial
    resto: int = 0              # nodo de los demás elementos
    min_items: int | None = None
    max_items: int | None = None
    hilos: tuple = ()           # grupos que nacen al abrir el arreglo
    hilos_elemento: tuple = ()  # grupos que nacen en cada elemento (contains)
    recoger: bool = False       # guardar las huellas de los elementos (uniqueItems)
    cierre: tuple = ()          # pruebas al cerrar: ('enum_huella', huellas), ('contiene', mín, máx), ('unicos',)


@dataclass
class Nodo:
    id: int
    formas: frozenset
    escalar: dict = None        # token → Escalar
    objeto: Objeto = None
    arreglo: Arreglo = None


def _admite(tipos, clase):
    return clase in tipos or (clase == 'number' and 'integer' in tipos)


def _miembros(formas, clase):
    """Clausura del conjunto para un valor de esa clase.
    Devuelve (miembros, prohibido, diferidas, grupos): diferidas es la lista de (ruta, palabra) sin
    verificar y grupos la de (clase de grupo, formas de sus ramas)."""
    miembros, vistos, diferidas, grupos, prohibido = [], set(), [], [], False
    pila = sorted(formas, key=lambda f: f.ruta, reverse=True)
    while pila:
        f = pila.pop()
        if f is None or id(f) in vistos:
            continue
        vistos.add(id(f))
        if f.booleano is not None:
            prohibido |= not f.booleano
            continue
        miembros.append(f)
        if f.tipos is not None and not _admite(f.tipos, clase):
            prohibido = True
        diferidas += [(f.ruta, palabra) for palabra in f.diferidas[clase]]
        if f.ref is not None:
            pila.append(f.ref)
        pila.extend(f.todas)
        for palabra, ramas in (('alguna', f.alguna), ('una', f.una)):
            if ramas is None:
                continue
            candidatas = [r for r in ramas if clase in r.posibles]
            if not candidatas:
                prohibido = True        # ninguna rama admite esta clase de valor
            elif len(candidatas) == 1:
                pila.append(candidatas[0])  # las demás ramas fallan seguro: decide el primer token
            else:
                grupos.append((palabra, tuple(candidatas)))
        if f.no is not None:
            if f.no.booleano is True:
                prohibido = True
            elif f.no.booleano is None and clase in f.no.posibles:
                grupos.append(('no', (f.no,)))
        if f.si is not None:
            if f.si.booleano is True:
                pila.append(f.entonces)
            elif f.si.booleano is False or clase not in f.si.posibles:
                pila.append(f.sino)     # el if falla seguro con esta clase de valor
            else:
                grupos.append(('si', (f.si, f.entonces, f.sino)))
        if clase == 'object':
            for condicion, esquema in f.esquemas_dependientes:
                grupos.append(('dependiente', (condicion, esquema, None)))
    return miembros, prohibido, diferidas, grupos


class Nodos:
    """Todos los nodos alcanzables desde la raíz. El nodo 0 es el libre (conjunto vacío): acepta cualquier valor."""

    def __init__(self, raiz: Forma):
        self.lista, self._por_conjunto, self.diferidas = [], {}, set()
        self.libre = self._nodo(frozenset())
        self.raiz = self._nodo(frozenset([raiz]))
        i = 0
        while i < len(self.lista):  # la lista crece mientras se resuelven los nodos
            self._resolver(self.lista[i])
            i += 1

    def _nodo(self, formas) -> int:
        formas = frozenset(formas)
        if formas not in self._por_conjunto:
            if len(self.lista) >= MAX_NODOS:
                raise ExplosionDeEstados(f'el producto de subesquemas supera los {MAX_NODOS} nodos')
            self._por_conjunto[formas] = len(self.lista)
            self.lista.append(Nodo(len(self.lista), formas))
        return self._por_conjunto[formas]

    def _grupos(self, grupos):
        return tuple((clase, tuple(None if f is None else self._nodo([f]) for f in ramas)) for clase, ramas in grupos)

    def _resolver(self, nodo):
        nodo.escalar = {token: self._escalar(nodo.formas, token) for token in ESCALARES}
        nodo.objeto = self._objeto(nodo.formas)
        nodo.arreglo = self._arreglo(nodo.formas)

    def _escalar(self, formas, token):
        clase = CLASE_DE_TOKEN[token]
        miembros, prohibido, diferidas, grupos = _miembros(formas, clase)
        if prohibido:
            return Escalar(peso=1)
        self.diferidas.update(diferidas)
        peso, pruebas = 0, []
        for f in miembros:
            if clase == 'number' and f.tipos is not None and 'number' not in f.tipos:
                pruebas.append(('entero',))  # "type" solo admite "integer"
            pruebas += f.pruebas.get(clase, [])
            for valores_permitidos in f.conjuntos:
                de_la_clase = [v for v in valores_permitidos if v.clase == _CLASE_AST[clase]]
                if clase == 'number' and de_la_clase:
                    pruebas.append(('enum_num', tuple(decimal(v) for v in de_la_clase)))
                elif clase == 'string' and de_la_clase:
                    pruebas.append(('enum_cad', tuple(sorted({v.dato for v in de_la_clase}))))
                elif clase == 'boolean':
                    peso += not any(v.dato is (token == 'TRUE') for v in de_la_clase)
                else:
                    peso += not de_la_clase  # ningún valor permitido es de esta clase
        return Escalar(peso, tuple(sorted(pruebas, key=costo)), len(diferidas), self._grupos(grupos))

    def _contenedor(self, formas, clase):
        miembros, prohibido, diferidas, grupos = _miembros(formas, clase)
        huellas = []
        for f in miembros:
            for valores_permitidos in f.conjuntos:
                de_la_clase = [v for v in valores_permitidos if v.clase == _CLASE_AST[clase]]
                if de_la_clase:  # se compara la huella del valor completo al cerrarlo
                    huellas.append(('enum_huella', tuple(sorted({valores.de_valor(v) for v in de_la_clase}))))
                else:
                    prohibido = True
        if not prohibido:
            self.diferidas.update(diferidas)
        return miembros, prohibido, len(diferidas), grupos, huellas

    def _objeto(self, formas):
        miembros, prohibido, pendientes, grupos, huellas = self._contenedor(formas, 'object')
        if prohibido:
            return Objeto(prohibido=True)
        nombres = sorted({n for f in miembros for n in f.propiedades} | {n for f in miembros for n in f.requeridas}
                         | {n for f in miembros for k, lista in f.dependientes for n in (k, *lista)})
        bit = {n: i for i, n in enumerate(nombres)}
        mascara = lambda ns: sum(1 << bit[n] for n in set(ns))

        def hijo(nombre, patrones):
            """Nodo del valor de una propiedad: por cada miembro, su esquema en "properties", los de los
            patrones con los que casa el nombre y, si no hay ninguno de los dos, "additionalProperties".
            Para una propiedad declarada nombre es su nombre; para las demás, None."""
            esquemas = []
            for f in miembros:
                aplicados = [s for p, s in f.patrones if p in patrones]
                if nombre is not None and nombre in f.propiedades:
                    esquemas.append(f.propiedades[nombre])
                elif not aplicados and f.adicional is not None:
                    esquemas.append(f.adicional)
                esquemas += aplicados
            return self._nodo(esquemas)

        patrones = sorted({p for f in miembros for p, _ in f.patrones})
        casa = lambda n: frozenset(p for p in patrones if expresiones.coincide(p, n))
        por_patrones = ()
        if patrones:
            try:
                afd = expresiones.afd([(p, 'patron', p) for p in patrones])
            except expresiones.NoRegular as e:
                raise ExplosionDeEstados(f'el producto de los patrones de patternProperties es demasiado grande: {e}')
            # cada conjunto de patrones con el que puede casar una clave no declarada da un nodo
            por_patrones = tuple((m, hijo(None, m)) for m in sorted((m for m in afd.alcanzables() if m), key=sorted))
        minimos = [f.min_props for f in miembros if f.min_props is not None]
        maximos = [f.max_props for f in miembros if f.max_props is not None]
        nombres_de_clave = tuple(('nombres', (self._nodo([f.nombres]),)) for f in miembros
                                 if f.nombres is not None and f.nombres.booleano is not True)
        return Objeto(
            pendientes=pendientes, nombres=tuple(nombres), hijos=tuple(hijo(n, casa(n)) for n in nombres),
            otra=hijo(None, frozenset()),
            requeridas=mascara(n for f in miembros for n in f.requeridas),
            dependencias=tuple(sorted({(bit[k], mascara(lista)) for f in miembros for k, lista in f.dependientes})),
            min_props=max(minimos, default=None), max_props=min(maximos, default=None),
            por_patrones=por_patrones, hilos=self._grupos(grupos), hilos_clave=nombres_de_clave,
            cierre=tuple(huellas))

    def _arreglo(self, formas):
        miembros, prohibido, pendientes, grupos, huellas = self._contenedor(formas, 'array')
        if prohibido:
            return Arreglo(prohibido=True)
        largo = max((len(f.prefijo) for f in miembros), default=0)
        # al elemento i se le aplica, por cada miembro, prefixItems[i] o si no "items"
        en = lambda i: self._nodo(s for s in ((f.prefijo[i] if i < len(f.prefijo) else f.elementos) for f in miembros)
                                  if s is not None)
        minimos = [f.min_items for f in miembros if f.min_items is not None]
        maximos = [f.max_items for f in miembros if f.max_items is not None]
        cierre, por_elemento = list(huellas), ()
        contienen = [f for f in miembros if f.contiene is not None]
        if contienen:
            # el primer contains se cuenta en el marco del arreglo; los demás, cada uno en una sub-ejecución
            # propia que solo tiene ese contains (el marco tiene un solo contador)
            esquema, minimo, maximo = contienen[0].contiene
            por_elemento = (('contiene', (self._nodo([esquema]),)),)
            cierre.append(('contiene', minimo, maximo))
            grupos += [('cumple', (f.solo_contiene,)) for f in contienen[1:]]
        recoger = any(f.unicos for f in miembros)
        if recoger:
            cierre.append(('unicos',))
        return Arreglo(pendientes=pendientes, prefijo=tuple(en(i) for i in range(largo)), resto=en(largo),
                       min_items=max(minimos, default=None), max_items=min(maximos, default=None),
                       hilos=self._grupos(grupos), hilos_elemento=por_elemento, recoger=recoger, cierre=tuple(cierre))


assert set(CLASES) == set(CLASE_DE_TOKEN.values())
