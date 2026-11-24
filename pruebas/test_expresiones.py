"""Pruebas de las estrategias de respaldo de la Fase 4: expresiones regulares → AFD (expresiones.py), y
huellas y múltiplos exactos (valores.py).

El oráculo de las expresiones es el módulo re de Python, con dos ajustes para que tenga la semántica de
ECMA-262: $ solo casa al final (en Python también antes de un salto de línea final) y \\d, \\w, \\s son
ASCII (re.ASCII).
"""
import random
import re
import time
from decimal import Decimal
from fractions import Fraction

import pytest

from compilador.automata import expresiones, valores
from compilador.automata.expresiones import ErrorDeExpresion, NoRegular, afd, coincide, secuencias_utf8
from compilador.semantico.analisis import analizar_fuente
from compilador.sintactico.ast_esquema import analizar_esquema

PATRONES = [r'^a+$', r'ab|cd', r'^[a-z]{2,4}$', r'\d{3}-\d{4}', r'^(foo|bar)?baz$', r'x*', r'^$', r'[^abc]', r'^.$',
            r'^[ñ-ü]+$', r'^\w+@\w+\.com$', r'(a|b)*abb', r'^(?:\+|-)?[0-9]+(\.[0-9]+)?$', r'a{3}', r'^.{2,3}$',
            r'é$', r'^😀', r'^[^😀]*$', r'\x41|é', r'^a|b$', r'a{2,}', r'(ab){1,2}c', r'^[\s]+$', r'^\S\S$',
            r'[.]', r'\.', r'^(?<año>\d{4})$', r'a+?b', r'[\]\-]', r'^\t\n?$', r'[a\-z]', r'\/', r'a{0}b']
ALFABETO = 'abcdxyzABC0123-+.@ñüé😀 \n\t]/'


def oraculo(patron, texto):
    patron = patron.replace('$', '\\Z').replace('(?<', '(?P<')   # en re, el grupo con nombre es (?P<...>)
    return re.search(patron, texto, re.ASCII) is not None


@pytest.mark.parametrize('patron', PATRONES)
def test_mismo_resultado_que_re(patron):
    rng = random.Random(patron)
    for _ in range(1500):
        texto = ''.join(rng.choice(ALFABETO) for _ in range(rng.randint(0, 8)))
        if rng.random() < 0.3:
            texto += rng.choice(['ab', 'cd', 'abb', 'foobaz', 'baz', 'aaa', '2024', 'x@y.com', 'abc'])
        assert coincide(patron, texto) == oraculo(patron, texto), (patron, texto)


@pytest.mark.parametrize('patron, motivo', [
    (r'(a)\1', 'referencia hacia atrás'), (r'(?<x>a)\k<x>', 'referencia hacia atrás'),
    (r'(?=a)', 'aserción'), (r'(?<!a)b', 'aserción'), (r'\bx', 'límite de palabra'), (r'\p{L}', 'propiedad Unicode'),
    ('(a{1000}){0,1}b{1000}' * 20, 'demasiado grande'),
])
def test_lo_que_no_se_compila_queda_diferido(patron, motivo):
    with pytest.raises((NoRegular, ErrorDeExpresion)) as e:
        expresiones.de_patron(patron)
    assert e.type is NoRegular and motivo in str(e.value)
    assert motivo in expresiones.regular(patron)


@pytest.mark.parametrize('patron', ['(', ')', 'a**', '[b-a]', '*', '\\', 'a{2}{3}', '(?x)', '[\\d-z]', '[a', '\\x4',
                                    '\\u{110000}', '+a'])
def test_expresiones_invalidas(patron):
    with pytest.raises(ErrorDeExpresion):
        expresiones.analizar(patron)


def test_escapes_unicode():
    barra = chr(92)   # se construyen así para que ningún editor convierta el escape en el carácter
    assert coincide(barra + 'u00e9', 'é') and coincide(barra + 'u{1F600}', '😀') and coincide(barra + 'x41', 'A')
    assert coincide('^' + barra + 'ud83d' + barra + 'ude00$', '😀')   # par de sustitutos
    assert coincide(barra + 'cJ', chr(10)) and not coincide(barra + 'u00e9', 'e')


def test_llave_literal_y_anexo_b():
    # «{» que no forma un cuantificador es un carácter (ECMA-262, anexo B), igual que «]» y «}» sueltos
    assert coincide('a{', 'xa{') and coincide(']}', ']}') and not coincide('a{2', 'aa')
    assert coincide('a{,2}', 'a{,2}')


def test_secuencias_utf8_exactas():
    rng = random.Random(3)
    for lo, hi in [(0, 0x7F), (0x80, 0x7FF), (0x7F0, 0x810), (0xD000, 0xE100), (0xFFF0, 0x10010), (0, 0x10FFFF)]:
        secuencias = secuencias_utf8(lo, hi)
        def cubre(datos):
            return any(len(s) == len(datos) and all(a <= b <= z for (a, z), b in zip(s, datos)) for s in secuencias)
        for _ in range(3000):
            c = rng.randint(max(0, lo - 50), min(0x10FFFF, hi + 50))
            if 0xD800 <= c <= 0xDFFF:
                continue
            assert cubre(chr(c).encode('utf-8')) == (lo <= c <= hi), hex(c)


def test_producto_de_varios_patrones():
    d = afd([('n', 'literal', b'abc'), ('p', 'patron', '^a'), ('q', 'patron', 'c$')])
    assert d.correr(b'abc') == {'n', 'p', 'q'} and d.correr(b'ac') == {'p', 'q'} and d.correr(b'b') == frozenset()
    assert sorted(map(sorted, d.alcanzables())) == [[], ['n', 'p', 'q'], ['p'], ['p', 'q'], ['q']]


def test_sin_redos():
    # (a+)+$ hace retroceder exponencialmente a los motores con retroceso; el AFD es lineal
    texto = 'a' * 20_000 + 'b'
    inicio = time.perf_counter()
    assert not coincide('^(a+)+$', texto) and not coincide('^(a|aa)*c', texto)
    assert time.perf_counter() - inicio < 2
    assert len(expresiones.de_patron('^(a+)+$').aristas) <= 3   # el AFD mínimo es diminuto


def test_ejemplos_salen_del_afd():
    rng = random.Random(5)
    for patrones in (['^[a-z]{2,4}$'], [r'^\d{3}-\d{4}$'], ['^x', 'z$'], [r'^\w+@\w+\.com$'], ['a{3}']):
        for _ in range(10):
            texto = expresiones.ejemplo(patrones, rng)
            assert texto is not None and all(coincide(p, texto) for p in patrones)
    assert expresiones.ejemplo([r'^[^\s\S]'], rng) is None   # no casa con nada


# ---------------------------------------------------------------------------
# Huellas y múltiplos
# ---------------------------------------------------------------------------
def huella(texto):
    """Huella de un valor JSON escrito tal cual (sin pasar por Python, para conservar 1.0, 1e2, \\u0061...)."""
    return valores.de_valor(analizar_esquema(b'{"const": ' + texto.encode() + b'}').claves[0].valor)


@pytest.mark.parametrize('a, b', [
    ('1', '1.0'), ('1', '1e0'), ('100', '1e2'), ('-0', '0'), ('0.5', '5e-1'), ('"a"', '"\\u0061"'),
    ('{"a": 1, "b": [2]}', '{"b": [2.0], "a": 1}'), ('[]', '[ ]'), ('{}', '{ }'),
])
def test_valores_iguales_tienen_la_misma_huella(a, b):
    assert huella(a) == huella(b)


@pytest.mark.parametrize('a, b', [
    ('1', '"1"'), ('[1, 2]', '[2, 1]'), ('{"a": 1}', '{"a": "1"}'), ('[]', '{}'), ('null', 'false'), ('0', 'false'),
    ('[[1], 2]', '[1, [2]]'), ('{"a": {"b": 1}}', '{"b": {"a": 1}}'), ('""', '[]'), ('1', '-1'), ('0.1', '1'),
])
def test_valores_distintos_tienen_huellas_distintas(a, b):
    assert huella(a) != huella(b)


def test_multiplos_exactos():
    rng = random.Random(9)
    divisores = ['0.5', '7', '0.01', '3.3', '1e-5', '2.5e3', '123456789012345678']
    for _ in range(3000):
        lexema = f'{rng.choice(["", "-"])}{rng.randint(0, 10 ** rng.randint(0, 8))}'
        if rng.random() < 0.5:
            lexema += f'.{rng.randint(0, 999):03d}'
        if rng.random() < 0.3:
            lexema += f'e{rng.randint(-6, 6)}'
        divisor = rng.choice(divisores)
        exacto = (Fraction(Decimal(lexema)) / Fraction(Decimal(divisor))).denominator == 1
        assert valores.es_multiplo(valores.canonico(lexema), valores.canonico_decimal(Decimal(divisor))) == exacto, \
            (lexema, divisor)
    assert not valores.divisor_admitido(Decimal('1234567890123456789'))   # 19 dígitos: no cabe en el motor
    assert analizar_fuente(b'{"multipleOf": 0.1}').ok


# ---------------------------------------------------------------------------
# Expresiones de XSD (xs:pattern)
# ---------------------------------------------------------------------------
B = chr(92)   # barra invertida
PATRONES_XSD = ['[A-Z]{3}-[0-9]+', 'a^b$', B + 'd{2,4}', B + 'i' + B + 'c*', '[a-z-[aeiou]]+', B + 'p{Lu}' + B + 'p{Ll}*',
                B + 'w+', '(ab|cd)?e', '[^' + B + 's]+', B + 'S' + B + 's' + B + 'S', '.', '[' + B + '-a]', B + 'P{L}+',
                '[' + B + 'p{N}x]+', 'a|', 'x{0}y', '[^a-c-[b]]', B + '.' + B + '*' + B + '{' + B + '}']
ALFABETO_XSD = 'abcexyzABZ019-^$.*{} _:ñÉé٣😀\n\t'


def oraculo_xsd(patron):
    from elementpath.regex import translate_pattern
    # fuera de una clase, elementpath deja \w y \s a re (que mete «_» en \w): entre corchetes usa los de XSD
    if not patron.startswith('['):
        for letra in 'wsS':
            patron = patron.replace(B + letra, '[' + B + letra + ']')
    return re.compile(translate_pattern(patron, anchors=False))


@pytest.mark.parametrize('patron', PATRONES_XSD)
def test_xsd_mismo_resultado_que_elementpath(patron):
    oraculo, rng = oraculo_xsd(patron), random.Random(patron)
    # elementpath no incluye el plano astral en \i y \c (la especificación sí): ahí no se compara
    alfabeto = ALFABETO_XSD.replace('😀', '') if B + 'i' in patron or B + 'c' in patron else ALFABETO_XSD
    for _ in range(1500):
        texto = ''.join(rng.choice(alfabeto) for _ in range(rng.randint(0, 6)))
        if rng.random() < 0.2:
            texto = rng.choice(['ABC-12', 'ab e', 'Hola', 'xyz', 'bcd', 'a^b$', '2024', '.*{}'])
        assert expresiones.coincide_xsd((patron,), texto) == (oraculo.fullmatch(texto) is not None), (patron, texto)


def test_xsd_alternativas_y_lo_que_no_se_compila():
    assert expresiones.coincide_xsd(('a+', 'b+'), 'bbb') and not expresiones.coincide_xsd(('a+', 'b+'), 'ab')
    assert expresiones.coincide_xsd(('',), '') and not expresiones.coincide_xsd(('',), 'a')   # anclada
    assert 'bloque Unicode' in expresiones.regular_xsd(B + 'p{IsBasicLatin}')
    for patron in ['(', 'a**', 'a+?', '[b-a]', '{', ']', 'a{,2}', B + 'q', B + 'p{Xx}', '[a[b]]', '(?:a)']:
        with pytest.raises(ErrorDeExpresion):
            expresiones.analizar_xsd(patron)
