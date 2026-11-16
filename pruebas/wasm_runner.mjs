// Ejecuta el validador generado como módulo WebAssembly (lo usa pruebas/test_generador.py).
//
//   node wasm_runner.mjs validador.wasm casos.json
//
// casos.json es una lista de payloads en hexadecimal. Escribe una lista con, por cada caso,
// [estado, violaciones, pendientes, profundidad, posición (-1 si no hay), motivo, ruta].
import fs from 'node:fs';

const [, , rutaWasm, rutaCasos] = process.argv;
const casos = JSON.parse(fs.readFileSync(rutaCasos, 'utf8')).map((hex) => Buffer.from(hex, 'hex'));
const { instance } = await WebAssembly.instantiate(fs.readFileSync(rutaWasm), {});
const pdv = instance.exports;

// El módulo no reserva memoria: se le añaden páginas y se le pasan sus direcciones.
const tamano = Number(pdv.pdv_tamano());
const mayor = Math.max(1, ...casos.map((c) => c.length));
const base = pdv.memory.buffer.byteLength;
pdv.memory.grow(Math.ceil((tamano + 512 + mayor) / 65536) + 1);
const validador = base;
const resultado = base + Math.ceil(tamano / 8) * 8;
const ruta = resultado + 32;
const datos = ruta + 256;

const salida = [];
for (const caso of casos) {
  pdv.pdv_iniciar(validador, 128, 0, 0n);   // sin fallo temprano ni límite de tamaño
  new Uint8Array(pdv.memory.buffer).set(caso, datos);
  // en dos bloques, para ejercitar el streaming
  const mitad = caso.length >> 1;
  pdv.pdv_feed(validador, datos, mitad);
  pdv.pdv_feed(validador, datos + mitad, caso.length - mitad);
  pdv.pdv_finish(validador, resultado);
  const r = new DataView(pdv.memory.buffer);
  const posicion = r.getBigUint64(resultado + 16, true);
  salida.push([
    r.getUint32(resultado, true), r.getUint32(resultado + 4, true), r.getUint32(resultado + 8, true),
    r.getUint32(resultado + 12, true), posicion === 0xffffffffffffffffn ? -1 : Number(posicion),
    r.getUint32(resultado + 24, true),
    Buffer.from(pdv.memory.buffer, ruta, Number(pdv.pdv_ruta(validador, ruta, 256))).toString('utf8'),
  ]);
}
console.log(JSON.stringify(salida));
