"""Análisis del grafo de referencias (Fase 3): tipos posibles, ciclos y recursión.

Los subesquemas son los nodos y hay dos clases de arista (ver simbolos.Tabla.aristas):

  en el sitio   $ref, allOf, anyOf, oneOf, not, if/then/else: el destino se aplica a la misma
                instancia, sin leer nada (como una transición ε).
  consume       properties, items, ...: el destino se aplica a un hijo, después de leer «{» o «[».

De ahí salen tres resultados, los mismos que se calculan sobre una gramática:

  ciclo no productivo   Un ciclo hecho solo de aristas en el sitio. Validar el esquema exigiría
                        validarse a sí mismo sin avanzar: es la recursión por la izquierda de una
                        gramática. Es un error.
  recursión             Un ciclo con alguna arista que consume. Es legítima (árboles, listas
                        enlazadas) y es lo que hace necesario el autómata de pila.
  esquema improductivo  Un esquema que no acepta ningún documento finito, como un no terminal que
                        no deriva ninguna cadena de terminales. Si es recursivo, es un error.
"""
from .metaesquema import decimal

TODOS = frozenset({'null', 'boolean', 'number', 'string', 'object', 'array'})
_TIPO_DE_CLASE = {'cadena': 'string', 'numero': 'number', 'booleano': 'boolean', 'nulo': 'null',
                  'arreglo': 'array', 'objeto': 'object'}


def calcular_tipos(tabla):
    """id(esquema) → tipos de JSON que el esquema podría aceptar ("integer" cuenta como "number").

    Es una cota superior: se parte de «todos» y se recorta con cada restricción hasta el punto fijo
    (como PRIMEROS, pero bajando). Sirve para saber si las ramas de un anyOf se distinguen por su
    primer token y para el análisis de productividad.
    """
    tipos = {id(e): TODOS for e in tabla.esquemas}

    def recortar(e):
        if e.booleano is not None:
            return TODOS if e.booleano else frozenset()
        t = TODOS
        for c in e.claves:
            if c.nombre == 'type':
                nombres = [c.valor] if c.valor.clase == 'cadena' else c.valor.dato
                t &= {'number' if n.dato == 'integer' else n.dato for n in nombres}
            elif c.nombre == 'const':
                t &= {_TIPO_DE_CLASE[c.valor.clase]}
            elif c.nombre == 'enum':
                t &= {_TIPO_DE_CLASE[v.clase] for v in c.valor.dato}
            elif c.nombre == 'allOf':
                for rama in c.valor:
                    t &= tipos[id(rama)]
            elif c.nombre in ('anyOf', 'oneOf'):
                t &= frozenset().union(*(tipos[id(rama)] for rama in c.valor))
        destino = tabla.referencias.get(id(e))
        if destino is not None:
            t &= tipos[id(destino)]
        return frozenset(t)

    cambio = True
    while cambio:
        cambio = False
        for e in reversed(tabla.esquemas):  # los hijos antes que los padres
            nuevo = recortar(e)
            if nuevo != tipos[id(e)]:
                tipos[id(e)], cambio = nuevo, True
    return tipos


def componentes(nodos, sucesores):
    """Componentes fuertemente conexas (algoritmo de Tarjan, sin recursión).
    nodos: lista de objetos; sucesores(n): lista de objetos. Devuelve una lista de listas."""
    indice, bajo, en_pila = {}, {}, set()
    pila, resultado = [], []
    for raiz in nodos:
        if id(raiz) in indice:
            continue
        trabajo = [(raiz, iter(sucesores(raiz)))]
        indice[id(raiz)] = bajo[id(raiz)] = len(indice)
        pila.append(raiz)
        en_pila.add(id(raiz))
        while trabajo:
            nodo, pendientes = trabajo[-1]
            for s in pendientes:
                if id(s) not in indice:
                    indice[id(s)] = bajo[id(s)] = len(indice)
                    pila.append(s)
                    en_pila.add(id(s))
                    trabajo.append((s, iter(sucesores(s))))
                    break
                if id(s) in en_pila:
                    bajo[id(nodo)] = min(bajo[id(nodo)], indice[id(s)])
            else:
                trabajo.pop()
                if trabajo:
                    padre = trabajo[-1][0]
                    bajo[id(padre)] = min(bajo[id(padre)], bajo[id(nodo)])
                if bajo[id(nodo)] == indice[id(nodo)]:
                    componente = []
                    while True:
                        n = pila.pop()
                        en_pila.discard(id(n))
                        componente.append(n)
                        if n is nodo:
                            break
                    resultado.append(componente)
    return resultado


def _ciclicas(tabla, solo_en_el_sitio):
    """Componentes con al menos un ciclo, cada una en el orden del texto."""
    def sucesores(e):
        return [d for d, consume in tabla.aristas[id(e)] if not (solo_en_el_sitio and consume)]

    posicion = {id(e): i for i, e in enumerate(tabla.esquemas)}
    ciclicas = []
    for componente in componentes(tabla.esquemas, sucesores):
        if len(componente) > 1 or any(d is componente[0] for d in sucesores(componente[0])):
            ciclicas.append(sorted(componente, key=lambda e: posicion[id(e)]))
    return sorted(ciclicas, key=lambda c: posicion[id(c[0])])


def ciclos_no_productivos(tabla):
    """Grupos de esquemas que se alcanzan entre sí sin consumir entrada."""
    return _ciclicas(tabla, solo_en_el_sitio=True)


def recursiones(tabla):
    """Grupos de esquemas mutuamente recursivos (incluye los ciclos no productivos)."""
    return _ciclicas(tabla, solo_en_el_sitio=False)


def calcular_productivos(tabla, tipos):
    """id(esquema) → True si se puede demostrar que algún documento finito lo cumple.

    Punto fijo creciente, igual que el cálculo de los no terminales que generan alguna cadena: un
    esquema es productivo si lo son los esquemas de los que depende sin alternativa. Lo que no se
    analiza (not, if, límites, patrones) se da por satisfacible, así que un «False» es seguro: ese
    esquema no acepta ningún documento finito.
    """
    productivo = {id(e): False for e in tabla.esquemas}
    ok = lambda e: productivo[id(e)]

    def objeto_posible(e):
        requeridas = e.clave('required')
        if requeridas is None:
            return True
        propiedades = e.clave('properties')
        declaradas = {en.nombre: en.esquema for en in propiedades.valor} if propiedades else {}
        adicionales = e.clave('additionalProperties')
        resto = adicionales.valor if adicionales and e.clave('patternProperties') is None else None
        for nombre in requeridas.valor.dato:
            sub = declaradas.get(nombre.dato, resto)
            if sub is not None and not ok(sub):
                return False
        return True

    def arreglo_posible(e):
        minimo = e.clave('minItems')
        n = decimal(minimo.valor) if minimo else None
        if n is None or n < 1:
            return True
        prefijo = e.clave('prefixItems')
        prefijo = prefijo.valor if prefijo else []
        if not all(ok(s) for s in prefijo[:int(min(n, len(prefijo)))]):
            return False
        elementos = e.clave('items')
        return not (n > len(prefijo) and elementos is not None and not ok(elementos.valor))

    def puede(e):
        if e.booleano is not None:
            return e.booleano
        destino = tabla.referencias.get(id(e))
        if destino is not None and not ok(destino):
            return False
        for c in e.claves:
            if c.nombre == 'allOf' and not all(ok(r) for r in c.valor):
                return False
            if c.nombre in ('anyOf', 'oneOf') and not any(ok(r) for r in c.valor):
                return False
        return any(objeto_posible(e) if t == 'object' else arreglo_posible(e) if t == 'array' else True
                   for t in tipos[id(e)])

    cambio = True
    while cambio:
        cambio = False
        for e in reversed(tabla.esquemas):
            if not productivo[id(e)] and puede(e):
                productivo[id(e)], cambio = True, True
    return productivo
