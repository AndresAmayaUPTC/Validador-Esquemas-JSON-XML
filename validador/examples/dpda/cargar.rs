//! Cargador de autómatas en el formato de texto de `compilador/generador/tablas.py::serializar`:
//! una línea por tabla, con su nombre y sus enteros.
//!
//! Lo comparten el programa de pruebas (`examples/dpda`) y el de medición (`bench`). Usa memoria
//! dinámica, pero solo para guardar las tablas: el motor las recibe como rebanadas prestadas.

use validador::automata::{
    Arista, Automata, Decimal, Dependencia, Grupo, NodoTrie, Prueba, Transicion,
};

#[derive(Default)]
pub struct Tablas {
    cabecera: Vec<i64>,
    delta: Vec<u32>,
    trie: Vec<u32>,
    otra: Vec<u32>,
    patron: Vec<u32>,
    retorno: Vec<u32>,
    transiciones: Vec<Transicion>,
    pruebas: Vec<Prueba>,
    decimales: Vec<Decimal>,
    digitos: Vec<u8>,
    mascaras: Vec<u64>,
    dependencias: Vec<Dependencia>,
    grupos: Vec<Grupo>,
    ramas: Vec<u32>,
    huellas: Vec<u64>,
    trie_nodos: Vec<NodoTrie>,
    trie_aristas: Vec<Arista>,
}

impl Tablas {
    pub fn cargar(&mut self, nombre: &str, v: &[i128]) {
        let u = |x: &i128| *x as u32;
        match nombre {
            "cabecera" => self.cabecera = v.iter().map(|x| *x as i64).collect(),
            "delta" => self.delta = v.iter().map(u).collect(),
            "trie" => self.trie = v.iter().map(u).collect(),
            "otra" => self.otra = v.iter().map(u).collect(),
            "patron" => self.patron = v.iter().map(u).collect(),
            "retorno" => self.retorno = v.iter().map(u).collect(),
            "digitos" => self.digitos = v.iter().map(|x| *x as u8).collect(),
            "mascaras" => self.mascaras = v.iter().map(|x| *x as u64).collect(),
            "ramas" => self.ramas = v.iter().map(u).collect(),
            "huellas" => self.huellas = v.iter().map(|x| *x as u64).collect(),
            "transiciones" => {
                self.transiciones = v
                    .chunks(16)
                    .map(|c| Transicion {
                        destino: u(&c[0]),
                        desapila: c[1] != 0,
                        cima: u(&c[2]),
                        apila: u(&c[3]),
                        accion: c[4] as u8,
                        a: u(&c[5]),
                        b: u(&c[6]),
                        c: u(&c[7]),
                        d: u(&c[8]),
                        peso: u(&c[9]),
                        pruebas: u(&c[10]),
                        n_pruebas: u(&c[11]),
                        pendientes: u(&c[12]),
                        hilos: u(&c[13]),
                        n_hilos: u(&c[14]),
                        recoger: c[15] != 0,
                    })
                    .collect()
            }
            "pruebas" => {
                self.pruebas = v
                    .chunks(3)
                    .map(|c| Prueba {
                        clase: c[0] as u8,
                        a: u(&c[1]),
                        b: u(&c[2]),
                    })
                    .collect()
            }
            "decimales" => {
                self.decimales = v
                    .chunks(4)
                    .map(|c| Decimal {
                        negativo: c[0] != 0,
                        exponente: c[1] as i64,
                        digitos: u(&c[2]),
                        n_digitos: u(&c[3]),
                    })
                    .collect()
            }
            "dependencias" => {
                self.dependencias = v
                    .chunks(2)
                    .map(|c| Dependencia {
                        bit: u(&c[0]),
                        mascara: u(&c[1]),
                    })
                    .collect()
            }
            "grupos" => {
                self.grupos = v
                    .chunks(3)
                    .map(|c| Grupo {
                        clase: c[0] as u8,
                        ramas: u(&c[1]),
                        n_ramas: u(&c[2]),
                    })
                    .collect()
            }
            "trie_nodos" => {
                self.trie_nodos = v
                    .chunks(3)
                    .map(|c| NodoTrie {
                        aristas: u(&c[0]),
                        n_aristas: u(&c[1]),
                        valor: u(&c[2]),
                    })
                    .collect()
            }
            "trie_aristas" => {
                self.trie_aristas = v
                    .chunks(3)
                    .map(|c| Arista {
                        desde: c[0] as u8,
                        hasta: c[1] as u8,
                        destino: u(&c[2]),
                    })
                    .collect()
            }
            otro => panic!("tabla desconocida: {otro}"),
        }
    }

    /// True si el autómata crea sub-ejecuciones o usa huellas: hace falta un validador con sitio
    /// para ellas.
    #[allow(dead_code)] // lo usa bench
    pub fn hilos_o_huellas(&self) -> bool {
        !self.grupos.is_empty() || self.cabecera[7] != 0
    }

    pub fn automata(&self) -> Automata<'_> {
        Automata {
            n_estados: self.cabecera[0] as u32,
            inicial: self.cabecera[1] as u32,
            acepta: self.cabecera[2] as u32,
            pila_inicial: self.cabecera[3] as u32,
            palabras: self.cabecera[4] as usize,
            fin_hilo: self.cabecera[5] as u32,
            pila_hilo: self.cabecera[6] as u32,
            usa_huellas: self.cabecera[7] != 0,
            delta: &self.delta,
            trie: &self.trie,
            otra: &self.otra,
            patron: &self.patron,
            retorno: &self.retorno,
            transiciones: &self.transiciones,
            pruebas: &self.pruebas,
            decimales: &self.decimales,
            digitos: &self.digitos,
            mascaras: &self.mascaras,
            dependencias: &self.dependencias,
            grupos: &self.grupos,
            ramas: &self.ramas,
            huellas: &self.huellas,
            trie_nodos: &self.trie_nodos,
            trie_aristas: &self.trie_aristas,
        }
    }
}
