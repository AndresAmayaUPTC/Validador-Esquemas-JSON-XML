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
    /// `a` = cuántos "pattern" se comprueban a la vez con el AFD `patron` del estado.
    pub const PATRON: u8 = 11;
    /// `a` = decimal del divisor de "multipleOf".
    pub const MULTIPLO: u8 = 12;
    /// Al cerrar un contenedor. `a`,`b` = rango de huellas permitidas ("enum" o "const").
    pub const ENUM_HUELLA: u8 = 13;
    /// Al cerrar un arreglo: ninguna huella de elemento se repitió ("uniqueItems").
    pub const UNICOS: u8 = 14;
    /// Al cerrar un arreglo. `a` = mínimo y `b` = máximo de elementos que cumplen "contains".
    pub const CONTIENE: u8 = 15;
}

/// Clases de grupos de sub-ejecuciones (`Grupo::clase`): cómo se combinan los pesos de sus ramas.
pub mod grupo {
    /// anyOf: falla si fallan todas.
    pub const ALGUNA: u8 = 1;
    /// oneOf: falla si no cumple exactamente una.
    pub const UNA: u8 = 2;
    /// not: falla si la rama cumple.
    pub const NO: u8 = 3;
    /// if/then/else: ramas (if, then, else).
    pub const SI: u8 = 4;
    /// dependentSchemas: ramas («required: [nombre]», esquema, ausente).
    pub const DEPENDIENTE: u8 = 5;
    /// propertyNames: la clave, leída como cadena.
    pub const NOMBRES: u8 = 6;
    /// contains: cuenta en el marco del arreglo los elementos que cumplen.
    pub const CONTIENE: u8 = 7;
    /// Un segundo contains del mismo arreglo: falla si la rama falla.
    pub const CUMPLE: u8 = 8;
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
    /// Primer índice y cantidad en la tabla de grupos de sub-ejecuciones que nacen con el token.
    pub hilos: u32,
    pub n_hilos: u32,
    /// El arreglo que se abre guarda las huellas de sus elementos ("uniqueItems").
    pub recoger: bool,
}

/// Grupo de sub-ejecuciones: su clase y el estado inicial de cada rama (en `ramas`; `NINGUNO` si
/// la rama no está, como un "else" ausente).
#[derive(Clone, Copy, Debug)]
pub struct Grupo {
    pub clase: u8,
    pub ramas: u32,
    pub n_ramas: u32,
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

/// Nodo de un AFD sobre bytes: un trie (nombres de propiedad, valores de "enum") o el AFD de una
/// expresión regular. `valor` es lo que se lee si la cadena termina aquí (0 si nada).
#[derive(Clone, Copy, Debug)]
pub struct NodoTrie {
    /// Primer índice y cantidad en la tabla de aristas, que están ordenadas y son disjuntas.
    pub aristas: u32,
    pub n_aristas: u32,
    pub valor: u32,
}

/// Arista para los bytes `desde..=hasta` (en un trie, un solo byte).
#[derive(Clone, Copy, Debug)]
pub struct Arista {
    pub desde: u8,
    pub hasta: u8,
    pub destino: u32,
}

/// M = (Q, Σ, Γ, δ, q0, Z0, F, w) en forma de tablas.
#[derive(Clone, Copy, Debug)]
pub struct Automata<'a> {
    pub n_estados: u32,
    pub inicial: u32,
    pub acepta: u32,
    pub pila_inicial: u32,
    /// Estado en que termina una sub-ejecución, y fondo de su pila (`NINGUNO` si no hay).
    pub fin_hilo: u32,
    pub pila_hilo: u32,
    /// Hay que calcular las huellas de los valores ("uniqueItems", "enum" con objetos o arreglos).
    pub usa_huellas: bool,
    /// Palabras de 64 bits que necesita la máscara de un marco.
    pub palabras: usize,
    /// `delta[estado * TOKENS + token]` = transición.
    pub delta: &'a [u32],
    /// Por estado: raíz del trie con el que se lee una cadena (nombres de propiedad o valores de
    /// "enum").
    pub trie: &'a [u32],
    /// Por estado: transición de «cualquier otra clave». Si existe, el estado lee una clave.
    pub otra: &'a [u32],
    /// Por estado: AFD de los "pattern" de la cadena que se lee como valor; su valor es el número
    /// de patrones con los que casa.
    pub patron: &'a [u32],
    /// Por símbolo de pila: estado al que se vuelve.
    pub retorno: &'a [u32],
    pub transiciones: &'a [Transicion],
    pub pruebas: &'a [Prueba],
    pub decimales: &'a [Decimal],
    pub digitos: &'a [u8],
    pub mascaras: &'a [u64],
    pub dependencias: &'a [Dependencia],
    pub grupos: &'a [Grupo],
    pub ramas: &'a [u32],
    pub huellas: &'a [u64],
    pub trie_nodos: &'a [NodoTrie],
    pub trie_aristas: &'a [Arista],
}

impl Automata<'_> {
    /// Un paso del AFD: nodo al que lleva `byte`, o `NINGUNO` (el estado muerto: la cadena ya no
    /// puede ser ninguna de las palabras, o ningún patrón puede casar).
    #[inline]
    pub fn avanzar(&self, nodo: u32, byte: u8) -> u32 {
        let n = &self.trie_nodos[nodo as usize];
        let aristas = &self.trie_aristas[n.aristas as usize..(n.aristas + n.n_aristas) as usize];
        // casi siempre hay pocas aristas (en un trie, una o dos): ahí un recorrido lineal es más rápido
        let i = if aristas.len() <= 8 {
            aristas
                .iter()
                .position(|arista| byte <= arista.hasta)
                .unwrap_or(aristas.len())
        } else {
            aristas.partition_point(|arista| arista.hasta < byte)
        };
        match aristas.get(i) {
            Some(arista) if arista.desde <= byte => arista.destino,
            _ => NINGUNO,
        }
    }

    /// Valor de la palabra que termina en `nodo` (0 si no termina ninguna).
    #[inline]
    pub fn valor(&self, nodo: u32) -> u32 {
        if nodo == NINGUNO {
            0
        } else {
            self.trie_nodos[nodo as usize].valor
        }
    }
}
