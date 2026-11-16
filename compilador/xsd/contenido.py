"""Modelo de contenido de un tipo complejo como autómata finito (Fase 4, XML).

El contenido de un elemento es una expresión regular sobre los nombres de sus hijos: `sequence` es la
concatenación, `choice` la unión y minOccurs/maxOccurs la repetición. Se convierte en un AFD en tres pasos:

  1. Glushkov: un estado por cada aparición de un nombre en la expresión (su «posición»), sin
     transiciones ε. Se calculan anulable, primeros, últimos y siguientes de cada subexpresión.
  2. Subconjuntos: se determiniza, porque el mismo nombre puede aparecer en varias posiciones.
  3. Moore: se fusionan los estados indistinguibles.

Las repeticiones acotadas se expanden en copias, así que el tamaño está acotado: una expresión con más
de MAX_POSICIONES posiciones, o cuyo AFD pase de MAX_ESTADOS, se rechaza.
"""
from dataclasses import dataclass

MAX_POSICIONES = 1000
MAX_ESTADOS = 2000


class ModeloDemasiadoGrande(Exception):
    pass


class TiposInconsistentes(Exception):
    """El mismo nombre de elemento aparece con dos tipos distintos en un modelo de contenido."""

    def __init__(self, nombre):
        super().__init__(nombre)
        self.nombre = nombre


@dataclass
class AFD:
    """El estado inicial es el 0."""
    acepta: list      # por estado: si el contenido puede terminar ahí
    delta: list       # por estado: {nombre del hijo: estado siguiente}
    tipo_de: dict     # nombre del hijo → su tipo

    def vacio(self):
        """True si el único contenido posible es no tener hijos."""
        return not any(self.delta)


def _tamano(p):
    """Posiciones que tendrá la partícula una vez expandida."""
    propio = 1 if p.clase == 'elemento' else sum(_tamano(h) for h in p.hijos)
    copias = p.minimo + 1 if p.maximo is None else p.maximo
    return propio * copias


def _expresion(p):
    """Partícula → expresión regular: ('sim', nombre, tipo), ('cat', [..]), ('alt', [..]), ('rep', x), ('opc', x)."""
    if p.clase == 'elemento':
        base = ('sim', p.nombre, p.tipo)
    else:
        base = ('cat' if p.clase == 'secuencia' else 'alt', [_expresion(h) for h in p.hijos])
    partes = [base] * p.minimo
    if p.maximo is None:
        partes.append(('rep', base))
    else:
        partes += [('opc', base)] * (p.maximo - p.minimo)   # x{0,k} = x? x? ... x?
    return partes[0] if len(partes) == 1 else ('cat', partes)


def construir_afd(particula) -> AFD:
    """AFD mínimo del modelo de contenido. particula puede ser None (sin hijos)."""
    if particula is None:
        return AFD([True], [{}], {})
    if _tamano(particula) > MAX_POSICIONES:
        raise ModeloDemasiadoGrande(f'el modelo de contenido tiene más de {MAX_POSICIONES} posiciones al expandir '
                                    'minOccurs y maxOccurs')

    # ---- 1. Glushkov ----
    nombres, siguientes, tipo_de = [], [], {}

    def glushkov(e):
        """Devuelve (anulable, primeros, últimos) y va llenando «siguientes»."""
        clase = e[0]
        if clase == 'sim':
            if tipo_de.setdefault(e[1], e[2]) is not e[2]:
                raise TiposInconsistentes(e[1])
            nombres.append(e[1])
            siguientes.append(set())
            return False, {len(nombres) - 1}, {len(nombres) - 1}
        if clase == 'cat':
            anulable, primeros, ultimos = True, set(), set()
            for h in e[1]:
                ha, hp, hu = glushkov(h)
                for i in ultimos:
                    siguientes[i] |= hp
                if anulable:
                    primeros |= hp
                ultimos = ultimos | hu if ha else hu
                anulable = anulable and ha
            return anulable, primeros, ultimos
        if clase == 'alt':
            anulable, primeros, ultimos = not e[1], set(), set()   # una unión sin ramas es la cadena vacía
            for h in e[1]:
                ha, hp, hu = glushkov(h)
                anulable, primeros, ultimos = anulable or ha, primeros | hp, ultimos | hu
            return anulable, primeros, ultimos
        ha, hp, hu = glushkov(e[1])
        if clase == 'rep':
            for i in hu:
                siguientes[i] |= hp
        return True, hp, hu

    anulable, primeros, ultimos = glushkov(_expresion(particula))

    # ---- 2. Subconjuntos: un estado es (posiciones que pueden venir ahora, se puede terminar) ----
    inicial = (frozenset(primeros), anulable)
    numero, estados, delta = {inicial: 0}, [inicial], []
    for candidatas, _ in estados:   # la lista crece mientras se recorre
        por_nombre = {}
        for i in candidatas:
            por_nombre.setdefault(nombres[i], []).append(i)
        fila = {}
        for nombre, leidas in sorted(por_nombre.items()):
            destino = (frozenset().union(*(siguientes[i] for i in leidas)), any(i in ultimos for i in leidas))
            if destino not in numero:
                if len(estados) >= MAX_ESTADOS:
                    raise ModeloDemasiadoGrande(f'el autómata del modelo de contenido supera los {MAX_ESTADOS} estados')
                numero[destino] = len(estados)
                estados.append(destino)
            fila[nombre] = numero[destino]
        delta.append(fila)
    acepta = [final for _, final in estados]

    # ---- 3. Moore ----
    bloque = [int(f) for f in acepta]
    while True:
        firmas = {}
        nuevo = [firmas.setdefault((bloque[q], tuple(sorted((n, bloque[d]) for n, d in delta[q].items()))), len(firmas))
                 for q in range(len(estados))]
        if len(firmas) == len(set(bloque)):
            break
        bloque = nuevo
    orden = {}  # bloque → número nuevo, con el inicial en 0
    for q in range(len(estados)):
        orden.setdefault(bloque[q], len(orden))
    m_acepta, m_delta = [False] * len(orden), [None] * len(orden)
    for q in range(len(estados)):
        b = orden[bloque[q]]
        m_acepta[b], m_delta[b] = acepta[q], {n: orden[bloque[d]] for n, d in delta[q].items()}
    return AFD(m_acepta, m_delta, tipo_de)
