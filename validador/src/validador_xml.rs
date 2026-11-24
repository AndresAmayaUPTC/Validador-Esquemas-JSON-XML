//! Motor de validación de XML: ejecuta el autómata de pila ponderado sobre los bytes del documento.
//!
//! Igual que `validador.rs` para JSON: una sola pasada, sin árbol y sin reservar memoria. `D` es la
//! profundidad máxima (elementos abiertos a la vez) y `W` las palabras de 64 bits de la máscara de
//! atributos de cada marco. Es la misma semántica que `compilador/xsd/interprete.py`.
//!
//! Orden de cada paso: acción sobre el marco de la cima; reescritura de la cima y apilado; peso y
//! pruebas (así la ruta de una violación ya incluye el elemento que se acaba de abrir);
//! desapilado; destino.
//!
//! Ningún valor se guarda entero. De un nombre se llevan su posición en el trie del estado, su
//! longitud, una huella de 64 bits y sus primeros bytes (para la ruta). De un texto o un valor de
//! atributo, su longitud en caracteres, su posición en el trie de enumeraciones y en el AFD de sus
//! facetas pattern y, si el tipo es numérico, el estado de dos autómatas finitos pequeños (forma de
//! `xs:integer` y de `xs:decimal`) y hasta `NUMERO_MAX` bytes para compararlo con los límites del
//! esquema.

use core::cmp::Ordering;

use crate::automata::{Automata, NINGUNO, SIN_OTRA, TOKENS, Transicion, accion, prueba};
use crate::decimal::Numero;
use crate::validador::{CLAVE_MAX, Estado, Fallo, Motivo, RUTA_MAX, Resultado, Ruta};
use crate::xml::{EventoXml, LexerXml, TipoXml};

/// Bytes de un valor numérico (sin los espacios de los lados) que se pueden comparar con un límite
/// o una enumeración. Con más, la validación se detiene con el estado `Limite`.
pub const NUMERO_MAX: usize = 256;
const PUNTOS: [u8; 3] = [0xE2, 0x80, 0xA6];

/// Columnas de δ. Las nueve primeras coinciden con `TipoXml`.
mod columna {
    pub const NOMBRE: usize = 3;
    pub const VALOR_ATRIB: usize = 5;
    pub const TEXTO: usize = 8;
    /// Texto que solo tiene espacios en blanco.
    pub const ESPACIOS: usize = 9;
    /// Atributo `xmlns:*` o `xsi:*`.
    pub const ATRIB_NS: usize = 10;
    pub const FIN: usize = 11;
}

/// Comienzo de un nombre: lo que cabe en `CLAVE_MAX` bytes sin partir ningún carácter.
#[derive(Clone, Copy)]
struct Recorte {
    bytes: [u8; CLAVE_MAX],
    largo: u8,
    truncado: bool,
}

impl Recorte {
    const fn nuevo() -> Self {
        Recorte {
            bytes: [0; CLAVE_MAX],
            largo: 0,
            truncado: false,
        }
    }

    fn byte(&mut self, b: u8) {
        if self.truncado {
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
                self.truncado = true;
                return;
            }
        }
        self.bytes[self.largo as usize] = b;
        self.largo += 1;
    }

    fn texto(&self) -> &[u8] {
        &self.bytes[..self.largo as usize]
    }
}

/// Marco de pila: el símbolo de Γ, los dos registros acotados y lo que se recuerda del nombre.
#[derive(Clone, Copy)]
struct Marco<const W: usize> {
    simbolo: u32,
    /// Hijos elemento leídos.
    cuenta: u32,
    /// Atributos vistos.
    mascara: [u64; W],
    /// Longitud en bytes y huella (FNV-1a de 64 bits) del nombre de la etiqueta de apertura.
    largo: u32,
    huella: u64,
    nombre: Recorte,
}

impl<const W: usize> Marco<W> {
    const fn nuevo() -> Self {
        Marco {
            simbolo: 0,
            cuenta: 0,
            mascara: [0; W],
            largo: 0,
            huella: 0,
            nombre: Recorte::nuevo(),
        }
    }
}

/// Valor numérico en curso: sus espacios de los lados se descartan (whiteSpace = collapse).
#[derive(Clone, Copy)]
struct Numerico {
    /// 0 = espacios iniciales, 1 = cuerpo, 2 = espacios finales, 3 = hay espacios en medio.
    fase: u8,
    /// Estados de los autómatas de `[+-]?[0-9]+` y de `[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)`.
    entero: u8,
    decimal: u8,
    desbordado: bool,
    largo: usize,
    bytes: [u8; NUMERO_MAX],
}

impl Numerico {
    const fn nuevo() -> Self {
        Numerico {
            fase: 0,
            entero: 0,
            decimal: 0,
            desbordado: false,
            largo: 0,
            bytes: [0; NUMERO_MAX],
        }
    }

    fn reiniciar(&mut self) {
        self.fase = 0;
        self.entero = 0;
        self.decimal = 0;
        self.desbordado = false;
        self.largo = 0;
    }

    fn byte(&mut self, b: u8) {
        let blanco = matches!(b, b' ' | b'\t' | b'\n' | b'\r');
        match (self.fase, blanco) {
            (0, true) | (2, true) | (3, _) => return,
            (1, true) => {
                self.fase = 2;
                return;
            }
            (2, false) => {
                self.fase = 3;
                return;
            }
            _ => self.fase = 1,
        }
        if self.largo < NUMERO_MAX {
            self.bytes[self.largo] = b;
            self.largo += 1;
        } else {
            self.desbordado = true;
        }
        const MUERTO: u8 = 9;
        self.entero = match (self.entero, b) {
            (0, b'+' | b'-') => 1,
            (0..=2, b'0'..=b'9') => 2,
            _ => MUERTO,
        };
        self.decimal = match (self.decimal, b) {
            (0, b'+' | b'-') => 1,
            (0..=2, b'0'..=b'9') => 2,
            (2, b'.') => 3,
            (0 | 1, b'.') => 4,
            (3..=5, b'0'..=b'9') => 5,
            _ => MUERTO,
        };
    }

    /// `true` si el valor tiene la forma léxica que pide la prueba.
    fn forma(&self, clase: u8) -> bool {
        if self.fase == 3 {
            return false;
        }
        match clase {
            prueba::XS_ENTERO => self.entero == 2,
            prueba::XS_DECIMAL => matches!(self.decimal, 2 | 3 | 5),
            _ => {
                !self.desbordado
                    && matches!(&self.bytes[..self.largo], b"true" | b"false" | b"1" | b"0")
            }
        }
    }

    /// El valor como número; solo tiene sentido si `forma` lo dio por bueno y no se desbordó.
    fn numero(&self) -> Numero<'_> {
        let cuerpo = &self.bytes[..self.largo];
        Numero::new(match cuerpo.first() {
            Some(b'+') => &cuerpo[1..],
            _ => cuerpo,
        })
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
    /// Lo que se está leyendo: 0 = un nombre; otro valor = un texto o un valor de atributo.
    leyendo_valor: bool,
    /// Posición en el trie del estado: nombres declarados o valores de una enumeración.
    nodo: u32,
    /// Posición en el AFD de los patrones del valor, y si se le saltan los espacios en blanco
    /// (whiteSpace = collapse: tipos numéricos y booleanos).
    patron: u32,
    colapsar: bool,
    // ---- nombre en curso ----
    largo: u32,
    huella: u64,
    nombre: Recorte,
    /// Último nombre de atributo, para la ruta.
    atributo: Recorte,
    hay_atributo: bool,
    // ---- texto o valor de atributo en curso ----
    longitud: u32,
    solo_espacios: bool,
    es_numero: bool,
    numerico: Numerico,
    r: Resultado,
    terminado: bool,
}

/// Validador de documentos XML contra un esquema XSD compilado.
pub struct ValidadorXml<'a, const D: usize, const W: usize> {
    lexer: LexerXml,
    nucleo: Nucleo<'a, D, W>,
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

impl<'a, const D: usize, const W: usize> ValidadorXml<'a, D, W> {
    /// Falla al compilar si `W` es menor que `automata.palabras` cuando el autómata es una
    /// constante; en otro caso, entra en pánico.
    pub const fn new(automata: &'a Automata<'a>) -> Self {
        assert!(
            automata.palabras <= W,
            "la máscara del autómata no cabe en W palabras"
        );
        ValidadorXml {
            lexer: LexerXml::new(),
            max_bytes: u64::MAX,
            cerrado: false,
            nucleo: Nucleo {
                a: automata,
                estado: automata.inicial,
                pila: [Marco::nuevo(); D],
                nivel: 0,
                max_profundidad: D,
                fallo_temprano: false,
                leyendo_valor: false,
                nodo: NINGUNO,
                patron: NINGUNO,
                colapsar: false,
                largo: 0,
                huella: 0,
                nombre: Recorte::nuevo(),
                atributo: Recorte::nuevo(),
                hay_atributo: false,
                longitud: 0,
                solo_espacios: true,
                es_numero: false,
                numerico: Numerico::nuevo(),
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

    /// Deja el validador listo para otro documento, con los mismos límites.
    pub fn reiniciar(&mut self) {
        self.lexer.reiniciar();
        self.cerrado = false;
        let n = &mut self.nucleo;
        n.estado = n.a.inicial;
        n.nivel = 0;
        n.nodo = NINGUNO;
        n.patron = NINGUNO;
        n.leyendo_valor = false;
        n.hay_atributo = false;
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
            nucleo.detener(Estado::Malformado, e.pos, Fallo::Lexico(e.tipo), false);
        }
        if excede {
            nucleo.detener(Estado::Limite, *max_bytes, Fallo::Tamano, false);
        }
    }

    /// Detiene la validación por tiempo. El motor no tiene reloj: quien lo usa mide el tiempo entre
    /// bloques y llama a esta función si se agotó el plazo.
    pub fn agotar_tiempo(&mut self) {
        let pos = self.lexer.posicion();
        self.nucleo
            .detener(Estado::Limite, pos, Fallo::Tiempo, false);
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
            nucleo.detener(Estado::Malformado, e.pos, Fallo::Lexico(e.tipo), false);
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
    /// Ruta del elemento abierto: `/raiz/hijo[k]`, y `/@atributo` si se pide.
    fn ruta(&self, con_atributo: bool) -> Ruta {
        let mut ruta = Ruta::vacia();
        let mut completa = true;
        let mut tramo = [0u8; CLAVE_MAX + 20];
        for (i, marco) in self.pila[..self.nivel].iter().enumerate() {
            let mut n = 0;
            let mut poner = |b: u8| {
                tramo[n] = b;
                n += 1;
            };
            poner(b'/');
            marco.nombre.texto().iter().for_each(|&b| poner(b));
            if marco.nombre.truncado {
                PUNTOS.iter().for_each(|&b| poner(b));
            }
            if i > 0 {
                // posición entre los hijos elemento del padre, desde 1
                poner(b'[');
                let mut cifras = [0u8; 10];
                let (mut k, mut c) = (self.pila[i - 1].cuenta, cifras.len());
                loop {
                    c -= 1;
                    cifras[c] = b'0' + (k % 10) as u8;
                    k /= 10;
                    if k == 0 {
                        break;
                    }
                }
                cifras[c..].iter().for_each(|&b| poner(b));
                poner(b']');
            }
            if ruta.largo() + n > RUTA_MAX - PUNTOS.len() {
                ruta.agregar(&PUNTOS);
                completa = false;
                break;
            }
            ruta.agregar(&tramo[..n]);
        }
        if con_atributo && completa && self.hay_atributo {
            let extra = if self.atributo.truncado {
                PUNTOS.len()
            } else {
                0
            };
            if ruta.largo() + 2 + self.atributo.largo as usize + extra <= RUTA_MAX - PUNTOS.len() {
                ruta.agregar(b"/@");
                ruta.agregar(self.atributo.texto());
                if self.atributo.truncado {
                    ruta.agregar(&PUNTOS);
                }
            } else {
                ruta.agregar(&PUNTOS);
            }
        }
        ruta
    }

    fn detener(&mut self, estado: Estado, pos: u64, fallo: Fallo, con_atributo: bool) {
        if !self.terminado {
            self.r.estado = estado;
            self.r.error = Some((pos, fallo));
            self.r.ruta = self.ruta(con_atributo);
            self.terminado = true;
        }
    }

    fn violar(&mut self, pos: u64, motivo: Motivo, cuantas: u32, con_atributo: bool) {
        if cuantas > 0 {
            if self.r.primera.is_none() {
                self.r.primera = Some((pos, motivo));
                self.r.ruta = self.ruta(con_atributo);
            }
            self.r.violaciones = self.r.violaciones.saturating_add(cuantas);
        }
    }

    fn evento(&mut self, evento: EventoXml<'_>) {
        if self.terminado {
            return;
        }
        let q = self.estado as usize;
        match evento {
            EventoXml::InicioNombre => {
                self.leyendo_valor = false;
                self.nodo = self.a.trie[q];
                self.largo = 0;
                self.huella = 0xCBF2_9CE4_8422_2325;
                self.nombre = Recorte::nuevo();
            }
            EventoXml::InicioValor => self.iniciar_valor(columna::VALOR_ATRIB),
            EventoXml::InicioTexto => self.iniciar_valor(columna::TEXTO),
            EventoXml::Fragmento(bytes) if self.leyendo_valor => {
                let caracteres = bytes.iter().filter(|&&b| b & 0xC0 != 0x80).count();
                self.longitud = self
                    .longitud
                    .saturating_add(caracteres.min(u32::MAX as usize) as u32);
                if self.solo_espacios {
                    self.solo_espacios = bytes
                        .iter()
                        .all(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r'));
                }
                for &b in bytes {
                    if self.nodo == NINGUNO {
                        break;
                    }
                    self.nodo = self.a.avanzar(self.nodo, b);
                }
                for &b in bytes {
                    if self.patron == NINGUNO {
                        break;
                    }
                    // en collapse, un espacio en medio ya hace fallar la forma léxica, que va antes
                    if !(self.colapsar && matches!(b, b' ' | b'\t' | b'\n' | b'\r')) {
                        self.patron = self.a.avanzar(self.patron, b);
                    }
                }
                if self.es_numero {
                    bytes.iter().for_each(|&b| self.numerico.byte(b));
                }
            }
            EventoXml::Fragmento(bytes) => {
                self.largo = self.largo.saturating_add(bytes.len() as u32);
                for &b in bytes {
                    self.huella = (self.huella ^ b as u64).wrapping_mul(0x0000_0100_0000_01B3);
                    if self.nodo != NINGUNO {
                        self.nodo = self.a.avanzar(self.nodo, b);
                    }
                    self.nombre.byte(b);
                }
            }
            EventoXml::Token { tipo, inicio, .. } => self.paso(tipo, inicio),
        }
    }

    /// Prepara los acumuladores de un texto o de un valor de atributo.
    fn iniciar_valor(&mut self, columna: usize) {
        let a = self.a;
        let q = self.estado as usize;
        self.leyendo_valor = true;
        self.nodo = a.trie[q];
        self.patron = a.patron[q];
        self.longitud = 0;
        self.solo_espacios = true;
        let id = a.delta[q * TOKENS + columna];
        let pruebas = match id {
            NINGUNO => &[][..],
            _ => {
                let t = &a.transiciones[id as usize];
                &a.pruebas[t.pruebas as usize..(t.pruebas + t.n_pruebas) as usize]
            }
        };
        self.es_numero = pruebas.iter().any(|p| {
            !matches!(
                p.clase,
                prueba::LONG_MIN | prueba::LONG_MAX | prueba::ENUM_CAD | prueba::PATRON
            )
        });
        self.colapsar = pruebas
            .iter()
            .any(|p| p.clase == prueba::PATRON && p.b != 0);
        if self.es_numero {
            self.numerico.reiniciar();
        }
    }

    /// Un paso del autómata: δ(estado, token, cima).
    fn paso(&mut self, tipo: TipoXml, pos: u64) {
        let a = self.a;
        let q = self.estado as usize;
        let fila = &a.delta[q * TOKENS..(q + 1) * TOKENS];
        let en_atributo = tipo == TipoXml::ValorAtrib
            || (tipo == TipoXml::Nombre && fila[columna::ATRIB_NS] != NINGUNO);
        let id = match tipo {
            TipoXml::Nombre => {
                let nombre = self.nombre.texto();
                if en_atributo {
                    self.atributo = self.nombre;
                    self.hay_atributo = true;
                }
                match fila[columna::NOMBRE] {
                    NINGUNO => NINGUNO, // este estado no lee nombres
                    _ if en_atributo
                        && (nombre.starts_with(b"xmlns:") || nombre.starts_with(b"xsi:")) =>
                    {
                        fila[columna::ATRIB_NS]
                    }
                    otra => match a.valor(self.nodo) {
                        0 if otra == SIN_OTRA => NINGUNO,
                        0 => otra,
                        valor => valor - 1,
                    },
                }
            }
            TipoXml::Texto if self.solo_espacios => fila[columna::ESPACIOS],
            TipoXml::Fin => fila[columna::FIN],
            otro => fila[otro as usize],
        };
        if id == NINGUNO {
            return self.detener(Estado::Malformado, pos, Fallo::Sintactico, false);
        }
        let t: &Transicion = &a.transiciones[id as usize];

        // ---- acción sobre los registros del marco de la cima ----
        if t.accion != 0 {
            let Some(cima) = self.nivel.checked_sub(1) else {
                return self.detener(Estado::Malformado, pos, Fallo::Sintactico, false);
            };
            match t.accion {
                accion::ATRIBUTO => {
                    let (palabra, bit) = ((t.a / 64) as usize, t.a % 64);
                    if self.pila[cima].mascara[palabra] >> bit & 1 == 1 {
                        return self.detener(
                            Estado::Malformado,
                            pos,
                            Fallo::AtributoRepetido,
                            true,
                        );
                    }
                    self.pila[cima].mascara[palabra] |= 1 << bit;
                }
                accion::CERRAR_ATRIBUTOS => {
                    let exigida =
                        &a.mascaras[t.a as usize * a.palabras..(t.a as usize + 1) * a.palabras];
                    let faltan = exigida
                        .iter()
                        .zip(self.pila[cima].mascara.iter())
                        .map(|(e, m)| (e & !m).count_ones())
                        .sum();
                    self.violar(pos, Motivo::AtributoRequerido, faltan, false);
                }
                accion::HIJO => {
                    self.pila[cima].cuenta = self.pila[cima].cuenta.saturating_add(1);
                }
                accion::CIERRE_LIBRE
                    if (self.pila[cima].largo, self.pila[cima].huella)
                        != (self.largo, self.huella) =>
                {
                    return self.detener(Estado::Malformado, pos, Fallo::Sintactico, false);
                }
                _ => {}
            }
        }

        // ---- pila: reescribir la cima y apilar ----
        if t.cima != NINGUNO && self.nivel > 0 {
            self.pila[self.nivel - 1].simbolo = t.cima;
        }
        if t.apila != NINGUNO {
            if self.nivel >= self.max_profundidad {
                return self.detener(Estado::Limite, pos, Fallo::Profundidad, false);
            }
            let marco = &mut self.pila[self.nivel];
            marco.simbolo = t.apila;
            marco.cuenta = 0;
            marco.mascara = [0; W];
            marco.largo = self.largo;
            marco.huella = self.huella;
            marco.nombre = self.nombre;
            self.nivel += 1;
            self.r.profundidad = self.r.profundidad.max(self.nivel as u32);
        }

        // ---- peso fijo y pruebas sobre el valor ----
        if t.peso > 0 {
            let motivo = match tipo {
                TipoXml::Nombre if en_atributo => Motivo::AtributoNoDeclarado,
                TipoXml::Nombre => Motivo::ElementoNoPermitido,
                TipoXml::Texto => Motivo::TextoNoPermitido,
                _ => Motivo::ContenidoIncompleto,
            };
            self.violar(pos, motivo, t.peso, en_atributo);
        }
        let pruebas = &a.pruebas[t.pruebas as usize..(t.pruebas + t.n_pruebas) as usize];
        if let Some(primera) = pruebas.first() {
            let compara = pruebas
                .iter()
                .any(|p| matches!(p.clase, prueba::MIN | prueba::MAX | prueba::ENUM_NUM));
            if compara && self.numerico.desbordado && self.numerico.forma(primera.clase) {
                return self.detener(Estado::Limite, pos, Fallo::NumeroLargo, en_atributo);
            }
            for p in pruebas {
                let constante = |i: u32| {
                    self.numerico
                        .numero()
                        .comparar(&a.decimales[i as usize], a.digitos)
                };
                let (fallos, motivo, lexica) = match p.clase {
                    prueba::LONG_MIN => ((self.longitud < p.a) as u32, Motivo::LongMin, false),
                    prueba::LONG_MAX => ((self.longitud > p.a) as u32, Motivo::LongMax, false),
                    // el trie da en cuántas enumeraciones está la cadena: fallan las demás
                    prueba::ENUM_CAD => (
                        p.a.saturating_sub(a.valor(self.nodo)),
                        Motivo::EnumCad,
                        false,
                    ),
                    // el AFD da con cuántos grupos de patrones casa el valor: fallan los demás
                    prueba::PATRON => (
                        p.a.saturating_sub(a.valor(self.patron)),
                        Motivo::Patron,
                        false,
                    ),
                    prueba::XS_ENTERO => {
                        (!self.numerico.forma(p.clase) as u32, Motivo::XsEntero, true)
                    }
                    prueba::XS_DECIMAL => (
                        !self.numerico.forma(p.clase) as u32,
                        Motivo::XsDecimal,
                        true,
                    ),
                    prueba::XS_BOOLEANO => (
                        !self.numerico.forma(p.clase) as u32,
                        Motivo::XsBooleano,
                        true,
                    ),
                    prueba::MIN => {
                        let orden = constante(p.a);
                        (
                            (orden == Ordering::Less || (p.b != 0 && orden == Ordering::Equal))
                                as u32,
                            Motivo::Min,
                            false,
                        )
                    }
                    prueba::MAX => {
                        let orden = constante(p.a);
                        (
                            (orden == Ordering::Greater || (p.b != 0 && orden == Ordering::Equal))
                                as u32,
                            Motivo::Max,
                            false,
                        )
                    }
                    prueba::ENUM_NUM => (
                        !(p.a..p.a + p.b).any(|i| constante(i) == Ordering::Equal) as u32,
                        Motivo::EnumNum,
                        false,
                    ),
                    _ => (0, Motivo::NoAdmitido, false),
                };
                if fallos > 0 {
                    let cuantas = if self.fallo_temprano { 1 } else { fallos };
                    self.violar(pos, motivo, cuantas, en_atributo);
                    // sin la forma léxica de su tipo, las demás pruebas no tienen sentido
                    if self.fallo_temprano || lexica {
                        break;
                    }
                }
            }
        }
        self.r.pendientes = self.r.pendientes.saturating_add(t.pendientes);

        // ---- desapilar y destino ----
        if t.desapila {
            self.nivel = self.nivel.saturating_sub(1);
        }
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
    core::mem::size_of::<ValidadorXml<'static, 0, 0>>()
}
