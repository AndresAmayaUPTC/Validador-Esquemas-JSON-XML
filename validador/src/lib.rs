//! Motor de validación en streaming: ejecuta las tablas que emite el compilador
//! (AFD léxico y autómata de pila ponderado) sobre los bytes del payload.
//!
//! No usa la biblioteca estándar ni asignación dinámica: el crate es `no_std` y no enlaza `alloc`,
//! así que el compilador garantiza que ninguna ruta de código puede reservar memoria.
#![cfg_attr(not(test), no_std)]
#![forbid(unsafe_code)]

pub mod automata;
pub mod decimal;
pub mod huellas;
pub mod lexer;
#[rustfmt::skip]
mod tablas_json;
#[rustfmt::skip]
mod tablas_xml;
pub mod validador;
pub mod validador_xml;
pub mod xml;

pub use automata::Automata;
pub use lexer::{ErrorLexico, Evento, Lexer, TipoError, TipoToken};
pub use validador::{Estado, Fallo, Motivo, Resultado, Validador};
pub use validador_xml::ValidadorXml;
pub use xml::{EventoXml, LexerXml, TipoXml};
