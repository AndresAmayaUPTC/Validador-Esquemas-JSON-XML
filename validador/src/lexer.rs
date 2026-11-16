//! Lexer de JSON en streaming (Fase 1).
//!
//! Ejecuta el AFD minimizado de `tablas_json.rs`: por cada byte hace una consulta a la tabla de
//! clases y otra a δ, así que el costo es constante por byte. Entre bloques solo conserva el estado
//! del autómata, el inicio del token en curso y el valor parcial de un número o de un escape `\u`;
//! un token o un carácter UTF-8 pueden quedar partidos entre dos bloques.

use core::fmt;

use crate::tablas_json as tablas;

/// Alfabeto de tokens de JSON (Tabla 9 del documento).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TipoToken {
    LlaveAbre,
    LlaveCierra,
    CorcheteAbre,
    CorcheteCierra,
    DosPuntos,
    Coma,
    Cadena,
    Numero,
    True,
    False,
    Null,
    Fin,
}

/// Lo que el lexer entrega mientras consume la entrada.
///
/// Una cadena llega como `InicioCadena`, cero o más `Fragmento` y el `Token` de tipo `Cadena`.
/// Un número llega como `Numero` seguido de su `Token` de tipo `Numero`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Evento<'a> {
    /// Token completo; `[inicio, fin)` es su intervalo en bytes dentro de la entrada.
    Token {
        tipo: TipoToken,
        inicio: u64,
        fin: u64,
    },
    /// Se abrió una cadena.
    InicioCadena,
    /// Parte del valor de la cadena, ya decodificado (sin comillas y con los escapes resueltos).
    /// La concatenación de los fragmentos es UTF-8 válido, pero un carácter puede quedar partido
    /// entre dos fragmentos.
    Fragmento(&'a [u8]),
    /// Lexema exacto del número. `entero` indica que no tiene parte decimal ni exponente.
    Numero { lexema: &'a [u8], entero: bool },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TipoError {
    /// El byte no puede continuar el token en curso ni empezar otro.
    ByteInesperado,
    /// La entrada terminó a mitad de un token.
    FinInesperado,
    /// El número no cabe en el búfer del lexer.
    NumeroDemasiadoLargo,
    /// XML: carácter no permitido en un nombre.
    NombreInvalido,
    /// XML: referencia a un carácter que no puede aparecer en un documento.
    ReferenciaInvalida,
    /// XML: el nombre supera la longitud máxima.
    NombreDemasiadoLargo,
}

/// Error léxico; `pos` es la posición en bytes donde el autómata no pudo continuar.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ErrorLexico {
    pub pos: u64,
    pub tipo: TipoError,
}

impl fmt::Display for ErrorLexico {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let motivo = match self.tipo {
            TipoError::ByteInesperado => "byte inesperado",
            TipoError::FinInesperado => "fin de entrada inesperado",
            TipoError::NumeroDemasiadoLargo => "número demasiado largo",
            TipoError::NombreInvalido => "carácter no permitido en un nombre",
            TipoError::ReferenciaInvalida => "referencia a un carácter no permitido",
            TipoError::NombreDemasiadoLargo => "nombre demasiado largo",
        };
        write!(f, "{motivo} en el byte {}", self.pos)
    }
}

// ---- Tipos de las tablas generadas ----------------------------------------------------------

/// Cómo se calcula el intervalo de un token emitido en una transición.
#[derive(Clone, Copy)]
pub(crate) enum Modo {
    /// `[inicio, pos)`: el byte actual es el delimitador y no pertenece al token.
    Previo,
    /// `[inicio, pos + 1)`: el byte actual cierra el token.
    Cierre,
    /// `[pos, pos + 1)`: token de un solo byte.
    Unico,
    /// `[pos, pos)`: fin de entrada.
    Fin,
}

pub(crate) struct Salida {
    pub tipo: TipoToken,
    pub modo: Modo,
}

/// Acción de valor de una transición (las mismas de `compilador/lexico/lexer.py`).
#[derive(Clone, Copy)]
pub(crate) enum Op {
    Nada,
    /// Comilla de apertura.
    InicioCadena,
    /// El byte pasa tal cual al valor de la cadena.
    Literal,
    /// Segundo byte de un escape simple (`\n`, `\"`, ...).
    Escape,
    /// Uno de los tres primeros dígitos de `\uXXXX`.
    Hex,
    /// Cuarto dígito de un sustituto alto: se guarda hasta que llegue el bajo.
    HexAlto,
    /// Cuarto dígito: se emite el punto de código.
    HexFin,
    /// El byte pertenece al lexema del número.
    Numero,
}

pub(crate) struct Accion {
    pub op: Op,
    /// El token siguiente empieza en este byte.
    pub marca: bool,
    pub salidas: [Option<Salida>; 2],
}

// ---- Lexer ----------------------------------------------------------------------------------

/// Lexer de JSON por bloques. `N` es el tamaño máximo en bytes del lexema de un número.
///
/// No reserva memoria: todo su estado vive dentro de la estructura.
#[derive(Clone, Debug)]
pub struct Lexer<const N: usize = 1024> {
    estado: u8,
    /// Bytes consumidos hasta ahora.
    pos: u64,
    /// Posición donde empezó el token en curso.
    inicio: u64,
    /// Dígitos leídos del escape `\u` en curso.
    acumulado: u32,
    /// Sustituto alto que espera a su pareja; 0 si no hay ninguno.
    alto: u32,
    numero: [u8; N],
    largo: usize,
    real: bool,
    error: Option<ErrorLexico>,
}

impl Lexer<1024> {
    /// Lexer con el límite por defecto de 1024 bytes por número.
    pub const fn new() -> Self {
        Self::con_limite()
    }
}

impl Default for Lexer<1024> {
    fn default() -> Self {
        Self::new()
    }
}

impl<const N: usize> Lexer<N> {
    /// Lexer cuyo límite por número es `N`: `Lexer::<64>::con_limite()`.
    pub const fn con_limite() -> Self {
        Lexer {
            estado: tablas::INICIAL,
            pos: 0,
            inicio: 0,
            acumulado: 0,
            alto: 0,
            numero: [0; N],
            largo: 0,
            real: false,
            error: None,
        }
    }

    /// Bytes consumidos hasta ahora.
    pub fn posicion(&self) -> u64 {
        self.pos
    }

    /// Consume un bloque y llama a `emitir` con cada evento que se complete.
    ///
    /// El análisis se detiene en el primer error; las llamadas posteriores lo devuelven de nuevo.
    pub fn feed<F: FnMut(Evento<'_>)>(
        &mut self,
        bloque: &[u8],
        mut emitir: F,
    ) -> Result<(), ErrorLexico> {
        if let Some(error) = self.error {
            return Err(error);
        }
        // Inicio, dentro del bloque, del tramo de bytes literales que aún no se ha entregado.
        let mut tramo: Option<usize> = None;
        let mut i = 0;
        while i < bloque.len() {
            let byte = bloque[i];
            let celda =
                self.estado as usize * tablas::N_COLUMNAS + tablas::CLASE[byte as usize] as usize;
            let siguiente = tablas::DELTA[celda];
            let id = tablas::ACCION[celda];

            // Camino rápido 1: el byte es contenido de una cadena y nada más.
            if id == tablas::LITERAL {
                if tramo.is_none() {
                    tramo = Some(i);
                }
                let desde = i;
                i += 1;
                if siguiente == tablas::ESTADO_CADENA {
                    // Dentro de la cadena, el texto ASCII corriente se salta de ocho en ocho bytes.
                    i += tramo_simple(&bloque[i..]);
                }
                self.pos += (i - desde) as u64;
                self.estado = siguiente;
                continue;
            }
            if let Some(desde) = tramo.take() {
                emitir(Evento::Fragmento(&bloque[desde..i]));
            }
            // Camino rápido 2: la transición no emite nada ni toca el token (o no existe).
            if id == 0 {
                if siguiente == tablas::ERROR {
                    return Err(self.fallar(TipoError::ByteInesperado));
                }
                self.estado = siguiente;
                self.pos += 1;
                i += 1;
                continue;
            }
            // Camino rápido 3: el byte continúa el número en curso.
            if id == tablas::DIGITO {
                if self.largo == N {
                    return Err(self.fallar(TipoError::NumeroDemasiadoLargo));
                }
                self.numero[self.largo] = byte;
                self.largo += 1;
                self.real |= matches!(byte, b'.' | b'e' | b'E');
                self.estado = siguiente;
                self.pos += 1;
                i += 1;
                continue;
            }

            let accion = &tablas::ACCIONES[id as usize];
            self.emitir_salidas(accion, &mut emitir);
            if accion.marca {
                self.inicio = self.pos;
            }
            match accion.op {
                Op::Nada => {}
                Op::InicioCadena => emitir(Evento::InicioCadena),
                Op::Literal => {
                    if tramo.is_none() {
                        tramo = Some(i);
                    }
                }
                Op::Escape => emitir(Evento::Fragmento(&[escape(byte)])),
                Op::Hex => self.acumulado = self.acumulado << 4 | hex(byte),
                Op::HexAlto => {
                    self.alto = self.acumulado << 4 | hex(byte);
                    self.acumulado = 0;
                }
                Op::HexFin => {
                    let mut punto = self.acumulado << 4 | hex(byte);
                    if self.alto != 0 {
                        // Par de sustitutos: el AFD garantiza que aquí llega el bajo.
                        punto = 0x10000 + ((self.alto - 0xD800) << 10) + (punto - 0xDC00);
                    }
                    self.acumulado = 0;
                    self.alto = 0;
                    let mut utf8 = [0u8; 4];
                    let c = char::from_u32(punto).unwrap_or(char::REPLACEMENT_CHARACTER);
                    emitir(Evento::Fragmento(c.encode_utf8(&mut utf8).as_bytes()));
                }
                Op::Numero => {
                    if accion.marca {
                        self.largo = 0;
                        self.real = false;
                    }
                    if self.largo == N {
                        return Err(self.fallar(TipoError::NumeroDemasiadoLargo));
                    }
                    self.numero[self.largo] = byte;
                    self.largo += 1;
                    self.real |= matches!(byte, b'.' | b'e' | b'E');
                }
            }
            self.estado = siguiente;
            self.pos += 1;
            i += 1;
        }
        if let Some(desde) = tramo {
            emitir(Evento::Fragmento(&bloque[desde..]));
        }
        Ok(())
    }

    /// Deja el lexer como recién creado, para analizar otro documento.
    pub fn reiniciar(&mut self) {
        self.estado = tablas::INICIAL;
        self.pos = 0;
        self.inicio = 0;
        self.acumulado = 0;
        self.alto = 0;
        self.largo = 0;
        self.real = false;
        self.error = None;
    }

    /// Entrega el fin de entrada ⊣: emite el token pendiente, si lo hay, y el token `Fin`.
    pub fn finish<F: FnMut(Evento<'_>)>(&mut self, mut emitir: F) -> Result<(), ErrorLexico> {
        if let Some(error) = self.error {
            return Err(error);
        }
        let celda = self.estado as usize * tablas::N_COLUMNAS + tablas::COLUMNA_FIN;
        let siguiente = tablas::DELTA[celda];
        if siguiente == tablas::ERROR {
            return Err(self.fallar(TipoError::FinInesperado));
        }
        self.emitir_salidas(
            &tablas::ACCIONES[tablas::ACCION[celda] as usize],
            &mut emitir,
        );
        self.estado = siguiente;
        Ok(())
    }

    fn emitir_salidas<F: FnMut(Evento<'_>)>(&self, accion: &Accion, emitir: &mut F) {
        for salida in accion.salidas.iter().flatten() {
            let (inicio, fin) = match salida.modo {
                Modo::Previo => (self.inicio, self.pos),
                Modo::Cierre => (self.inicio, self.pos + 1),
                Modo::Unico => (self.pos, self.pos + 1),
                Modo::Fin => (self.pos, self.pos),
            };
            if salida.tipo == TipoToken::Numero {
                emitir(Evento::Numero {
                    lexema: &self.numero[..self.largo],
                    entero: !self.real,
                });
            }
            emitir(Evento::Token {
                tipo: salida.tipo,
                inicio,
                fin,
            });
        }
    }

    fn fallar(&mut self, tipo: TipoError) -> ErrorLexico {
        let error = ErrorLexico {
            pos: self.pos,
            tipo,
        };
        self.error = Some(error);
        error
    }
}

/// Valor de un dígito hexadecimal; el AFD garantiza que el byte lo es.
fn hex(byte: u8) -> u32 {
    (if byte <= b'9' {
        byte - b'0'
    } else {
        (byte | 0x20) - b'a' + 10
    }) as u32
}

/// Byte que representa el escape simple `\x`; el AFD garantiza que `x` es válido.
fn escape(byte: u8) -> u8 {
    match byte {
        b'b' => 0x08,
        b'f' => 0x0C,
        b'n' => b'\n',
        b'r' => b'\r',
        b't' => b'\t',
        otro => otro, // comilla, barra invertida y barra
    }
}

/// Cuántos bytes del principio de `bytes` son contenido corriente de una cadena: ASCII imprimible
/// (0x20 a 0x7F) que no sea la comilla ni la barra invertida.
///
/// Mira ocho bytes a la vez como un entero de 64 bits; los que no son corrientes (escapes, fin de
/// cadena, caracteres de control y UTF-8 de varios bytes) vuelven al autómata, que es quien decide.
fn tramo_simple(bytes: &[u8]) -> usize {
    const UNOS: u64 = 0x0101_0101_0101_0101;
    const ALTOS: u64 = 0x8080_8080_8080_8080;
    // Marca con su bit alto cada byte de `x` que es cero; el primero que marca es exacto.
    let cero = |x: u64| x.wrapping_sub(UNOS) & !x & ALTOS;
    let mut i = 0;
    while let Some(ocho) = bytes.get(i..i + 8) {
        let palabra = u64::from_le_bytes([
            ocho[0], ocho[1], ocho[2], ocho[3], ocho[4], ocho[5], ocho[6], ocho[7],
        ]);
        let especiales = (palabra & ALTOS)                       // UTF-8 de varios bytes
            | (palabra.wrapping_sub(UNOS * 0x20) & !palabra & ALTOS) // menores que 0x20: control
            | cero(palabra ^ (UNOS * 0x22))                      // comilla
            | cero(palabra ^ (UNOS * 0x5C)); // barra invertida
        if especiales != 0 {
            return i + (especiales.trailing_zeros() / 8) as usize;
        }
        i += 8;
    }
    while i < bytes.len() && matches!(bytes[i], 0x20..=0x7F) && bytes[i] != 0x22 && bytes[i] != 0x5C
    {
        i += 1;
    }
    i
}
