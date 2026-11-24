"""Árboles de derivación de payloads y esquemas (Fase 2).

    python -m compilador.sintactico.arbol payload.json                  # árbol como texto
    python -m compilador.sintactico.arbol payload.json --formato dot    # Graphviz
    python -m compilador.sintactico.arbol esquema.json --esquema        # con la gramática de esquemas
    python -m compilador.sintactico.arbol documento.xml --xml           # documento XML

Los recorridos son iterativos: por la recursión por la derecha de las listas, un arreglo de n elementos
da un árbol de profundidad proporcional a n, y un recorrido recursivo desbordaría la pila de Python.
"""
import json

from ..lexico.esquema import clasificar
from ..lexico.lexer import tokenizar
from .gramatica import EPSILON
from ..lexico.lexer_xml import tokenizar_xml
from .gramaticas import ESQUEMA, JSON, XML
from .parser import AnalizadorLL1
from .xml_dom import construir as construir_xml

_ANALIZADORES = {}


def analizador(gramatica):
    if id(gramatica) not in _ANALIZADORES:
        _ANALIZADORES[id(gramatica)] = AnalizadorLL1(gramatica)
    return _ANALIZADORES[id(gramatica)]


def derivar(datos: bytes, esquema=False, max_anidamiento=None, xml=False):
    """Árbol de derivación de un payload JSON (o de un esquema, con esquema=True; o de un documento
    XML, con xml=True). Lanza ErrorLexico o ErrorSintactico."""
    if xml:
        tokens, error = tokenizar_xml(datos)
        if error:
            raise error
        raiz = analizador(XML).analizar(tokens)
        construir_xml(tokens)   # lo que la gramática no expresa: cierre = apertura, atributos sin repetir...
        return raiz
    tokens, error = tokenizar(datos)
    if error:
        raise error
    if esquema:
        tokens = [clasificar(t) for t in tokens]
    return analizador(ESQUEMA if esquema else JSON).analizar(tokens, max_anidamiento)


def hojas(raiz):
    """Tokens del árbol de izquierda a derecha: la cadena derivada."""
    salida, pila = [], [raiz]
    while pila:
        nodo = pila.pop()
        if nodo.token is not None:
            salida.append(nodo.token)
        pila.extend(reversed(nodo.hijos))
    return salida


def _lexema(nodo, datos):
    return datos[nodo.token.inicio:nodo.token.fin].decode('utf-8')


def a_dict(raiz, datos: bytes):
    """El árbol como diccionarios anidados: {'simbolo', 'hijos'} o, en las hojas, {'simbolo', 'lexema', 'inicio', 'fin'}."""
    resultado = {}
    pila = [(raiz, resultado)]
    while pila:
        nodo, d = pila.pop()
        d['simbolo'] = nodo.simbolo
        if nodo.token is not None:
            d.update(lexema=_lexema(nodo, datos), inicio=nodo.token.inicio, fin=nodo.token.fin)
        else:
            d['hijos'] = [{} for _ in nodo.hijos]
            pila.extend(zip(nodo.hijos, d['hijos']))
    return resultado


def a_texto(raiz, datos: bytes) -> str:
    """El árbol dibujado con sangrías, un símbolo por línea."""
    lineas, pila = [], [(raiz, '', '')]
    while pila:
        nodo, prefijo, rama = pila.pop()
        if nodo.token is not None:
            etiqueta = f'{nodo.simbolo}  «{_lexema(nodo, datos)}»'
        else:
            etiqueta = nodo.simbolo + ('' if nodo.hijos else f' → {EPSILON}')
        lineas.append(prefijo + rama + etiqueta)
        sangria = prefijo + ('' if not rama else '   ' if rama.startswith('└') else '│  ')
        ultimo = len(nodo.hijos) - 1
        pila.extend((h, sangria, '└─ ' if i == ultimo else '├─ ') for i, h in reversed(list(enumerate(nodo.hijos))))
    return '\n'.join(lineas)


def a_dot(raiz, datos: bytes) -> str:
    """El árbol en el lenguaje DOT de Graphviz."""
    escapar = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')
    # identidad UPTC: no terminales como texto, tokens (las hojas) en cajas sobre amarillo pálido
    lineas = ['digraph derivacion {', '  bgcolor="transparent"; pad=0.25; nodesep=0.18; ranksep=0.32;',
              '  node [fontname="Helvetica", fontsize=11, shape=plaintext, fontcolor="#262521", class="no-terminal"];',
              '  edge [arrowhead=none, color="#a39e8f"];']
    pila, n = [(raiz, 0)], 1
    while pila:
        nodo, i = pila.pop()
        if nodo.token is not None:
            lineas.append(f'  n{i} [label="{escapar(nodo.simbolo)}\\n{escapar(_lexema(nodo, datos))}", shape=box, '
                          'style=filled, fillcolor="#fff4d1", color="#1a1a1a", fontsize=10, class="token"];')
            continue
        lineas.append(f'  n{i} [label="{escapar(nodo.simbolo)}"];')
        if not nodo.hijos:
            lineas.append(f'  n{n} [label="{EPSILON}", fontcolor="#a39e8f", class="epsilon"];  n{i} -> n{n};')
            n += 1
        hijos = list(zip(nodo.hijos, range(n, n + len(nodo.hijos))))
        n += len(hijos)
        lineas += [f'  n{i} -> n{j};' for _, j in hijos]
        pila.extend(reversed(hijos))
    return '\n'.join(lineas + ['}'])


if __name__ == '__main__':
    import argparse
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description='Árbol de derivación de un payload JSON o de un esquema.')
    p.add_argument('archivo')
    p.add_argument('--esquema', action='store_true', help='usar la gramática del metalenguaje de esquemas')
    p.add_argument('--xml', action='store_true', help='el archivo es un documento XML')
    p.add_argument('--formato', choices=['texto', 'json', 'dot'], default='texto')
    args = p.parse_args()
    with open(args.archivo, 'rb') as f:
        contenido = f.read()
    arbol = derivar(contenido, args.esquema, xml=args.xml)
    if args.formato == 'json':
        print(json.dumps(a_dict(arbol, contenido), ensure_ascii=False, indent=1))
    else:
        print({'texto': a_texto, 'dot': a_dot}[args.formato](arbol, contenido))
