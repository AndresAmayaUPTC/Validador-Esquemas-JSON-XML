"""Exporta el AFD del tokenizador a tablas densas para el validador de Rust (Fase 1, paso 5).

    python -m compilador.lexico.tablas      # reescribe validador/src/tablas_json.rs

Pasos: minimizar el transductor (con sus acciones de valor), numerar los estados, agrupar los bytes en
clases de equivalencia y escribir tres tablas: byte → clase, δ y acción. Es la «tabla densa» de la
Tabla 17 del documento: estados × (clases + ⊣), un byte por celda.
"""
from dataclasses import dataclass
from pathlib import Path

from .afd import EOF, construir_json
from .lexer import LITERAL, NADA, NOMBRES_OP, NUMERO, acciones_json

ERROR = 0xFF  # celda de δ sin transición
MODOS = {'prev': 'Previo', 'close': 'Cierre', 'single': 'Unico', 'eof': 'Fin'}
DESTINO = Path(__file__).resolve().parents[2] / 'validador' / 'src' / 'tablas_json.rs'


@dataclass
class Tablas:
    estados: list    # nombre en afd.py del estado que representa a cada número de estado
    clase: list      # 256 entradas: byte → clase
    n_clases: int
    delta: list      # [estado][columna] → estado siguiente o None; la última columna es ⊣
    accion: list     # [estado][columna] → índice en acciones
    acciones: list   # (op, marca, salidas); la 0 es «no hacer nada»


def compilar(afd, op, nada=NADA, error=ERROR) -> Tablas:
    bloque = afd.minimizar(op)
    representante = {}
    for s in afd.delta:
        representante.setdefault(bloque[s], s)
    # numeración por anchura desde el estado inicial, que queda como 0
    numero, cola = {bloque[afd.inicial]: 0}, [bloque[afd.inicial]]
    for b in cola:
        for t in afd.delta[representante[b]]:
            if t is not None and bloque[t[0]] not in numero:
                numero[bloque[t[0]]] = len(numero)
                cola.append(bloque[t[0]])
    assert len(numero) == len(representante), 'el AFD tiene estados inalcanzables'
    assert len(numero) < error, 'el estado no cabe en una celda'
    estados = [representante[b] for b in cola]

    acciones, filas = [(nada, False, ())], []
    for s in estados:
        fila = []
        for sym, t in enumerate(afd.delta[s]):
            if t is None:
                fila.append((None, 0))
                continue
            a = (op(s, sym), t[2], t[1])
            assert len(a[2]) <= 2, 'una transición emite más de dos tokens'
            if a not in acciones:
                acciones.append(a)
            fila.append((numero[bloque[t[0]]], acciones.index(a)))
        filas.append(fila)
    assert len(acciones) <= 256

    # clases de equivalencia: bytes con la misma columna en todas las filas
    columnas, clase = {}, []
    for byte in range(256):
        clase.append(columnas.setdefault(tuple(f[byte] for f in filas), len(columnas)))
    n = len(columnas)
    simbolos = [clase.index(k) for k in range(n)] + [EOF]  # un byte por clase, y ⊣
    return Tablas(estados, clase, n,
                  [[f[c][0] for c in simbolos] for f in filas],
                  [[f[c][1] for c in simbolos] for f in filas], acciones)


def simular(t: Tablas, datos: bytes):
    """Ejecuta las tablas igual que AFD.lex: devuelve (tokens, posición del error o None)."""
    s, ini, toks = 0, 0, []
    for pos, sym in enumerate(list(datos) + [EOF]):
        col = t.n_clases if sym == EOF else t.clase[sym]
        if t.delta[s][col] is None:
            return toks, pos
        _, marca, salidas = t.acciones[t.accion[s][col]]
        for tok, modo in salidas:
            toks.append((tok, {'prev': (ini, pos), 'close': (ini, pos + 1),
                               'single': (pos, pos + 1), 'eof': (pos, pos)}[modo]))
        if marca:
            ini = pos
        s = t.delta[s][col]
    return toks, None


def camel(nombre):
    """LLAVE_ABRE → LlaveAbre (nombre de la variante de TipoToken en Rust)."""
    return ''.join(p.capitalize() for p in nombre.split('_'))


def _caminos_rapidos(t: Tablas):
    """(acción «literal puro», estado «dentro de una cadena»), para los caminos rápidos del lexer de Rust.

    El estado de cadena es el que, con cualquier byte ASCII imprimible distinto de la comilla y la barra
    invertida, se queda donde está y solo copia el byte: ahí el motor puede saltar texto de 8 en 8 bytes."""
    literal = t.acciones.index((LITERAL, False, ()))
    corrientes = {b for b in range(0x20, 0x80) if b not in (0x22, 0x5C)}
    estados = [s for s in range(len(t.estados))
               if {b for b in range(256) if t.delta[s][t.clase[b]] == s and t.accion[s][t.clase[b]] == literal} == corrientes]
    assert len(estados) == 1, 'no se pudo identificar el estado de cadena'
    return literal, estados[0], t.acciones.index((NUMERO, False, ()))


def emitir_rust(t: Tablas) -> str:
    cols = t.n_clases + 1
    literal, estado_cadena, digito = _caminos_rapidos(t)
    fila = lambda xs: ' '.join(f'{x:3},' for x in xs)
    tabla = lambda xs, ancho: '\n'.join('    ' + fila(xs[i:i + ancho]) for i in range(0, len(xs), ancho))
    matriz = lambda m, f: '\n'.join(f'    {fila(f(x) for x in r)} // {i} {t.estados[i]}' for i, r in enumerate(m))

    def accion(a):
        op, marca, salidas = a
        s = [f'Some(Salida {{ tipo: TipoToken::{camel(tok)}, modo: Modo::{MODOS[modo]} }})' for tok, modo in salidas]
        s += ['None'] * (2 - len(s))
        return f'    Accion {{ op: Op::{NOMBRES_OP[op]}, marca: {str(marca).lower()}, salidas: [{", ".join(s)}] }},'

    return f'''// Generado por `python -m compilador.lexico.tablas` a partir de compilador/lexico/afd.py. No editar a mano.
//
// AFD minimizado del tokenizador de JSON: {len(t.estados)} estados × ({t.n_clases} clases de bytes + ⊣).

use crate::lexer::{{Accion, Modo, Op, Salida, TipoToken}};

pub const N_ESTADOS: usize = {len(t.estados)};
/// Columnas de δ: una por clase de bytes y la última para el fin de entrada ⊣.
pub const N_COLUMNAS: usize = {cols};
pub const COLUMNA_FIN: usize = {t.n_clases};
pub const INICIAL: u8 = 0;
/// Valor de δ cuando no hay transición.
pub const ERROR: u8 = {ERROR};
/// Acción «el byte pasa tal cual al valor de la cadena», sin emitir nada más. La acción 0 no hace nada.
pub const LITERAL: u8 = {literal};
/// Estado «dentro de una cadena»: con ASCII imprimible distinto de `"` y `\` se queda en él.
pub const ESTADO_CADENA: u8 = {estado_cadena};
/// Acción «el byte se añade al lexema del número en curso», sin emitir nada más.
pub const DIGITO: u8 = {digito};

/// Byte → clase de equivalencia.
pub static CLASE: [u8; 256] = [
{tabla(t.clase, 16)}
];

/// δ[estado * N_COLUMNAS + columna] = estado siguiente. Una fila por estado.
pub static DELTA: [u8; N_ESTADOS * N_COLUMNAS] = [
{matriz(t.delta, lambda x: ERROR if x is None else x)}
];

/// ACCION[estado * N_COLUMNAS + columna] = índice en ACCIONES.
pub static ACCION: [u8; N_ESTADOS * N_COLUMNAS] = [
{matriz(t.accion, lambda x: x)}
];

pub static ACCIONES: [Accion; {len(t.acciones)}] = [
{chr(10).join(accion(a) for a in t.acciones)}
];
'''


def tablas_json() -> Tablas:
    afd = construir_json()
    return compilar(afd, acciones_json(afd))


if __name__ == '__main__':
    t = tablas_json()
    DESTINO.write_text(emitir_rust(t), encoding='utf-8', newline='\n')
    print(f'{DESTINO.name}: {len(t.estados)} estados, {t.n_clases} clases, {len(t.acciones)} acciones')
