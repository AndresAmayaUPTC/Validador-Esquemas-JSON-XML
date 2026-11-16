"""Nodos de validación (Fase 4, segundo paso): qué se exige de un valor en cada posición del documento.

Sobre un mismo valor pueden aplicarse varios subesquemas a la vez: el propio, el destino de su $ref,
las ramas de su allOf... Un Nodo es ese conjunto de formas, y validar el valor es validarlo contra todas
(una conjunción). Es el producto de autómatas hecho sobre el esquema: como todos los miembros leen el
mismo valor, apilan y desapilan a la vez, y el estado del producto es el conjunto de miembros.

El conjunto se cierra siguiendo los enlaces que no consumen entrada, igual que una clausura-ε. En un
anyOf o un oneOf se entra por la única rama que admite la clase del token que llega (despacho por el
primer token); si hay más de una candidata, esa comprobación queda en verificación diferida.
"""
from dataclasses import dataclass

from ..semantico.metaesquema import decimal
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
    """Qué pasa al leer un token escalar: violaciones fijas, pruebas sobre su valor y comprobaciones diferidas."""
    peso: int = 0
    pruebas: tuple = ()
    pendientes: int = 0


@dataclass(frozen=True)
class Objeto:
    prohibido: bool = False     # el nodo no admite objetos: una violación y se salta con el nodo libre
    pendientes: int = 0
    nombres: tuple = ()         # propiedades con bit propio, ordenadas; el bit i es de nombres[i]
    hijos: tuple = ()           # nodo del valor de cada una
    otra: int = 0               # nodo del valor de cualquier otra propiedad
    requeridas: int = 0         # máscara de bits
    dependencias: tuple = ()    # ((bit que la activa, máscara que exige), ...)
    min_props: int | None = None
    max_props: int | None = None


@dataclass(frozen=True)
class Arreglo:
    prohibido: bool = False
    pendientes: int = 0
    prefijo: tuple = ()         # nodo de cada posición inicial
    resto: int = 0              # nodo de los demás elementos
    min_items: int | None = None
    max_items: int | None = None


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
    Devuelve (miembros, prohibido, diferidas): diferidas es la lista de (ruta, palabra) sin verificar."""
    miembros, vistos, diferidas, prohibido = [], set(), [], False
    pila = sorted(formas, key=lambda f: f.ruta, reverse=True)
    while pila:
        f = pila.pop()
        if id(f) in vistos:
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
        for palabra, ramas in (('anyOf', f.alguna), ('oneOf', f.una)):
            if ramas is None:
                continue
            candidatas = [r for r in ramas if clase in r.posibles]
            if not candidatas:
                prohibido = True        # ninguna rama admite esta clase de valor
            elif len(candidatas) == 1:
                pila.append(candidatas[0])  # las demás ramas fallan seguro: decide el primer token
            else:
                diferidas.append((f.ruta, palabra))
    return miembros, prohibido, diferidas


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

    def _resolver(self, nodo):
        nodo.escalar = {token: self._escalar(nodo.formas, token) for token in ESCALARES}
        nodo.objeto = self._objeto(nodo.formas)
        nodo.arreglo = self._arreglo(nodo.formas)

    def _escalar(self, formas, token):
        clase = CLASE_DE_TOKEN[token]
        miembros, prohibido, diferidas = _miembros(formas, clase)
        if prohibido:
            return Escalar(peso=1)
        self.diferidas.update(diferidas)
        peso, pruebas = 0, []
        for f in miembros:
            if clase == 'number' and f.tipos is not None and 'number' not in f.tipos:
                pruebas.append(('entero',))  # "type" solo admite "integer"
            pruebas += f.pruebas.get(clase, [])
            for valores in f.conjuntos:
                de_la_clase = [v for v in valores if v.clase == _CLASE_AST[clase]]
                if clase == 'number' and de_la_clase:
                    pruebas.append(('enum_num', tuple(decimal(v) for v in de_la_clase)))
                elif clase == 'string' and de_la_clase:
                    pruebas.append(('enum_cad', tuple(sorted({v.dato for v in de_la_clase}))))
                elif clase == 'boolean':
                    peso += not any(v.dato is (token == 'TRUE') for v in de_la_clase)
                else:
                    peso += not de_la_clase  # ningún valor permitido es de esta clase
        return Escalar(peso, tuple(sorted(pruebas, key=costo)), len(diferidas))

    def _contenedor(self, formas, clase):
        miembros, prohibido, diferidas = _miembros(formas, clase)
        for f in miembros:
            for valores in f.conjuntos:
                if any(v.clase == _CLASE_AST[clase] for v in valores):
                    diferidas.append((f.ruta, 'enum'))  # comparar objetos o arreglos completos queda diferido
                else:
                    prohibido = True
        if not prohibido:
            self.diferidas.update(diferidas)
        return miembros, prohibido, len(diferidas)

    def _objeto(self, formas):
        miembros, prohibido, pendientes = self._contenedor(formas, 'object')
        if prohibido:
            return Objeto(prohibido=True)
        nombres = sorted({n for f in miembros for n in f.propiedades} | {n for f in miembros for n in f.requeridas}
                         | {n for f in miembros for k, lista in f.dependientes for n in (k, *lista)})
        bit = {n: i for i, n in enumerate(nombres)}
        mascara = lambda ns: sum(1 << bit[n] for n in set(ns))
        # a cada propiedad se le aplica, por cada miembro, su esquema en "properties" o si no "additionalProperties"
        hijo = lambda n: self._nodo(s for s in (f.propiedades.get(n, f.adicional) for f in miembros) if s is not None)
        minimos = [f.min_props for f in miembros if f.min_props is not None]
        maximos = [f.max_props for f in miembros if f.max_props is not None]
        return Objeto(
            pendientes=pendientes, nombres=tuple(nombres), hijos=tuple(hijo(n) for n in nombres),
            otra=self._nodo(f.adicional for f in miembros if f.adicional is not None),
            requeridas=mascara(n for f in miembros for n in f.requeridas),
            dependencias=tuple(sorted({(bit[k], mascara(lista)) for f in miembros for k, lista in f.dependientes})),
            min_props=max(minimos, default=None), max_props=min(maximos, default=None))

    def _arreglo(self, formas):
        miembros, prohibido, pendientes = self._contenedor(formas, 'array')
        if prohibido:
            return Arreglo(prohibido=True)
        largo = max((len(f.prefijo) for f in miembros), default=0)
        # al elemento i se le aplica, por cada miembro, prefixItems[i] o si no "items"
        en = lambda i: self._nodo(s for s in ((f.prefijo[i] if i < len(f.prefijo) else f.elementos) for f in miembros)
                                  if s is not None)
        minimos = [f.min_items for f in miembros if f.min_items is not None]
        maximos = [f.max_items for f in miembros if f.max_items is not None]
        return Arreglo(pendientes=pendientes, prefijo=tuple(en(i) for i in range(largo)), resto=en(largo),
                       min_items=max(minimos, default=None), max_items=min(maximos, default=None))
