"""Generación del validador nativo (Fase 5): un crate de Rust por esquema, sea JSON Schema o XSD.

    carpeta/
    ├─ Cargo.toml
    ├─ src/tablas.rs          las tablas del autómata (generado)
    ├─ src/lib.rs             API de Rust y API en C (C-ABI) sobre el motor
    ├─ include/validador.h    cabecera para usar la biblioteca desde C
    ├─ tests/casos.rs         casos generados desde el esquema, con su resultado esperado
    └─ motor/                 copia del motor, si se pide un crate autocontenido

El crate se compila como biblioteca de Rust y como biblioteca dinámica; con
`cargo build --release --target wasm32-unknown-unknown` sale además un módulo WebAssembly con la
misma API en C.
"""
import shutil
from pathlib import Path

from ..automata.compilar import Compilado
from .tablas import aplanar, emitir_rust

MOTOR = Path(__file__).resolve().parents[2] / 'validador'

ESTADOS = ('valido', 'pendiente', 'invalido', 'malformado', 'limite')
# el orden es el de los enum Motivo y Fallo del motor; los de XML van al final
MOTIVOS = ('no_admitido', 'required', 'dependentRequired', 'minProperties', 'maxProperties', 'minItems', 'maxItems',
           'entero', 'min', 'max', 'long_min', 'long_max', 'enum_num', 'enum_cad',
           'xs_entero', 'xs_decimal', 'xs_booleano', 'elemento_no_permitido', 'atributo_no_declarado',
           'atributo_requerido', 'texto_no_permitido', 'contenido_incompleto')
FALLOS = ('lexico-byte', 'lexico-fin', 'lexico-largo', 'sintactico', 'clave-repetida', 'profundidad', 'tamano', 'tiempo',
          'lexico-nombre', 'lexico-referencia', 'lexico-nombre-largo', 'atributo-repetido', 'numero-largo')

_CARGO = '''[package]
name = "{nombre}"
version = "0.1.0"
edition = "2024"
description = "Validador generado a partir de un esquema {formato}"

[lib]
crate-type = ["rlib", "cdylib"]

[dependencies]
validador = {{ path = "{motor}" }}

[profile.release]
opt-level = 3
lto = true
panic = "abort"
'''

_LIB = '''//! Validador generado a partir de un esquema {formato}. No editar a mano.
//!
//! Toda la lógica está en el motor (`validador`), que es `no_std` y no reserva memoria; este crate
//! solo aporta las tablas del autómata y dos envoltorios: la API de Rust y la API en C.

#[rustfmt::skip]
mod tablas;

pub use validador::{{Estado, Fallo, Motivo, Resultado}};

/// Profundidad máxima de la pila: contenedores (o elementos) abiertos a la vez.
pub const PROFUNDIDAD: usize = {profundidad};

/// Validador de este esquema. Su tamaño es fijo: no reserva memoria al validar.
pub type Validador = validador::{motor}<'static, PROFUNDIDAD, {{ tablas::PALABRAS }}>;

/// Un validador recién iniciado.
pub fn nuevo() -> Validador {{
    Validador::new(&tablas::AUTOMATA)
}}

/// Valida un documento que ya está completo en memoria.
pub fn validar(datos: &[u8]) -> Resultado {{
    let mut v = nuevo();
    v.feed(datos);
    v.finish()
}}

/// Como `validar`, pero abandona si se agota el plazo. El tiempo se mira cada 64 KiB.
pub fn validar_con_plazo(datos: &[u8], plazo: std::time::Duration) -> Resultado {{
    let inicio = std::time::Instant::now();
    let mut v = nuevo();
    for bloque in datos.chunks(64 * 1024) {{
        if inicio.elapsed() >= plazo {{
            v.agotar_tiempo();
            break;
        }}
        v.feed(bloque);
    }}
    v.finish()
}}

// ---------------------------------------------------------------------------------------------
// API en C. El llamador aporta la memoria del validador: `pdv_tamano()` bytes con la alineación
// `pdv_alineacion()`. Ver include/validador.h.
// ---------------------------------------------------------------------------------------------

/// Sin posición.
pub const PDV_SIN_POSICION: u64 = u64::MAX;

#[repr(C)]
pub struct PdvResultado {{
    /// 0 válido, 1 pendiente, 2 inválido, 3 mal formado, 4 límite de profundidad.
    pub estado: u32,
    pub violaciones: u32,
    pub pendientes: u32,
    pub profundidad: u32,
    /// Byte de la primera violación, o del error si el estado es 3 o 4.
    pub posicion: u64,
    /// Motivo de la primera violación (1..) o causa del error (101..); 0 si no hay.
    pub motivo: u32,
}}

#[unsafe(no_mangle)]
pub extern "C" fn pdv_tamano() -> usize {{
    core::mem::size_of::<Validador>()
}}

#[unsafe(no_mangle)]
pub extern "C" fn pdv_alineacion() -> usize {{
    core::mem::align_of::<Validador>()
}}

/// Inicia un validador en la memoria apuntada por `v`. `max_bytes` = 0 significa sin límite de tamaño.
///
/// # Safety
/// `v` debe apuntar a `pdv_tamano()` bytes escribibles y alineados a `pdv_alineacion()`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pdv_iniciar(v: *mut Validador, profundidad: usize, fallo_temprano: u32, max_bytes: u64) {{
    let limite = if max_bytes == 0 {{ u64::MAX }} else {{ max_bytes }};
    let nuevo = nuevo().con_profundidad(profundidad).con_fallo_temprano(fallo_temprano != 0).con_limite_de_bytes(limite);
    unsafe {{ v.write(nuevo) }}
}}

/// Detiene la validación por tiempo: la llama quien mide el plazo entre bloque y bloque.
///
/// # Safety
/// `v` debe venir de `pdv_iniciar`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pdv_agotar_tiempo(v: *mut Validador) {{
    unsafe {{ (*v).agotar_tiempo() }}
}}

/// Copia en `destino` la ruta (UTF-8) del error o de la primera violación y devuelve
/// su longitud; si no cabe en `capacidad`, no copia nada. Se llama después de `pdv_finish`.
///
/// # Safety
/// `v` debe venir de `pdv_iniciar` y `destino` apuntar a `capacidad` bytes escribibles.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pdv_ruta(v: *mut Validador, destino: *mut u8, capacidad: usize) -> usize {{
    let resultado = unsafe {{ (*v).finish() }};
    let ruta = resultado.ruta.as_bytes();
    if ruta.len() <= capacidad && !ruta.is_empty() {{
        unsafe {{ core::ptr::copy_nonoverlapping(ruta.as_ptr(), destino, ruta.len()) }}
    }}
    ruta.len()
}}

/// Entrega un bloque del documento.
///
/// # Safety
/// `v` debe venir de `pdv_iniciar` y `datos` apuntar a `n` bytes legibles.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pdv_feed(v: *mut Validador, datos: *const u8, n: usize) {{
    if n > 0 {{
        unsafe {{ (*v).feed(core::slice::from_raw_parts(datos, n)) }}
    }}
}}

/// Cierra el documento y escribe el veredicto en `r`.
///
/// # Safety
/// `v` debe venir de `pdv_iniciar` y `r` apuntar a un `PdvResultado` escribible.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pdv_finish(v: *mut Validador, r: *mut PdvResultado) {{
    let resultado = unsafe {{ (*v).finish() }};
    let (posicion, motivo) = match (resultado.error, resultado.primera) {{
        (Some((pos, fallo)), _) => (pos, 101 + codigo_de_fallo(fallo)),
        (None, Some((pos, motivo))) => (pos, 1 + motivo as u32),
        (None, None) => (PDV_SIN_POSICION, 0),
    }};
    let salida = PdvResultado {{
        estado: resultado.estado as u32,
        violaciones: resultado.violaciones,
        pendientes: resultado.pendientes,
        profundidad: resultado.profundidad,
        posicion,
        motivo,
    }};
    unsafe {{ r.write(salida) }}
}}

fn codigo_de_fallo(fallo: Fallo) -> u32 {{
    use validador::TipoError::*;
    match fallo {{
        Fallo::Lexico(ByteInesperado) => 0,
        Fallo::Lexico(FinInesperado) => 1,
        Fallo::Lexico(NumeroDemasiadoLargo) => 2,
        Fallo::Sintactico => 3,
        Fallo::ClaveRepetida => 4,
        Fallo::Profundidad => 5,
        Fallo::Tamano => 6,
        Fallo::Tiempo => 7,
        Fallo::Lexico(NombreInvalido) => 8,
        Fallo::Lexico(ReferenciaInvalida) => 9,
        Fallo::Lexico(NombreDemasiadoLargo) => 10,
        Fallo::AtributoRepetido => 11,
        Fallo::NumeroLargo => 12,
    }}
}}
'''

_CABECERA = '''/* Validador generado a partir de un esquema {formato}. API en C.
 *
 * El validador no reserva memoria: el llamador aporta pdv_tamano() bytes alineados a
 * pdv_alineacion() y los pasa a todas las funciones.
 *
 *     void *v = aligned_alloc(pdv_alineacion(), pdv_tamano());
 *     pdv_iniciar(v, 128, 0, 0);       // profundidad máxima, fallo temprano, tamaño máximo (0 = sin límite)
 *     pdv_feed(v, bloque, n);          // una vez por cada bloque del documento
 *     pdv_resultado r;
 *     pdv_finish(v, &r);
 *     char ruta[PDV_RUTA_MAX];
 *     size_t n_ruta = pdv_ruta(v, (uint8_t *)ruta, sizeof ruta);   // ruta del error
 */
#ifndef VALIDADOR_H
#define VALIDADOR_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {{
#endif

enum {{ {estados} }};

/* motivo de la primera violación */
enum {{ {motivos} }};

/* causa del error cuando el estado es PDV_MALFORMADO o PDV_LIMITE */
enum {{ {fallos} }};

#define PDV_SIN_POSICION UINT64_MAX
#define PDV_RUTA_MAX 256

typedef struct {{
    uint32_t estado;
    uint32_t violaciones;   /* peso de la ejecución: número de violaciones */
    uint32_t pendientes;    /* comprobaciones en verificación diferida que se cruzaron */
    uint32_t profundidad;   /* máximo de contenedores abiertos a la vez */
    uint64_t posicion;      /* byte de la primera violación o del error */
    uint32_t motivo;
}} pdv_resultado;

size_t pdv_tamano(void);
size_t pdv_alineacion(void);
void pdv_iniciar(void *validador, size_t profundidad, uint32_t fallo_temprano, uint64_t max_bytes);
void pdv_feed(void *validador, const uint8_t *datos, size_t n);
void pdv_agotar_tiempo(void *validador);
void pdv_finish(void *validador, pdv_resultado *resultado);
size_t pdv_ruta(void *validador, uint8_t *destino, size_t capacidad);

#ifdef __cplusplus
}}
#endif

#endif
'''

_PRUEBAS = '''//! Casos generados desde el esquema. El resultado esperado lo calculó el intérprete de referencia
//! del compilador; aquí se comprueba que el validador nativo da lo mismo.

use {crate_}::{{nuevo, validar, Estado}};

/// (payload, estado esperado, violaciones esperadas, descripción)
#[rustfmt::skip]
const CASOS: &[(&[u8], Estado, u32, &str)] = &[
{casos}
];

#[test]
fn casos_generados() {{
    for (payload, estado, violaciones, descripcion) in CASOS {{
        let r = validar(payload);
        assert_eq!((r.estado, r.violaciones), (*estado, *violaciones), "{{descripcion}}: {{}}", String::from_utf8_lossy(payload));
    }}
}}

#[test]
fn byte_a_byte_da_lo_mismo() {{
    for (payload, ..) in CASOS {{
        let mut v = nuevo();
        for b in payload.iter() {{
            v.feed(core::slice::from_ref(b));
        }}
        assert_eq!(v.finish(), validar(payload));
    }}
}}
'''


def _literal_de_bytes(datos: bytes) -> str:
    """Literal b"..." de Rust."""
    partes = []
    for b in datos:
        if b in (0x22, 0x5C):
            partes.append('\\' + chr(b))
        elif 0x20 <= b < 0x7F:
            partes.append(chr(b))
        else:
            partes.append(f'\\x{b:02x}')
    return 'b"' + ''.join(partes) + '"'


def _literal_de_texto(texto: str) -> str:
    return '"' + ''.join(c if c.isascii() and c.isprintable() and c not in '"\\{}' else f'\\u{{{ord(c):x}}}' for c in texto) + '"'


def generar_crate(compilado: Compilado, destino, nombre='validador_generado', profundidad=128, casos=(),
                  autocontenido=False, origen=''):
    """Escribe en la carpeta destino el crate del validador de este esquema y devuelve la ruta."""
    destino = Path(destino)
    (destino / 'src').mkdir(parents=True, exist_ok=True)
    (destino / 'include').mkdir(exist_ok=True)
    motor = MOTOR.as_posix()
    if autocontenido:
        copia = destino / 'motor'
        if copia.exists():
            shutil.rmtree(copia)
        shutil.copytree(MOTOR, copia, ignore=shutil.ignore_patterns('target', '.cargo', 'examples', 'tests', 'Cargo.lock'))
        motor = 'motor'
    escribir = lambda ruta, texto: (destino / ruta).write_text(texto, encoding='utf-8', newline='\n')
    tablas = aplanar(compilado.automata)
    formato = 'XSD' if tablas.xml else 'JSON'
    escribir('Cargo.toml', _CARGO.format(nombre=nombre, motor=motor, formato=formato))
    escribir('src/tablas.rs', emitir_rust(tablas, origen))
    escribir('src/lib.rs', _LIB.format(profundidad=profundidad, formato=formato,
                                       motor='ValidadorXml' if tablas.xml else 'Validador'))
    enumerar = lambda prefijo, nombres, base: ', '.join(
        f'PDV_{prefijo}{n.upper().replace("-", "_")} = {base + i}' for i, n in enumerate(nombres))
    escribir('include/validador.h', _CABECERA.format(
        formato=formato, estados=enumerar('', ESTADOS, 0), motivos=enumerar('MOTIVO_', MOTIVOS, 1), fallos=enumerar('FALLO_', FALLOS, 101)))
    if casos:
        (destino / 'tests').mkdir(exist_ok=True)
        filas = '\n'.join(f'    ({_literal_de_bytes(c.payload)}, Estado::{c.estado.capitalize()}, {c.violaciones}, '
                          f'{_literal_de_texto(c.clase + ": " + c.descripcion)}),' for c in casos)
        escribir('tests/casos.rs', _PRUEBAS.format(crate_=nombre, casos=filas))
    return destino
