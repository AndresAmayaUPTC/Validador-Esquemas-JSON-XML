"""Pruebas del parser LL(1) y de los árboles de derivación de payloads (Fase 2)."""
import json

import pytest

from compilador.lexico.lexer import ErrorLexico, tokenizar
from compilador.sintactico.arbol import a_dict, a_dot, a_texto, derivar, hojas
from compilador.sintactico.gramaticas import JSON
from compilador.sintactico.parser import ErrorSintactico
from pruebas.corpus import ENTRADAS_JSON, mutantes


def acepta_json_de_python(datos):
    try:
        json.loads(datos)
        return True
    except (ValueError, RecursionError):
        return False


def test_coincide_con_json_loads():
    """Si la entrada pasa el lexer, el parser la acepta exactamente cuando lo hace el módulo json."""
    comparadas = aceptadas = 0
    for datos in ENTRADAS_JSON + mutantes():
        try:
            derivar(datos)
            valida = True
        except ErrorLexico:
            continue
        except ErrorSintactico:
            valida = False
        comparadas += 1
        aceptadas += valida
        assert valida == acepta_json_de_python(datos), datos
    assert comparadas > 500 and 100 < aceptadas < comparadas  # la prueba no es vacía


@pytest.mark.parametrize('datos', [
    b'{"edad": 25, "ok": true}', b'[1, 2, {"a": [null, false, true]}] ', b'"x"', b'{}', b'[[], {}]',
])
def test_la_cadena_derivada_son_los_tokens(datos):
    assert hojas(derivar(datos)) == tokenizar(datos)[0]


def test_arbol_de_un_arreglo():
    datos = b'[1]'
    hoja = lambda simbolo, lexema, i: {'simbolo': simbolo, 'lexema': lexema, 'inicio': i, 'fin': i + len(lexema)}
    rama = lambda simbolo, *hijos: {'simbolo': simbolo, 'hijos': list(hijos)}
    assert a_dict(derivar(datos), datos) == rama(
        'json',
        rama('valor', rama(
            'arreglo',
            hoja('CORCHETE_ABRE', '[', 0),
            rama('elementos', rama('valor', hoja('NUMERO', '1', 1)), rama('mas_valores')),
            hoja('CORCHETE_CIERRA', ']', 2))),
        hoja('FIN', '', 3))


def test_producciones_en_orden_de_derivacion_por_la_izquierda():
    raiz = derivar(b'{"a": 1}')
    aplicadas, pila = [], [raiz]
    while pila:  # recorrido en preorden = derivación por la izquierda
        nodo = pila.pop()
        if nodo.produccion is not None:
            aplicadas.append(JSON.texto_produccion(nodo.produccion))
        pila.extend(reversed(nodo.hijos))
    assert aplicadas == [
        'json → valor FIN', 'valor → objeto', 'objeto → LLAVE_ABRE miembros LLAVE_CIERRA',
        'miembros → par mas_pares', 'par → CADENA DOS_PUNTOS valor', 'valor → NUMERO', 'mas_pares → ε',
    ]


def test_arbol_como_texto_y_dot():
    datos = '{"año": [true]}'.encode()
    arbol = derivar(datos)
    texto = a_texto(arbol, datos).splitlines()
    assert texto[0] == 'json'
    assert texto[1] == '├─ valor'
    assert texto[2:7] == [
        '│  └─ objeto',
        '│     ├─ LLAVE_ABRE  «{»',
        '│     ├─ miembros',
        '│     │  ├─ par',
        '│     │  │  ├─ CADENA  «"año"»',
    ]
    assert texto[-1] == '└─ FIN  «»'
    assert sum('→ ε' in linea for linea in texto) == 2  # mas_valores y mas_pares
    dot = a_dot(arbol, datos)
    assert dot.startswith('digraph derivacion {') and dot.endswith('}')
    assert dot.count('->') == len(texto) - 1 + 2  # una arista por nodo, más las dos hojas ε
    assert 'label="CADENA\\n\\"año\\""' in dot


@pytest.mark.parametrize('datos, pos, encontrado, esperados', [
    (b'', 0, 'FIN', ['CADENA', 'CORCHETE_ABRE', 'FALSE', 'LLAVE_ABRE', 'NULL', 'NUMERO', 'TRUE']),
    (b'{"a" 1}', 5, 'NUMERO', ['DOS_PUNTOS']),
    (b'{"a": 1,}', 8, 'LLAVE_CIERRA', ['CADENA']),
    (b'[1 2]', 3, 'NUMERO', ['COMA', 'CORCHETE_CIERRA']),
    (b'[1,]', 3, 'CORCHETE_CIERRA', ['CADENA', 'CORCHETE_ABRE', 'FALSE', 'LLAVE_ABRE', 'NULL', 'NUMERO', 'TRUE']),
    (b'{1: 2}', 1, 'NUMERO', ['CADENA', 'LLAVE_CIERRA']),
    (b'[1] 2', 4, 'NUMERO', ['FIN']),
    (b'{"a": [1, 2}', 11, 'LLAVE_CIERRA', ['COMA', 'CORCHETE_CIERRA']),
    (b'[', 1, 'FIN', ['CADENA', 'CORCHETE_ABRE', 'CORCHETE_CIERRA', 'FALSE', 'LLAVE_ABRE', 'NULL', 'NUMERO', 'TRUE']),
])
def test_errores_sintacticos(datos, pos, encontrado, esperados):
    with pytest.raises(ErrorSintactico) as e:
        derivar(datos)
    assert (e.value.pos, e.value.encontrado, e.value.esperados, e.value.motivo) == (pos, encontrado, esperados, 'token')


def test_limite_de_anidamiento():
    profundo = lambda n: b'[' * n + b']' * n
    derivar(profundo(64), max_anidamiento=64)
    with pytest.raises(ErrorSintactico) as e:
        derivar(profundo(65), max_anidamiento=64)
    assert (e.value.pos, e.value.motivo) == (64, 'anidamiento')


def test_listas_largas_sin_recursion():
    datos = b'[' + b','.join(b'%d' % i for i in range(20000)) + b']'
    arbol = derivar(datos)  # el árbol tiene profundidad ~20000: los recorridos no pueden ser recursivos
    assert len(hojas(arbol)) == 2 * 20000 + 2
    assert a_dot(arbol, datos).count('label="NUMERO') == 20000
    assert a_dict(arbol, datos)['simbolo'] == 'json'
    # el dibujo con sangrías crece con el cuadrado de la longitud de la lista: se prueba con una más corta
    corta = b'[' + b','.join(b'%d' % i for i in range(1500)) + b']'
    assert a_texto(derivar(corta), corta).count('NUMERO') == 1500
