"""Pruebas de los AFD del tokenizador (Fase 1, sección 4 del documento)."""
import pytest

from compilador.lexico.afd import construir_json, construir_xml

J, X = construir_json(), construir_xml()

# (AFD, entrada, byte del error léxico o None si la entrada es válida)
CASOS = [
    (J, b'{"edad": 25, "ok": true}', None), (J, b'[01]', 2), (J, b'[1.]', 3), (J, b'truex', 4),
    (J, b'"a\\qb"', 3), (J, b'"ab', 3), (J, '"café 😀"'.encode(), None), (J, b'-12.5e+3', None),
    (J, b'"\\ud83d\\ude00"', None), (J, b'"\\ud83d"', 7), (J, b'"\\udc00"', 4), (J, b'"\xc0\xaf"', 1),
    (J, b'\xef\xbb\xbf[1]', None), (J, b'[1,\xef\xbb\xbf]', 3), (J, b'"tab\tx"', 4), (J, b'nul', 3),
    (J, b'0.5E-3', None), (J, b'-', 1),
    (X, '<p id="7">Hola &amp; adiós</p>'.encode(), None), (X, b'<a x="1"y="2">', 8), (X, b'<1a>', 1),
    (X, b'a & b', 3), (X, b'<!-- a -- b -->', 9), (X, b'<!DOCTYPE r>', 2),
    (X, b'<?xml version="1.0" encoding="UTF-8"?><r/>', None),
    (X, b"\xef\xbb\xbf<?xml version='1.0' standalone='yes'?><r/>", None),
    (X, b' <?xml version="1.0"?><r/>', 6), (X, b'<?XML version="1.0"?>', 5), (X, b'<?xml-hoja a?><r/>', None),
    (X, b'<r><![CDATA[a<b]]></r>', None), (X, b'<r>a]]>b</r>', 6), (X, b"<r a='x&lt;y' b=\"&#x41;&#65;\"/>", None),
    (X, b'<r>\xef\xbf\xbe</r>', 5), (X, b'<a>&nbsp;</a>', 4), (X, '<año mes="ene">x</año>'.encode(), None),
    (X, b'<r><!-- ok --->', 13), (X, b'<a\x01>', 2), (X, b'<a b="<">', 6),
]


def _id(caso):
    afd, entrada, _ = caso
    return f"{'json' if afd is J else 'xml'}:{entrada!r}"


@pytest.mark.parametrize('afd, entrada, error', CASOS, ids=[_id(c) for c in CASOS])
def test_posicion_del_error(afd, entrada, error):
    _, obtenido = afd.lex(entrada)
    assert obtenido == error


def test_tokens_json_tabla_11():
    tokens, error = J.lex(b'{"edad": 25, "ok": true}')
    assert error is None
    assert tokens == [
        ('LLAVE_ABRE', (0, 1)), ('CADENA', (1, 7)), ('DOS_PUNTOS', (7, 8)), ('NUMERO', (9, 11)),
        ('COMA', (11, 12)), ('CADENA', (13, 17)), ('DOS_PUNTOS', (17, 18)), ('TRUE', (19, 23)),
        ('LLAVE_CIERRA', (23, 24)), ('FIN', (24, 24)),
    ]


def test_tokens_xml_tabla_11():
    tokens, error = X.lex('<p id="7">Hola &amp; adiós</p>'.encode())
    assert error is None
    assert tokens == [
        ('ETIQ_ABRE', (0, 1)), ('NOMBRE', (1, 2)), ('NOMBRE', (3, 5)), ('IGUAL', (5, 6)),
        ('VALOR_ATRIB', (6, 9)), ('ETIQ_FIN', (9, 10)), ('TEXTO', (10, 27)), ('ETIQ_CIERRE_ABRE', (27, 29)),
        ('NOMBRE', (29, 30)), ('ETIQ_FIN', (30, 31)), ('FIN', (31, 31)),
    ]


@pytest.mark.parametrize('afd, estados, minimo, clases, transiciones', [
    (J, 46, 46, 46, 856),
    (X, 298, 242, 66, 9531),
], ids=['json', 'xml'])
def test_cifras_tabla_17(afd, estados, minimo, clases, transiciones):
    r = afd.resumen()
    assert (r['estados'], r['minimo'], r['clases'], r['transiciones']) == (estados, minimo, clases, transiciones)
