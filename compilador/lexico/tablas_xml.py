"""Exporta el AFD del tokenizador de XML a tablas densas para el validador de Rust.

    python -m compilador.lexico.tablas_xml      # reescribe validador/src/tablas_xml.rs

Mismos pasos que tablas.py para JSON. Con las acciones de valor el transductor mínimo tiene más de 255
estados, así que cada celda de δ ocupa 16 bits.
"""
from pathlib import Path

from .afd import construir_xml
from .lexer_xml import acciones_xml
from .tablas import MODOS, Tablas, camel, compilar

ERROR = 0xFFFF
NADA = ('nada',)
DESTINO = Path(__file__).resolve().parents[2] / 'validador' / 'src' / 'tablas_xml.rs'
# bytes que el lexer de Rust salta de ocho en ocho dentro del texto: ASCII imprimible salvo «<», «&» y «]»
CORRIENTES = [b for b in range(0x20, 0x80) if b not in b'<&]']


def tablas_xml() -> Tablas:
    afd = construir_xml()
    return compilar(afd, acciones_xml(afd), nada=NADA, error=ERROR)


def _op(op):
    clase = op[0]
    if clase in ('texto', 'nombre'):
        return f'OpXml::{clase.capitalize()}({op[1]})'
    if clase == 'ref_num':
        return f'OpXml::RefNum({str(op[1] == "texto").lower()})'
    if clase == 'entidad':
        return f'OpXml::Entidad({str(op[1] == "texto").lower()}, {op[2]})'
    return 'OpXml::' + camel(clase.upper())


def caminos_rapidos(t: Tablas):
    """(acción «byte de texto y nada más», estado «dentro del texto»)."""
    puro = t.acciones.index((('texto', 0), False, ()))
    estados = [s for s in range(len(t.estados))
               if all(t.delta[s][t.clase[b]] == s and t.accion[s][t.clase[b]] == puro for b in CORRIENTES)]
    assert len(estados) == 1, 'no se pudo identificar el estado de texto'
    return puro, estados[0]


def emitir_rust(t: Tablas) -> str:
    cols = t.n_clases + 1
    puro, estado_texto = caminos_rapidos(t)
    fila = lambda xs, ancho: ' '.join(f'{x:{ancho}},' for x in xs)
    tabla = lambda xs, n: '\n'.join('    ' + fila(xs[i:i + n], 3) for i in range(0, len(xs), n))
    matriz = lambda m, f, ancho: '\n'.join(f'    {fila((f(x) for x in r), ancho)} // {i} {t.estados[i]}' for i, r in enumerate(m))

    def accion(a):
        op, marca, salidas = a
        s = [f'Some(SalidaXml {{ tipo: TipoXml::{camel(tok)}, modo: Modo::{MODOS[modo]} }})' for tok, modo in salidas]
        s += ['None'] * (2 - len(s))
        return f'    AccionXml {{ op: {_op(op)}, marca: {str(marca).lower()}, salidas: [{", ".join(s)}] }},'

    return f'''// Generado por `python -m compilador.lexico.tablas_xml` a partir de compilador/lexico/afd.py. No editar a mano.
//
// AFD minimizado del tokenizador de XML: {len(t.estados)} estados × ({t.n_clases} clases de bytes + ⊣).

use crate::lexer::Modo;
use crate::xml::{{AccionXml, OpXml, SalidaXml, TipoXml}};

pub const N_ESTADOS: usize = {len(t.estados)};
/// Columnas de δ: una por clase de bytes y la última para el fin de entrada ⊣.
pub const N_COLUMNAS: usize = {cols};
pub const COLUMNA_FIN: usize = {t.n_clases};
pub const INICIAL: u16 = 0;
/// Valor de δ cuando no hay transición.
pub const ERROR: u16 = {ERROR};
/// Acción «el byte es texto», sin emitir nada más. La acción 0 no hace nada.
pub const TEXTO_PURO: u8 = {puro};
/// Estado «dentro del texto»: con ASCII imprimible distinto de `<`, `&` y `]` se queda en él.
pub const ESTADO_TEXTO: u16 = {estado_texto};

/// Byte → clase de equivalencia.
pub static CLASE: [u8; 256] = [
{tabla(t.clase, 16)}
];

/// δ[estado * N_COLUMNAS + columna] = estado siguiente. Una fila por estado.
pub static DELTA: [u16; N_ESTADOS * N_COLUMNAS] = [
{matriz(t.delta, lambda x: ERROR if x is None else x, 5)}
];

/// ACCION[estado * N_COLUMNAS + columna] = índice en ACCIONES.
pub static ACCION: [u8; N_ESTADOS * N_COLUMNAS] = [
{matriz(t.accion, lambda x: x, 2)}
];

pub static ACCIONES: [AccionXml; {len(t.acciones)}] = [
{chr(10).join(accion(a) for a in t.acciones)}
];
'''


if __name__ == '__main__':
    t = tablas_xml()
    DESTINO.write_text(emitir_rust(t), encoding='utf-8', newline='\n')
    print(f'{DESTINO.name}: {len(t.estados)} estados, {t.n_clases} clases, {len(t.acciones)} acciones')
