"""Pruebas del programa de medición (Fase 6).

No comprueban velocidades, que dependen de la máquina, sino lo que debe cumplirse siempre: el validador
no reserva memoria, su memoria crece solo con la profundidad y su veredicto coincide con el de la línea
base (serde_json + jsonschema). Si falta cargo o llvm-ar, se omiten.
"""
import pytest

from bench import correr as bench


@pytest.fixture(scope='module')
def mediciones():
    try:
        return bench.correr(rapido=True)
    except RuntimeError as e:
        if 'no está instalado' in str(e) or 'falta llvm-ar' in str(e):
            pytest.skip(str(e))
        raise


def serie(mediciones, nombre):
    return [f for f in mediciones[0] if f['serie'] == nombre]


def test_el_validador_no_reserva_memoria(mediciones):
    filas = mediciones[0]
    assert len(filas) >= 9
    for f in filas:
        assert (f['asignaciones'], f['memoria_dinamica']) == (0, 0), f['caso']
        assert f['asignaciones_dom'] > 0 and f['memoria_dinamica_dom'] > 0   # la línea base sí construye el árbol


def test_mismo_veredicto_que_la_linea_base(mediciones):
    for f in mediciones[0]:
        if not f['serie'].startswith('xml'):   # en XML la línea base no valida: solo dice si está bien formado
            assert f['estado'] == f['estado_dom'], f['caso']
    por_caso = {f['caso']: f for f in serie(mediciones, 'comparacion')}
    assert por_caso['registros válidos']['estado'] == 'valido'
    assert por_caso['registros con errores']['estado'] == 'invalido'
    assert por_caso['registros con errores']['violaciones'] == 30   # uno de cada diez de los 300 registros


def test_bombeo_la_memoria_crece_solo_con_la_profundidad(mediciones):
    filas = serie(mediciones, 'bombeo')
    assert [f['profundidad'] for f in filas] == [2 * f['parametro'] + 1 for f in filas]   # k objetos, k arreglos y la hoja
    # memoria = fija + bytes por nivel × profundidad: la pendiente es la misma entre cada par de puntos
    pendientes = {(b['memoria_del_validador'] - a['memoria_del_validador']) / (b['profundidad'] - a['profundidad'])
                  for a, b in zip(filas, filas[1:])}
    assert len(pendientes) == 1 and 16 <= pendientes.pop() <= 128
    # la línea base reserva más cuanto más grande es el documento
    assert [f['asignaciones_dom'] for f in filas] == sorted(f['asignaciones_dom'] for f in filas)


def test_arreglos_largos_con_memoria_constante(mediciones):
    filas = serie(mediciones, 'arreglos')
    assert {f['profundidad'] for f in filas} == {1}
    assert len({f['memoria_del_validador'] for f in filas}) == 1          # no depende de la longitud
    assert filas[-1]['memoria_dinamica_dom'] > filas[0]['memoria_dinamica_dom']


def test_series_de_xml(mediciones):
    validos, con_errores = serie(mediciones, 'xml')
    assert (validos['estado'], validos['violaciones'], validos['estado_dom']) == ('valido', 0, 'bien formado')
    assert (con_errores['estado'], con_errores['violaciones']) == ('invalido', 30)   # uno de cada diez de los 300 pedidos
    filas = serie(mediciones, 'xml-bombeo')
    assert [f['profundidad'] for f in filas] == [f['parametro'] + 2 for f in filas]   # k nodos, la hoja y su <v>
    assert all(f['estado'] == 'valido' for f in filas)
    pendientes = {(b['memoria_del_validador'] - a['memoria_del_validador']) / (b['profundidad'] - a['profundidad'])
                  for a, b in zip(filas, filas[1:])}
    assert len(pendientes) == 1 and 32 <= pendientes.pop() <= 128


def test_carga_sostenida(mediciones):
    s = mediciones[1]['sostenida']
    assert s['documentos'] == s['validos'] > 10
    assert s['asignaciones'] == 0
    assert 0 < s['mb_s_min'] <= s['mb_s_mediana'] <= s['mb_s_max']


def test_tabla_de_texto():
    filas = [{'a': 1, 'bb': 'x'}, {'a': 222, 'bb': 'yy'}]
    assert bench.tabla(filas, ['a', 'bb']).splitlines() == ['a    bb', '1    x ', '222  yy']
