//! Huellas de 64 bits de la forma canónica de los valores JSON ("uniqueItems" y los "enum"/"const"
//! con objetos o arreglos).
//!
//! Es exactamente el cálculo de `compilador/automata/valores.py`: el compilador lo usa para las
//! constantes del esquema y el motor para los valores del documento, en streaming. Dos valores
//! iguales para JSON Schema (1 y 1.0, "a" y "a", objetos con las propiedades en otro orden)
//! tienen la misma huella; dos distintos coinciden con probabilidad del orden de 2^-64.

pub const FNV_INICIO: u64 = 0xCBF2_9CE4_8422_2325;
const FNV_PRIMO: u64 = 0x0000_0100_0000_01B3;
const ORO: u64 = 0x9E37_79B9_7F4A_7C15;

pub const CADENA: u8 = 1;
pub const NUMERO: u8 = 2;
pub const VERDADERO: u8 = 3;
pub const FALSO: u8 = 4;
pub const NULO: u8 = 5;
const ARREGLO: u8 = 6;
const OBJETO: u8 = 7;

/// Acumulado inicial de un arreglo y de un objeto.
pub const INICIO_ARREGLO: u64 = ARREGLO as u64;
pub const INICIO_OBJETO: u64 = 0;

/// Finalizador de MurmurHash3: dispersa todos los bits.
pub const fn mezclar(mut k: u64) -> u64 {
    k ^= k >> 33;
    k = k.wrapping_mul(0xFF51_AFD7_ED55_8CCD);
    k ^= k >> 33;
    k = k.wrapping_mul(0xC4CE_B9FE_1A85_EC53);
    k ^ (k >> 33)
}

/// FNV-1a sobre `datos`, a partir de `h`.
pub fn fnv(mut h: u64, datos: &[u8]) -> u64 {
    for &b in datos {
        h = (h ^ b as u64).wrapping_mul(FNV_PRIMO);
    }
    h
}

pub fn de_literal(etiqueta: u8) -> u64 {
    mezclar(fnv(FNV_INICIO, &[etiqueta]))
}

/// Encadena un elemento de un arreglo (el orden importa).
pub const fn elemento(acumulado: u64, huella: u64) -> u64 {
    mezclar(acumulado.wrapping_mul(ORO).wrapping_add(huella))
}

/// Suma un par clave-valor de un objeto (el orden no importa).
pub const fn miembro(acumulado: u64, clave: u64, huella: u64) -> u64 {
    acumulado.wrapping_add(mezclar(clave.wrapping_mul(ORO).wrapping_add(huella)))
}

pub const fn cerrar_arreglo(acumulado: u64) -> u64 {
    mezclar(acumulado ^ ((ARREGLO as u64) << 56))
}

pub const fn cerrar_objeto(acumulado: u64) -> u64 {
    mezclar(acumulado ^ ((OBJETO as u64) << 56))
}
