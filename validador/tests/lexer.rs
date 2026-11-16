//! Pruebas del lexer de JSON. Las diferenciales contra el lexer de Python están en
//! `pruebas/test_lexer_rust.py`.

use validador::{ErrorLexico, Evento, Lexer, TipoError, TipoToken, TipoToken::*};

/// Un token con su valor ya reunido (cadena decodificada o lexema del número).
#[derive(Debug, PartialEq)]
struct Tok {
    tipo: TipoToken,
    inicio: u64,
    fin: u64,
    valor: Option<Vec<u8>>,
    entero: bool,
}

/// Tokeniza `datos` en bloques de `bloque` bytes (0 = un solo bloque).
fn tokenizar<const N: usize>(
    mut lexer: Lexer<N>,
    datos: &[u8],
    bloque: usize,
) -> (Vec<Tok>, Option<ErrorLexico>) {
    let mut tokens = Vec::new();
    let mut cadena = Vec::new();
    let mut numero = (Vec::new(), false);
    let mut recibir = |evento: Evento<'_>| match evento {
        Evento::InicioCadena => cadena.clear(),
        Evento::Fragmento(bytes) => cadena.extend_from_slice(bytes),
        Evento::Numero { lexema, entero } => numero = (lexema.to_vec(), entero),
        Evento::Token { tipo, inicio, fin } => {
            let valor = match tipo {
                Cadena => Some(cadena.clone()),
                Numero => Some(numero.0.clone()),
                _ => None,
            };
            tokens.push(Tok {
                tipo,
                inicio,
                fin,
                valor,
                entero: tipo == Numero && numero.1,
            });
        }
    };
    let bloques: Vec<&[u8]> = if bloque == 0 {
        vec![datos]
    } else {
        datos.chunks(bloque).collect()
    };
    let mut resultado = Ok(());
    for b in bloques {
        resultado = lexer.feed(b, &mut recibir);
        if resultado.is_err() {
            break;
        }
    }
    if resultado.is_ok() {
        resultado = lexer.finish(&mut recibir);
    }
    (tokens, resultado.err())
}

fn json(datos: &[u8]) -> (Vec<Tok>, Option<ErrorLexico>) {
    tokenizar(Lexer::new(), datos, 0)
}

fn valor(datos: &[u8]) -> Vec<u8> {
    let (tokens, error) = json(datos);
    assert_eq!(error, None, "{:?}", String::from_utf8_lossy(datos));
    tokens[0]
        .valor
        .clone()
        .expect("el primer token no tiene valor")
}

#[test]
fn tokens_de_la_tabla_11() {
    let (tokens, error) = json(br#"{"edad": 25, "ok": true}"#);
    assert_eq!(error, None);
    let resumen: Vec<_> = tokens.iter().map(|t| (t.tipo, t.inicio, t.fin)).collect();
    assert_eq!(
        resumen,
        [
            (LlaveAbre, 0, 1),
            (Cadena, 1, 7),
            (DosPuntos, 7, 8),
            (Numero, 9, 11),
            (Coma, 11, 12),
            (Cadena, 13, 17),
            (DosPuntos, 17, 18),
            (True, 19, 23),
            (LlaveCierra, 23, 24),
            (Fin, 24, 24),
        ]
    );
    assert_eq!(tokens[1].valor.as_deref(), Some(&b"edad"[..]));
    assert_eq!(tokens[3].valor.as_deref(), Some(&b"25"[..]));
    assert!(tokens[3].entero);
}

#[test]
fn valores_de_cadenas() {
    assert_eq!(valor(br#""""#), b"");
    assert_eq!(valor(br#""caf\u00e9""#), "café".as_bytes());
    assert_eq!(valor("\"café 😀\"".as_bytes()), "café 😀".as_bytes());
    assert_eq!(
        valor(br#""\" \\ \/ \b \f \n \r \t""#),
        b"\" \\ / \x08 \x0c \n \r \t"
    );
    assert_eq!(valor(br#""\ud83d\ude00""#), "😀".as_bytes());
    assert_eq!(valor(br#""\uD83D\uDE00x""#), "😀x".as_bytes());
    assert_eq!(valor(br#""a\u0000b""#), b"a\0b");
    assert_eq!(valor(br#""\uFFFF""#), "\u{FFFF}".as_bytes());
}

#[test]
fn valores_de_numeros() {
    for (entrada, entero) in [
        ("0", true),
        ("-0", true),
        ("25", true),
        ("-12.5e+3", false),
        ("1e400", false),
        ("0.5E-3", false),
    ] {
        let (tokens, error) = json(entrada.as_bytes());
        assert_eq!(error, None, "{entrada}");
        assert_eq!(tokens[0].valor.as_deref(), Some(entrada.as_bytes()));
        assert_eq!(tokens[0].entero, entero, "{entrada}");
    }
}

#[test]
fn posicion_de_los_errores() {
    use TipoError::*;
    let casos: [(&[u8], u64, TipoError); 14] = [
        (b"[01]", 2, ByteInesperado),
        (b"[1.]", 3, ByteInesperado),
        (b".5", 0, ByteInesperado),
        (b"truex", 4, ByteInesperado),
        (br#""a\qb""#, 3, ByteInesperado),
        (br#""ab"#, 3, FinInesperado),
        (br#""\ud83d""#, 7, ByteInesperado),
        (br#""\udc00""#, 4, ByteInesperado),
        (b"\"\xc0\xaf\"", 1, ByteInesperado),
        (b"\"\xed\xa0\x80\"", 2, ByteInesperado),
        (b"\"\xe2\x82", 3, FinInesperado),
        (b"[1,\xef\xbb\xbf]", 3, ByteInesperado),
        (b"\"tab\tx\"", 4, ByteInesperado),
        (b"-", 1, FinInesperado),
    ];
    for (entrada, pos, tipo) in casos {
        let (_, error) = json(entrada);
        assert_eq!(
            error,
            Some(ErrorLexico { pos, tipo }),
            "{:?}",
            String::from_utf8_lossy(entrada)
        );
    }
}

#[test]
fn cualquier_tamano_de_bloque_da_lo_mismo() {
    let entradas: [&[u8]; 6] = [
        br#"{"edad": 25, "ok": true, "x": [null, false, -1.5e-7]}"#,
        "\u{feff}[\"café 😀\", \"\\ud83d\\ude00\", \"\\n\\u00e9\"]".as_bytes(),
        br#"[1, 2, {"a": "b\\c"}] "#,
        br#"["sin cerrar", 12"#,
        b"[\"\xf0\x9f\x98\"]",
        br#"{"a": tru }"#,
    ];
    for entrada in entradas {
        let esperado = json(entrada);
        for bloque in 1..=entrada.len() {
            assert_eq!(
                tokenizar(Lexer::new(), entrada, bloque),
                esperado,
                "bloques de {bloque}"
            );
        }
    }
}

#[test]
fn numero_demasiado_largo() {
    let (tokens, error) = tokenizar(Lexer::<4>::con_limite(), b"[1234]", 0);
    assert_eq!(error, None);
    assert_eq!(tokens[1].valor.as_deref(), Some(&b"1234"[..]));

    let (_, error) = tokenizar(Lexer::<4>::con_limite(), b"[12345]", 0);
    assert_eq!(
        error,
        Some(ErrorLexico {
            pos: 5,
            tipo: TipoError::NumeroDemasiadoLargo
        })
    );
}

#[test]
fn el_error_se_conserva() {
    let mut lexer = Lexer::new();
    let error = lexer.feed(b"[x", |_| {}).unwrap_err();
    assert_eq!(
        error,
        ErrorLexico {
            pos: 1,
            tipo: TipoError::ByteInesperado
        }
    );
    assert_eq!(lexer.feed(b"]", |_| {}), Err(error));
    assert_eq!(lexer.finish(|_| {}), Err(error));
    assert_eq!(lexer.posicion(), 1);
}
