"""Pruebas de las tablas que se exportan a Rust (Fase 1, paso 5)."""
import pytest

from compilador.lexico.afd import construir_json
from compilador.lexico.tablas import DESTINO, emitir_rust, simular, tablas_json
from pruebas.corpus import ENTRADAS_JSON, mutantes

AFD = construir_json()
T = tablas_json()


def test_dimensiones_de_la_tabla_17():
    # 46 estados × (46 clases + ⊣): las acciones de valor no añaden estados ni clases
    assert (len(T.estados), T.n_clases) == (46, 46)
    assert all(len(fila) == 47 for fila in T.delta + T.accion)
    assert len(T.clase) == 256 and set(T.clase) == set(range(46))
    assert T.estados[0] == AFD.inicial
    assert T.acciones[0][1:] == (False, ())  # la acción 0 no hace nada


@pytest.mark.parametrize('datos', ENTRADAS_JSON, ids=repr)
def test_las_tablas_equivalen_al_afd(datos):
    assert simular(T, datos) == AFD.lex(datos)


def test_las_tablas_equivalen_al_afd_en_los_mutantes():
    for datos in mutantes():
        assert simular(T, datos) == AFD.lex(datos), datos


def test_el_archivo_de_rust_esta_al_dia():
    actual = DESTINO.read_bytes().decode('utf-8')
    assert actual == emitir_rust(T), 'ejecuta: python -m compilador.lexico.tablas'
