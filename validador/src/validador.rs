//! Motor de validación: ejecuta el autómata de pila ponderado sobre los bytes del payload.
//!
//! Una sola pasada, sin construir ningún árbol y sin reservar memoria: todo el estado vive dentro
//! de `Validador`. `D` es la profundidad máxima de la pila (contenedores abiertos a la vez) y `W`
//! las palabras de 64 bits de la máscara de cada marco. El tamaño de la estructura es fijo y se
//! conoce al compilar: es la cota O(d) de memoria.
//!
//! Es la misma semántica que `compilador/automata/interprete.py`, que sirve de referencia.

use core::cmp::Ordering;
use core::fmt;

use crate::automata::{Automata, NINGUNO, TOKENS, Transicion, accion, prueba};
use crate::decimal::Numero;
use crate::lexer::{Evento, Lexer, TipoError, TipoToken};

/// Bytes que se conservan de cada clave para la ruta del error.
pub const CLAVE_MAX: usize = 24;
/// Bytes de la ruta completa.
pub const RUTA_MAX: usize = 256;
/// «…» en UTF-8: marca lo que no cupo.
const PUNTOS: [u8; 3] = [0xE2, 0x80, 0xA6];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Estado {
    /// Cumple el esquema.
    Valido,
    /// Cumple todo lo que el autómata verifica, pero cruzó comprobaciones diferidas.
    Pendiente,
    /// Tiene violaciones (peso > 0).
    Invalido,
    /// No es JSON bien formado.
    Malformado,
    /// Superó un límite: profundidad de la pila, tamaño o tiempo.
    Limite,
}

/// Por qué se contó una violación.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Motivo {
    NoAdmitido,
    Required,
    DependentRequired,
    MinProperties,
    MaxProperties,
    MinItems,
    MaxItems,
    Entero,
    Min,
    Max,
    LongMin,
    LongMax,
    EnumNum,
    EnumCad,
    // ---- XML ----
    XsEntero,
    XsDecimal,
    XsBooleano,
    ElementoNoPermitido,
    AtributoNoDeclarado,
    AtributoRequerido,
    TextoNoPermitido,
    ContenidoIncompleto,
}

/// Por qué se detuvo la validación sin llegar al final.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fallo {
    Lexico(TipoError),
    Sintactico,
    ClaveRepetida,
    /// XML: un atributo declarado aparece dos veces en el mismo elemento.
    AtributoRepetido,
    /// XML: un valor numérico que hay que comparar no cabe en el búfer del motor.
    NumeroLargo,
    Profundidad,
    Tamano,
    Tiempo,
}

/// JSON Pointer (RFC 6901) de un lugar del documento, en un búfer de tamaño fijo.
///
/// Se reconstruye desde la pila. De cada clave se guardan como mucho `CLAVE_MAX` bytes y la ruta no
/// pasa de `RUTA_MAX`; lo que no cabe se sustituye por «…».
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct Ruta {
    bytes: [u8; RUTA_MAX],
    largo: u16,
}

impl Ruta {
    pub const fn vacia() -> Self {
        Ruta {
            bytes: [0; RUTA_MAX],
            largo: 0,
        }
    }

    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes[..self.largo as usize]
    }

    pub fn as_str(&self) -> &str {
        core::str::from_utf8(self.as_bytes()).unwrap_or("")
    }

    pub(crate) fn largo(&self) -> usize {
        self.largo as usize
    }

    pub(crate) fn agregar(&mut self, tramo: &[u8]) {
        let inicio = self.largo as usize;
        self.bytes[inicio..inicio + tramo.len()].copy_from_slice(tramo);
        self.largo += tramo.len() as u16;
    }
}

impl fmt::Debug for Ruta {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}", self.as_str())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Resultado {
    pub estado: Estado,
    /// Peso total de la ejecución.
    pub violaciones: u32,
    pub pendientes: u32,
    /// Máximo de contenedores abiertos a la vez.
    pub profundidad: u32,
    /// Posición en bytes y motivo de la primera violación.
    pub primera: Option<(u64, Motivo)>,
    /// Posición y causa, si el estado es `Malformado` o `Limite`.
    pub error: Option<(u64, Fallo)>,
    /// Ruta del error o, si no lo hay, de la primera violación.
    pub ruta: Ruta,
}

impl Estado {
    pub fn nombre(self) -> &'static str {
        match self {
            Estado::Valido => "valido",
            Estado::Pendiente => "pendiente",
            Estado::Invalido => "invalido",
            Estado::Malformado => "malformado",
            Estado::Limite => "limite",
        }
    }
}

impl Motivo {
    pub fn nombre(self) -> &'static str {
        match self {
            Motivo::NoAdmitido => "no_admitido",
            Motivo::Required => "required",
            Motivo::DependentRequired => "dependentRequired",
            Motivo::MinProperties => "minProperties",
            Motivo::MaxProperties => "maxProperties",
            Motivo::MinItems => "minItems",
            Motivo::MaxItems => "maxItems",
            Motivo::Entero => "entero",
            Motivo::Min => "min",
            Motivo::Max => "max",
            Motivo::LongMin => "long_min",
            Motivo::LongMax => "long_max",
            Motivo::EnumNum => "enum_num",
            Motivo::EnumCad => "enum_cad",
            Motivo::XsEntero => "xs_entero",
            Motivo::XsDecimal => "xs_decimal",
            Motivo::XsBooleano => "xs_booleano",
            Motivo::ElementoNoPermitido => "elemento_no_permitido",
            Motivo::AtributoNoDeclarado => "atributo_no_declarado",
            Motivo::AtributoRequerido => "atributo_requerido",
            Motivo::TextoNoPermitido => "texto_no_permitido",
            Motivo::ContenidoIncompleto => "contenido_incompleto",
        }
    }
}

impl Fallo {
    pub fn nombre(self) -> &'static str {
        match self {
            Fallo::Lexico(TipoError::ByteInesperado) => "lexico-byte",
            Fallo::Lexico(TipoError::FinInesperado) => "lexico-fin",
            Fallo::Lexico(TipoError::NumeroDemasiadoLargo) => "lexico-largo",
            Fallo::Lexico(TipoError::NombreInvalido) => "lexico-nombre",
            Fallo::Lexico(TipoError::ReferenciaInvalida) => "lexico-referencia",
            Fallo::Lexico(TipoError::NombreDemasiadoLargo) => "lexico-nombre-largo",
            Fallo::Sintactico => "sintactico",
            Fallo::ClaveRepetida => "clave-repetida",
            Fallo::AtributoRepetido => "atributo-repetido",
            Fallo::NumeroLargo => "numero-largo",
            Fallo::Profundidad => "profundidad",
            Fallo::Tamano => "tamano",
            Fallo::Tiempo => "tiempo",
        }
    }
}

/// Marco de pila: el símbolo de Γ, los dos registros acotados y la posición actual para la ruta.
#[derive(Clone, Copy)]
struct Marco<const W: usize> {
    simbolo: u32,
    /// Propiedades o elementos leídos.
    cuenta: u32,
    /// Propiedades vistas.
    mascara: [u64; W],
    es_objeto: bool,
    /// Ya se leyó alguna clave.
    con_clave: bool,
    /// La clave actual no cupo entera.
    truncada: bool,
    largo_clave: u8,
    /// Comienzo de la clave actual.
    clave: [u8; CLAVE_MAX],
}

impl<const W: usize> Marco<W> {
    const fn nuevo(simbolo: u32, es_objeto: bool) -> Self {
        Marco {
            simbolo,
            cuenta: 0,
            mascara: [0; W],
            es_objeto,
            con_clave: false,
            truncada: false,
            largo_clave: 0,
            clave: [0; CLAVE_MAX],
        }
    }

    /// Añade un byte de la clave que se está leyendo, sin partir ningún carácter.
    fn byte_de_clave(&mut self, b: u8) {
        if self.truncada {
            return;
        }
        if b & 0xC0 != 0x80 {
            // primer byte de un carácter: solo entra si cabe entero
            let necesita = if b < 0x80 {
                1
            } else if b < 0xE0 {
                2
            } else if b < 0xF0 {
                3
            } else {
                4
            };
            if self.largo_clave as usize + necesita > CLAVE_MAX {
                self.truncada = true;
                return;
            }
        }
        self.clave[self.largo_clave as usize] = b;
        self.largo_clave += 1;
    }

    /// Escribe en `tramo` lo que este marco aporta a la ruta y devuelve su longitud (0 si nada).
    fn segmento(&self, tramo: &mut [u8; 2 * CLAVE_MAX + 4]) -> usize {
        let mut n = 0;
        let mut poner = |b: u8| {
            tramo[n] = b;
            n += 1;
        };
        if self.es_objeto {
            if !self.con_clave {
                return 0;
            }
            poner(b'/');
            for &b in &self.clave[..self.largo_clave as usize] {
                match b {
                    b'~' => {
                        poner(b'~');
                        poner(b'0');
                    }
                    b'/' => {
                        poner(b'~');
                        poner(b'1');
                    }
                    _ => poner(b),
                }
            }
            if self.truncada {
                PUNTOS.iter().for_each(|&b| poner(b));
            }
        } else {
            if self.cuenta == 0 {
                return 0;
            }
            poner(b'/');
            let mut cifras = [0u8; 10];
            let (mut indice, mut i) = (self.cuenta - 1, cifras.len());
            loop {
                i -= 1;
                cifras[i] = b'0' + (indice % 10) as u8;
                indice /= 10;
                if indice == 0 {
                    break;
                }
            }
            cifras[i..].iter().for_each(|&b| poner(b));
        }
        n
    }
}

/// El autómata en ejecución, sin el lexer.
struct Nucleo<'a, const D: usize, const W: usize> {
    a: &'a Automata<'a>,
    estado: u32,
    pila: [Marco<W>; D],
    nivel: usize,
    max_profundidad: usize,
    fallo_temprano: bool,
    /// Cadena en curso: posición en su trie, longitud en caracteres y si es una clave.
    nodo: u32,
    longitud: u32,
    leyendo_clave: bool,
    /// Número en curso: pruebas que no cumple y motivo de la primera.
    fallos_numero: u32,
    motivo_numero: Motivo,
    r: Resultado,
    terminado: bool,
}

/// Validador de documentos JSON contra un esquema compilado.
pub struct Validador<'a, const D: usize, const W: usize> {
    lexer: Lexer,
    nucleo: Nucleo<'a, D, W>,
    max_bytes: u64,
    cerrado: bool,
}

impl<'a, const D: usize, const W: usize> Validador<'a, D, W> {
    /// Falla al compilar si `W` es menor que `automata.palabras` cuando el autómata es una
    /// constante; en otro caso, entra en pánico.
    pub const fn new(automata: &'a Automata<'a>) -> Self {
        assert!(
            automata.palabras <= W,
            "la máscara del autómata no cabe en W palabras"
        );
        Validador {
            lexer: Lexer::new(),
            max_bytes: u64::MAX,
            cerrado: false,
            nucleo: Nucleo {
                a: automata,
                estado: automata.inicial,
                pila: [Marco::nuevo(0, false); D],
                nivel: 0,
                max_profundidad: D,
                fallo_temprano: false,
                nodo: NINGUNO,
                longitud: 0,
                leyendo_clave: false,
                fallos_numero: 0,
                motivo_numero: Motivo::Entero,
                r: Resultado {
                    estado: Estado::Valido,
                    violaciones: 0,
                    pendientes: 0,
                    profundidad: 0,
                    primera: None,
                    error: None,
                    ruta: Ruta::vacia(),
                },
                terminado: false,
            },
        }
    }

    /// Limita la profundidad por debajo de `D`.
    pub fn con_profundidad(mut self, maxima: usize) -> Self {
        self.nucleo.max_profundidad = if maxima < D { maxima } else { D };
        self
    }

    /// Detiene la validación en la primera violación, sin leer el resto del documento.
    pub fn con_fallo_temprano(mut self, activo: bool) -> Self {
        self.nucleo.fallo_temprano = activo;
        self
    }

    /// Limita el tamaño del documento en bytes.
    pub fn con_limite_de_bytes(mut self, maximo: u64) -> Self {
        self.max_bytes = maximo;
        self
    }

    /// Bytes consumidos hasta ahora.
    pub fn posicion(&self) -> u64 {
        self.lexer.posicion()
    }

    /// Deja el validador listo para otro documento, con los mismos límites. Es mucho más barato que
    /// crear uno nuevo: no toca la pila, cuyos marcos se inician al apilarse.
    pub fn reiniciar(&mut self) {
        self.lexer.reiniciar();
        self.cerrado = false;
        let n = &mut self.nucleo;
        n.estado = n.a.inicial;
        n.nivel = 0;
        n.nodo = NINGUNO;
        n.longitud = 0;
        n.leyendo_clave = false;
        n.fallos_numero = 0;
        n.terminado = false;
        n.r = Resultado {
            estado: Estado::Valido,
            violaciones: 0,
            pendientes: 0,
            profundidad: 0,
            primera: None,
            error: None,
            ruta: Ruta::vacia(),
        };
    }

    /// Consume un bloque del documento.
    pub fn feed(&mut self, bloque: &[u8]) {
        if self.nucleo.terminado || self.cerrado {
            return;
        }
        let Self {
            lexer,
            nucleo,
            max_bytes,
            ..
        } = self;
        let disponible = *max_bytes - lexer.posicion();
        let excede = bloque.len() as u64 > disponible;
        let bloque = if excede {
            &bloque[..disponible as usize]
        } else {
            bloque
        };
        if let Err(e) = lexer.feed(bloque, |evento| nucleo.evento(evento)) {
            nucleo.detener(Estado::Malformado, e.pos, Fallo::Lexico(e.tipo));
        }
        if excede {
            nucleo.detener(Estado::Limite, *max_bytes, Fallo::Tamano);
        }
    }

    /// Detiene la validación por tiempo. El motor no tiene reloj: quien lo usa mide el tiempo entre
    /// bloques y llama a esta función si se agotó el plazo.
    pub fn agotar_tiempo(&mut self) {
        let pos = self.lexer.posicion();
        self.nucleo.detener(Estado::Limite, pos, Fallo::Tiempo);
    }

    /// Cierra el documento y devuelve el veredicto. Llamarla otra vez devuelve lo mismo.
    pub fn finish(&mut self) -> Resultado {
        let Self {
            lexer,
            nucleo,
            cerrado,
            ..
        } = self;
        if !core::mem::replace(cerrado, true)
            && !nucleo.terminado
            && let Err(e) = lexer.finish(|evento| nucleo.evento(evento))
        {
            nucleo.detener(Estado::Malformado, e.pos, Fallo::Lexico(e.tipo));
        }
        let r = &mut nucleo.r;
        if r.error.is_none() {
            r.estado = if r.violaciones > 0 {
                Estado::Invalido
            } else if r.pendientes > 0 {
                Estado::Pendiente
            } else {
                Estado::Valido
            };
        }
        *r
    }
}

impl<const D: usize, const W: usize> Nucleo<'_, D, W> {
    /// JSON Pointer de la posición actual. Sin la cima, es la ruta del contenedor abierto.
    fn ruta(&self, con_la_cima: bool) -> Ruta {
        let mut ruta = Ruta::vacia();
        let hasta = if con_la_cima {
            self.nivel
        } else {
            self.nivel.saturating_sub(1)
        };
        let mut tramo = [0u8; 2 * CLAVE_MAX + 4];
        for marco in &self.pila[..hasta] {
            let n = marco.segmento(&mut tramo);
            if n == 0 {
                break;
            }
            if ruta.largo as usize + n > RUTA_MAX - PUNTOS.len() {
                ruta.agregar(&PUNTOS);
                break;
            }
            ruta.agregar(&tramo[..n]);
        }
        ruta
    }

    fn detener(&mut self, estado: Estado, pos: u64, fallo: Fallo) {
        if !self.terminado {
            self.r.estado = estado;
            self.r.error = Some((pos, fallo));
            self.r.ruta = self.ruta(false);
            self.terminado = true;
        }
    }

    /// Suma violaciones. `del_contenedor`: la violación es del objeto o arreglo de la cima (falta
    /// una propiedad, sobran elementos), no del valor que se está leyendo.
    fn violar(&mut self, pos: u64, motivo: Motivo, cuantas: u32, del_contenedor: bool) {
        if cuantas > 0 {
            if self.r.primera.is_none() {
                self.r.primera = Some((pos, motivo));
                self.r.ruta = self.ruta(!del_contenedor);
            }
            self.r.violaciones = self.r.violaciones.saturating_add(cuantas);
        }
    }

    fn evento(&mut self, evento: Evento<'_>) {
        if self.terminado {
            return;
        }
        match evento {
            Evento::InicioCadena => {
                self.longitud = 0;
                self.nodo = self.a.trie[self.estado as usize];
                self.leyendo_clave = self.a.otra[self.estado as usize] != NINGUNO && self.nivel > 0;
                if self.leyendo_clave {
                    let marco = &mut self.pila[self.nivel - 1];
                    marco.con_clave = true;
                    marco.truncada = false;
                    marco.largo_clave = 0;
                }
            }
            Evento::Fragmento(bytes) => {
                // Tres recorridos separados: el primero es un simple conteo y los otros dos se
                // abandonan pronto (cuando la cadena ya no puede ser ninguna palabra del trie, o
                // cuando ya se guardó el comienzo de la clave).
                let caracteres = bytes.iter().filter(|&&b| b & 0xC0 != 0x80).count();
                self.longitud = self
                    .longitud
                    .saturating_add(caracteres.min(u32::MAX as usize) as u32);
                for &b in bytes {
                    if self.nodo == NINGUNO {
                        break;
                    }
                    self.nodo = self.a.avanzar(self.nodo, b);
                }
                if self.leyendo_clave {
                    let marco = &mut self.pila[self.nivel - 1];
                    for &b in bytes {
                        if marco.truncada {
                            break;
                        }
                        marco.byte_de_clave(b);
                    }
                }
            }
            Evento::Numero { lexema, .. } => self.probar_numero(lexema),
            Evento::Token { tipo, inicio, .. } => {
                self.paso(tipo, inicio);
                self.leyendo_clave = false;
            }
        }
    }

    /// Evalúa las pruebas numéricas mientras el lexema está disponible; se aplican en `paso`.
    fn probar_numero(&mut self, lexema: &[u8]) {
        self.fallos_numero = 0;
        let a = self.a;
        let id = a.delta[self.estado as usize * TOKENS + TipoToken::Numero as usize];
        if id == NINGUNO {
            return;
        }
        let t = &a.transiciones[id as usize];
        let numero = Numero::new(lexema);
        for p in &a.pruebas[t.pruebas as usize..(t.pruebas + t.n_pruebas) as usize] {
            let constante = |i: u32| numero.comparar(&a.decimales[i as usize], a.digitos);
            let (falla, motivo) = match p.clase {
                prueba::ENTERO => (!numero.es_entero(), Motivo::Entero),
                prueba::MIN => {
                    let orden = constante(p.a);
                    (
                        orden == Ordering::Less || (p.b != 0 && orden == Ordering::Equal),
                        Motivo::Min,
                    )
                }
                prueba::MAX => {
                    let orden = constante(p.a);
                    (
                        orden == Ordering::Greater || (p.b != 0 && orden == Ordering::Equal),
                        Motivo::Max,
                    )
                }
                prueba::ENUM_NUM => (
                    !(p.a..p.a + p.b).any(|i| constante(i) == Ordering::Equal),
                    Motivo::EnumNum,
                ),
                _ => (false, Motivo::Entero),
            };
            if falla {
                if self.fallos_numero == 0 {
                    self.motivo_numero = motivo;
                }
                self.fallos_numero += 1;
                if self.fallo_temprano {
                    break;
                }
            }
        }
    }

    /// Un paso del autómata: δ(estado, token, cima).
    fn paso(&mut self, tipo: TipoToken, pos: u64) {
        let a = self.a;
        let q = self.estado as usize;
        let id = if tipo == TipoToken::Cadena && a.otra[q] != NINGUNO {
            // posición de clave: el trie dice qué propiedad es
            match a.valor(self.nodo) {
                0 => a.otra[q],
                valor => valor - 1,
            }
        } else {
            a.delta[q * TOKENS + tipo as usize]
        };
        if id == NINGUNO {
            return self.detener(Estado::Malformado, pos, Fallo::Sintactico);
        }
        let t: &Transicion = &a.transiciones[id as usize];

        // ---- acción sobre los registros del marco de la cima ----
        if t.accion != 0 {
            let Some(cima) = self.nivel.checked_sub(1) else {
                return self.detener(Estado::Malformado, pos, Fallo::Sintactico);
            };
            let (cuenta, mascara) = (self.pila[cima].cuenta, self.pila[cima].mascara);
            match t.accion {
                accion::CLAVE => {
                    let cuenta = cuenta.saturating_add(1);
                    self.pila[cima].cuenta = cuenta;
                    if t.b != NINGUNO && cuenta == t.b + 1 {
                        self.violar(pos, Motivo::MaxProperties, 1, true);
                    }
                    if t.a != NINGUNO {
                        let (palabra, bit) = ((t.a / 64) as usize, t.a % 64);
                        if mascara[palabra] >> bit & 1 == 1 {
                            return self.detener(Estado::Malformado, pos, Fallo::ClaveRepetida);
                        }
                        self.pila[cima].mascara[palabra] |= 1 << bit;
                    }
                }
                accion::CERRAR_OBJETO => {
                    let faltan = |i: u32| -> u32 {
                        let exigida =
                            &a.mascaras[i as usize * a.palabras..(i as usize + 1) * a.palabras];
                        exigida
                            .iter()
                            .zip(mascara.iter())
                            .map(|(e, m)| (e & !m).count_ones())
                            .sum()
                    };
                    if t.a != NINGUNO {
                        self.violar(pos, Motivo::Required, faltan(t.a), true);
                    }
                    for dep in &a.dependencias[t.b as usize..(t.b + t.c) as usize] {
                        if mascara[(dep.bit / 64) as usize] >> (dep.bit % 64) & 1 == 1 {
                            self.violar(pos, Motivo::DependentRequired, faltan(dep.mascara), true);
                        }
                    }
                    if t.d != NINGUNO && cuenta < t.d {
                        self.violar(pos, Motivo::MinProperties, 1, true);
                    }
                }
                accion::ELEMENTO => {
                    let cuenta = cuenta.saturating_add(1);
                    self.pila[cima].cuenta = cuenta;
                    if t.a != NINGUNO && cuenta == t.a + 1 {
                        self.violar(pos, Motivo::MaxItems, 1, true);
                    }
                }
                accion::CERRAR_ARREGLO if t.a != NINGUNO && cuenta < t.a => {
                    self.violar(pos, Motivo::MinItems, 1, true);
                }
                _ => {}
            }
        }

        // ---- peso fijo y pruebas sobre el valor, de menor a mayor costo ----
        self.violar(pos, Motivo::NoAdmitido, t.peso, false);
        if tipo == TipoToken::Numero {
            self.violar(pos, self.motivo_numero, self.fallos_numero, false);
        } else if tipo == TipoToken::Cadena {
            for p in &a.pruebas[t.pruebas as usize..(t.pruebas + t.n_pruebas) as usize] {
                let (fallos, motivo) = match p.clase {
                    prueba::LONG_MIN => ((self.longitud < p.a) as u32, Motivo::LongMin),
                    prueba::LONG_MAX => ((self.longitud > p.a) as u32, Motivo::LongMax),
                    // el trie da en cuántos de los "enum" está la cadena: fallan los demás
                    prueba::ENUM_CAD => (p.a.saturating_sub(a.valor(self.nodo)), Motivo::EnumCad),
                    _ => (0, Motivo::NoAdmitido),
                };
                if fallos > 0 {
                    self.violar(
                        pos,
                        motivo,
                        if self.fallo_temprano { 1 } else { fallos },
                        false,
                    );
                    if self.fallo_temprano {
                        break;
                    }
                }
            }
        }
        self.r.pendientes = self.r.pendientes.saturating_add(t.pendientes);

        // ---- pila ----
        if t.desapila {
            self.nivel = self.nivel.saturating_sub(1);
        }
        if t.cima != NINGUNO && self.nivel > 0 {
            self.pila[self.nivel - 1].simbolo = t.cima;
        }
        if t.apila != NINGUNO {
            if self.nivel >= self.max_profundidad {
                return self.detener(Estado::Limite, pos, Fallo::Profundidad);
            }
            self.pila[self.nivel] = Marco::nuevo(t.apila, tipo == TipoToken::LlaveAbre);
            self.nivel += 1;
            self.r.profundidad = self.r.profundidad.max(self.nivel as u32);
        }

        // ---- destino ----
        self.estado = if t.destino != NINGUNO {
            t.destino
        } else {
            let cima = if self.nivel == 0 {
                a.pila_inicial
            } else {
                self.pila[self.nivel - 1].simbolo
            };
            a.retorno[cima as usize]
        };
        if self.r.violaciones > 0 && self.fallo_temprano {
            self.terminado = true;
        }
    }
}

/// Bytes de un marco de pila cuando la máscara ocupa `palabras` palabras de 64 bits.
pub const fn bytes_por_marco(palabras: usize) -> usize {
    core::mem::size_of::<Marco<1>>() + palabras.saturating_sub(1) * 8
}

/// Bytes del validador sin contar la pila: el lexer, el resultado y el estado del autómata.
pub const fn bytes_fijos() -> usize {
    core::mem::size_of::<Validador<'static, 0, 0>>()
}
