"""Pruebas de PRIMEROS, SIGUIENTES y la tabla LL(1) (Fase 2)."""
import pytest

from compilador.lexico.esquema import PALABRAS_CLAVE
from compilador.sintactico.gramatica import EPSILON, ConflictoLL1, Gramatica
from compilador.sintactico.gramaticas import ESQUEMA, FORMAS, JSON, SUELTAS

# Gramática de expresiones de los libros de texto, ya sin recursión por la izquierda
EXPRESIONES = Gramatica('''
    e  → t e2
    e2 → "+" t e2 | ε
    t  → f t2
    t2 → "*" f t2 | ε
    f  → "(" e ")" | ID
''')


def test_primeros_y_siguientes_de_la_gramatica_de_expresiones():
    g = EXPRESIONES
    assert g.inicial == 'e'
    assert g.no_terminales == ['e', 'e2', 't', 't2', 'f']
    assert g.terminales == ['"("', '")"', '"*"', '"+"', 'ID']
    assert g.primeros == {
        'e': {'"("', 'ID'}, 't': {'"("', 'ID'}, 'f': {'"("', 'ID'},
        'e2': {'"+"', EPSILON}, 't2': {'"*"', EPSILON},
    }
    # sin marcador de fin: SIGUIENTES(e) solo tiene lo que la propia gramática pone detrás
    assert g.siguientes == {
        'e': {'")"'}, 'e2': {'")"'},
        't': {'"+"', '")"'}, 't2': {'"+"', '")"'},
        'f': {'"*"', '"+"', '")"'},
    }
    assert g.conflictos() == []
    tabla = g.tabla_ll1()
    assert g.texto_produccion(tabla['e2', '")"']) == 'e2 → ε'
    assert g.texto_produccion(tabla['f', 'ID']) == 'f → ID'
    assert ('f', '"+"') not in tabla


@pytest.mark.parametrize('texto, celda', [
    ('s → s A | B', ('s', 'B')),                                  # recursión por la izquierda
    ('s → A b | A c\n b → B\n c → C', ('s', 'A')),                # prefijo común
    ('s → IF s resto | X\n resto → ELSE s | ε', ('resto', 'ELSE')),  # else ambiguo
    ('s → a B\n a → ε | B', ('a', 'B')),                          # B ∈ PRIMEROS(B) y B ∈ SIGUIENTES(a)
])
def test_detecta_gramaticas_que_no_son_ll1(texto, celda):
    g = Gramatica(texto)
    assert celda in [(A, a) for A, a, _ in g.conflictos()]
    with pytest.raises(ConflictoLL1):
        g.tabla_ll1()


def test_rechaza_gramaticas_mal_escritas():
    with pytest.raises(ValueError):
        Gramatica('s → a B')        # «a» no tiene producciones
    with pytest.raises(ValueError):
        Gramatica('s → A |')        # alternativa vacía sin ε


def test_la_gramatica_de_json_es_ll1():
    assert JSON.conflictos() == []
    assert len(JSON.producciones) == 19 and len(JSON.no_terminales) == 9
    assert JSON.terminales == sorted(['LLAVE_ABRE', 'LLAVE_CIERRA', 'CORCHETE_ABRE', 'CORCHETE_CIERRA', 'DOS_PUNTOS',
                                      'COMA', 'CADENA', 'NUMERO', 'TRUE', 'FALSE', 'NULL', 'FIN'])  # Σ_T de la Tabla 9
    assert JSON.primeros['valor'] == {'LLAVE_ABRE', 'CORCHETE_ABRE', 'CADENA', 'NUMERO', 'TRUE', 'FALSE', 'NULL'}
    assert JSON.primeros['miembros'] == {'CADENA', EPSILON}
    assert JSON.siguientes['valor'] == {'FIN', 'COMA', 'LLAVE_CIERRA', 'CORCHETE_CIERRA'}
    assert JSON.siguientes['miembros'] == {'LLAVE_CIERRA'}
    assert JSON.siguientes['elementos'] == {'CORCHETE_CIERRA'}


def test_la_gramatica_de_esquemas_es_ll1():
    assert ESQUEMA.conflictos() == []
    ESQUEMA.tabla_ll1()
    # cada palabra clave del lexer es un terminal y aparece en exactamente un grupo de forma
    en_grupos = [p for palabras in FORMAS.values() for p in palabras] + list(SUELTAS)
    assert sorted(en_grupos) == sorted(PALABRAS_CLAVE)
    palabras = {f'"{p}"' for p in PALABRAS_CLAVE}
    assert palabras <= set(ESQUEMA.terminales)
    # «cadena» admite una cadena cualquiera o cualquier palabra clave; «clave» empieza por lo mismo
    assert ESQUEMA.primeros['cadena'] == palabras | {'CADENA'}
    assert ESQUEMA.primeros['clave'] == palabras | {'CADENA'}
    assert ESQUEMA.primeros['esquema'] == {'TRUE', 'FALSE', 'LLAVE_ABRE'}


def test_informe():
    texto = JSON.informe()
    assert '  0. json → valor FIN' in texto
    assert 'Es LL(1)' in texto
    assert 'NO es LL(1)' in Gramatica('s → s A | B').informe()
