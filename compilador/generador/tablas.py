"""Tablas planas del autómata (Fase 5): el DPDA convertido en arreglos de enteros.

El motor de Rust (validador/src/validador.rs) no conoce esquemas: solo recorre estas tablas. Todo lo que
en Python es un objeto aquí es un índice, y lo que tiene tamaño variable vive en un fondo común:

  delta          estados × 12 tokens → transición (o NINGUNO). En un autómata de XML las columnas son las
                 de TOKENS_XML, y la de NOMBRE vale, en los estados que leen un nombre, la transición de
                 «cualquier otro nombre» (o SIN_OTRA si no la hay)
  trie, otra     por estado: el trie con el que se lee una cadena y la transición de «otra clave»
  transiciones   destino, operación de pila, acción con sus parámetros, peso y rango de pruebas
  pruebas        comprobaciones sobre el valor de un token
  decimales      constantes numéricas normalizadas: signo, exponente y dígitos significativos
  máscaras       conjuntos de bits de propiedades, en palabras de 64 bits
  tries          AFD con forma de árbol sobre bytes: nombres de propiedad y valores de "enum"

Las mismas tablas salen de dos formas: como código Rust (emitir_rust) para el validador generado, y como
texto (serializar) para los programas de pruebas y de medición, que las cargan sin recompilar.
"""
from dataclasses import dataclass, field

from ..automata.dpda import RETORNO, Automata

NINGUNO = 0xFFFFFFFF
# el orden es el del enum TipoToken de Rust
TOKENS = ('LLAVE_ABRE', 'LLAVE_CIERRA', 'CORCHETE_ABRE', 'CORCHETE_CIERRA', 'DOS_PUNTOS', 'COMA',
          'CADENA', 'NUMERO', 'TRUE', 'FALSE', 'NULL', 'FIN')
SIN_OTRA = 0xFFFFFFFE
# el orden de los nueve primeros es el del enum TipoXml de Rust
TOKENS_XML = ('DECL_XML', 'ETIQ_ABRE', 'ETIQ_CIERRE_ABRE', 'NOMBRE', 'IGUAL', 'VALOR_ATRIB', 'ETIQ_FIN',
              'ETIQ_AUTOCIERRE', 'TEXTO', 'ESPACIOS', 'ATRIB_NS', 'FIN')
VALORES = ('CADENA', 'TEXTO', 'ESPACIOS', 'VALOR_ATRIB')   # tokens cuyo valor se busca en el trie de enumeraciones
ACCION = {'clave': 1, 'cerrar_objeto': 2, 'elemento': 3, 'cerrar_arreglo': 4,
          'atributo': 5, 'cerrar_atributos': 6, 'hijo': 7, 'cierre_libre': 8}
PRUEBA = {'entero': 1, 'min': 2, 'max': 3, 'long_min': 4, 'long_max': 5, 'enum_num': 6, 'enum_cad': 7,
          'xs_entero': 8, 'xs_decimal': 9, 'xs_booleano': 10}
CAMPOS_TRANSICION = ('destino', 'desapila', 'cima', 'apila', 'accion', 'a', 'b', 'c', 'd', 'peso',
                     'pruebas', 'n_pruebas', 'pendientes')


@dataclass
class Tablas:
    n_estados: int = 0
    inicial: int = 0
    acepta: int = 0
    pila_inicial: int = 0
    palabras: int = 1                                   # palabras de 64 bits de cada máscara
    xml: bool = False                                   # el autómata es de XML (columnas TOKENS_XML)
    delta: list = field(default_factory=list)
    trie: list = field(default_factory=list)
    otra: list = field(default_factory=list)
    retorno: list = field(default_factory=list)
    transiciones: list = field(default_factory=list)    # tuplas en el orden de CAMPOS_TRANSICION
    pruebas: list = field(default_factory=list)         # (clase, a, b)
    decimales: list = field(default_factory=list)       # (negativo, exponente, primer dígito, n dígitos)
    digitos: list = field(default_factory=list)
    mascaras: list = field(default_factory=list)        # palabras de 64 bits
    dependencias: list = field(default_factory=list)    # (bit, máscara)
    trie_nodos: list = field(default_factory=list)      # (primera arista, n aristas, valor)
    trie_aristas: list = field(default_factory=list)    # (byte, nodo destino), ordenadas por byte

    def bytes_de_tabla(self):
        """Tamaño aproximado de las tablas en el binario."""
        return (4 * (len(self.delta) + len(self.trie) + len(self.otra) + len(self.retorno))
                + 48 * len(self.transiciones) + 12 * len(self.pruebas) + 24 * len(self.decimales)
                + len(self.digitos) + 8 * len(self.mascaras) + 8 * len(self.dependencias)
                + 12 * len(self.trie_nodos) + 8 * len(self.trie_aristas))


class _Aplanador:
    def __init__(self, a: Automata):
        self.a, self.t = a, Tablas()
        self._transiciones, self._tries, self._mascaras, self._trie_de = {}, {}, {}, {}
        bits = [0]
        for transiciones in a.delta:
            for tr in transiciones.values():
                if tr.accion and tr.accion[0] == 'clave' and tr.accion[1] is not None:
                    bits.append(tr.accion[1] + 1)
                elif tr.accion and tr.accion[0] == 'cerrar_objeto':
                    bits += [tr.accion[1].bit_length()] + [m.bit_length() for _, m in tr.accion[2]]
                elif tr.accion and tr.accion[0] == 'atributo':
                    bits.append(tr.accion[1] + 1)
                elif tr.accion and tr.accion[0] == 'cerrar_atributos':
                    bits.append(tr.accion[1].bit_length())
        self.t.palabras = max(1, -(-max(bits) // 64))

    def mascara(self, m):
        if m not in self._mascaras:
            self._mascaras[m] = len(self._mascaras)
            self.t.mascaras += [(m >> (64 * i)) & 0xFFFFFFFFFFFFFFFF for i in range(self.t.palabras)]
        return self._mascaras[m]

    def decimal(self, d):
        """0.DÍGITOS × 10^exponente, sin ceros a los lados; el cero no tiene dígitos."""
        signo, digitos, exp = d.as_tuple()
        digitos = list(digitos)
        while digitos and digitos[0] == 0:
            digitos.pop(0)
        exponente = len(digitos) + exp if digitos else 0
        while digitos and digitos[-1] == 0:
            digitos.pop()
        self.t.decimales.append((int(bool(signo) and bool(digitos)), exponente, len(self.t.digitos), len(digitos)))
        self.t.digitos += digitos
        return len(self.t.decimales) - 1

    def trie_de(self, mapa):
        """Raíz del trie de un diccionario bytes → valor (> 0). Los tries iguales se comparten."""
        clave = frozenset(mapa.items())
        if clave in self._tries:
            return self._tries[clave]
        raiz = [0, {}]  # [valor, hijos por byte]
        for palabra, valor in mapa.items():
            nodo = raiz
            for b in palabra:
                nodo = nodo[1].setdefault(b, [0, {}])
            nodo[0] = valor
        base = len(self.t.trie_nodos)
        orden, indice = [raiz], {id(raiz): base}
        for nodo in orden:  # en anchura: cada nodo recibe su índice al encolarse
            for b in sorted(nodo[1]):
                indice[id(nodo[1][b])] = base + len(orden)
                orden.append(nodo[1][b])
        for nodo in orden:
            self.t.trie_nodos.append((len(self.t.trie_aristas), len(nodo[1]), nodo[0]))
            self.t.trie_aristas += [(b, indice[id(nodo[1][b])]) for b in sorted(nodo[1])]
        self._tries[clave] = base
        return base

    def transicion(self, tr):
        if tr in self._transiciones:
            return self._transiciones[tr]
        o = lambda x: NINGUNO if x is None else x
        accion, p = 0, [NINGUNO] * 4
        if tr.accion:
            clase, *args = tr.accion
            accion = ACCION[clase]
            if clase == 'clave':
                p[0], p[1] = o(args[0]), o(args[1])
            elif clase == 'cerrar_objeto':
                requeridas, dependencias, minimo = args
                p[0] = self.mascara(requeridas) if requeridas else NINGUNO
                p[1], p[2] = len(self.t.dependencias), len(dependencias)
                self.t.dependencias += [(bit, self.mascara(m)) for bit, m in dependencias]
                p[3] = o(minimo)
            elif clase == 'cerrar_atributos':
                p[0] = self.mascara(args[0])
            elif args:
                p[0] = o(args[0])
        # todos los "enum" de cadenas de la transición van en un solo trie: el valor de cada cadena
        # es el número de "enum" que la contienen, y faltan los demás
        enums = [prueba[1] for prueba in tr.pruebas if prueba[0] == 'enum_cad']
        inicio, emitido = len(self.t.pruebas), False
        for prueba in tr.pruebas:
            clase = prueba[0]
            if clase == 'enum_cad':
                if not emitido:
                    self.t.pruebas.append((PRUEBA[clase], len(enums), 0))
                emitido = True
            elif clase == 'enum_num':
                primero = len(self.t.decimales)
                for d in prueba[1]:
                    self.decimal(d)
                self.t.pruebas.append((PRUEBA[clase], primero, len(prueba[1])))
            elif clase in ('min', 'max'):
                self.t.pruebas.append((PRUEBA[clase], self.decimal(prueba[1]), int(prueba[2])))
            else:
                self.t.pruebas.append((PRUEBA[clase], prueba[1] if len(prueba) > 1 else 0, 0))
        fila = (o(tr.destino) if tr.destino is not RETORNO else NINGUNO, int(tr.desapila), o(tr.cima), o(tr.apila),
                accion, *p, tr.peso, inicio, len(self.t.pruebas) - inicio, tr.pendientes)
        numero = self._transiciones[tr] = len(self.t.transiciones)
        self.t.transiciones.append(fila)
        if enums:
            cuenta = {}
            for valores in enums:
                for v in set(valores):
                    cuenta[v.encode('utf-8')] = cuenta.get(v.encode('utf-8'), 0) + 1
            self._trie_de[numero] = self.trie_de(cuenta)
        return numero

    def aplanar(self):
        a, t = self.a, self.t
        (acepta,) = a.finales
        t.n_estados, t.inicial, t.acepta, t.pila_inicial = len(a.estados), a.inicial, acepta, a.pila_inicial
        t.retorno = [NINGUNO if q is None else q for q in a.retorno]
        t.xml = any('ETIQ_ABRE' in transiciones for transiciones in a.delta)
        tokens = TOKENS_XML if t.xml else TOKENS
        for transiciones in a.delta:
            fila, claves, otra, trie, refinado = [NINGUNO] * len(tokens), {}, NINGUNO, NINGUNO, False
            for simbolo, tr in transiciones.items():
                numero = self.transicion(tr)
                if isinstance(simbolo, str):
                    fila[tokens.index(simbolo)] = numero
                    if simbolo in VALORES and numero in self._trie_de:
                        trie = self._trie_de[numero]  # trie de los "enum" del valor
                elif simbolo[1] is None:
                    otra, refinado = numero, True
                else:
                    claves[simbolo[1].encode('utf-8')] = numero + 1
                    refinado = True
            if refinado:
                trie = self.trie_de(claves)  # estado que lee una clave o un nombre: trie de los declarados
                if t.xml:
                    fila[tokens.index('NOMBRE')] = otra if otra != NINGUNO else SIN_OTRA
            t.delta += fila
            t.trie.append(trie)
            t.otra.append(otra)
        return t


def aplanar(a: Automata) -> Tablas:
    return _Aplanador(a).aplanar()


_ARREGLOS = ('delta', 'trie', 'otra', 'retorno', 'transiciones', 'pruebas', 'decimales', 'digitos', 'mascaras',
             'dependencias', 'trie_nodos', 'trie_aristas')


def serializar(t: Tablas) -> str:
    """Texto que entiende validador/examples/dpda/cargar.rs: una línea por tabla, con sus enteros."""
    plano = lambda xs: ' '.join(str(v) for x in xs for v in (x if isinstance(x, tuple) else (x,)))
    lineas = [f'cabecera {t.n_estados} {t.inicial} {t.acepta} {t.pila_inicial} {t.palabras}']
    lineas += [f'{nombre} {plano(getattr(t, nombre))}'.rstrip() for nombre in _ARREGLOS]
    return '\n'.join(lineas) + '\n'


def emitir_rust(t: Tablas, origen='') -> str:
    """Código Rust con las tablas como datos estáticos y el autómata AUTOMATA que las reúne."""
    n = lambda x: 'N' if x == NINGUNO else str(x)

    def arreglo(nombre, tipo, elementos, por_linea):
        cuerpo = '\n'.join('    ' + ' '.join(e + ',' for e in elementos[i:i + por_linea])
                           for i in range(0, len(elementos), por_linea))
        return f'static {nombre}: [{tipo}; {len(elementos)}] = [\n{cuerpo}\n];\n' if elementos \
            else f'static {nombre}: [{tipo}; 0] = [];\n'

    transiciones = ['Transicion { ' + ', '.join(
        f'{campo}: {"true" if v else "false"}' if campo == 'desapila' else f'{campo}: {n(v)}'
        for campo, v in zip(CAMPOS_TRANSICION, fila)) + ' }' for fila in t.transiciones]
    partes = [
        f'// Generado por el compilador de esquemas{" a partir de " + origen if origen else ""}. No editar a mano.',
        '//',
        f'// {t.n_estados} estados, {len(t.transiciones)} transiciones distintas, {len(t.retorno)} símbolos de pila; '
        f'unos {t.bytes_de_tabla()} bytes de tablas.',
        '',
        'use validador::automata::{Arista, Automata, Decimal, Dependencia, NodoTrie, Prueba, Transicion, NINGUNO as N};',
        '',
        f'/// Palabras de 64 bits de la máscara de cada marco de pila.',
        f'pub const PALABRAS: usize = {t.palabras};',
        '',
        arreglo('DELTA', 'u32', [n(x) for x in t.delta], len(TOKENS)),
        arreglo('TRIE', 'u32', [n(x) for x in t.trie], 16),
        arreglo('OTRA', 'u32', [n(x) for x in t.otra], 16),
        arreglo('RETORNO', 'u32', [n(x) for x in t.retorno], 16),
        arreglo('TRANSICIONES', 'Transicion', transiciones, 1),
        arreglo('PRUEBAS', 'Prueba', [f'Prueba {{ clase: {c}, a: {n(a)}, b: {n(b)} }}' for c, a, b in t.pruebas], 1),
        arreglo('DECIMALES', 'Decimal', [f'Decimal {{ negativo: {"true" if s else "false"}, exponente: {e}, '
                                         f'digitos: {i}, n_digitos: {k} }}' for s, e, i, k in t.decimales], 1),
        arreglo('DIGITOS', 'u8', [str(x) for x in t.digitos], 32),
        arreglo('MASCARAS', 'u64', [hex(x) for x in t.mascaras], 4),
        arreglo('DEPENDENCIAS', 'Dependencia', [f'Dependencia {{ bit: {b}, mascara: {m} }}' for b, m in t.dependencias], 2),
        arreglo('TRIE_NODOS', 'NodoTrie', [f'NodoTrie {{ aristas: {i}, n_aristas: {k}, valor: {v} }}'
                                           for i, k, v in t.trie_nodos], 2),
        arreglo('TRIE_ARISTAS', 'Arista', [f'Arista {{ byte: {b}, destino: {d} }}' for b, d in t.trie_aristas], 4),
        'pub static AUTOMATA: Automata<\'static> = Automata {',
        f'    n_estados: {t.n_estados},',
        f'    inicial: {t.inicial},',
        f'    acepta: {t.acepta},',
        f'    pila_inicial: {t.pila_inicial},',
        '    palabras: PALABRAS,',
        '    delta: &DELTA,',
        '    trie: &TRIE,',
        '    otra: &OTRA,',
        '    retorno: &RETORNO,',
        '    transiciones: &TRANSICIONES,',
        '    pruebas: &PRUEBAS,',
        '    decimales: &DECIMALES,',
        '    digitos: &DIGITOS,',
        '    mascaras: &MASCARAS,',
        '    dependencias: &DEPENDENCIAS,',
        '    trie_nodos: &TRIE_NODOS,',
        '    trie_aristas: &TRIE_ARISTAS,',
        '};',
        '',
    ]
    return '\n'.join(partes)
