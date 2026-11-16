"""Pruebas del lexer de JSON en streaming de Python (Fase 1, pasos 5 y 7)."""
import json
import random

import pytest

from compilador.lexico.afd import construir_json
from compilador.lexico.lexer import ErrorLexico, Lexer, Token, tokenizar
from pruebas.corpus import ENTRADAS_JSON, mutantes, particion

AFD = construir_json()
TODAS = ENTRADAS_JSON + mutantes()


def resumen(resultado):
    """Resultado de tokenizar() en una forma comparable: tokens y (posición, motivo) del error."""
    tokens, error = resultado
    return tokens, error and (error.pos, error.motivo)


def valor(datos):
    tokens, error = tokenizar(datos)
    assert error is None, error
    return tokens[0].valor


# ---- el lexer es el mismo autómata que el AFD de la sección 4 ----

@pytest.mark.parametrize('datos', ENTRADAS_JSON, ids=repr)
def test_coincide_con_el_afd(datos):
    tokens, error = tokenizar(datos)
    esperados, pos = AFD.lex(datos)
    obtenidos = [(t.tipo, (t.inicio, t.fin)) for t in tokens]
    if error is not None and error.motivo == 'largo':
        # el límite del número es del lexer, no del AFD: solo coincide lo anterior al número
        assert obtenidos == esperados[:len(obtenidos)]
    else:
        assert obtenidos == esperados
        assert (error and error.pos) == pos


# ---- valores ----

def test_valores_de_cadenas():
    assert valor(b'""') == ''
    assert valor(b'"caf\\u00e9"') == 'café'
    assert valor('"café 😀"'.encode()) == 'café 😀'
    assert valor(b'"\\" \\\\ \\/ \\b \\f \\n \\r \\t"') == '" \\ / \b \f \n \r \t'
    assert valor(b'"\\ud83d\\ude00"') == '😀'
    assert valor(b'"\\uD83D\\uDE00x"') == '😀x'
    assert valor(b'"a\\u0000b"') == 'a\0b'
    assert valor(b'"\\uD7FF\\uE000"') == '\ud7ff\ue000'


@pytest.mark.parametrize('lexema, entero', [
    ('0', True), ('-0', True), ('25', True), ('123456789012345678901234567890', True),
    ('-12.5e+3', False), ('1e400', False), ('0.5E-3', False), ('1E5', False),
])
def test_valores_de_numeros(lexema, entero):
    tokens, error = tokenizar(lexema.encode())
    assert error is None
    assert tokens == [Token('NUMERO', 0, len(lexema), lexema), Token('FIN', len(lexema), len(lexema))]
    assert tokens[0].es_entero is entero


def test_el_numero_se_reinicia_en_cada_token():
    tokens, _ = tokenizar(b'[12, 3.5,-7]')
    assert [t.valor for t in tokens if t.tipo == 'NUMERO'] == ['12', '3.5', '-7']


def test_numero_demasiado_largo():
    assert tokenizar(b'9' * 1024)[1] is None
    error = tokenizar(b'[' + b'9' * 1025 + b']')[1]
    assert (error.pos, error.motivo) == (1025, 'largo')
    assert resumen(tokenizar(b'[12345]', max_numero=4))[1] == (5, 'largo')


# ---- oráculo independiente: el módulo json de Python ----

def test_cadenas_contra_json_loads():
    rng = random.Random(7)
    alfabeto = 'ab"\\/\b\f\n\r\t\x00\x1f é€😀\ud7ff\ue000\uffff\U0010ffff'
    for _ in range(500):
        texto = ''.join(rng.choice(alfabeto) for _ in range(rng.randint(0, 12)))
        for ascii_puro in (True, False):  # con escapes \uXXXX o con UTF-8 directo
            fuente = json.dumps(texto, ensure_ascii=ascii_puro).encode('utf-8')
            assert valor(fuente) == texto == json.loads(fuente)


@pytest.mark.parametrize('lexema', [
    '0', '-0', '1e400', '0.5E-3', '-12.5e+3', '10', '1E+2', '0e0', '0.0',
    '01', '1.', '.5', '-', '+1', '1e', '1e+', '--1', '0x10', '1.5.2', '00', '-01', '1.e5', 'e5',
])
def test_numeros_contra_json_loads(lexema):
    try:
        json.loads(lexema)
        valido = True
    except json.JSONDecodeError:
        valido = False
    assert (tokenizar(lexema.encode())[1] is None) is valido


# ---- streaming ----

@pytest.mark.parametrize('tam', [1, 2, 3, 7])
def test_bloques_de_tamano_fijo(tam):
    for datos in TODAS:
        cortes = [tam] * (len(datos) // tam + 1)
        assert resumen(tokenizar(datos, cortes)) == resumen(tokenizar(datos)), datos


def test_particiones_aleatorias():
    rng = random.Random(11)
    for datos in TODAS:
        for _ in range(3):
            cortes = particion(rng, len(datos))
            assert resumen(tokenizar(datos, cortes)) == resumen(tokenizar(datos)), (datos, cortes)


def test_token_partido_entre_bloques():
    lexer = Lexer()
    assert lexer.feed(b'[12') == [Token('CORCHETE_ABRE', 0, 1)]  # 12 aún puede continuar
    assert lexer.feed(b'3, "caf\xc3') == [Token('NUMERO', 1, 4, '123'), Token('COMA', 4, 5)]
    assert lexer.feed(b'\xa9"]') == [Token('CADENA', 6, 13, 'café'), Token('CORCHETE_CIERRA', 13, 14)]
    assert lexer.finish() == [Token('FIN', 14, 14)]


def test_el_error_se_conserva():
    lexer = Lexer()
    with pytest.raises(ErrorLexico) as primero:
        lexer.feed(b'[1, x')
    assert (primero.value.pos, primero.value.motivo) == (4, 'byte')
    assert primero.value.tokens == [Token('CORCHETE_ABRE', 0, 1), Token('NUMERO', 1, 2, '1'), Token('COMA', 2, 3)]
    with pytest.raises(ErrorLexico) as segundo:
        lexer.finish()
    assert segundo.value is primero.value


def test_fin_inesperado():
    for datos, pos in [(b'"ab', 3), (b'-', 1), (b'"\xe2\x82', 3), (b'tru', 3), (b'1.', 2)]:
        assert resumen(tokenizar(datos))[1] == (pos, 'fin')
