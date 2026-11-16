"""Entradas de JSON compartidas por las pruebas del lexer (válidas e inválidas)."""
import random

ENTRADAS_JSON = [
    # estructura
    b'', b' \t\r\n', b'{}', b'[]', b'{"edad": 25, "ok": true}', b'[1, 2, {"a": [null, false, true]}] ',
    b'{"a":{"b":{"c":[[[]]]}}}', b'}{][,:', b'\xef\xbb\xbf[1]', b'[1,\xef\xbb\xbf]', b'\xef\xbb[1]',
    # números límite y números inválidos
    b'0', b'-0', b'25', b'-12.5e+3', b'0.5E-3', b'1e400', b'1E-400', b'123456789012345678901234567890',
    b'[0,-1,2.5,3e7]', b'01', b'[01]', b'1.', b'[1.]', b'.5', b'-', b'+1', b'1e', b'1e+', b'--1', b'0x10',
    b'1.5.2', b'1e5e3', b'-a', b'9' * 1024, b'9' * 1025, b'[' + b'1' * 1030 + b']',
    # literales
    b'true', b'false', b'null', b'[true,false,null]', b'truex', b'nul', b'True', b'tru e', b'nulll',
    # cadenas: escapes, control y pares de sustitutos
    b'""', b'"abc"', b'"\\" \\\\ \\/ \\b \\f \\n \\r \\t"', b'"caf\\u00e9"', b'"\\u0000"', b'"\\uFFFF"',
    b'"\\ud83d\\ude00"', b'"\\uD83D\\uDE00"', b'"\\uD7FF\\uE000"', b'"a\\qb"', b'"ab', b'"\\', b'"\\u12',
    b'"\\u12G4"', b'"\\ud83d"', b'"\\ud83dx"', b'"\\ud83d\\n"', b'"\\ud83d\\u0041"', b'"\\udc00"',
    b'"\\ud83d\\ud83d"', b'"tab\tx"', b'"nul\x00"', b'"salto\n"', b'"\x7f"',
    # UTF-8: válido, sobrelargo, sustitutos, bytes F5–FF y secuencias truncadas
    '"café 😀"'.encode(), '"ñ€𐍈\u00a0\u07ff\u0800\uffff\U0010ffff"'.encode(), '{"año": "日本語"}'.encode(),
    b'"\xc0\xaf"', b'"\xc1\xbf"', b'"\xe0\x80\xaf"', b'"\xe0\x9f\xbf"', b'"\xf0\x80\x80\xaf"', b'"\xf0\x8f\xbf\xbf"',
    b'"\xed\xa0\x80"', b'"\xed\xbf\xbf"', b'"\xf4\x90\x80\x80"', b'"\xf5\x80\x80\x80"', b'"\xff"', b'"\xfe"',
    b'"\x80"', b'"\xbf"', b'"\xc3"', b'"\xc3', b'"\xe2\x82"', b'"\xe2\x82', b'"\xf0\x9f\x98"', b'"\xf0\x9f\x98',
    b'"\xc3\xa9\xa9"', b'\xc3\xa9', b'[\xe2\x82\xac]',
]

# bytes con los que se construyen los mutantes: sintaxis de JSON y límites de UTF-8
_INTERESANTES = (b'{}[]:,"\\/bfnrtu0123456789dDaAcCeE+-. \t\n\x00\x1f\x7f'
                 b'\x80\x8f\x90\x9f\xa0\xbf\xc0\xc2\xdf\xe0\xed\xef\xf0\xf4\xf5\xff')


def mutantes(semilla=2026, por_entrada=25):
    """Variantes de ENTRADAS_JSON con uno a tres cambios de un byte (sustituir, insertar, borrar o truncar).
    Es determinista: la misma semilla produce siempre la misma lista."""
    rng, salida = random.Random(semilla), []
    for base in ENTRADAS_JSON:
        if len(base) > 200:
            continue
        for _ in range(por_entrada):
            m = bytearray(base)
            for _ in range(rng.randint(1, 3)):
                i, b = rng.randint(0, len(m)), rng.choice(_INTERESANTES)
                operacion = rng.randrange(4)
                if operacion == 0 and i < len(m):
                    m[i] = b
                elif operacion == 1:
                    m.insert(i, b)
                elif operacion == 2 and i < len(m):
                    del m[i]
                elif operacion == 3:
                    del m[i:]
            salida.append(bytes(m))
    return salida


def particion(rng, n):
    """Tamaños de bloque aleatorios (algunos de cero bytes) para una entrada de n bytes."""
    cortes, resto = [], n
    while resto > 0 and rng.random() < 0.85:
        k = rng.randint(0, min(resto, 6))
        cortes.append(k)
        resto -= k
    return cortes
