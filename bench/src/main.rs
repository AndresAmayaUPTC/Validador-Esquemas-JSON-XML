//! Medición del validador en streaming frente a la línea base (Fase 6).
//!
//! ```text
//! bench TABLAS ESQUEMA PAYLOAD [--xml] [--bloque N] [--ms N] [--sostenida SEGUNDOS] [--etiqueta TEXTO]
//! ```
//!
//! TABLAS es el autómata en el formato de `compilador/generador/tablas.py::serializar`; ESQUEMA, el
//! mismo esquema en JSON, para la línea base. Escribe un objeto JSON con tres mediciones:
//!
//! * `streaming`   el autómata de pila: una pasada sobre los bytes, por bloques, sin árbol.
//! * `dom_parseo`  solo construir el árbol del documento con `serde_json`.
//! * `dom`         construir el árbol y validarlo con `jsonschema` (DOM + validador genérico).
//!
//! Con `--xml` las tablas son las de un XSD y el documento es XML. No hay en Rust un validador de
//! XSD comparable, así que la línea base solo construye el árbol con `roxmltree` y `dom` repite esa
//! medida con `"solo_parseo": true`: es una cota favorable a la línea base, que hace menos trabajo.
//!
//! De cada una: tiempo mínimo, MB/s, ciclos de CPU por byte, asignaciones de memoria y pico de
//! memoria dinámica. El asignador global cuenta cada reserva, así que «0 asignaciones» es una medida,
//! no una suposición.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering::Relaxed};
use std::time::{Duration, Instant};

use serde::Deserialize;
use validador::validador::{EJECUCIONES, HUELLAS};
use validador::{Estado, Resultado, Validador, ValidadorXml};

#[path = "../../validador/examples/dpda/cargar.rs"]
mod cargar;

/// Capacidad del validador de medición: 4096 niveles de pila y máscaras de 256 propiedades.
const D: usize = 4096;
const W: usize = 4;

// ---- asignador que cuenta -----------------------------------------------------------------------

struct Contador;

static ASIGNACIONES: AtomicU64 = AtomicU64::new(0);
static EN_USO: AtomicUsize = AtomicUsize::new(0);
static PICO: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Contador {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        ASIGNACIONES.fetch_add(1, Relaxed);
        let ahora = EN_USO.fetch_add(layout.size(), Relaxed) + layout.size();
        PICO.fetch_max(ahora, Relaxed);
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        EN_USO.fetch_sub(layout.size(), Relaxed);
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, nuevo: usize) -> *mut u8 {
        ASIGNACIONES.fetch_add(1, Relaxed);
        if nuevo >= layout.size() {
            let ahora = EN_USO.fetch_add(nuevo - layout.size(), Relaxed) + nuevo - layout.size();
            PICO.fetch_max(ahora, Relaxed);
        } else {
            EN_USO.fetch_sub(layout.size() - nuevo, Relaxed);
        }
        unsafe { System.realloc(ptr, layout, nuevo) }
    }
}

#[global_allocator]
static ASIGNADOR: Contador = Contador;

// ---- medición -----------------------------------------------------------------------------------

fn ciclos() -> u64 {
    #[cfg(target_arch = "x86_64")]
    #[allow(unused_unsafe)]
    unsafe {
        core::arch::x86_64::_rdtsc()
    }
    #[cfg(not(target_arch = "x86_64"))]
    0
}

struct Medida {
    /// Nanosegundos y ciclos de una ejecución (la media del lote más rápido).
    ns: f64,
    ciclos: f64,
    asignaciones: u64,
    pico: usize,
    repeticiones: u32,
}

/// Repite `f` hasta acumular `minimo` de tiempo y se queda con la ejecución más rápida. La memoria
/// se mide en la primera ejecución.
///
/// Una sola ejecución sobre un documento pequeño dura menos que la resolución del reloj, así que
/// se cronometran lotes de unas decenas de microsegundos y se divide por el tamaño del lote.
fn medir<R>(minimo: Duration, mut f: impl FnMut() -> R) -> (R, Medida) {
    let base = EN_USO.load(Relaxed);
    PICO.store(base, Relaxed);
    let antes = ASIGNACIONES.load(Relaxed);
    let resultado = f();
    let asignaciones = ASIGNACIONES.load(Relaxed) - antes;
    let pico = PICO.load(Relaxed).saturating_sub(base);

    let t = Instant::now();
    std::hint::black_box(f());
    let una = t.elapsed().as_nanos().max(1);
    let lote = (50_000 / una).clamp(1, 100_000) as u32;

    let (mut ns, mut ciclos_minimos, mut repeticiones) = (f64::MAX, f64::MAX, 0u32);
    let inicio = Instant::now();
    while repeticiones < 3 * lote || inicio.elapsed() < minimo {
        let (t, c) = (Instant::now(), ciclos());
        for _ in 0..lote {
            std::hint::black_box(f());
        }
        let (dc, dt) = (ciclos().wrapping_sub(c), t.elapsed().as_nanos());
        ns = ns.min(dt as f64 / lote as f64);
        ciclos_minimos = ciclos_minimos.min(dc as f64 / lote as f64);
        repeticiones += lote;
        if repeticiones >= 20_000_000 {
            break;
        }
    }
    (
        resultado,
        Medida {
            ns,
            ciclos: ciclos_minimos,
            asignaciones,
            pico,
            repeticiones,
        },
    )
}

fn json_de(m: &Medida, bytes: usize) -> String {
    let segundos = m.ns / 1e9;
    format!(
        "\"ns\": {:.1}, \"mb_s\": {:.2}, \"ciclos_por_byte\": {:.3}, \"asignaciones\": {}, \"pico_heap\": {}, \"repeticiones\": {}",
        m.ns,
        bytes as f64 / 1e6 / segundos.max(1e-12),
        m.ciclos / bytes.max(1) as f64,
        m.asignaciones,
        m.pico,
        m.repeticiones
    )
}

/// Lo que la medición necesita de un validador, sea el de JSON o el de XML.
trait Motor {
    fn reiniciar(&mut self);
    fn feed(&mut self, bloque: &[u8]);
    fn finish(&mut self) -> Resultado;
}

macro_rules! motor {
    ($tipo:ty) => {
        impl Motor for $tipo {
            fn reiniciar(&mut self) {
                Self::reiniciar(self)
            }
            fn feed(&mut self, bloque: &[u8]) {
                Self::feed(self, bloque)
            }
            fn finish(&mut self) -> Resultado {
                Self::finish(self)
            }
        }
    };
}
// Sin sub-ejecuciones ni huellas (lo que usa un esquema sin esas estrategias) y con ellas.
motor!(Validador<'_, D, W>);
motor!(Validador<'_, D, W, EJECUCIONES, HUELLAS, D>);
motor!(ValidadorXml<'_, D, W>);

/// Valida un documento reutilizando el validador, como haría un servidor entre petición y petición.
fn validar(v: &mut dyn Motor, datos: &[u8], bloque: usize) -> Resultado {
    v.reiniciar();
    for trozo in datos.chunks(bloque.max(1)) {
        v.feed(trozo);
    }
    v.finish()
}

/// Línea base de XML: comprobar que es UTF-8 y construir el árbol del documento.
fn arbol_xml(datos: &[u8]) -> bool {
    std::str::from_utf8(datos)
        .ok()
        .and_then(|texto| roxmltree::Document::parse(texto).ok())
        .is_some()
}

fn arbol(datos: &[u8]) -> Result<serde_json::Value, serde_json::Error> {
    let mut de = serde_json::Deserializer::from_slice(datos);
    de.disable_recursion_limit();
    let valor = serde_json::Value::deserialize(&mut de)?;
    de.end()?;
    Ok(valor)
}

fn opcion<T: std::str::FromStr>(args: &[String], nombre: &str) -> Option<T> {
    args.iter()
        .position(|a| a == nombre)
        .and_then(|i| args.get(i + 1))
        .and_then(|v| v.parse().ok())
}

fn principal() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 3 {
        eprintln!(
            "uso: bench TABLAS ESQUEMA PAYLOAD [--xml] [--bloque N] [--ms N] [--sostenida SEGUNDOS] [--etiqueta TEXTO]"
        );
        std::process::exit(2);
    }
    let bloque: usize = opcion(&args, "--bloque").unwrap_or(64 * 1024);
    let minimo = Duration::from_millis(opcion(&args, "--ms").unwrap_or(200));
    let sostenida: f64 = opcion(&args, "--sostenida").unwrap_or(0.0);
    let etiqueta: String = opcion(&args, "--etiqueta").unwrap_or_default();
    let xml = args.iter().any(|a| a == "--xml");

    let mut tablas = cargar::Tablas::default();
    for linea in std::fs::read_to_string(&args[0])
        .expect("no se pudo leer las tablas")
        .lines()
    {
        let mut campos = linea.split_ascii_whitespace();
        if let Some(nombre) = campos.next() {
            let valores: Vec<i128> = campos.map(|x| x.parse().expect("entero")).collect();
            tablas.cargar(nombre, &valores);
        }
    }
    let automata = tablas.automata();
    let datos = std::fs::read(&args[2]).expect("no se pudo leer el payload");
    let n = datos.len();

    // ---- streaming ----
    let (mut v, fijos, pila): (Box<dyn Motor + '_>, usize, usize) = if xml {
        use validador::validador_xml::{bytes_fijos, bytes_por_marco};
        (
            Box::new(ValidadorXml::<D, W>::new(&automata)),
            bytes_fijos(),
            bytes_por_marco(automata.palabras),
        )
    } else if tablas.hilos_o_huellas() {
        use validador::validador::{bytes_fijos, bytes_por_nivel};
        (
            Box::new(Validador::<D, W, EJECUCIONES, HUELLAS, D>::new(&automata)),
            bytes_fijos::<EJECUCIONES, HUELLAS>(),
            bytes_por_nivel(automata.palabras, EJECUCIONES, true),
        )
    } else {
        use validador::validador::{bytes_fijos, bytes_por_nivel};
        (
            Box::new(Validador::<D, W>::new(&automata)),
            bytes_fijos::<1, 0>(),
            bytes_por_nivel(automata.palabras, 1, false),
        )
    };
    let (r, m) = medir(minimo, || validar(v.as_mut(), &datos, bloque));
    let mut salida = format!(
        "{{\"etiqueta\": {:?}, \"bytes\": {n}, \"bloque\": {bloque}, \"automata\": {{\"estados\": {}, \"transiciones\": {}, \"simbolos_de_pila\": {}}}, ",
        etiqueta,
        automata.n_estados,
        automata.transiciones.len(),
        automata.retorno.len()
    );
    salida += &format!(
        "\"streaming\": {{{}, \"estado\": {:?}, \"violaciones\": {}, \"profundidad\": {}, \"memoria_fija\": {}, \"memoria_por_nivel\": {}, \"memoria_usada\": {}}}, ",
        json_de(&m, n),
        r.estado.nombre(),
        r.violaciones,
        r.profundidad,
        fijos,
        pila,
        fijos + pila * r.profundidad as usize
    );

    // ---- línea base: DOM + validador genérico ----
    if xml {
        let (parseo, m) = medir(minimo, || arbol_xml(&datos));
        let texto = if parseo { "bien formado" } else { "malformado" };
        salida += &format!("\"dom_parseo\": {{{}, \"ok\": {parseo}}}, ", json_de(&m, n));
        salida += &format!(
            "\"dom\": {{{}, \"estado\": {texto:?}, \"solo_parseo\": true}}",
            json_de(&m, n)
        );
    } else {
        let esquema: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&args[1]).expect("no se pudo leer el esquema"))
                .expect("esquema inválido");
        let (parseo, m) = medir(minimo, || arbol(&datos).is_ok());
        salida += &format!("\"dom_parseo\": {{{}, \"ok\": {parseo}}}, ", json_de(&m, n));
        salida += &linea_base_json(&esquema, &datos, minimo);
    }
    if sostenida > 0.0 {
        salida += &carga_sostenida(v.as_mut(), &datos, bloque, sostenida);
    }
    println!("{salida}}}");
}

/// DOM + validador genérico: construir el árbol con `serde_json` y validarlo con `jsonschema`.
fn linea_base_json(esquema: &serde_json::Value, datos: &[u8], minimo: Duration) -> String {
    match jsonschema::validator_for(esquema) {
        Ok(generico) => {
            let (valido, m) = medir(minimo, || {
                arbol(datos).map(|doc| generico.is_valid(&doc)).ok()
            });
            let texto = match valido {
                Some(true) => "\"valido\"",
                Some(false) => "\"invalido\"",
                None => "\"malformado\"",
            };
            format!(
                "\"dom\": {{{}, \"estado\": {texto}}}",
                json_de(&m, datos.len())
            )
        }
        Err(e) => format!("\"dom\": {{\"error\": {:?}}}", e.to_string()),
    }
}

/// El mismo documento una y otra vez, midiendo cada ventana de 100 ms.
fn carga_sostenida(v: &mut dyn Motor, datos: &[u8], bloque: usize, sostenida: f64) -> String {
    let n = datos.len();
    let mut salida = String::new();
    {
        // el vector de ventanas se reserva antes de empezar a contar: lo que se cuente es del validador
        let mut ventanas = Vec::with_capacity((sostenida * 12.0) as usize + 16);
        let (mut documentos, mut validos) = (0u64, 0u64);
        let antes = ASIGNACIONES.load(Relaxed);
        let inicio = Instant::now();
        while inicio.elapsed().as_secs_f64() < sostenida {
            let (t, mut hechos) = (Instant::now(), 0u64);
            while t.elapsed() < Duration::from_millis(100) {
                validos += (validar(v, datos, bloque).estado == Estado::Valido) as u64;
                hechos += 1;
            }
            ventanas.push(hechos as f64 * n as f64 / 1e6 / t.elapsed().as_secs_f64());
            documentos += hechos;
        }
        let asignaciones = ASIGNACIONES.load(Relaxed) - antes;
        ventanas.sort_by(|a, b| a.total_cmp(b));
        salida += &format!(
            ", \"sostenida\": {{\"segundos\": {:.2}, \"documentos\": {documentos}, \"validos\": {validos}, \"mb_s_min\": {:.2}, \"mb_s_mediana\": {:.2}, \"mb_s_max\": {:.2}, \"asignaciones\": {asignaciones}}}",
            inicio.elapsed().as_secs_f64(),
            ventanas[0],
            ventanas[ventanas.len() / 2],
            ventanas[ventanas.len() - 1]
        );
    }
    salida
}

fn main() {
    // La línea base es recursiva: con documentos muy profundos necesita una pila grande.
    std::thread::Builder::new()
        .stack_size(1 << 30)
        .spawn(principal)
        .unwrap()
        .join()
        .unwrap();
}
