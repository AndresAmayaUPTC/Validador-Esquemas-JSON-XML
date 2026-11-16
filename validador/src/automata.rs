//! Tablas del autómata de pila ponderado que emite el compilador (`compilador/generador/tablas.py`).
//!
//! Son solo datos: arreglos de enteros con índices entre ellos. El motor de `validador.rs` los
//! recorre sin modificarlos, así que pueden vivir en memoria de solo lectura.

/// Índice ausente: sin transición, sin trie, sin límite...
pub const NINGUNO: u32 = u32::MAX;

/// En un autómata de XML, valor de la columna NOMBRE de δ en los estados que leen un nombre pero no
/// tienen transición para «cualquier otro nombre» (los que esperan el nombre de cierre).
pub const SIN_OTRA: u32 = u32::MAX - 1;

/// Número de columnas de δ: una por tipo de token, en el orden de `TipoToken`. Los autómatas de XML
/// tienen también doce columnas: las de `TipoXml` más ESPACIOS y ATRIB_NS (ver `validador_xml.rs`).
pub const TOKENS: usize = 12;

/// Acciones sobre los registros del marco de la cima (`Transicion::accion`).
pub mod accion {
    /// `a` = bit de la propiedad, `b` = máximo de propiedades.
    pub const CLAVE: u8 = 1;
    /// `a` = máscara requerida, `b`,`c` = rango de dependencias, `d` = mínimo de propiedades.
    pub const CERRAR_OBJETO: u8 = 2;
    /// `a` = máximo de elementos.
    pub const ELEMENTO: u8 = 3;
    /// `a` = mínimo de elementos.
    pub const CERRAR_ARREGLO: u8 = 4;
    /// XML. `a` = bit del atributo; repetido = mal formado.
    pub const ATRIBUTO: u8 = 5;
    /// XML. `a` = máscara de atributos obligatorios.
    pub const CERRAR_ATRIBUTOS: u8 = 6;
    /// XML. Cuenta un hijo elemento.
    pub const HIJO: u8 = 7;
    /// XML. El nombre de cierre debe tener la longitud y la huella del de apertura.
    pub const CIERRE_LIBRE: u8 = 8;
}

/// Pruebas sobre el valor de un token (`Prueba::clase`).
pub mod prueba {
    pub const ENTERO: u8 = 1;
    /// `a` = decimal, `b` = 1 si el límite es exclusivo.
    pub const MIN: u8 = 2;
    pub const MAX: u8 = 3;
    /// `a` = longitud en caracteres.
    pub const LONG_MIN: u8 = 4;
    pub const LONG_MAX: u8 = 5;
    /// `a`,`b` = rango de decimales permitidos.
    pub const ENUM_NUM: u8 = 6;
    /// `a` = cuántos "enum" de cadenas se comprueban a la vez con el trie del estado.
    pub const ENUM_CAD: u8 = 7;
    /// XML. Forma léxica de `xs:integer`, `xs:decimal` y `xs:boolean`.
    pub const XS_ENTERO: u8 = 8;
    pub const XS_DECIMAL: u8 = 9;
    pub const XS_BOOLEANO: u8 = 10;
}

/// δ(q, a, Z) para cualquier Z. Orden de ejecución: acción, peso y pruebas, pila y destino.
#[derive(Clone, Copy, Debug)]
pub struct Transicion {
    /// Estado siguiente, o `NINGUNO` para volver al que indique la cima de la pila.
    pub destino: u32,
    pub desapila: bool,
    /// Nuevo símbolo del marco de la cima (conserva sus registros).
    pub cima: u32,
    /// Símbolo del marco que se apila.
    pub apila: u32,
    pub accion: u8,
    pub a: u32,
    pub b: u32,
    pub c: u32,
    pub d: u32,
    /// Violaciones fijas de la transición.
    pub peso: u32,
    /// Primer índice y cantidad en la tabla de pruebas.
    pub pruebas: u32,
    pub n_pruebas: u32,
    /// Comprobaciones en verificación diferida que cruza la transición.
    pub pendientes: u32,
}

#[derive(Clone, Copy, Debug)]
pub struct Prueba {
    pub clase: u8,
    pub a: u32,
    pub b: u32,
}

/// Constante numérica del esquema: ±0.DÍGITOS × 10^exponente, sin ceros a los lados.
/// El cero no tiene dígitos.
#[derive(Clone, Copy, Debug)]
pub struct Decimal {
    pub negativo: bool,
    pub exponente: i64,
    /// Primer índice y cantidad en el fondo de dígitos.
    pub digitos: u32,
    pub n_digitos: u32,
}

/// «Si la propiedad `bit` está presente, deben estarlo las de `mascara`».
#[derive(Clone, Copy, Debug)]
pub struct Dependencia {
    pub bit: u32,
    pub mascara: u32,
}

/// Nodo de un trie sobre bytes. `valor` es 0 si aquí no termina ninguna palabra.
#[derive(Clone, Copy, Debug)]
pub struct NodoTrie {
    /// Primer índice y cantidad en la tabla de aristas, que están ordenadas por byte.
    pub aristas: u32,
    pub n_aristas: u32,
    pub valor: u32,
}

#[derive(Clone, Copy, Debug)]
pub struct Arista {
    pub byte: u8,
    pub destino: u32,
}

/// M = (Q, Σ, Γ, δ, q0, Z0, F, w) en forma de tablas.
#[derive(Clone, Copy, Debug)]
pub struct Automata<'a> {
    pub n_estados: u32,
    pub inicial: u32,
    pub acepta: u32,
    pub pila_inicial: u32,
    /// Palabras de 64 bits que necesita la máscara de un marco.
    pub palabras: usize,
    /// `delta[estado * TOKENS + token]` = transición.
    pub delta: &'a [u32],
    /// Por estado: raíz del trie con el que se lee una cadena (nombres de propiedad o valores de
    /// "enum").
    pub trie: &'a [u32],
    /// Por estado: transición de «cualquier otra clave». Si existe, el estado lee una clave.
    pub otra: &'a [u32],
    /// Por símbolo de pila: estado al que se vuelve.
    pub retorno: &'a [u32],
    pub transiciones: &'a [Transicion],
    pub pruebas: &'a [Prueba],
    pub decimales: &'a [Decimal],
    pub digitos: &'a [u8],
    pub mascaras: &'a [u64],
    pub dependencias: &'a [Dependencia],
    pub trie_nodos: &'a [NodoTrie],
    pub trie_aristas: &'a [Arista],
}

impl Automata<'_> {
    /// Un paso del trie: nodo al que lleva `byte`, o `NINGUNO` si la cadena ya no puede ser ninguna
    /// de las palabras.
    pub fn avanzar(&self, nodo: u32, byte: u8) -> u32 {
        let n = &self.trie_nodos[nodo as usize];
        let aristas = &self.trie_aristas[n.aristas as usize..(n.aristas + n.n_aristas) as usize];
        match aristas.binary_search_by_key(&byte, |arista| arista.byte) {
            Ok(i) => aristas[i].destino,
            Err(_) => NINGUNO,
        }
    }

    /// Valor de la palabra que termina en `nodo` (0 si no termina ninguna).
    pub fn valor(&self, nodo: u32) -> u32 {
        if nodo == NINGUNO {
            0
        } else {
            self.trie_nodos[nodo as usize].valor
        }
    }
}
