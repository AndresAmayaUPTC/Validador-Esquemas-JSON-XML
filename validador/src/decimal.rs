//! Aritmética decimal exacta sobre el lexema de un número, sin copiarlo ni convertirlo.
//!
//! Un número de JSON puede tener cientos de dígitos o un exponente enorme, así que no se convierte a
//! `f64`. Se lleva a la forma ±0.DÍGITOS × 10^e (sin ceros a la izquierda) recorriendo el lexema, y
//! en esa forma dos números se comparan por signo, luego por exponente y luego dígito a dígito.

use core::cmp::Ordering;

use crate::automata::Decimal;
use crate::huellas;

/// Exponentes mayores se tratan como este: ya son más grandes que cualquier constante del esquema.
const TOPE: i64 = 4_000_000_000_000_000_000;

/// Número del payload, visto sobre su lexema.
#[derive(Clone, Copy, Debug)]
pub struct Numero<'a> {
    negativo: bool,
    cero: bool,
    /// Posición de la coma decimal respecto al primer dígito significativo.
    exponente: i64,
    /// Parte del lexema que va desde el primer dígito significativo hasta el fin de la mantisa
    /// (puede incluir el punto).
    mantisa: &'a [u8],
}

impl<'a> Numero<'a> {
    /// `lexema` debe ser un número válido de JSON; el lexer lo garantiza.
    pub fn new(lexema: &'a [u8]) -> Self {
        let negativo = lexema.first() == Some(&b'-');
        let sin_signo = &lexema[negativo as usize..];
        let fin = sin_signo
            .iter()
            .position(|b| matches!(b, b'e' | b'E'))
            .unwrap_or(sin_signo.len());
        let (mantisa, resto) = sin_signo.split_at(fin);

        // Exponente escrito, con saturación.
        let mut escrito: i64 = 0;
        let mut exponente_negativo = false;
        for &b in resto.iter().skip(1) {
            match b {
                b'-' => exponente_negativo = true,
                b'+' => {}
                _ => {
                    escrito = escrito
                        .saturating_mul(10)
                        .saturating_add((b - b'0') as i64)
                        .min(TOPE)
                }
            }
        }
        if exponente_negativo {
            escrito = -escrito;
        }

        let enteros = mantisa
            .iter()
            .position(|&b| b == b'.')
            .unwrap_or(mantisa.len()) as i64;
        // Ceros a la izquierda: cada uno corre la coma un lugar.
        let mut ceros: i64 = 0;
        let mut inicio = mantisa.len();
        for (i, &b) in mantisa.iter().enumerate() {
            match b {
                b'0' => ceros += 1,
                b'.' => {}
                _ => {
                    inicio = i;
                    break;
                }
            }
        }
        Numero {
            negativo,
            cero: inicio == mantisa.len(),
            exponente: (enteros - ceros).saturating_add(escrito).clamp(-TOPE, TOPE),
            mantisa: &mantisa[inicio..],
        }
    }

    fn digitos(&self) -> impl Iterator<Item = u8> + 'a {
        self.mantisa
            .iter()
            .filter(|b| b.is_ascii_digit())
            .map(|b| b - b'0')
    }

    /// `true` si el valor es entero: 1.0 y 1e2 lo son; 1.5 y 1e-3 no.
    pub fn es_entero(&self) -> bool {
        if self.cero {
            return true;
        }
        if self.exponente <= 0 {
            return false;
        }
        // Es entero si no hay ningún dígito distinto de cero más allá de la coma.
        let ultimo = self
            .digitos()
            .enumerate()
            .filter(|&(_, d)| d != 0)
            .map(|(i, _)| i)
            .last()
            .unwrap_or(0);
        (ultimo as u64) < self.exponente as u64
    }

    /// Cantidad de dígitos significativos: hasta el último distinto de cero.
    fn significativos(&self) -> usize {
        if self.cero {
            return 0;
        }
        self.digitos()
            .enumerate()
            .filter(|&(_, d)| d != 0)
            .map(|(i, _)| i + 1)
            .last()
            .unwrap_or(0)
    }

    /// Huella de la forma canónica ±0.DÍGITOS × 10^e (ver `huellas.rs`): 1, 1.0 y 1e0 dan la misma.
    pub fn huella(&self) -> u64 {
        let (negativo, exponente) = if self.cero {
            (false, 0)
        } else {
            (self.negativo, self.exponente)
        };
        let mut h = huellas::fnv(huellas::FNV_INICIO, &[huellas::NUMERO, negativo as u8]);
        h = huellas::fnv(h, &exponente.to_le_bytes());
        for d in self.digitos().take(self.significativos()) {
            h = huellas::fnv(h, &[d]);
        }
        huellas::mezclar(h)
    }

    /// `true` si el número es múltiplo de la constante (positiva, de 18 dígitos como mucho).
    ///
    /// Con el número = D·10^a y la constante = M·10^b (D y M enteros sin ceros finales), el cociente
    /// es entero si y solo si M divide a D·10^(a−b). Si a < b no lo es: D no acaba en cero. Si no,
    /// se calcula D módulo M dígito a dígito y 10^(a−b) módulo M por potencias sucesivas.
    pub fn es_multiplo(&self, c: &Decimal, fondo: &[u8]) -> bool {
        if self.cero {
            return true;
        }
        let n = self.significativos();
        let s = (self.exponente as i128 - n as i128) - (c.exponente as i128 - c.n_digitos as i128);
        if s < 0 {
            return false;
        }
        let m = fondo[c.digitos as usize..(c.digitos + c.n_digitos) as usize]
            .iter()
            .fold(0u128, |m, &d| m * 10 + d as u128);
        let resto = self
            .digitos()
            .take(n)
            .fold(0u128, |r, d| (r * 10 + d as u128) % m);
        let (mut potencia, mut base, mut e) = (1 % m, 10 % m, s as u128);
        while e > 0 {
            if e & 1 == 1 {
                potencia = potencia * base % m;
            }
            base = base * base % m;
            e >>= 1;
        }
        resto * potencia % m == 0
    }

    /// Compara el número con una constante del esquema.
    pub fn comparar(&self, c: &Decimal, fondo: &[u8]) -> Ordering {
        let constante_cero = c.n_digitos == 0;
        match (self.cero, constante_cero) {
            (true, true) => return Ordering::Equal,
            (true, false) => {
                return if c.negativo {
                    Ordering::Greater
                } else {
                    Ordering::Less
                };
            }
            (false, true) => {
                return if self.negativo {
                    Ordering::Less
                } else {
                    Ordering::Greater
                };
            }
            (false, false) => {}
        }
        if self.negativo != c.negativo {
            return if self.negativo {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        let mut magnitud = self.exponente.cmp(&c.exponente);
        if magnitud == Ordering::Equal {
            let mut propios = self.digitos();
            let mut ajenos = fondo[c.digitos as usize..(c.digitos + c.n_digitos) as usize]
                .iter()
                .copied();
            loop {
                // Al que se le acaban los dígitos se le suponen ceros.
                let (x, y) = (propios.next(), ajenos.next());
                if x.is_none() && y.is_none() {
                    break;
                }
                magnitud = x.unwrap_or(0).cmp(&y.unwrap_or(0));
                if magnitud != Ordering::Equal {
                    break;
                }
            }
        }
        if self.negativo {
            magnitud.reverse()
        } else {
            magnitud
        }
    }
}
