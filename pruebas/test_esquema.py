"""Pruebas del lexer del esquema (Fase 1, paso 6)."""
import pytest

from compilador.lexico.esquema import PALABRAS_CLAVE, AFDPalabras, tokenizar_esquema
from compilador.lexico.lexer import ErrorLexico

ESQUEMA = b'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "edad": {"type": "integer", "minimum": 0},
    "type": {"enum": ["a", "b"]}
  },
  "required": ["edad"]
}'''


def test_palabras_clave_del_esquema():
    tokens = tokenizar_esquema(ESQUEMA)
    claves = [t.valor for t in tokens if t.tipo == 'PALABRA_CLAVE']
    # el lexer no distingue contexto: el nombre de propiedad "type" también sale como palabra clave
    assert claves == ['$schema', 'type', 'properties', 'type', 'minimum', 'type', 'enum', 'required']
    cadenas = [t.valor for t in tokens if t.tipo == 'CADENA']
    assert cadenas == ['https://json-schema.org/draft/2020-12/schema', 'object', 'edad', 'integer', 'a', 'b', 'edad']
    assert tokens[-1].tipo == 'FIN'


def test_la_palabra_clave_se_reconoce_por_su_valor_decodificado():
    (token, _) = tokenizar_esquema(b'"\\u0074ype"')
    assert (token.tipo, token.valor, token.inicio, token.fin) == ('PALABRA_CLAVE', 'type', 0, 11)


@pytest.mark.parametrize('texto', ['Type', 'typ', 'types', 'type ', '', 'ref', '$Ref', 'minimo'])
def test_cadenas_que_no_son_palabra_clave(texto):
    (token, _) = tokenizar_esquema(f'"{texto}"'.encode())
    assert token.tipo == 'CADENA'


def test_afd_de_palabras():
    afd = AFDPalabras(PALABRAS_CLAVE)
    assert len(set(PALABRAS_CLAVE)) == len(PALABRAS_CLAVE) == 57
    for i, palabra in enumerate(PALABRAS_CLAVE):
        assert afd.reconocer(palabra.encode()) == i
        assert afd.reconocer(palabra.encode()[:-1]) != i   # un prefijo propio no es la palabra
        assert afd.reconocer(palabra.encode() + b'x') is None
    # prefijos compartidos: «min» lleva a minimum, minLength, minItems, minContains y minProperties
    q = 0
    for b in b'min':
        q = afd.paso(q, b)
    assert sorted(afd.delta[q]) == sorted(b'iLICP') and afd.acepta[q] is None


def test_error_lexico_en_el_esquema():
    with pytest.raises(ErrorLexico) as e:
        tokenizar_esquema(b'{"type": 01}')
    assert e.value.pos == 10
