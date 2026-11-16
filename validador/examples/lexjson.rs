//! Programa auxiliar de las pruebas diferenciales (`pruebas/test_lexer_rust.py`).
//!
//! Lee casos por la entrada estándar, uno por línea, con el formato `HEX:CORTES`:
//! el payload en hexadecimal y los tamaños de los bloques separados por comas (lo que sobre va en
//! un último bloque). Por cada caso escribe una línea con los tokens y el resultado:
//!
//! ```text
//! Tipo,inicio,fin[,valor en hex[,e|r]] ... OK | ERR,pos,motivo
//! ```

use std::fmt::Write as _;
use std::io::{self, BufRead, Write as _};

use validador::{ErrorLexico, Evento, Lexer, TipoError, TipoToken};

fn hex(datos: &[u8]) -> String {
    datos.iter().map(|b| format!("{b:02x}")).collect()
}

fn caso(datos: &[u8], cortes: &[usize]) -> String {
    let mut salida = String::new();
    let mut cadena: Vec<u8> = Vec::new();
    let mut numero = String::new();
    let mut recibir = |evento: Evento<'_>| match evento {
        Evento::InicioCadena => cadena.clear(),
        Evento::Fragmento(bytes) => cadena.extend_from_slice(bytes),
        Evento::Numero { lexema, entero } => {
            numero = format!(",{},{}", hex(lexema), if entero { 'e' } else { 'r' })
        }
        Evento::Token { tipo, inicio, fin } => {
            write!(salida, "{tipo:?},{inicio},{fin}").unwrap();
            match tipo {
                TipoToken::Cadena => write!(salida, ",{}", hex(&cadena)).unwrap(),
                TipoToken::Numero => salida.push_str(&numero),
                _ => {}
            }
            salida.push(' ');
        }
    };

    let mut lexer = Lexer::new();
    let mut resto = datos;
    let mut resultado: Result<(), ErrorLexico> = Ok(());
    for &n in cortes {
        let (bloque, despues) = resto.split_at(n.min(resto.len()));
        resto = despues;
        resultado = lexer.feed(bloque, &mut recibir);
        if resultado.is_err() {
            break;
        }
    }
    if resultado.is_ok() {
        resultado = lexer.feed(resto, &mut recibir);
    }
    if resultado.is_ok() {
        resultado = lexer.finish(&mut recibir);
    }
    match resultado {
        Ok(()) => salida.push_str("OK"),
        Err(e) => {
            let motivo = match e.tipo {
                TipoError::ByteInesperado => "byte",
                TipoError::FinInesperado => "fin",
                TipoError::NumeroDemasiadoLargo => "largo",
                _ => "xml",
            };
            write!(salida, "ERR,{},{motivo}", e.pos).unwrap();
        }
    }
    salida
}

fn main() {
    let mut out = io::BufWriter::new(io::stdout().lock());
    for linea in io::stdin().lock().lines() {
        let linea = linea.expect("entrada ilegible");
        let (payload, cortes) = linea
            .trim_end()
            .split_once(':')
            .expect("formato HEX:CORTES");
        let datos: Vec<u8> = (0..payload.len() / 2)
            .map(|i| {
                u8::from_str_radix(&payload[2 * i..2 * i + 2], 16).expect("hexadecimal inválido")
            })
            .collect();
        let cortes: Vec<usize> = cortes
            .split(',')
            .filter(|s| !s.is_empty())
            .map(|s| s.parse().expect("tamaño de bloque inválido"))
            .collect();
        writeln!(out, "{}", caso(&datos, &cortes)).unwrap();
    }
}
