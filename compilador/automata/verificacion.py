"""Verificación y minimización del autómata (Fase 4).

verificar() comprueba, sin ejecutar ningún documento, que el autómata es un DPDA bien formado:

  determinismo   Para cada estado y cada token hay como mucho una transición. En los estados que leen
                 una clave, la CADENA se reparte entre las propiedades declaradas y «cualquier otra»,
                 que son disjuntas; por eso ahí no puede haber además una transición con CADENA.
                 No hay transiciones ε: toda transición consume un token.
  pila           Con un punto fijo se calcula qué símbolos pueden estar en la cima en cada estado y
                 cuáles pueden quedar debajo de cada uno. Con eso se comprueba que nunca se desapila
                 Z0, que cada acción actúa sobre un marco de su clase y que solo se acepta con la pila
                 en Z0.

Las sub-ejecuciones empiezan en el estado de cada rama con la pila en Zh: el punto fijo las recorre
igual que la ejecución principal, y Zh tampoco se puede desapilar.

De paso se obtiene el tamaño de δ en su forma expandida δ(q, a, Z).

minimizar() fusiona los estados (y los símbolos de pila) que se comportan igual, con el mismo
refinamiento de particiones de Moore que se usó para el AFD del lexer.
"""
from collections import deque
from dataclasses import dataclass, replace

from .dpda import RETORNO, Automata

TOKENS = {'LLAVE_ABRE', 'LLAVE_CIERRA', 'CORCHETE_ABRE', 'CORCHETE_CIERRA', 'DOS_PUNTOS', 'COMA',
          'CADENA', 'NUMERO', 'TRUE', 'FALSE', 'NULL', 'FIN'}
TOKENS_XML = {'DECL_XML', 'ETIQ_ABRE', 'ETIQ_CIERRE_ABRE', 'IGUAL', 'VALOR_ATRIB', 'ETIQ_FIN', 'ETIQ_AUTOCIERRE',
              'TEXTO', 'ESPACIOS', 'ATRIB_NS', 'FIN'}
# símbolos refinados: el token que se refina y si hace falta la transición de «cualquier otro»
REFINADOS = {'CLAVE': ('CADENA', True), 'ELEM': ('NOMBRE', True), 'ATRIB': ('NOMBRE', True), 'CIERRE': ('NOMBRE', False)}
CLASE_DE_ACCION = {'clave': 'O', 'cerrar_objeto': 'O', 'elemento': 'A', 'cerrar_arreglo': 'A',
                   'atributo': 'E', 'cerrar_atributos': 'E', 'hijo': 'E', 'cierre_libre': 'E'}


@dataclass
class Verificacion:
    problemas: list          # vacía si el autómata es correcto
    cimas: list              # por estado: símbolos de pila que pueden estar en la cima
    debajo: list             # por símbolo de pila: símbolos que pueden estar justo debajo
    transiciones_expandidas: int   # |δ| contando cada (q, a, Z) alcanzable

    @property
    def ok(self):
        return not self.problemas

    @property
    def alcanzables(self):
        return [q for q, c in enumerate(self.cimas) if c]


def verificar(a: Automata) -> Verificacion:
    problemas = []
    nombre = lambda q: a.estados[q]

    # ---- determinismo ----
    for q, transiciones in enumerate(a.delta):
        claves = [s for s in transiciones if isinstance(s, tuple)]
        for s in transiciones:
            if not (s in TOKENS or s in TOKENS_XML or (isinstance(s, tuple) and len(s) == 2 and s[0] in REFINADOS)):
                problemas.append(f'{nombre(q)}: símbolo de entrada desconocido {s!r}')
        clases = {s[0] for s in claves if s[0] in REFINADOS}
        if len(clases) > 1:
            problemas.append(f'{nombre(q)}: el mismo token se refina de dos maneras')
        for clase in clases:
            token, exige_otro = REFINADOS[clase]
            if token in transiciones:
                problemas.append(f'{nombre(q)}: un {token} tendría dos transiciones (refinado y sin refinar)')
            if exige_otro and (clase, None) not in transiciones:
                problemas.append(f'{nombre(q)}: falta la transición para las entradas no declaradas')

    # ---- pila: punto fijo sobre (estado, cima) ----
    cimas = [set() for _ in a.estados]
    debajo = [set() for _ in a.simbolos_pila]
    desapilan = [set() for _ in a.simbolos_pila]  # estados que desapilan con ese símbolo en la cima
    reescrito = [set() for _ in a.simbolos_pila]  # símbolos por los que se reescribe ese símbolo
    cola = deque()
    vistos = set()

    def llegar(q, z):
        if z not in cimas[q]:
            cimas[q].add(z)
            cola.append((q, z))

    def poner_debajo(z, y):
        trabajo = [z]
        while trabajo:
            z = trabajo.pop()
            if y not in debajo[z]:
                debajo[z].add(y)
                trabajo.extend(reescrito[z])  # al reescribir la cima se conserva lo que había debajo
                for q in desapilan[z]:        # quienes desapilan z pueden descubrir ahora a y
                    cola.append((q, z))

    llegar(a.inicial, a.pila_inicial)
    fondos = {a.pila_inicial, a.pila_hilo} - {None}
    while cola:
        q, z = cola.popleft()
        primera_vez = (q, z) not in vistos
        vistos.add((q, z))
        for s, t in a.delta[q].items():
            if primera_vez:
                clase = CLASE_DE_ACCION.get(t.accion[0]) if t.accion else None
                if clase and a.clase_pila[z] != clase:
                    problemas.append(f'{nombre(q)} con {s}: la acción «{t.accion[0]}» actúa sobre {a.simbolos_pila[z]}')
                if (t.desapila or t.cima is not None) and z in fondos:
                    problemas.append(f'{nombre(q)} con {s}: modifica el fondo de la pila')
                if t.hilos and a.pila_hilo is None:
                    problemas.append(f'{nombre(q)} con {s}: crea sub-ejecuciones pero no hay fondo para ellas')
                if t.desapila and (t.cima is not None or t.apila is not None):
                    problemas.append(f'{nombre(q)} con {s}: desapila y apila a la vez')
            if t.desapila:
                desapilan[z].add(q)
                nuevas = list(debajo[z])
            else:
                cima = z
                if t.cima is not None:
                    reescrito[z].add(t.cima)
                    for y in list(debajo[z]):
                        poner_debajo(t.cima, y)
                    cima = t.cima
                if t.apila is not None:
                    poner_debajo(t.apila, cima)
                    cima = t.apila
                nuevas = [cima]
            for cima in nuevas:
                llegar(a.retorno[cima] if t.destino is RETORNO else t.destino, cima)
            if a.pila_hilo is not None:
                for _, inicios in t.hilos:
                    for inicio in inicios:
                        if inicio is not None:
                            llegar(inicio, a.pila_hilo)

    for q in a.finales:
        if cimas[q] - {a.pila_inicial}:
            problemas.append(f'{nombre(q)}: se puede aceptar con la pila sin vaciar')
    if not any(cimas[q] for q in a.finales):
        problemas.append('ningún estado de aceptación es alcanzable')

    expandidas = sum(len(a.delta[q]) * len(cimas[q]) for q in range(len(a.estados)))
    return Verificacion(sorted(set(problemas)), cimas, debajo, expandidas)


def minimizar(a: Automata) -> Automata:
    """Autómata equivalente sin estados inalcanzables y con los estados y símbolos de pila indistinguibles fusionados."""
    v = verificar(a)
    vivos = v.alcanzables
    simbolos = sorted(set().union(*v.cimas))

    def numerar(firmas):
        numeros = {}
        return {x: numeros.setdefault(f, len(numeros)) for x, f in firmas.items()}, len(numeros)

    bq, nq = numerar({q: (q in a.finales, q == a.fin_hilo) for q in vivos})
    bz, nz = numerar({z: a.clase_pila[z] for z in simbolos})
    while True:
        # un símbolo de pila se distingue por su clase y por el estado al que hace volver
        fz = {z: (bz[z], bq.get(a.retorno[z])) for z in simbolos}
        fq = {q: (bq[q], frozenset(
            (s, None if t.destino is RETORNO else bq[t.destino], t.desapila,
             None if t.cima is None else bz[t.cima], None if t.apila is None else bz[t.apila],
             t.accion, t.peso, t.pruebas, t.pendientes, hilos(t.hilos, bq), t.recoger)
            for s, t in a.delta[q].items())) for q in vivos}
        (nbq, nnq), (nbz, nnz) = numerar(fq), numerar(fz)
        estable = (nnq, nnz) == (nq, nz)
        bq, nq, bz, nz = nbq, nnq, nbz, nnz
        if estable:
            break

    m = Automata(diferidas=list(a.diferidas))
    representante_z = {}
    for z in simbolos:
        if bz[z] not in representante_z:
            representante_z[bz[z]] = z
            m.simbolo(a.simbolos_pila[z], a.clase_pila[z])
    representante_q = {}
    for q in vivos:
        if bq[q] not in representante_q:
            representante_q[bq[q]] = q
            m.estado(a.estados[q])
    mapa_z = lambda z: None if z is None else bz[z]
    for bloque, q in representante_q.items():
        m.delta[bloque] = {s: replace(t, destino=None if t.destino is RETORNO else bq[t.destino],
                                      cima=mapa_z(t.cima), apila=mapa_z(t.apila), hilos=hilos(t.hilos, bq))
                           for s, t in a.delta[q].items()}
    for bloque, z in representante_z.items():
        m.retorno[bloque] = bq.get(a.retorno[z])
    m.inicial, m.pila_inicial = bq[a.inicial], bz[a.pila_inicial]
    m.finales = frozenset(bq[q] for q in a.finales if q in bq)
    m.fin_hilo = bq.get(a.fin_hilo)
    m.pila_hilo = bz.get(a.pila_hilo)
    return m


def hilos(grupos, bloque):
    """Los grupos de sub-ejecuciones con cada estado inicial cambiado por su bloque."""
    return tuple((clase, tuple(None if q is None else bloque[q] for q in inicios)) for clase, inicios in grupos)
