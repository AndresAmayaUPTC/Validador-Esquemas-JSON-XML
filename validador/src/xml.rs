//! Lexer de XML en streaming (Fase 1, XML).
//!
//! Ejecuta el AFD minimizado de `tablas_xml.rs` con las mismas acciones de valor que
//! `compilador/lexico/lexer_xml.py`, que sirve de referencia. No guarda ningún valor: los nombres,
//! los valores de atributo y el texto se entregan por fragmentos según llegan, ya decodificados
//! (referencias resueltas, fines de línea normalizados). Entre bloques solo conserva el estado del
//! autómata y unos pocos contadores.
//!
//! Se rechaza todo `<!DOCTYPE`: sin DTD no hay entidades propias, así que no son posibles ni las
//! entidades externas (XXE) ni la expansión de entidades.

use crate::lexer::{ErrorLexico, Modo, TipoError};
use crate::tablas_xml as tablas;

/// Longitud máxima de un nombre, en bytes.
pub const NOMBRE_MAX: u32 = 1024;

/// Alfabeto de tokens de XML (Tabla 10 del documento).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TipoXml {
    DeclXml,
    EtiqAbre,
    EtiqCierreAbre,
    Nombre,
    Igual,
    ValorAtrib,
    EtiqFin,
    EtiqAutocierre,
    Texto,
    Fin,
}

/// Lo que el lexer entrega mientras consume la entrada.
///
/// Un nombre llega como `InicioNombre`, sus `Fragmento` y el `Token` de tipo `Nombre`; un valor de
/// atributo, como `InicioValor`, cero o más `Fragmento` y su `Token`. Todo el texto que hay entre
/// dos etiquetas es un solo token: llega como `InicioTexto`, sus `Fragmento` y, justo antes del
/// token de la etiqueta siguiente, el `Token` de tipo `Texto`. Nunca hay un texto vacío.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventoXml<'a> {
    /// Token completo; `[inicio, fin)` es su intervalo en bytes dentro de la entrada.
    Token {
        tipo: TipoXml,
        inicio: u64,
        fin: u64,
    },
    InicioNombre,
    InicioValor,
    InicioTexto,
    /// Parte del valor en curso. La concatenación de los fragmentos es UTF-8 válido, pero un
    /// carácter puede quedar partido entre dos fragmentos.
    Fragmento(&'a [u8]),
}

// ---- Tipos de las tablas generadas ----------------------------------------------------------

pub(crate) struct SalidaXml {
    pub tipo: TipoXml,
    pub modo: Modo,
}

/// Acción de valor de una transición (las mismas de `compilador/lexico/lexer_xml.py`).
#[derive(Clone, Copy)]
pub(crate) enum OpXml {
    Nada,
    /// Byte de texto; el parámetro son los bytes de un BOM incompleto que van delante.
    Texto(u8),
    /// Byte de una sección CDATA.
    Cdata,
    /// `]` dentro de CDATA: puede ser el principio de `]]>`.
    CdataCorchete,
    /// `>` que cierra la sección: los `]]` pendientes no son texto.
    CdataFin,
    /// Byte de un nombre: 0 si es ASCII, 1 si empieza o continúa un carácter de varios bytes y 2
    /// si lo completa.
    Nombre(u8),
    /// Comilla que abre un valor de atributo.
    ValorIni,
    /// Byte de un valor de atributo.
    Valor,
    RefIni,
    RefDec,
    RefHex,
    /// `;` de una referencia numérica; `true` si va al texto y `false` si va a un valor.
    RefNum(bool),
    /// `;` de una entidad predefinida, que vale el byte indicado.
    Entidad(bool, u8),
}

pub(crate) struct AccionXml {
    pub op: OpXml,
    pub marca: bool,
    pub salidas: [Option<SalidaXml>; 2],
}

// ---- Clases de caracteres de XML 1.0 (quinta edición) ---------------------------------------

const INICIO_DE_NOMBRE: [(u32, u32); 12] = [
    (0xC0, 0xD6),
    (0xD8, 0xF6),
    (0xF8, 0x2FF),
    (0x370, 0x37D),
    (0x37F, 0x1FFF),
    (0x200C, 0x200D),
    (0x2070, 0x218F),
    (0x2C00, 0x2FEF),
    (0x3001, 0xD7FF),
    (0xF900, 0xFDCF),
    (0xFDF0, 0xFFFD),
    (0x10000, 0xEFFFF),
];
const RESTO_DE_NOMBRE: [(u32, u32); 3] = [(0xB7, 0xB7), (0x300, 0x36F), (0x203F, 0x2040)];

fn en(rangos: &[(u32, u32)], c: u32) -> bool {
    rangos.iter().any(|&(a, b)| a <= c && c <= b)
}

/// Producción `Char`: lo que puede aparecer en un documento.
fn es_char(c: u32) -> bool {
    matches!(c, 0x9 | 0xA | 0xD | 0x20..=0xD7FF | 0xE000..=0xFFFD | 0x10000..=0x10FFFF)
}

// ---- Lexer ----------------------------------------------------------------------------------

/// Lexer de XML por bloques. No reserva memoria: todo su estado vive dentro de la estructura.
#[derive(Clone, Debug)]
pub struct LexerXml {
    estado: u16,
    /// Bytes consumidos hasta ahora.
    pos: u64,
    /// Posición donde empezó el token en curso.
    inicio: u64,
    /// Intervalo del texto pendiente de emitir, si `hay_tramo`.
    texto_inicio: u64,
    texto_fin: u64,
    hay_tramo: bool,
    /// Ya se entregó `InicioTexto` (el texto pendiente no está vacío).
    texto_abierto: bool,
    /// El byte anterior del valor fue un CR literal.
    cr: bool,
    /// `]` de CDATA que aún no se sabe si son texto.
    corchetes: u8,
    /// Valor de la referencia numérica en curso.
    referencia: u32,
    /// Bytes del nombre en curso.
    largo_nombre: u32,
    /// Carácter de varios bytes del nombre en curso, y si es el primero del nombre.
    punto: u32,
    primero: bool,
    error: Option<ErrorLexico>,
}

impl Default for LexerXml {
    fn default() -> Self {
        Self::new()
    }
}

impl LexerXml {
    pub const fn new() -> Self {
        LexerXml {
            estado: tablas::INICIAL,
            pos: 0,
            inicio: 0,
            texto_inicio: 0,
            texto_fin: 0,
            hay_tramo: false,
            texto_abierto: false,
            cr: false,
            corchetes: 0,
            referencia: 0,
            largo_nombre: 0,
            punto: 0,
            primero: false,
            error: None,
        }
    }

    /// Bytes consumidos hasta ahora.
    pub fn posicion(&self) -> u64 {
        self.pos
    }

    /// Deja el lexer como recién creado, para analizar otro documento.
    pub fn reiniciar(&mut self) {
        *self = Self::new();
    }

    /// Consume un bloque y llama a `emitir` con cada evento que se complete.
    ///
    /// El análisis se detiene en el primer error; las llamadas posteriores lo devuelven de nuevo.
    pub fn feed<F: FnMut(EventoXml<'_>)>(
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
        // Entrega el tramo pendiente, que termina justo antes del byte actual.
        macro_rules! vaciar {
            () => {
                if let Some(desde) = tramo.take() {
                    emitir(EventoXml::Fragmento(&bloque[desde..i]));
                }
            };
        }
        macro_rules! abrir_texto {
            () => {
                if !self.texto_abierto {
                    self.texto_abierto = true;
                    emitir(EventoXml::InicioTexto);
                }
            };
        }
        // Añade al texto el byte actual: CRLF y CR pasan a LF (XML 1.0 §2.11).
        macro_rules! byte_de_texto {
            ($byte:expr) => {
                abrir_texto!();
                if $byte == b'\r' {
                    vaciar!();
                    emitir(EventoXml::Fragmento(b"\n"));
                    self.cr = true;
                } else if $byte == b'\n' && self.cr {
                    vaciar!();
                    self.cr = false;
                } else {
                    if tramo.is_none() {
                        tramo = Some(i);
                    }
                    self.cr = false;
                }
            };
        }

        while i < bloque.len() {
            let byte = bloque[i];
            let celda =
                self.estado as usize * tablas::N_COLUMNAS + tablas::CLASE[byte as usize] as usize;
            let siguiente = tablas::DELTA[celda];
            let id = tablas::ACCION[celda];

            // Camino rápido 1: el byte es texto corriente y nada más.
            if id == tablas::TEXTO_PURO && self.texto_abierto && !self.cr && byte != b'\r' {
                if tramo.is_none() {
                    tramo = Some(i);
                }
                let desde = i;
                i += 1;
                if siguiente == tablas::ESTADO_TEXTO {
                    i += tramo_de_texto(&bloque[i..]);
                }
                self.pos += (i - desde) as u64;
                self.estado = siguiente;
                continue;
            }
            // Camino rápido 2: la transición no hace nada (o no existe).
            if id == 0 {
                vaciar!();
                if siguiente == tablas::ERROR {
                    return Err(self.fallar(TipoError::ByteInesperado));
                }
                self.estado = siguiente;
                self.pos += 1;
                i += 1;
                continue;
            }

            let accion = &tablas::ACCIONES[id as usize];
            if accion.salidas[0].is_some() {
                vaciar!();
                self.emitir_salidas(accion, &mut emitir);
            }
            if accion.marca {
                self.inicio = self.pos;
            }
            match accion.op {
                OpXml::Nada => {
                    vaciar!();
                }
                OpXml::Texto(bom) => {
                    if bom > 0 {
                        vaciar!();
                        abrir_texto!();
                        emitir(EventoXml::Fragmento(&[0xEF, 0xBB][..bom as usize]));
                        self.cr = false;
                    }
                    byte_de_texto!(byte);
                }
                OpXml::Cdata => {
                    if self.corchetes > 0 {
                        vaciar!();
                        abrir_texto!();
                        emitir(EventoXml::Fragmento(&b"]]"[..self.corchetes as usize]));
                        self.corchetes = 0;
                        self.cr = false;
                    }
                    byte_de_texto!(byte);
                }
                OpXml::CdataCorchete => {
                    vaciar!();
                    if self.corchetes == 2 {
                        // con tres seguidos, el primero ya es texto seguro
                        abrir_texto!();
                        emitir(EventoXml::Fragmento(b"]"));
                        self.cr = false;
                    } else {
                        self.corchetes += 1;
                    }
                }
                OpXml::CdataFin => {
                    vaciar!();
                    self.corchetes = 0;
                }
                OpXml::Nombre(clase) => {
                    if accion.marca {
                        vaciar!();
                        emitir(EventoXml::InicioNombre);
                        self.largo_nombre = 0;
                    }
                    if self.largo_nombre == NOMBRE_MAX {
                        return Err(self.fallar(TipoError::NombreDemasiadoLargo));
                    }
                    if clase != 0 {
                        if byte & 0xC0 != 0x80 {
                            self.primero = self.largo_nombre == 0;
                            self.punto = (byte
                                & if byte >= 0xF0 {
                                    0x07
                                } else if byte >= 0xE0 {
                                    0x0F
                                } else {
                                    0x1F
                                }) as u32;
                        } else {
                            self.punto = self.punto << 6 | (byte & 0x3F) as u32;
                        }
                        if clase == 2
                            && !(en(&INICIO_DE_NOMBRE, self.punto)
                                || (!self.primero && en(&RESTO_DE_NOMBRE, self.punto)))
                        {
                            return Err(self.fallar(TipoError::NombreInvalido));
                        }
                    }
                    self.largo_nombre += 1;
                    if tramo.is_none() {
                        tramo = Some(i);
                    }
                }
                OpXml::ValorIni => {
                    vaciar!();
                    emitir(EventoXml::InicioValor);
                    self.cr = false;
                }
                OpXml::Valor => {
                    // Normalización de XML 1.0 §3.3.3: tab, CR y LF literales pasan a espacio.
                    if matches!(byte, b'\t' | b'\n' | b'\r') {
                        vaciar!();
                        if !(byte == b'\n' && self.cr) {
                            emitir(EventoXml::Fragmento(b" "));
                        }
                        self.cr = byte == b'\r';
                    } else {
                        if tramo.is_none() {
                            tramo = Some(i);
                        }
                        self.cr = false;
                    }
                }
                OpXml::RefIni => {
                    vaciar!();
                    self.referencia = 0;
                }
                OpXml::RefDec => {
                    self.referencia = (self.referencia * 10 + (byte - b'0') as u32).min(0x11_0000);
                }
                OpXml::RefHex => {
                    self.referencia = (self.referencia * 16 + hex(byte)).min(0x11_0000);
                }
                OpXml::RefNum(a_texto) => {
                    if !es_char(self.referencia) {
                        return Err(self.fallar(TipoError::ReferenciaInvalida));
                    }
                    if a_texto {
                        abrir_texto!();
                    }
                    let mut utf8 = [0u8; 4];
                    let c = char::from_u32(self.referencia).unwrap_or(char::REPLACEMENT_CHARACTER);
                    emitir(EventoXml::Fragmento(c.encode_utf8(&mut utf8).as_bytes()));
                    self.cr = false;
                }
                OpXml::Entidad(a_texto, valor) => {
                    if a_texto {
                        abrir_texto!();
                    }
                    emitir(EventoXml::Fragmento(&[valor]));
                    self.cr = false;
                }
            }
            self.estado = siguiente;
            self.pos += 1;
            i += 1;
        }
        vaciar!();
        Ok(())
    }

    /// Entrega el fin de entrada ⊣: emite el texto pendiente, si lo hay, y el token `Fin`.
    pub fn finish<F: FnMut(EventoXml<'_>)>(&mut self, mut emitir: F) -> Result<(), ErrorLexico> {
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

    fn emitir_salidas<F: FnMut(EventoXml<'_>)>(&mut self, accion: &AccionXml, emitir: &mut F) {
        for salida in accion.salidas.iter().flatten() {
            let (inicio, fin) = match salida.modo {
                Modo::Previo => (self.inicio, self.pos),
                Modo::Cierre => (self.inicio, self.pos + 1),
                Modo::Unico => (self.pos, self.pos + 1),
                Modo::Fin => (self.pos, self.pos),
            };
            match salida.tipo {
                TipoXml::Texto => {
                    // se acumula: puede seguir tras un comentario o una sección CDATA
                    if !self.hay_tramo {
                        self.texto_inicio = inicio;
                    }
                    self.texto_fin = fin;
                    self.hay_tramo = true;
                    continue;
                }
                TipoXml::EtiqAbre | TipoXml::EtiqCierreAbre | TipoXml::Fin => {
                    if self.hay_tramo && self.texto_abierto {
                        emitir(EventoXml::Token {
                            tipo: TipoXml::Texto,
                            inicio: self.texto_inicio,
                            fin: self.texto_fin,
                        });
                    }
                    self.hay_tramo = false;
                    self.texto_abierto = false;
                    self.cr = false;
                }
                _ => {}
            }
            emitir(EventoXml::Token {
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

/// Cuántos bytes del principio de `bytes` son texto corriente: ASCII imprimible (0x20 a 0x7F) que
/// no sea `<`, `&` ni `]`. Mira ocho bytes a la vez; los demás vuelven al autómata.
fn tramo_de_texto(bytes: &[u8]) -> usize {
    const UNOS: u64 = 0x0101_0101_0101_0101;
    const ALTOS: u64 = 0x8080_8080_8080_8080;
    let cero = |x: u64| x.wrapping_sub(UNOS) & !x & ALTOS;
    let mut i = 0;
    while let Some(ocho) = bytes.get(i..i + 8) {
        let palabra = u64::from_le_bytes([
            ocho[0], ocho[1], ocho[2], ocho[3], ocho[4], ocho[5], ocho[6], ocho[7],
        ]);
        let especiales = (palabra & ALTOS)
            | (palabra.wrapping_sub(UNOS * 0x20) & !palabra & ALTOS)
            | cero(palabra ^ (UNOS * 0x3C))
            | cero(palabra ^ (UNOS * 0x26))
            | cero(palabra ^ (UNOS * 0x5D));
        if especiales != 0 {
            return i + (especiales.trailing_zeros() / 8) as usize;
        }
        i += 8;
    }
    while i < bytes.len()
        && matches!(bytes[i], 0x20..=0x7F)
        && !matches!(bytes[i], b'<' | b'&' | b']')
    {
        i += 1;
    }
    i
}
