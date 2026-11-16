"""Parser descendente LL(1) dirigido por tabla (Fase 2).

Es un autómata de pila: la pila guarda los símbolos que faltan por derivar y cada paso consulta
M[cima, token]. No hay retroceso, así que el costo es lineal en el número de tokens, y la pila crece
con la profundidad de anidamiento, no con la longitud de la entrada.

El resultado es el árbol de derivación. El validador generado nunca lo construye: solo se usa en
tiempo de compilación (para el esquema) y para documentar derivaciones de payloads de ejemplo.
"""
from .gramatica import Gramatica

ABRE = {'LLAVE_ABRE', 'CORCHETE_ABRE'}
CIERRA = {'LLAVE_CIERRA', 'CORCHETE_CIERRA'}


def terminal(token):
    """Terminal de la gramática que corresponde a un token: su tipo, o la palabra clave entre comillas."""
    return f'"{token.valor}"' if token.tipo == 'PALABRA_CLAVE' else token.tipo


class ErrorSintactico(Exception):
    """Error en la posición pos (bytes). motivo: 'token' (no se esperaba ese token) o 'anidamiento'."""

    def __init__(self, pos, encontrado, esperados=(), motivo='token'):
        self.pos, self.encontrado, self.esperados, self.motivo = pos, encontrado, sorted(esperados), motivo
        if motivo == 'anidamiento':
            mensaje = f'anidamiento demasiado profundo en el byte {pos}'
        else:
            mensaje = f'se encontró {encontrado} en el byte {pos}; se esperaba: {", ".join(self.esperados)}'
        super().__init__(mensaje)


class Nodo:
    """Nodo del árbol de derivación.

    No terminal: produccion es el índice de la producción aplicada e hijos sus símbolos (vacío si es ε).
    Terminal: token es el token que lo cubre.
    """
    __slots__ = ('simbolo', 'hijos', 'token', 'produccion')

    def __init__(self, simbolo):
        self.simbolo, self.hijos, self.token, self.produccion = simbolo, [], None, None

    def __repr__(self):
        return f'Nodo({self.simbolo})'


class AnalizadorLL1:
    def __init__(self, gramatica: Gramatica):
        self.gramatica = gramatica
        self.tabla = gramatica.tabla_ll1()  # lanza ConflictoLL1 si la gramática no es LL(1)
        self._terminales = set(gramatica.terminales)
        self._esperados = {}
        for A, a in self.tabla:
            self._esperados.setdefault(A, []).append(a)

    def analizar(self, tokens, max_anidamiento=None) -> Nodo:
        """Devuelve la raíz del árbol de derivación de la secuencia de tokens (que debe acabar en FIN).
        Lanza ErrorSintactico en el primer token que no puede continuar ninguna derivación."""
        producciones = self.gramatica.producciones
        raiz = Nodo(self.gramatica.inicial)
        pila, profundidad = [raiz], 0
        flujo = iter(tokens)
        token = next(flujo)
        while pila:
            if token is None:
                raise ValueError('la secuencia de tokens no termina en FIN')
            nodo, a = pila.pop(), terminal(token)
            if nodo.simbolo in self._terminales:
                if nodo.simbolo != a:
                    raise ErrorSintactico(token.inicio, a, [nodo.simbolo])
                nodo.token = token
                if a in ABRE:
                    profundidad += 1
                    if max_anidamiento is not None and profundidad > max_anidamiento:
                        raise ErrorSintactico(token.inicio, a, motivo='anidamiento')
                elif a in CIERRA:
                    profundidad -= 1
                token = next(flujo, None)
            else:
                i = self.tabla.get((nodo.simbolo, a))
                if i is None:
                    raise ErrorSintactico(token.inicio, a, self._esperados[nodo.simbolo])
                nodo.produccion = i
                nodo.hijos = [Nodo(s) for s in producciones[i][1]]
                pila.extend(reversed(nodo.hijos))  # el primer símbolo del cuerpo queda en la cima
        if token is not None:
            raise ErrorSintactico(token.inicio, terminal(token), ['fin de la entrada'])
        return raiz
