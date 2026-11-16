//! Programa auxiliar de las pruebas diferenciales del lexer de XML (`pruebas/test_lexer_xml_rust.py`).
//!
//! Mismo protocolo que `lexjson`: lee casos `HEX:CORTES` por la entrada estándar y por cada uno
//! escribe una línea con los tokens y el resultado:
//!
//! ```text
//! Tipo,inicio,fin[,valor en hex] ... OK | ERR,pos,motivo
//! ```

use std::fmt::Write as _;
use std::io::{self, BufRead, Write as _};

use validador::{ErrorLexico, EventoXml, LexerXml, TipoError, TipoXml};

fn hex(datos: &[u8]) -> String {
    datos.iter().map(|b| format!("{b:02x}")).collect()
}

fn caso(datos: &[u8], cortes: &[usize]) -> String {
    let mut salida = String::new();
    // un búfer por clase de valor: el texto se va llenando mientras llegan otros tokens
    let (mut nombre, mut valor, mut texto) = (Vec::new(), Vec::new(), Vec::new());
    let mut destino = 0u8;
    let mut recibir = |evento: EventoXml<'_>| match evento {
        EventoXml::InicioNombre => {
            nombre.clear();
            destino = 0;
        }
        EventoXml::InicioValor => {
            valor.clear();
            destino = 1;
        }
        EventoXml::InicioTexto => {
            texto.clear();
            destino = 2;
        }
        EventoXml::Fragmento(bytes) => match destino {
            0 => nombre.extend_from_slice(bytes),
            1 => valor.extend_from_slice(bytes),
            _ => texto.extend_from_slice(bytes),
        },
        EventoXml::Token { tipo, inicio, fin } => {
            write!(salida, "{tipo:?},{inicio},{fin}").unwrap();
            match tipo {
                TipoXml::Nombre => write!(salida, ",{}", hex(&nombre)).unwrap(),
                TipoXml::ValorAtrib => write!(salida, ",{}", hex(&valor)).unwrap(),
                TipoXml::Texto => write!(salida, ",{}", hex(&texto)).unwrap(),
                _ => {}
            }
            salida.push(' ');
            // tras una etiqueta, lo que llegue sin `Inicio` es la continuación del texto
            destino = 2;
        }
    };

    let mut lexer = LexerXml::new();
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
                TipoError::NombreInvalido => "nombre",
                TipoError::ReferenciaInvalida => "referencia",
                TipoError::NombreDemasiadoLargo => "nombre-largo",
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
