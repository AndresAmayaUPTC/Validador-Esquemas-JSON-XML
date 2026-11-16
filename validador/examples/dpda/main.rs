//! Programa auxiliar de las pruebas diferenciales (`pruebas/test_motor_rust.py`).
//!
//! Carga autómatas en el formato de texto de `compilador/generador/tablas.py::serializar` y valida
//! documentos con ellos, todo por la entrada estándar:
//!
//! ```text
//! automata [xml]
//! cabecera N_ESTADOS INICIAL ACEPTA PILA_INICIAL PALABRAS
//! delta ...            (una línea por tabla)
//! fin
//! caso HEX CORTES PROFUNDIDAD FALLO_TEMPRANO [MAX_BYTES|-] [T]
//! ```
//!
//! Por cada caso escribe una línea:
//! `estado violaciones pendientes profundidad pos_primera motivo pos_error fallo ruta_en_hex`
//! (con `-` donde no hay dato). `T` simula que el plazo ya venció antes del primer bloque.

use std::io::{self, BufRead, Write as _};

use validador::automata::Automata;
use validador::{Resultado, Validador, ValidadorXml};

mod cargar;
use cargar::Tablas;

/// Capacidad del validador de pruebas: 256 niveles y máscaras de hasta 64 × 64 = 4096 propiedades.
const D: usize = 256;
const W: usize = 64;

fn caso(automata: &Automata<'_>, xml: bool, campos: &[&str]) -> String {
    let payload = campos[0].trim_matches('.');
    let datos: Vec<u8> = (0..payload.len() / 2)
        .map(|i| u8::from_str_radix(&payload[2 * i..2 * i + 2], 16).expect("hex"))
        .collect();
    let cortes: Vec<usize> = campos[1]
        .split(',')
        .filter(|s| !s.is_empty() && *s != "-")
        .map(|s| s.parse().unwrap())
        .collect();
    let profundidad: usize = campos[2].parse().unwrap();

    let max_bytes = campos
        .get(4)
        .and_then(|s| s.parse().ok())
        .unwrap_or(u64::MAX);
    // El validador ocupa bastante con estas capacidades; se deja en el montón para no cargar la pila.
    macro_rules! correr {
        ($tipo:ident) => {{
            let mut v = Box::new(
                $tipo::<D, W>::new(automata)
                    .con_profundidad(profundidad)
                    .con_fallo_temprano(campos[3] == "1")
                    .con_limite_de_bytes(max_bytes),
            );
            if campos.get(5) == Some(&"T") {
                v.agotar_tiempo();
            }
            let mut resto = &datos[..];
            for &n in &cortes {
                let (bloque, despues) = resto.split_at(n.min(resto.len()));
                v.feed(bloque);
                resto = despues;
            }
            v.feed(resto);
            v.finish()
        }};
    }
    let r: Resultado = if xml {
        correr!(ValidadorXml)
    } else {
        correr!(Validador)
    };
    let par = |p: Option<(u64, &str)>| p.map_or("- -".to_string(), |(pos, n)| format!("{pos} {n}"));
    let ruta = if r.primera.is_none() && r.error.is_none() {
        "-".to_string()
    } else {
        format!(
            ".{}",
            r.ruta
                .as_bytes()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        )
    };
    format!(
        "{} {} {} {} {} {} {}",
        r.estado.nombre(),
        r.violaciones,
        r.pendientes,
        r.profundidad,
        par(r.primera.map(|(p, m)| (p, m.nombre()))),
        par(r.error.map(|(p, f)| (p, f.nombre()))),
        ruta,
    )
}

fn main() {
    let mut out = io::BufWriter::new(io::stdout().lock());
    let mut tablas = Tablas::default();
    let mut xml = false;
    for linea in io::stdin().lock().lines() {
        let linea = linea.expect("entrada ilegible");
        let mut campos = linea.split_ascii_whitespace();
        match campos.next() {
            None | Some("fin") => {}
            Some("automata") => {
                tablas = Tablas::default();
                xml = campos.next() == Some("xml");
            }
            Some("caso") => {
                let campos: Vec<&str> = campos.collect();
                writeln!(out, "{}", caso(&tablas.automata(), xml, &campos)).unwrap();
            }
            Some(nombre) => {
                let valores: Vec<i128> = campos.map(|x| x.parse().expect("entero")).collect();
                tablas.cargar(nombre, &valores);
            }
        }
    }
}
