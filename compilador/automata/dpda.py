"""Autómata de pila determinista ponderado (Fase 4).

    M = (Q, Σ, Γ, δ, q0, Z0, F, w)

  Q   estados. Por cada nodo de validación n:  V(n) espera un valor;  OI, OC, OS recorren un objeto
      (inicio, clave, siguiente);  DP espera «:»;  AI, AE, AS recorren un arreglo (inicio, elemento,
      siguiente). Además FIN (espera el fin de entrada) y ACEPTA.
  Σ   los tokens del lexer. En la posición de una clave, la CADENA se refina con el nombre de la
      propiedad: ('CLAVE', nombre) para las declaradas y ('CLAVE', None) para cualquier otra.
  Γ   Z0, un símbolo O por cada objeto abierto y un símbolo A por cada arreglo abierto (que además
      recuerda cuántas posiciones de prefixItems van leídas). El símbolo dice a qué estado se vuelve
      cuando termina el valor que está encima.
  δ   determinista y sin transiciones ε: cada paso consume exactamente un token.
  F   {ACEPTA}; se acepta con la pila en Z0.
  w   peso de cada transición en el semianillo tropical (min, +): las violaciones que aporta. El
      peso de una ejecución es la suma, es decir, el número de violaciones del documento; 0 significa
      que el documento cumple el esquema.

Cada marco de pila lleva dos registros acotados: una máscara de bits (propiedades vistas) y un contador
(propiedades o elementos). Las acciones de las transiciones los actualizan y los consultan.

Si un token no tiene transición, el documento está mal formado: eso no es una violación, es un rechazo.
"""
from dataclasses import dataclass, field

from ..semantico.analisis import Analisis
from .formas import construir_formas
from .nodos import CLASE_DE_TOKEN, ESCALARES, Nodos

RETORNO = None  # destino «el estado que indique la cima de la pila»


@dataclass(frozen=True)
class Transicion:
    """δ(q, a, Z) para cualquier Z: lo que cambia con Z es solo el destino cuando es RETORNO.

    Orden de ejecución: acción sobre el marco de la cima, operación de pila (desapilar, o reescribir el
    símbolo de la cima y/o apilar uno nuevo), y destino.
    """
    destino: int | None = RETORNO
    desapila: bool = False
    cima: int | None = None       # nuevo símbolo para el marco de la cima (conserva sus registros)
    apila: int | None = None      # símbolo del marco nuevo
    accion: tuple = ()            # ver ACCIONES
    peso: int = 0                 # violaciones fijas de la transición
    pruebas: tuple = ()           # pruebas sobre el valor del token; cada una que falla suma 1 al peso
    pendientes: int = 0           # comprobaciones en verificación diferida que se cruzan

ACCIONES = {
    'clave': '(bit o None, máximo de propiedades): cuenta la propiedad, marca su bit; repetida = mal formado',
    'cerrar_objeto': '(máscara requerida, dependencias, mínimo): suma las propiedades que faltan',
    'elemento': '(máximo de elementos): cuenta el elemento',
    'cerrar_arreglo': '(mínimo de elementos)',
}


@dataclass
class Automata:
    estados: list = field(default_factory=list)          # nombre de cada estado; el índice es el estado
    delta: list = field(default_factory=list)            # por estado: {símbolo de entrada: Transicion}
    simbolos_pila: list = field(default_factory=list)    # nombre de cada símbolo de Γ
    clase_pila: list = field(default_factory=list)       # 'Z', 'O' o 'A'
    retorno: list = field(default_factory=list)          # por símbolo de pila: estado al que se vuelve
    inicial: int = 0
    pila_inicial: int = 0
    finales: frozenset = frozenset()
    diferidas: list = field(default_factory=list)        # (ruta, palabra clave) que el autómata no verifica

    def estado(self, nombre):
        self.estados.append(nombre)
        self.delta.append({})
        return len(self.estados) - 1

    def simbolo(self, nombre, clase, retorno=None):
        self.simbolos_pila.append(nombre)
        self.clase_pila.append(clase)
        self.retorno.append(retorno)
        return len(self.simbolos_pila) - 1

    def alfabeto(self):
        """Σ: los símbolos de entrada que aparecen en δ."""
        return sorted({s for d in self.delta for s in d}, key=lambda s: (isinstance(s, tuple), str(s)))

    def tamano(self):
        return {'estados': len(self.estados), 'simbolos_de_pila': len(self.simbolos_pila),
                'simbolos_de_entrada': len(self.alfabeto()), 'transiciones': sum(map(len, self.delta))}

    def a_dict(self):
        """Forma serializable a JSON (para la visualización y la generación de código)."""
        def simbolo(s):
            if isinstance(s, str):
                return s
            nombre = '*' if s[1] is None else s[1]
            return ('*' if s[1] is None else 'k:' + s[1]) if s[0] == 'CLAVE' else f'{s[0].lower()}:{nombre}'
        prueba = lambda p: [str(x) if not isinstance(x, (int, str, bool, tuple)) else
                            ([str(y) for y in x] if isinstance(x, tuple) else x) for x in p]

        def transicion(t):
            d = {'destino': t.destino}
            d.update({k: v for k, v in (('desapila', t.desapila), ('cima', t.cima), ('apila', t.apila),
                                        ('accion', list(t.accion)), ('peso', t.peso),
                                        ('pruebas', [prueba(p) for p in t.pruebas]), ('pendientes', t.pendientes))
                      if v not in (False, None, 0, [], ())})
            return d
        return {'estados': self.estados, 'simbolos_pila': self.simbolos_pila, 'clase_pila': self.clase_pila,
                'retorno': self.retorno, 'inicial': self.inicial, 'pila_inicial': self.pila_inicial,
                'finales': sorted(self.finales), 'diferidas': [list(d) for d in self.diferidas],
                'delta': [{simbolo(s): transicion(t) for s, t in d.items()} for d in self.delta]}


def construir(analisis: Analisis) -> Automata:
    """Construcción composicional: cada nodo de validación aporta su fragmento de estados y transiciones."""
    nodos = Nodos(construir_formas(analisis))
    a = Automata(diferidas=sorted(nodos.diferidas))
    a.pila_inicial = a.simbolo('Z0', 'Z')
    fin, acepta = a.estado('FIN'), a.estado('ACEPTA')
    a.delta[fin]['FIN'] = Transicion(destino=acepta)
    a.retorno[a.pila_inicial] = fin
    a.finales = frozenset([acepta])

    # estados y símbolos de pila de cada nodo
    V, DP, OI, OC, OS, AI, AE, AS, SO, SA = ({} for _ in range(10))
    for n in nodos.lista:
        i = n.id
        V[i], DP[i] = a.estado(f'V{i}'), a.estado(f'DP{i}')
        if not n.objeto.prohibido:
            OI[i], OC[i], OS[i] = a.estado(f'OI{i}'), a.estado(f'OC{i}'), a.estado(f'OS{i}')
            SO[i] = a.simbolo(f'O{i}', 'O', OS[i])
        if not n.arreglo.prohibido:
            largo = len(n.arreglo.prefijo)
            AI[i] = a.estado(f'AI{i}')
            SA[i, 0] = a.simbolo(f'A{i}.0', 'A')
            # c = posiciones de prefixItems ya leídas, saturado en «largo»
            for c in range(min(1, largo), largo + 1):
                AE[i, c], AS[i, c] = a.estado(f'AE{i}.{c}'), a.estado(f'AS{i}.{c}')
                if c > 0:
                    SA[i, c] = a.simbolo(f'A{i}.{c}', 'A')
                a.retorno[SA[i, c]] = AS[i, c]
            if largo > 0:
                a.retorno[SA[i, 0]] = AS[i, 1]  # no se usa: antes de volver, el símbolo ya es A.1
    a.inicial = V[nodos.raiz]

    def al_valor(n):
        """Transiciones de «leer un valor del nodo n»: token → Transicion."""
        t = {tok: Transicion(peso=e.peso, pruebas=e.pruebas, pendientes=e.pendientes) for tok, e in n.escalar.items()}
        for tok, info, inicio, simbolo in (('LLAVE_ABRE', n.objeto, OI, SO), ('CORCHETE_ABRE', n.arreglo, AI, SA)):
            clave = (lambda i: i) if tok == 'LLAVE_ABRE' else (lambda i: (i, 0))
            if info.prohibido:  # una violación, y el contenedor se lee con el nodo libre
                t[tok] = Transicion(destino=inicio[nodos.libre], apila=simbolo[clave(nodos.libre)], peso=1)
            else:
                t[tok] = Transicion(destino=inicio[n.id], apila=simbolo[clave(n.id)], pendientes=info.pendientes)
        return t

    for n in nodos.lista:
        i = n.id
        a.delta[V[i]].update(al_valor(n))
        a.delta[DP[i]]['DOS_PUNTOS'] = Transicion(destino=V[i])

        if not n.objeto.prohibido:
            o = n.objeto
            claves = {('CLAVE', nombre): Transicion(destino=DP[hijo], accion=('clave', bit, o.max_props))
                      for bit, (nombre, hijo) in enumerate(zip(o.nombres, o.hijos))}
            claves['CLAVE', None] = Transicion(destino=DP[o.otra], accion=('clave', None, o.max_props))
            cerrar = Transicion(desapila=True, accion=('cerrar_objeto', o.requeridas, o.dependencias, o.min_props))
            a.delta[OI[i]].update(claves, LLAVE_CIERRA=cerrar)
            a.delta[OC[i]].update(claves)
            a.delta[OS[i]].update(COMA=Transicion(destino=OC[i]), LLAVE_CIERRA=cerrar)

        if not n.arreglo.prohibido:
            r = n.arreglo
            largo = len(r.prefijo)
            cerrar = Transicion(desapila=True, accion=('cerrar_arreglo', r.min_items))

            def al_elemento(c):
                """Leer el elemento que va en la posición c: como al_valor, pero además cuenta el
                elemento y deja en la cima el símbolo de la posición siguiente."""
                hijo = nodos.lista[r.prefijo[c] if c < largo else r.resto]
                sig = min(c + 1, largo)
                nueva_cima = SA[i, sig] if sig != c else None
                salida = {}
                for tok, t in al_valor(hijo).items():
                    destino = AS[i, sig] if tok in ESCALARES else t.destino
                    salida[tok] = Transicion(destino, False, nueva_cima, t.apila, ('elemento', r.max_items),
                                             t.peso, t.pruebas, t.pendientes)
                return salida

            a.delta[AI[i]].update(al_elemento(0), CORCHETE_CIERRA=cerrar)
            for c in range(min(1, largo), largo + 1):
                a.delta[AE[i, c]].update(al_elemento(c))
                a.delta[AS[i, c]].update(COMA=Transicion(destino=AE[i, c]), CORCHETE_CIERRA=cerrar)
    return a


assert set(CLASE_DE_TOKEN) == set(ESCALARES) | {'LLAVE_ABRE', 'CORCHETE_ABRE'}
