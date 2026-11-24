//! Motor de validación: ejecuta el autómata de pila ponderado sobre los bytes del payload.
//!
//! Una sola pasada, sin construir ningún árbol y sin reservar memoria: todo el estado vive dentro
//! de `Validador`. Sus parámetros fijan el tamaño, que se conoce al compilar (la cota O(d)):
//!
//! - `D`: profundidad máxima de la pila (contenedores abiertos a la vez);
//! - `W`: palabras de 64 bits de la máscara de cada marco;
//! - `E`: ejecuciones a la vez, la principal y `E - 1` sub-ejecuciones (producto de autómatas en
//!   ejecución: anyOf/oneOf ambiguos, not, if/then/else, contains, propertyNames, dependentSchemas);
//! - `U`: huellas de elementos que se pueden guardar a la vez ("uniqueItems");
//! - `C`: niveles de la pila de huellas (`D` si el esquema usa huellas, 0 si no).
//!
//! Los tres últimos valen por omisión lo mínimo (una ejecución y nada de huellas): un esquema sin
//! estas estrategias no paga nada por ellas. `new` comprueba que el autómata quepa.
//!
//! Es la misma semántica que `compilador/automata/interprete.py`, que sirve de referencia. Por cada
//! token: nacen las sub-ejecuciones que piden las transiciones, se actualizan las huellas, da su paso
//! cada ejecución (la principal primero) y se combinan los grupos cuyas ramas terminaron, del más
//! reciente al más antiguo.

use core::cmp::Ordering;
use core::fmt;

use crate::automata::{Automata, NINGUNO, TOKENS, Transicion, accion, grupo, prueba};
use crate::decimal::Numero;
use crate::huellas;
use crate::lexer::{Evento, Lexer, TipoError, TipoToken};

/// Bytes que se conservan de cada clave para la ruta del error.
pub const CLAVE_MAX: usize = 24;
/// Bytes de la ruta completa.
pub const RUTA_MAX: usize = 256;
/// «…» en UTF-8: marca lo que no cupo.
const PUNTOS: [u8; 3] = [0xE2, 0x80, 0xA6];
/// Ejecuciones a la vez para un esquema que crea sub-ejecuciones: la principal y hasta 16 más.
pub const EJECUCIONES: usize = 17;
/// Huellas de elementos que se pueden guardar a la vez para un esquema con "uniqueItems".
pub const HUELLAS: usize = 1024;

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
    /// Superó un límite: profundidad de la pila, tamaño, tiempo o sub-ejecuciones.
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
    // ---- estrategias de respaldo ----
    Patron,
    Multiplo,
    EnumHuella,
    Unicos,
    Contiene,
    AnyOf,
    OneOf,
    Not,
    If,
    DependentSchemas,
    PropertyNames,
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
    /// Hacían falta más sub-ejecuciones a la vez de las que caben (`E - 1`).
    Hilos,
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
            Motivo::Patron => "patron",
            Motivo::Multiplo => "multiplo",
            Motivo::EnumHuella => "enum_huella",
            Motivo::Unicos => "unicos",
            Motivo::Contiene => "contiene",
            Motivo::AnyOf => "anyOf",
            Motivo::OneOf => "oneOf",
            Motivo::Not => "not",
            Motivo::If => "if",
            Motivo::DependentSchemas => "dependentSchemas",
            Motivo::PropertyNames => "propertyNames",
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
            Fallo::Hilos => "hilos",
        }
    }
}

/// Marco de pila: el símbolo de Γ y los registros acotados.
#[derive(Clone, Copy)]
struct Marco<const W: usize> {
    simbolo: u32,
    /// Propiedades o elementos leídos.
    cuenta: u32,
    /// Elementos que cumplen "contains": seguro y quizá (con comprobaciones diferidas).
    coincidencias: u32,
    dudosas: u32,
    /// Propiedades vistas.
    mascara: [u64; W],
}

impl<const W: usize> Marco<W> {
    const fn nuevo(simbolo: u32) -> Self {
        Marco {
            simbolo,
            cuenta: 0,
            coincidencias: 0,
            dudosas: 0,
            mascara: [0; W],
        }
    }
}

/// Lo que cada nivel abierto aporta a la ruta: si es un objeto y el comienzo de su clave actual.
/// Lo lleva solo la ejecución principal.
#[derive(Clone, Copy)]
struct Clave {
    es_objeto: bool,
    /// Ya se leyó alguna clave.
    con_clave: bool,
    /// La clave actual no cupo entera.
    truncada: bool,
    largo: u8,
    bytes: [u8; CLAVE_MAX],
}

impl Clave {
    const fn nueva(es_objeto: bool) -> Self {
        Clave {
            es_objeto,
            con_clave: false,
            truncada: false,
            largo: 0,
            bytes: [0; CLAVE_MAX],
        }
    }

    /// Añade un byte de la clave que se está leyendo, sin partir ningún carácter.
    fn byte(&mut self, b: u8) {
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
            if self.largo as usize + necesita > CLAVE_MAX {
                self.truncada = true;
                return;
            }
        }
        self.bytes[self.largo as usize] = b;
        self.largo += 1;
    }

    /// Escribe en `tramo` lo que este nivel aporta a la ruta y devuelve su longitud (0 si nada).
    /// `cuenta` es el número de elementos leídos, si el nivel es un arreglo.
    fn segmento(&self, cuenta: u32, tramo: &mut [u8; 2 * CLAVE_MAX + 4]) -> usize {
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
            for &b in &self.bytes[..self.largo as usize] {
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
            if cuenta == 0 {
                return 0;
            }
            poner(b'/');
            let mut cifras = [0u8; 10];
            let (mut indice, mut i) = (cuenta - 1, cifras.len());
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

/// Una ejecución del autómata: la principal (índice 0) o una sub-ejecución de un grupo.
#[derive(Clone, Copy)]
struct Hilo<const D: usize, const W: usize> {
    vivo: bool,
    terminado: bool,
    estado: u32,
    /// Marcos apilados, sin contar el fondo.
    nivel: usize,
    pila: [Marco<W>; D],
    /// Peso y comprobaciones pendientes (en la principal se usan los del resultado).
    peso: u32,
    pendientes: u32,
    /// Cadena en curso: posición en el trie del estado y en el AFD de sus patrones.
    nodo: u32,
    patron: u32,
    /// Número en curso: pruebas que no cumple y motivo de la primera.
    fallos_numero: u32,
    motivo_numero: Motivo,
    /// Grupo al que pertenece y lugar de su rama (if, then, else...).
    grupo: u8,
    rol: u8,
}

impl<const D: usize, const W: usize> Hilo<D, W> {
    const fn nuevo() -> Self {
        Hilo {
            vivo: false,
            terminado: false,
            estado: 0,
            nivel: 0,
            pila: [Marco::nuevo(0); D],
            peso: 0,
            pendientes: 0,
            nodo: NINGUNO,
            patron: NINGUNO,
            fallos_numero: 0,
            motivo_numero: Motivo::Entero,
            grupo: 0,
            rol: 0,
        }
    }
}

/// Grupo de sub-ejecuciones vivo.
#[derive(Clone, Copy)]
struct GrupoVivo {
    vivo: bool,
    clase: u8,
    /// Ejecución que lo creó y, para "contains", el marco de su arreglo.
    madre: u8,
    marco: usize,
}

/// Objeto o arreglo abierto, para las huellas: lo mismo para todas las ejecuciones.
#[derive(Clone, Copy)]
struct Contenedor {
    objeto: bool,
    espera_clave: bool,
    /// Guarda las huellas de sus elementos (uniqueItems), desde `base` en el fondo común.
    recoger: bool,
    repetido: bool,
    desbordado: bool,
    acumulado: u64,
    clave: u64,
    base: u32,
}

impl Contenedor {
    const fn nuevo(objeto: bool, base: u32) -> Self {
        Contenedor {
            objeto,
            espera_clave: objeto,
            recoger: false,
            repetido: false,
            desbordado: false,
            acumulado: if objeto {
                huellas::INICIO_OBJETO
            } else {
                huellas::INICIO_ARREGLO
            },
            clave: 0,
            base,
        }
    }
}

/// Resultado de una rama o de un grupo.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Veredicto {
    Cumple,
    Falla,
    Duda,
}

/// El autómata en ejecución, sin el lexer.
struct Nucleo<'a, const D: usize, const W: usize, const E: usize, const U: usize, const C: usize> {
    a: &'a Automata<'a>,
    hilos: [Hilo<D, W>; E],
    /// Sub-ejecuciones vivas, en orden de nacimiento.
    orden: [u8; E],
    n_orden: usize,
    grupos: [GrupoVivo; E],
    /// Grupos vivos, en orden de nacimiento.
    orden_grupos: [u8; E],
    n_grupos: usize,
    /// Faltó sitio para una sub-ejecución: se detiene al terminar el token.
    sin_hilos: bool,
    claves: [Clave; D],
    contenedores: [Contenedor; C],
    n_contenedores: usize,
    guardadas: [u64; U],
    n_guardadas: usize,
    /// Huella del último valor terminado, el último contenedor cerrado y los valores en curso.
    ultima: u64,
    cerrado: Contenedor,
    h_cadena: u64,
    h_numero: u64,
    max_profundidad: usize,
    /// Algún estado tiene el AFD de unos "pattern" (si no, ni se mira).
    con_patrones: bool,
    fallo_temprano: bool,
    /// Longitud en caracteres de la cadena en curso, y si es una clave de la ejecución principal.
    longitud: u32,
    leyendo_clave: bool,
    r: Resultado,
    terminado: bool,
}

/// Validador de documentos JSON contra un esquema compilado.
pub struct Validador<
    'a,
    const D: usize,
    const W: usize,
    const E: usize = 1,
    const U: usize = 0,
    const C: usize = 0,
> {
    lexer: Lexer,
    nucleo: Nucleo<'a, D, W, E, U, C>,
    max_bytes: u64,
    cerrado: bool,
}

const RESULTADO_INICIAL: Resultado = Resultado {
    estado: Estado::Valido,
    violaciones: 0,
    pendientes: 0,
    profundidad: 0,
    primera: None,
    error: None,
    ruta: Ruta::vacia(),
};

impl<'a, const D: usize, const W: usize, const E: usize, const U: usize, const C: usize>
    Validador<'a, D, W, E, U, C>
{
    /// Falla al compilar si el autómata no cabe cuando es una constante; en otro caso, entra en
    /// pánico: si `W` es menor que `automata.palabras`, si `E` es 0 (no cabe la ejecución
    /// principal) o 1 y el autómata crea sub-ejecuciones, o si usa huellas y `C` es menor que `D`.
    pub const fn new(automata: &'a Automata<'a>) -> Self {
        assert!(
            automata.palabras <= W,
            "la máscara del autómata no cabe en W palabras"
        );
        assert!(
            E >= 1,
            "E cuenta la ejecución principal: debe ser al menos 1"
        );
        assert!(
            E > 1 || automata.grupos.is_empty(),
            "el autómata crea sub-ejecuciones: E debe ser mayor que 1"
        );
        assert!(
            C >= D || !automata.usa_huellas,
            "el autómata usa huellas: C debe ser al menos D"
        );
        let mut hilos = [Hilo::nuevo(); E];
        hilos[0].vivo = true;
        hilos[0].estado = automata.inicial;
        Validador {
            lexer: Lexer::new(),
            max_bytes: u64::MAX,
            cerrado: false,
            nucleo: Nucleo {
                a: automata,
                hilos,
                orden: [0; E],
                n_orden: 0,
                grupos: [GrupoVivo {
                    vivo: false,
                    clase: 0,
                    madre: 0,
                    marco: 0,
                }; E],
                orden_grupos: [0; E],
                n_grupos: 0,
                sin_hilos: false,
                claves: [Clave::nueva(false); D],
                contenedores: [Contenedor::nuevo(false, 0); C],
                n_contenedores: 0,
                guardadas: [0; U],
                n_guardadas: 0,
                ultima: 0,
                cerrado: Contenedor::nuevo(false, 0),
                h_cadena: 0,
                h_numero: 0,
                max_profundidad: D,
                con_patrones: tiene_patrones(automata.patron),
                fallo_temprano: false,
                longitud: 0,
                leyendo_clave: false,
                r: RESULTADO_INICIAL,
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
    /// crear uno nuevo: no toca las pilas, cuyos marcos se inician al apilarse.
    pub fn reiniciar(&mut self) {
        self.lexer.reiniciar();
        self.cerrado = false;
        let n = &mut self.nucleo;
        for h in n.hilos.iter_mut() {
            h.vivo = false;
        }
        for g in n.grupos.iter_mut() {
            g.vivo = false;
        }
        let principal = &mut n.hilos[0];
        principal.vivo = true;
        principal.estado = n.a.inicial;
        principal.nivel = 0;
        principal.nodo = NINGUNO;
        principal.patron = NINGUNO;
        principal.fallos_numero = 0;
        n.n_orden = 0;
        n.n_grupos = 0;
        n.sin_hilos = false;
        n.n_contenedores = 0;
        n.n_guardadas = 0;
        n.longitud = 0;
        n.leyendo_clave = false;
        n.terminado = false;
        n.r = RESULTADO_INICIAL;
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

impl<const D: usize, const W: usize, const E: usize, const U: usize, const C: usize>
    Nucleo<'_, D, W, E, U, C>
{
    /// Índice de la k-ésima ejecución viva: 0 es la principal y las demás van en orden de nacimiento.
    #[inline(always)]
    fn ejecucion(&self, k: usize) -> usize {
        if k == 0 {
            0
        } else {
            self.orden[k - 1] as usize
        }
    }

    /// JSON Pointer de la posición actual. Sin la cima, es la ruta del contenedor abierto.
    fn ruta(&self, con_la_cima: bool) -> Ruta {
        let principal = &self.hilos[0];
        let mut ruta = Ruta::vacia();
        let hasta = if con_la_cima {
            principal.nivel
        } else {
            principal.nivel.saturating_sub(1)
        };
        let mut tramo = [0u8; 2 * CLAVE_MAX + 4];
        for (clave, marco) in self.claves[..hasta].iter().zip(&principal.pila[..hasta]) {
            let n = clave.segmento(marco.cuenta, &mut tramo);
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

    /// Suma violaciones a la ejecución `i`. `del_contenedor`: la violación es del objeto o arreglo
    /// de la cima (falta una propiedad, sobran elementos), no del valor que se está leyendo.
    #[inline(always)]
    fn violar(&mut self, i: usize, pos: u64, motivo: Motivo, cuantas: u32, del_contenedor: bool) {
        if cuantas == 0 {
            return;
        }
        if i != 0 {
            self.hilos[i].peso = self.hilos[i].peso.saturating_add(cuantas);
            return;
        }
        if self.r.primera.is_none() {
            self.r.primera = Some((pos, motivo));
            self.r.ruta = self.ruta(!del_contenedor);
        }
        self.r.violaciones = self.r.violaciones.saturating_add(cuantas);
    }

    #[inline(always)]
    fn pendiente(&mut self, i: usize, cuantas: u32) {
        let contador = if i == 0 {
            &mut self.r.pendientes
        } else {
            &mut self.hilos[i].pendientes
        };
        *contador = contador.saturating_add(cuantas);
    }

    fn evento(&mut self, evento: Evento<'_>) {
        if self.terminado {
            return;
        }
        let a = self.a;
        match evento {
            Evento::InicioCadena => {
                if self.con_hilos() {
                    self.nacer(TipoToken::Cadena);
                }
                self.longitud = 0;
                for k in 0..=self.vivas() {
                    let i = self.ejecucion(k);
                    let q = self.hilos[i].estado as usize;
                    self.hilos[i].nodo = a.trie[q];
                    if self.con_patrones {
                        self.hilos[i].patron = a.patron[q];
                    }
                }
                let principal = &self.hilos[0];
                self.leyendo_clave =
                    a.otra[principal.estado as usize] != NINGUNO && principal.nivel > 0;
                if self.leyendo_clave {
                    let clave = &mut self.claves[principal.nivel - 1];
                    clave.con_clave = true;
                    clave.truncada = false;
                    clave.largo = 0;
                }
                if self.con_huellas() {
                    self.h_cadena = huellas::fnv(huellas::FNV_INICIO, &[huellas::CADENA]);
                }
            }
            Evento::Fragmento(bytes) => {
                // Recorridos separados: el conteo es sencillo y los demás se abandonan pronto
                // (cuando la cadena ya no puede casar, o cuando ya se guardó el comienzo de la clave).
                let caracteres = bytes.iter().filter(|&&b| b & 0xC0 != 0x80).count();
                self.longitud = self
                    .longitud
                    .saturating_add(caracteres.min(u32::MAX as usize) as u32);
                for k in 0..=self.vivas() {
                    let i = self.ejecucion(k);
                    let h = &mut self.hilos[i];
                    for &b in bytes {
                        if h.nodo == NINGUNO {
                            break;
                        }
                        h.nodo = a.avanzar(h.nodo, b);
                    }
                    if self.con_patrones {
                        for &b in bytes {
                            if h.patron == NINGUNO {
                                break;
                            }
                            h.patron = a.avanzar(h.patron, b);
                        }
                    }
                }
                if self.leyendo_clave {
                    let clave = &mut self.claves[self.hilos[0].nivel - 1];
                    for &b in bytes {
                        if clave.truncada {
                            break;
                        }
                        clave.byte(b);
                    }
                }
                if self.con_huellas() {
                    self.h_cadena = huellas::fnv(self.h_cadena, bytes);
                }
            }
            Evento::Numero { lexema, .. } => {
                if self.con_hilos() {
                    self.nacer(TipoToken::Numero);
                }
                let numero = Numero::new(lexema);
                if self.con_huellas() {
                    self.h_numero = numero.huella();
                }
                for k in 0..=self.vivas() {
                    let i = self.ejecucion(k);
                    self.probar_numero(i, &numero);
                }
            }
            Evento::Token { tipo, inicio, .. } => {
                if tipo != TipoToken::Cadena && tipo != TipoToken::Numero && self.con_hilos() {
                    self.nacer(tipo);
                }
                if E > 1 && self.sin_hilos {
                    return self.detener(Estado::Limite, inicio, Fallo::Hilos);
                }
                if self.con_huellas() {
                    self.huella(tipo);
                }
                self.paso(0, tipo, inicio);
                let mut k = 0;
                while k < self.vivas() && !self.terminado {
                    self.paso(self.orden[k] as usize, tipo, inicio);
                    k += 1;
                }
                self.leyendo_clave = false;
                if !self.terminado && self.vivas() > 0 {
                    self.combinar(inicio);
                }
            }
        }
    }

    /// Sub-ejecuciones vivas. Con `E = 1` es la constante 0 y los bucles sobre ellas desaparecen al compilar.
    #[inline(always)]
    fn vivas(&self) -> usize {
        if E > 1 { self.n_orden } else { 0 }
    }

    /// Hay que calcular huellas. Con `C = 0` es la constante `false` (`new` garantiza que entonces el
    /// autómata no las usa).
    #[inline(always)]
    fn con_huellas(&self) -> bool {
        C > 0 && self.a.usa_huellas
    }

    /// El autómata crea sub-ejecuciones y caben: si no, ni se buscan grupos (el caso común es rápido).
    #[inline(always)]
    fn con_hilos(&self) -> bool {
        E > 1 && !self.a.grupos.is_empty()
    }

    /// Hace nacer los grupos que piden las transiciones de todas las ejecuciones con el token que
    /// empieza. También las recién nacidas: leen ese mismo token.
    fn nacer(&mut self, tipo: TipoToken) {
        let a = self.a;
        let mut k = 0;
        while k <= self.n_orden {
            let madre = self.ejecucion(k);
            k += 1;
            let q = self.hilos[madre].estado as usize;
            // en un estado que lee claves, todas las transiciones de clave tienen los mismos grupos
            let id = if tipo == TipoToken::Cadena && a.otra[q] != NINGUNO {
                a.otra[q]
            } else {
                a.delta[q * TOKENS + tipo as usize]
            };
            if id == NINGUNO {
                continue;
            }
            let t = &a.transiciones[id as usize];
            for g in &a.grupos[t.hilos as usize..(t.hilos + t.n_hilos) as usize] {
                let Some(ranura) = (0..E).find(|&s| !self.grupos[s].vivo) else {
                    self.sin_hilos = true;
                    return;
                };
                self.grupos[ranura] = GrupoVivo {
                    vivo: true,
                    clase: g.clase,
                    madre: madre as u8,
                    marco: self.hilos[madre].nivel.wrapping_sub(1),
                };
                self.orden_grupos[self.n_grupos] = ranura as u8;
                self.n_grupos += 1;
                let ramas = &a.ramas[g.ramas as usize..(g.ramas + g.n_ramas) as usize];
                for (rol, &inicio) in ramas.iter().enumerate() {
                    if inicio == NINGUNO {
                        continue;
                    }
                    let Some(s) = (1..E).find(|&s| !self.hilos[s].vivo) else {
                        self.sin_hilos = true;
                        return;
                    };
                    let h = &mut self.hilos[s];
                    h.vivo = true;
                    h.terminado = false;
                    h.estado = inicio;
                    h.nivel = 0;
                    h.peso = 0;
                    h.pendientes = 0;
                    h.nodo = NINGUNO;
                    h.patron = NINGUNO;
                    h.fallos_numero = 0;
                    h.grupo = ranura as u8;
                    h.rol = rol as u8;
                    self.orden[self.n_orden] = s as u8;
                    self.n_orden += 1;
                }
            }
        }
    }

    /// Combina los grupos cuyas ramas terminaron con este token, del más reciente al más antiguo.
    fn combinar(&mut self, pos: u64) {
        let mut gi = self.n_grupos;
        while gi > 0 {
            gi -= 1;
            let g = self.orden_grupos[gi] as usize;
            let (mut terminado, mut cumplen, mut dudan) = (false, 0u32, 0u32);
            let mut por_rol = [Veredicto::Cumple; 3];
            for &s in &self.orden[..self.n_orden] {
                let h = &self.hilos[s as usize];
                if !h.vivo || h.grupo as usize != g {
                    continue;
                }
                terminado = h.terminado; // todas las ramas leen el mismo valor: terminan a la vez
                let v = if h.peso > 0 {
                    Veredicto::Falla
                } else if h.pendientes > 0 {
                    Veredicto::Duda
                } else {
                    Veredicto::Cumple
                };
                cumplen += (v == Veredicto::Cumple) as u32;
                dudan += (v == Veredicto::Duda) as u32;
                if (h.rol as usize) < por_rol.len() {
                    por_rol[h.rol as usize] = v;
                }
            }
            if !terminado {
                continue;
            }
            for &s in &self.orden[..self.n_orden] {
                let h = &mut self.hilos[s as usize];
                if h.vivo && h.grupo as usize == g {
                    h.vivo = false;
                }
            }
            self.orden_grupos.copy_within(gi + 1..self.n_grupos, gi);
            self.n_grupos -= 1;
            let grupo_vivo = self.grupos[g];
            self.grupos[g].vivo = false;

            let veredicto = match grupo_vivo.clase {
                grupo::ALGUNA if cumplen > 0 => Veredicto::Cumple,
                grupo::ALGUNA if dudan == 0 => Veredicto::Falla,
                grupo::UNA if cumplen > 1 || cumplen + dudan == 0 => Veredicto::Falla,
                grupo::UNA if cumplen == 1 && dudan == 0 => Veredicto::Cumple,
                grupo::ALGUNA | grupo::UNA => Veredicto::Duda,
                grupo::NO => match por_rol[0] {
                    Veredicto::Cumple => Veredicto::Falla,
                    Veredicto::Falla => Veredicto::Cumple,
                    Veredicto::Duda => Veredicto::Duda,
                },
                grupo::SI | grupo::DEPENDIENTE => match por_rol[0] {
                    Veredicto::Cumple => por_rol[1],
                    Veredicto::Falla => por_rol[2],
                    Veredicto::Duda if por_rol[1] == por_rol[2] => por_rol[1],
                    Veredicto::Duda => Veredicto::Duda,
                },
                _ => por_rol[0],
            };
            let madre = grupo_vivo.madre as usize;
            if grupo_vivo.clase == grupo::CONTIENE {
                let marco = &mut self.hilos[madre].pila[grupo_vivo.marco];
                match veredicto {
                    Veredicto::Cumple => {
                        marco.coincidencias = marco.coincidencias.saturating_add(1)
                    }
                    Veredicto::Duda => marco.dudosas = marco.dudosas.saturating_add(1),
                    Veredicto::Falla => {}
                }
            } else if veredicto == Veredicto::Falla {
                let motivo = match grupo_vivo.clase {
                    grupo::ALGUNA => Motivo::AnyOf,
                    grupo::UNA => Motivo::OneOf,
                    grupo::NO => Motivo::Not,
                    grupo::SI => Motivo::If,
                    grupo::DEPENDIENTE => Motivo::DependentSchemas,
                    grupo::NOMBRES => Motivo::PropertyNames,
                    _ => Motivo::Contiene,
                };
                self.violar(madre, pos, motivo, 1, false);
            } else if veredicto == Veredicto::Duda {
                self.pendiente(madre, 1);
            }
        }
        let mut j = 0;
        for k in 0..self.n_orden {
            if self.hilos[self.orden[k] as usize].vivo {
                self.orden[j] = self.orden[k];
                j += 1;
            }
        }
        self.n_orden = j;
        if self.r.violaciones > 0 && self.fallo_temprano {
            self.terminado = true;
        }
    }

    /// Huellas de los valores: dependen solo del documento, así que son comunes a todas las ejecuciones.
    fn huella(&mut self, tipo: TipoToken) {
        match tipo {
            TipoToken::LlaveAbre | TipoToken::CorcheteAbre => {
                if self.n_contenedores < C {
                    self.contenedores[self.n_contenedores] =
                        Contenedor::nuevo(tipo == TipoToken::LlaveAbre, self.n_guardadas as u32);
                    self.n_contenedores += 1;
                }
            }
            TipoToken::LlaveCierra | TipoToken::CorcheteCierra => {
                if self.n_contenedores > 0 {
                    self.n_contenedores -= 1;
                    let c = self.contenedores[self.n_contenedores];
                    self.cerrado = c;
                    self.n_guardadas = c.base as usize;
                    self.terminar_valor(if c.objeto {
                        huellas::cerrar_objeto(c.acumulado)
                    } else {
                        huellas::cerrar_arreglo(c.acumulado)
                    });
                }
            }
            TipoToken::Cadena => {
                let h = huellas::mezclar(self.h_cadena);
                match self.n_contenedores.checked_sub(1) {
                    Some(c) if self.contenedores[c].espera_clave => {
                        self.contenedores[c].clave = h;
                        self.contenedores[c].espera_clave = false;
                    }
                    _ => self.terminar_valor(h),
                }
            }
            TipoToken::Numero => self.terminar_valor(self.h_numero),
            TipoToken::True => self.terminar_valor(huellas::de_literal(huellas::VERDADERO)),
            TipoToken::False => self.terminar_valor(huellas::de_literal(huellas::FALSO)),
            TipoToken::Null => self.terminar_valor(huellas::de_literal(huellas::NULO)),
            TipoToken::Coma => {
                if let Some(c) = self.n_contenedores.checked_sub(1)
                    && self.contenedores[c].objeto
                {
                    self.contenedores[c].espera_clave = true;
                }
            }
            _ => {}
        }
    }

    fn terminar_valor(&mut self, h: u64) {
        self.ultima = h;
        let Some(i) = self.n_contenedores.checked_sub(1) else {
            return;
        };
        let c = &mut self.contenedores[i];
        if c.objeto {
            c.acumulado = huellas::miembro(c.acumulado, c.clave, h);
            return;
        }
        c.acumulado = huellas::elemento(c.acumulado, h);
        if c.recoger && !c.repetido {
            if self.guardadas[c.base as usize..self.n_guardadas].contains(&h) {
                c.repetido = true;
            } else if self.n_guardadas >= U {
                c.desbordado = true;
            } else {
                self.guardadas[self.n_guardadas] = h;
                self.n_guardadas += 1;
            }
        }
    }

    /// Evalúa las pruebas numéricas de la ejecución `i` mientras el lexema está disponible; se
    /// aplican en `paso`.
    #[inline(always)]
    fn probar_numero(&mut self, i: usize, numero: &Numero<'_>) {
        let a = self.a;
        let h = &mut self.hilos[i];
        h.fallos_numero = 0;
        let id = a.delta[h.estado as usize * TOKENS + TipoToken::Numero as usize];
        if id == NINGUNO {
            return;
        }
        let t = &a.transiciones[id as usize];
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
                prueba::MULTIPLO => (
                    !numero.es_multiplo(&a.decimales[p.a as usize], a.digitos),
                    Motivo::Multiplo,
                ),
                _ => (false, Motivo::Entero),
            };
            if falla {
                if h.fallos_numero == 0 {
                    h.motivo_numero = motivo;
                }
                h.fallos_numero += 1;
                if self.fallo_temprano {
                    break;
                }
            }
        }
    }

    /// Prueba de una transición que cierra un contenedor, sobre el marco de su cima.
    fn cierre(&self, i: usize, clase: u8, pa: u32, pb: u32) -> (Veredicto, Motivo) {
        match clase {
            prueba::ENUM_HUELLA => {
                let permitidas = &self.a.huellas[pa as usize..(pa + pb) as usize];
                let v = if permitidas.contains(&self.ultima) {
                    Veredicto::Cumple
                } else {
                    Veredicto::Falla
                };
                (v, Motivo::EnumHuella)
            }
            prueba::UNICOS => {
                let v = if self.cerrado.repetido {
                    Veredicto::Falla
                } else if self.cerrado.desbordado {
                    Veredicto::Duda
                } else {
                    Veredicto::Cumple
                };
                (v, Motivo::Unicos)
            }
            prueba::CONTIENE => {
                // el número de elementos que cumplen está en [c, c + d]
                let h = &self.hilos[i];
                let marco = &h.pila[h.nivel - 1];
                let (c, d) = (marco.coincidencias, marco.dudosas);
                let tope = c.saturating_add(d);
                let v = if c >= pa && (pb == NINGUNO || tope <= pb) {
                    Veredicto::Cumple
                } else if tope < pa || (pb != NINGUNO && c > pb) {
                    Veredicto::Falla
                } else {
                    Veredicto::Duda
                };
                (v, Motivo::Contiene)
            }
            _ => (Veredicto::Cumple, Motivo::NoAdmitido),
        }
    }

    /// Un paso de la ejecución `i`: δ(estado, token, cima).
    #[inline]
    fn paso(&mut self, i: usize, tipo: TipoToken, pos: u64) {
        let a = self.a;
        let q = self.hilos[i].estado as usize;
        let id = if tipo == TipoToken::Cadena && a.otra[q] != NINGUNO {
            // posición de clave: el AFD de las claves dice qué transición toca
            match a.valor(self.hilos[i].nodo) {
                0 => a.otra[q],
                valor => valor - 1,
            }
        } else {
            a.delta[q * TOKENS + tipo as usize]
        };
        if id == NINGUNO {
            if i == 0 {
                self.detener(Estado::Malformado, pos, Fallo::Sintactico);
            }
            return;
        }
        let t: &Transicion = &a.transiciones[id as usize];

        // ---- acción sobre los registros del marco de la cima ----
        if t.accion != 0 {
            let Some(cima) = self.hilos[i].nivel.checked_sub(1) else {
                if i == 0 {
                    self.detener(Estado::Malformado, pos, Fallo::Sintactico);
                }
                return;
            };
            let (cuenta, mascara) = (
                self.hilos[i].pila[cima].cuenta,
                self.hilos[i].pila[cima].mascara,
            );
            match t.accion {
                accion::CLAVE => {
                    let cuenta = cuenta.saturating_add(1);
                    self.hilos[i].pila[cima].cuenta = cuenta;
                    if t.b != NINGUNO && cuenta == t.b + 1 {
                        self.violar(i, pos, Motivo::MaxProperties, 1, true);
                    }
                    if t.a != NINGUNO {
                        let (palabra, bit) = ((t.a / 64) as usize, t.a % 64);
                        if mascara[palabra] >> bit & 1 == 1 {
                            // la vea la ejecución que la vea, el documento está mal formado
                            return self.detener(Estado::Malformado, pos, Fallo::ClaveRepetida);
                        }
                        self.hilos[i].pila[cima].mascara[palabra] |= 1 << bit;
                    }
                }
                accion::CERRAR_OBJETO => {
                    let faltan = |m: u32| -> u32 {
                        let exigida =
                            &a.mascaras[m as usize * a.palabras..(m as usize + 1) * a.palabras];
                        exigida
                            .iter()
                            .zip(mascara.iter())
                            .map(|(e, m)| (e & !m).count_ones())
                            .sum()
                    };
                    if t.a != NINGUNO {
                        self.violar(i, pos, Motivo::Required, faltan(t.a), true);
                    }
                    for dep in &a.dependencias[t.b as usize..(t.b + t.c) as usize] {
                        if mascara[(dep.bit / 64) as usize] >> (dep.bit % 64) & 1 == 1 {
                            self.violar(
                                i,
                                pos,
                                Motivo::DependentRequired,
                                faltan(dep.mascara),
                                true,
                            );
                        }
                    }
                    if t.d != NINGUNO && cuenta < t.d {
                        self.violar(i, pos, Motivo::MinProperties, 1, true);
                    }
                }
                accion::ELEMENTO => {
                    let cuenta = cuenta.saturating_add(1);
                    self.hilos[i].pila[cima].cuenta = cuenta;
                    if t.a != NINGUNO && cuenta == t.a + 1 {
                        self.violar(i, pos, Motivo::MaxItems, 1, true);
                    }
                }
                accion::CERRAR_ARREGLO if t.a != NINGUNO && cuenta < t.a => {
                    self.violar(i, pos, Motivo::MinItems, 1, true);
                }
                _ => {}
            }
        }

        // ---- peso fijo y pruebas, de menor a mayor costo ----
        self.violar(i, pos, Motivo::NoAdmitido, t.peso, false);
        // (el rango de pruebas solo se toma en los tokens que pueden tenerlas)
        let pruebas = || &a.pruebas[t.pruebas as usize..(t.pruebas + t.n_pruebas) as usize];
        match tipo {
            TipoToken::Numero => {
                let (motivo, fallos) = (self.hilos[i].motivo_numero, self.hilos[i].fallos_numero);
                self.violar(i, pos, motivo, fallos, false);
            }
            TipoToken::Cadena => {
                for p in pruebas() {
                    let h = &self.hilos[i];
                    let (fallos, motivo) = match p.clase {
                        prueba::LONG_MIN => ((self.longitud < p.a) as u32, Motivo::LongMin),
                        prueba::LONG_MAX => ((self.longitud > p.a) as u32, Motivo::LongMax),
                        // el trie da en cuántos de los "enum" está la cadena: fallan los demás
                        prueba::ENUM_CAD => (p.a.saturating_sub(a.valor(h.nodo)), Motivo::EnumCad),
                        // el AFD da con cuántos patrones casa: fallan los demás
                        prueba::PATRON => (p.a.saturating_sub(a.valor(h.patron)), Motivo::Patron),
                        _ => (0, Motivo::NoAdmitido),
                    };
                    if fallos > 0 {
                        let cuantas = if self.fallo_temprano { 1 } else { fallos };
                        self.violar(i, pos, motivo, cuantas, false);
                        if self.fallo_temprano {
                            break;
                        }
                    }
                }
            }
            TipoToken::LlaveCierra | TipoToken::CorcheteCierra if self.hilos[i].nivel > 0 => {
                for p in pruebas() {
                    match self.cierre(i, p.clase, p.a, p.b) {
                        (Veredicto::Falla, motivo) => {
                            self.violar(i, pos, motivo, 1, true);
                            if self.fallo_temprano {
                                break;
                            }
                        }
                        (Veredicto::Duda, _) => self.pendiente(i, 1),
                        (Veredicto::Cumple, _) => {}
                    }
                }
            }
            _ => {}
        }
        self.pendiente(i, t.pendientes);

        // ---- pila ----
        let h = &mut self.hilos[i];
        if t.desapila {
            h.nivel = h.nivel.saturating_sub(1);
        }
        if t.cima != NINGUNO && h.nivel > 0 {
            h.pila[h.nivel - 1].simbolo = t.cima;
        }
        if t.apila != NINGUNO {
            if h.nivel >= D || (i == 0 && h.nivel >= self.max_profundidad) {
                return self.detener(Estado::Limite, pos, Fallo::Profundidad);
            }
            h.pila[h.nivel] = Marco::nuevo(t.apila);
            h.nivel += 1;
            if i == 0 {
                self.claves[h.nivel - 1] = Clave::nueva(tipo == TipoToken::LlaveAbre);
                self.r.profundidad = self.r.profundidad.max(h.nivel as u32);
            }
        }
        if self.con_huellas()
            && t.recoger
            && let Some(c) = self.n_contenedores.checked_sub(1)
        {
            self.contenedores[c].recoger = true;
        }

        // ---- destino ----
        let h = &mut self.hilos[i];
        h.estado = if t.destino != NINGUNO {
            t.destino
        } else {
            let cima = if h.nivel > 0 {
                h.pila[h.nivel - 1].simbolo
            } else if i == 0 {
                a.pila_inicial
            } else {
                a.pila_hilo
            };
            a.retorno[cima as usize]
        };
        if i != 0 {
            h.terminado = h.estado == a.fin_hilo;
        } else if self.r.violaciones > 0 && self.fallo_temprano {
            self.terminado = true;
        }
    }
}

/// `true` si algún estado tiene un AFD de patrones.
const fn tiene_patrones(patron: &[u32]) -> bool {
    let mut i = 0;
    while i < patron.len() {
        if patron[i] != NINGUNO {
            return true;
        }
        i += 1;
    }
    false
}

/// Bytes que cuesta cada nivel de profundidad cuando la máscara ocupa `palabras` palabras de 64
/// bits y caben `ejecuciones` ejecuciones: un marco por ejecución, la clave de la ruta y, si se
/// usan huellas, el contenedor de las huellas.
pub const fn bytes_por_nivel(palabras: usize, ejecuciones: usize, con_huellas: bool) -> usize {
    ejecuciones * (core::mem::size_of::<Marco<1>>() + palabras.saturating_sub(1) * 8)
        + core::mem::size_of::<Clave>()
        + if con_huellas {
            core::mem::size_of::<Contenedor>()
        } else {
            0
        }
}

/// Bytes del validador sin contar los niveles: el lexer, el resultado, el estado de cada ejecución
/// y el fondo de huellas.
pub const fn bytes_fijos<const E: usize, const U: usize>() -> usize {
    core::mem::size_of::<Validador<'static, 0, 0, E, U, 0>>()
}
