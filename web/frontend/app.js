// Interfaz de la aplicación: habla con el backend por REST y WebSocket. Sin dependencias salvo Graphviz
// (vendor/viz.js), que dibuja el autómata y los árboles de derivación a partir del DOT que envía el servidor.
import { instance as graphviz } from './vendor/viz.js';

const $ = (id) => document.getElementById(id);
const html = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const codificar = (texto) => new TextEncoder().encode(texto);
const numero = (n, decimales = 0) => Number(n).toLocaleString('es-CO', { maximumFractionDigits: decimales, minimumFractionDigits: decimales });
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

const ESTADOS = {
  valido: 'Válido', pendiente: 'Válido, con comprobaciones pendientes', invalido: 'Inválido',
  malformado: 'Mal formado', limite: 'Límite superado',
};
const CATEGORIAS = {
  valido: 'Válidos', invalido: 'Inválidos', malformado: 'Malformados', 'fuera-del-dpda': 'Fuera del DPDA puro',
  bombeo: 'Bombeo', extremo: 'Extremos',
};
const estado = { compilado: null, bytesFijos: null, salud: {}, wasm: null, generados: [] };

function aBase64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
const deBase64 = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

/** Bytes del documento: los del editor, o los de un caso que no es UTF-8 válido y no se ha tocado. */
const bytesDelDocumento = () => estado.bytesFijos ?? codificar($('documento').value);

async function api(ruta, cuerpo, metodo) {
  const r = await fetch(ruta, {
    method: metodo ?? (cuerpo === undefined ? 'GET' : 'POST'),
    headers: cuerpo === undefined ? {} : { 'Content-Type': 'application/json' },
    body: cuerpo === undefined ? undefined : JSON.stringify(cuerpo),
  });
  const tipo = r.headers.get('content-type') ?? '';
  if (r.status === 204) return { ok: true, estado: 204, datos: null };   // sin cuerpo: no hay JSON que leer
  if (tipo.includes('json')) {
    const texto = await r.text();
    return { ok: r.ok, estado: r.status, datos: texto ? JSON.parse(texto) : null };
  }
  return { ok: r.ok, estado: r.status, datos: r.ok ? await r.blob() : null };
}

const violaciones = (n) => `${numero(n)} ${n === 1 ? 'violación' : 'violaciones'}`;

/** Mensaje legible de una respuesta de error del backend. */
function motivoDe(r) {
  const d = r.datos ?? {};
  if (d.esquema) return 'El esquema no compila: revisa la pestaña Análisis.';
  if (d.linea) return `${d.detalle ?? d.mensaje} (línea ${d.linea}, columna ${d.columna})`;
  return d.mensaje ?? d.detail ?? `El servidor respondió ${r.estado}.`;
}

function aviso(id, texto, esError = false) {
  $(id).textContent = texto;
  $(id).classList.toggle('error', esError);
}

// ---- posiciones: el servidor habla en bytes y el editor en caracteres ----
function indiceDeByte(texto, byte) {
  let b = 0;
  for (let i = 0; i < texto.length; i++) {
    if (b >= byte) return i;
    const c = texto.codePointAt(i);
    b += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
    if (c > 0xffff) i++;
  }
  return texto.length;
}

function irA(idEditor, byte, hasta) {
  const area = $(idEditor);
  const inicio = indiceDeByte(area.value, byte);
  const fin = hasta === undefined ? inicio + 1 : indiceDeByte(area.value, hasta);
  area.focus();
  area.setSelectionRange(inicio, Math.max(fin, inicio + 1));
  const linea = area.value.slice(0, inicio).split('\n').length;
  area.scrollTop = Math.max(0, (linea - 4) * parseFloat(getComputedStyle(area).lineHeight));
}

/** Botón que lleva al editor a una posición. */
const enlaceA = (editor, d, texto) =>
  `<button class="enlace" data-ir="${editor}" data-byte="${d.byte}"${d.fin ? ` data-fin="${d.fin}"` : ''}>${html(texto ?? `línea ${d.linea}, columna ${d.columna}`)}</button>`;

document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-ir]');
  if (b) irA(b.dataset.ir, Number(b.dataset.byte), b.dataset.fin ? Number(b.dataset.fin) : undefined);
});

// ---- pestañas ----
const pestanas = [...document.querySelectorAll('[role="tab"]')];
function abrir(id) {
  for (const p of pestanas) {
    const activa = p.id === `t-${id}`;
    p.setAttribute('aria-selected', activa);
    p.tabIndex = activa ? 0 : -1;
    $(p.getAttribute('aria-controls')).hidden = !activa;
  }
}
pestanas.forEach((p, i) => {
  p.addEventListener('click', () => abrir(p.id.slice(2)));
  p.addEventListener('keydown', (e) => {
    const paso = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!paso) return;
    const otra = pestanas[(i + paso + pestanas.length) % pestanas.length];
    abrir(otra.id.slice(2));
    otra.focus();
  });
});

// ---- dibujo de grafos ----
let viz;
async function dibujar(dot, destino) {
  try {
    viz ??= await graphviz();
    const lamina = document.createElement('div');
    lamina.className = 'lamina';
    lamina.append(viz.renderSVGElement(dot));
    // por defecto a tamaño real (se lee); el botón lo encoge para verlo entero
    const barra = document.createElement('div');
    barra.className = 'sobre-lamina';
    const boton = Object.assign(document.createElement('button'), { textContent: 'Ver entero' });
    boton.addEventListener('click', () => {
      boton.textContent = lamina.classList.toggle('entera') ? 'Ver a tamaño real' : 'Ver entero';
    });
    barra.append(boton);
    destino.replaceChildren(barra, lamina);
  } catch (e) {
    destino.innerHTML = `<p class="vacio">No se pudo dibujar el diagrama: ${html(e.message)}</p>`;
  }
}

const tabla = (cabeceras, filas, clases = []) =>
  `<table><thead><tr>${cabeceras.map((c, i) => `<th class="${clases[i] ?? ''}">${html(c)}</th>`).join('')}</tr></thead>
   <tbody>${filas.map((f) => `<tr>${f.map((c, i) => `<td class="${clases[i] ?? ''}">${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;

// =====================================================================
// Compilación
// =====================================================================
async function compilar() {
  const texto = $('esquema').value;
  $('compilar').disabled = true;
  aviso('aviso-esquema', 'Compilando…');
  try {
    const r = await api('/api/compilar', { esquema: texto });
    if (!r.ok) return aviso('aviso-esquema', motivoDe(r), true);
    const d = r.datos;
    estado.compilado = d.ok ? d : null;
    estado.wasm = null;
    if (!d.ok) {
      $('tamanos').innerHTML = 'El esquema no compila: no hay autómata.';
      $('p-automata').innerHTML = '<p class="vacio">El autómata se construye cuando el esquema compila sin errores.</p>';
      pintarErrorDeEsquema(d);
      abrir('analisis');
      return aviso('aviso-esquema', `Error ${d.fase}${d.linea ? ` en la línea ${d.linea}, columna ${d.columna}` : ''}.`, true);
    }
    const t = d.automata.tamano;
    $('tamanos').innerHTML = `|<i>Q</i>| = <b>${t.estados}</b> &nbsp; |Σ| = <b>${t.simbolos_de_entrada}</b> &nbsp; ` +
      `|Γ| = <b>${t.simbolos_de_pila}</b> &nbsp; |δ| = <b>${t.transiciones}</b>`;
    pintarAnalisis(d);
    pintarAutomata(d.automata);
    elegirFormato(d.formato);
    const avisos = d.diagnosticos.length ? ` con ${d.diagnosticos.length} avisos` : '';
    aviso('aviso-esquema', `Compilado${avisos}. ${d.nivel_texto[0].toUpperCase()}${d.nivel_texto.slice(1)}.`);
  } catch (e) {
    aviso('aviso-esquema', `No se pudo contactar con el servidor: ${e.message}`, true);
  } finally {
    $('compilar').disabled = false;
  }
}

function listaDeDiagnosticos(diagnosticos) {
  if (!diagnosticos.length) return '<p class="vacio">Sin diagnósticos.</p>';
  return `<ul class="lista">${diagnosticos.map((d) => `<li><span class="severidad-${d.severidad}">${d.severidad}</span>
    <span class="donde">${enlaceA('esquema', d)}</span>${html(d.mensaje)}
    <span class="donde">${html(d.ruta || '(raíz)')}</span></li>`).join('')}</ul>`;
}

function pintarErrorDeEsquema(d) {
  let cuerpo = `<h3>El esquema no compila: error ${html(d.fase)}</h3>`;
  if (d.diagnosticos) cuerpo += listaDeDiagnosticos(d.diagnosticos);
  else {
    cuerpo += `<p>${html(d.mensaje)}</p><p>${enlaceA('esquema', d, `Ir a la línea ${d.linea}, columna ${d.columna}`)}</p>`;
    if (d.esperados?.length) cuerpo += `<p class="vacio">Se esperaba: <code>${d.esperados.map(html).join('</code>, <code>')}</code>${d.esperados.length >= 12 ? '…' : ''}</p>`;
  }
  $('p-analisis').innerHTML = cuerpo;
}

function pintarAnalisis(d) {
  const tipos = (t) => (Array.isArray(t) ? (t.length === 6 ? 'cualquiera' : t.join(', ') || 'ninguno') : t);
  const xml = d.formato === 'xml';
  $('p-analisis').innerHTML = `
    <p class="vacio">Formato reconocido: ${xml ? 'XSD (documentos XML)' : 'JSON Schema (documentos JSON)'}.</p>
    <h3>Diagnósticos</h3>${listaDeDiagnosticos(d.diagnosticos)}
    <h3>Clasificación de ${xml ? 'construcciones' : 'palabras clave'}</h3>
    <p class="vacio">Nivel del esquema: ${html(d.nivel_texto)}.</p>
    ${tabla(['Categoría', xml ? 'Construcción' : 'Palabra clave', 'Usos', 'Cómo se resuelve'],
      d.clasificacion.map((c) => [`<span class="cat">${c.categoria.length === 1 ? `(${c.categoria})` : html(c.categoria)}</span>`,
        `<code>${html(c.palabra)}</code>`, c.usos, html(c.estrategia)]), ['', '', 'num', ''])}
    <h3>Tabla de símbolos</h3>
    ${tabla(xml ? ['Elemento o tipo', 'Posición', 'Contenido', 'Recursión'] : ['Subesquema', 'Posición', 'Tipos que admite', 'Recursión'],
      d.simbolos.map((s) => [`<code>${html(s.ruta || '(raíz)')}</code>`, enlaceA('esquema', s, `línea ${s.linea}`), html(tipos(s.tipos)),
        s.recursivo ? 'recursivo' : '']))}
    <h3>Referencias</h3>
    ${d.referencias.length ? tabla(['Desde', 'Hacia', 'Cómo se resuelve'], d.referencias.map((r) => [
      `<code>${html(r.origen || '(raíz)')}</code>`, `<code>${html(r.destino || '(raíz)')}</code>`,
      r.recursiva ? 'llamada recursiva: usa la pila' : 'expansión en línea']))
      : `<p class="vacio">El esquema no usa ${xml ? '<code>type</code> ni <code>ref</code> con nombre' : '<code>$ref</code>'}.</p>`}`;
}

function pintarAutomata(a) {
  const panel = $('p-automata');
  const diferidas = a.diferidas.length
    ? `<h3>Verificación diferida</h3><p class="vacio">El autómata no comprueba estas palabras clave; un documento que las
       cruce queda como «válido, con comprobaciones pendientes».</p>
       <ul class="lista">${a.diferidas.map((d) => `<li><code>${html(d.palabra)}</code> en <code>${html(d.ruta || '(raíz)')}</code></li>`).join('')}</ul>`
    : '';
  panel.innerHTML = `
    <h3>Definición formal</h3>
    <pre class="formal">${html(a.descripcion)}</pre>
    <div class="controles">
      <button id="bajar-crate">Descargar el validador en Rust (.zip)</button>
      <button id="bajar-wasm" ${estado.salud.wasm ? '' : 'hidden'}>Descargar el módulo WebAssembly</button>
      <span class="metricas">Tablas: ${numero(a.bytes_de_tablas)} bytes</span>
    </div>
    ${diferidas}
    <h3>Diagrama de estados</h3>
    <div id="diagrama">${a.dot ? '<p class="vacio">Dibujando…</p>'
      : `<p class="vacio">Con ${a.tamano.estados} estados el diagrama no se lee; la tabla de transiciones está debajo.</p>`}</div>
    <h3>Función de transición δ</h3>
    <div class="controles"><label>Filtrar por estado o símbolo <input id="filtro-delta" placeholder="por ejemplo V1 o NUMERO"></label></div>
    <div class="tabla-alta" id="tabla-delta"></div>
    ${a.delta_truncada ? `<p class="vacio">Se muestran las primeras ${a.delta.length} transiciones de ${a.tamano.transiciones}.</p>` : ''}`;
  const pintarDelta = () => {
    const f = $('filtro-delta').value.trim().toLowerCase();
    const filas = a.delta.filter((x) => !f || x.estado.toLowerCase().includes(f) || x.simbolo.toLowerCase().includes(f) || x.destino.toLowerCase().includes(f));
    $('tabla-delta').innerHTML = tabla(['Estado', 'Entrada', 'Destino', 'Pila', 'Acción, peso y pruebas'],
      filas.map((x) => [html(x.estado), html(x.simbolo), html(x.destino), html(x.pila), html(x.efecto)]),
      ['codigo', 'codigo', 'codigo', 'codigo', 'codigo']);
  };
  $('filtro-delta').addEventListener('input', pintarDelta);
  pintarDelta();
  $('bajar-crate').addEventListener('click', () => descargar('/api/crate', { esquema: $('esquema').value, nombre: 'validador_generado' }, 'validador_generado.zip', 'bajar-crate'));
  $('bajar-wasm').addEventListener('click', () => descargar('/api/wasm', { esquema: $('esquema').value }, 'validador.wasm', 'bajar-wasm'));
  if (a.dot) dibujar(a.dot, $('diagrama'));
}

async function descargar(ruta, cuerpo, nombre, idBoton) {
  const boton = $(idBoton);
  const texto = boton.textContent;
  boton.disabled = true;
  boton.textContent = 'Preparando…';
  try {
    const r = await api(ruta, cuerpo);
    if (!r.ok) return aviso('aviso-esquema', motivoDe(r), true);
    const enlace = Object.assign(document.createElement('a'), { href: URL.createObjectURL(r.datos), download: nombre });
    enlace.click();
    URL.revokeObjectURL(enlace.href);
  } finally {
    boton.disabled = false;
    boton.textContent = texto;
  }
}

// =====================================================================
// Validación
// =====================================================================
const cuerpoDeValidacion = () => {
  const bytes = bytesDelDocumento();
  return { esquema: $('esquema').value, payload_b64: aBase64(bytes), fallo_temprano: $('temprano').checked };
};

function textoDeVeredicto(d) {
  const partes = [];
  if (d.violaciones) partes.push(violaciones(d.violaciones));
  if (d.pendientes) partes.push(`${numero(d.pendientes)} comprobaciones pendientes`);
  partes.push(`profundidad máxima ${d.profundidad}`);
  return partes.join(', ');
}

function pintarVeredicto(d) {
  let cuerpo = `<p class="veredicto"><span class="estado ${d.estado}">${ESTADOS[d.estado]}</span><small>${textoDeVeredicto(d)}</small></p>`;
  if (d.error) {
    cuerpo += `<p>${html(d.error.motivo)}: ${enlaceA('documento', d.error)}${d.ruta ? `, dentro de <code>${html(d.ruta)}</code>` : ''}.</p>`;
  }
  if (d.detalle.length) {
    cuerpo += `<h3>Violaciones</h3><p class="vacio">La primera está en <code>${html(d.ruta || '(raíz)')}</code>.</p>
      <ul class="lista">${d.detalle.map((v) => `<li><span class="donde">${enlaceA('documento', v)}</span>${html(v.mensaje)}</li>`).join('')}</ul>
      ${d.detalle_truncado ? '<p class="vacio">Se muestran las primeras 200.</p>' : ''}`;
  }
  cuerpo += '<p id="resultado-wasm" class="metricas"></p>';
  $('veredicto').innerHTML = cuerpo;
  $('wasm').hidden = !estado.salud.wasm;
}

async function validar() {
  abrir('validacion');
  $('maquina').hidden = true;
  aviso('aviso-documento', 'Validando…');
  try {
    const r = await api('/api/validar', cuerpoDeValidacion());
    if (!r.ok) {
      if (r.datos?.esquema) await compilar();
      return aviso('aviso-documento', motivoDe(r), true);
    }
    pintarVeredicto(r.datos);
    aviso('aviso-documento', `${ESTADOS[r.datos.estado]}: ${textoDeVeredicto(r.datos)}.`, !['valido', 'pendiente'].includes(r.datos.estado));
  } catch (e) {
    aviso('aviso-documento', `No se pudo contactar con el servidor: ${e.message}`, true);
  }
}

/** WebSocket con los mensajes en cola, para leerlos con await. */
function conectar(ruta) {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${ruta}`);
  const cola = [];
  let pendiente = null;
  const entregar = (m) => (pendiente ? (pendiente(m), (pendiente = null)) : cola.push(m));
  ws.onmessage = (e) => entregar(JSON.parse(e.data));
  ws.onclose = () => entregar({ tipo: 'cerrado' });
  const siguiente = () => (cola.length ? Promise.resolve(cola.shift()) : new Promise((r) => (pendiente = r)));
  return new Promise((resolver, rechazar) => {
    ws.onopen = () => resolver({ ws, siguiente });
    ws.onerror = () => rechazar(new Error('no se pudo abrir la conexión con el servidor'));
  });
}

function pintarMaquina(p, bytes) {
  const decodificar = (trozo) => new TextDecoder().decode(trozo);
  const desde = Math.max(0, p.bytes - 160);
  const cabeza = p.bytes < bytes.length ? decodificar(bytes.subarray(p.bytes, Math.min(bytes.length, p.bytes + 4))).slice(0, 1) : '';
  const resto = p.bytes < bytes.length ? decodificar(bytes.subarray(p.bytes, Math.min(bytes.length, p.bytes + 260))).slice(cabeza.length) : '';
  $('cinta').innerHTML = `${desde ? '…' : ''}<span class="leido">${html(decodificar(bytes.subarray(desde, p.bytes)))}</span>` +
    `<span class="cabeza">${html(cabeza || '⊣')}</span>${html(resto)}${p.bytes + 260 < bytes.length ? '…' : ''}`;
  $('contadores').innerHTML = [
    ['Leído', `${numero(p.bytes)} de ${numero(bytes.length)} bytes`], ['Estado', p.estado_del_automata],
    ['Violaciones', numero(p.violaciones)], ['Profundidad', `${p.profundidad} (máxima ${p.profundidad_maxima})`],
  ].map(([k, v]) => `<div><dt>${k}</dt><dd>${html(v)}</dd></div>`).join('');
  const tramos = p.ruta.split('/').slice(1);
  const marcos = p.pila.map((simbolo, i) => ({ simbolo, tramo: i === 0 ? '' : tramos[i - 1] ?? '' })).reverse();
  const visibles = marcos.slice(0, 11);
  $('pila').innerHTML = visibles.map((m, i) => `<li class="${m.simbolo === 'Z0' ? 'fondo' : ''}">
      <span class="simbolo">${html(m.simbolo)}</span><span class="tramo">${html(m.tramo)}</span></li>`).join('') +
    (marcos.length > visibles.length ? `<li class="mas">… ${marcos.length - visibles.length} más</li>` : '');
}

let cancelarStreaming = null;
async function validarPorBloques() {
  if (cancelarStreaming) return cancelarStreaming();
  abrir('validacion');
  const bytes = bytesDelDocumento();
  const tamano = Number($('bloque').value);
  const pausa = Number($('pausa').value);
  let cancelado = false;
  let canal;
  try {
    canal = await conectar('/ws/validar');
  } catch (e) {
    return aviso('aviso-documento', `${e.message[0].toUpperCase()}${e.message.slice(1)}.`, true);
  }
  cancelarStreaming = () => { cancelado = true; };
  $('streaming').textContent = 'Detener';
  try {
    canal.ws.send(JSON.stringify({ tipo: 'inicio', esquema: $('esquema').value, fallo_temprano: $('temprano').checked }));
    const listo = await canal.siguiente();
    if (listo.tipo !== 'listo') {
      if (listo.esquema) await compilar();
      return aviso('aviso-documento', listo.esquema ? 'El esquema no compila: revisa la pestaña Análisis.' : (listo.mensaje ?? 'El servidor cerró la conexión.'), true);
    }
    $('maquina').hidden = false;
    $('veredicto').innerHTML = '<h3>Violaciones</h3><ul class="lista" id="en-vivo"></ul>';
    pintarMaquina({ bytes: 0, violaciones: 0, profundidad: 0, profundidad_maxima: 0, estado_del_automata: '—', ruta: '', pila: ['Z0'] }, bytes);
    for (let i = 0; i < bytes.length && !cancelado; i += tamano) {
      canal.ws.send(bytes.subarray(i, i + tamano));
      const p = await canal.siguiente();
      if (p.tipo !== 'progreso') return aviso('aviso-documento', p.mensaje ?? 'El servidor cerró la conexión.', true);
      pintarMaquina(p, bytes);
      for (const v of p.nuevas) {
        $('en-vivo').insertAdjacentHTML('beforeend', `<li><span class="donde">${enlaceA('documento', v, `byte ${v.byte}`)}</span>${html(v.mensaje)}</li>`);
      }
      aviso('aviso-documento', `Leyendo… ${numero(p.bytes)} de ${numero(bytes.length)} bytes.`);
      if (p.terminado) break;
      if (pausa) await esperar(pausa);
    }
    if (cancelado) return aviso('aviso-documento', 'Validación detenida.');
    canal.ws.send(JSON.stringify({ tipo: 'fin' }));
    const final = await canal.siguiente();
    if (final.tipo === 'resultado') {
      pintarVeredicto(final);
      aviso('aviso-documento', `${ESTADOS[final.estado]}: ${textoDeVeredicto(final)}.`, !['valido', 'pendiente'].includes(final.estado));
    }
  } finally {
    canal.ws.close();
    cancelarStreaming = null;
    $('streaming').textContent = 'Validar por bloques';
  }
}

/** Ejecuta el validador nativo, compilado a WebAssembly, dentro del navegador. */
async function validarConWasm() {
  const salida = $('resultado-wasm') ?? $('veredicto');
  const esquema = $('esquema').value;
  $('wasm').disabled = true;
  salida.textContent = 'Compilando el validador a WebAssembly… (la primera vez tarda unos segundos)';
  try {
    if (estado.wasm?.esquema !== esquema) {
      const r = await fetch('/api/wasm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ esquema }) });
      if (!r.ok) {
        salida.textContent = (await r.json().catch(() => ({}))).mensaje ?? 'El servidor no pudo compilar el módulo.';
        return;
      }
      const { instance } = await WebAssembly.instantiate(await r.arrayBuffer(), {});
      estado.wasm = { esquema, pdv: instance.exports, base: instance.exports.memory.buffer.byteLength };
    }
    const { pdv, base } = estado.wasm;
    const bytes = bytesDelDocumento();
    const tamano = Number(pdv.pdv_tamano());
    const resultado = base + Math.ceil(tamano / 8) * 8;
    const ruta = resultado + 32;
    const datos = ruta + 256;
    const faltan = datos + bytes.length + 16 - pdv.memory.buffer.byteLength;
    if (faltan > 0) pdv.memory.grow(Math.ceil(faltan / 65536));
    new Uint8Array(pdv.memory.buffer).set(bytes, datos);
    const una = () => {
      pdv.pdv_iniciar(base, 128, $('temprano').checked ? 1 : 0, 0n);
      pdv.pdv_feed(base, datos, bytes.length);
      pdv.pdv_finish(base, resultado);
    };
    let veces = 0;
    const inicio = performance.now();
    do { una(); veces++; } while (performance.now() - inicio < 60);
    const segundos = (performance.now() - inicio) / 1000 / veces;
    const r = new DataView(pdv.memory.buffer);
    const nombre = Object.keys(ESTADOS)[r.getUint32(resultado, true)];
    const largo = Number(pdv.pdv_ruta(base, ruta, 256));
    const donde = new TextDecoder().decode(new Uint8Array(pdv.memory.buffer, ruta, largo));
    const violaciones = r.getUint32(resultado + 4, true);
    salida.innerHTML = `WebAssembly en este navegador: <span class="estado ${nombre}">${ESTADOS[nombre]}</span>` +
      `${violaciones ? `, ${numero(violaciones)} ${violaciones === 1 ? 'violación' : 'violaciones'}, la primera en <code>${html(donde || '(raíz)')}</code>` : ''}. ` +
      `${numero(bytes.length / 1e6 / segundos, 1)} MB/s, sin salir de la página.`;
  } catch (e) {
    salida.textContent = `No se pudo ejecutar el módulo: ${e.message}`;
  } finally {
    $('wasm').disabled = false;
  }
}

// =====================================================================
// Árbol de derivación
// =====================================================================
async function derivar(esEsquema) {
  const texto = esEsquema ? $('esquema').value : $('documento').value;
  $('arbol').innerHTML = '<p class="vacio">Derivando…</p>';
  const r = await api('/api/arbol', { texto, es_esquema: esEsquema });
  if (!r.ok) {
    $('arbol').innerHTML = `<p>${html(motivoDe(r))}</p>` +
      (r.datos?.linea ? `<p>${enlaceA(esEsquema ? 'esquema' : 'documento', r.datos, 'Ir a esa posición')}</p>` : '');
    return;
  }
  $('arbol').innerHTML = `<p class="vacio">${r.datos.tokens} tokens. Cada nodo interior es un no terminal con la producción
    que se le aplicó; las hojas son los tokens, de izquierda a derecha.</p><div id="dibujo-arbol"></div>
    <details><summary>Ver como texto</summary><pre class="formal">${html(r.datos.texto)}</pre></details>`;
  dibujar(r.datos.dot, $('dibujo-arbol'));
}

// =====================================================================
// Comparación en vivo
// =====================================================================
const mediana = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];
const tamanoLegible = (b) => (b >= 1048576 ? `${numero(b / 1048576, 1)} MB` : b >= 1024 ? `${numero(b / 1024, 1)} KB` : `${numero(b)} bytes`);

/** Gráfico de líneas: una serie por validador, rondas en el eje horizontal y MB/s en el vertical (desde cero). */
function graficoDeLineas(destino, series, rondas) {
  const [ancho, alto, m] = [640, 250, { arriba: 14, derecha: 96, abajo: 30, izquierda: 46 }];
  const maximo = Math.max(1, ...series.flatMap((s) => s.valores));
  const paso = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000].find((p) => maximo / p <= 5);
  const tope = Math.ceil(maximo / paso) * paso;
  const x = (i) => m.izquierda + (rondas === 1 ? 0.5 : i / (rondas - 1)) * (ancho - m.izquierda - m.derecha);
  const y = (v) => alto - m.abajo - (v / tope) * (alto - m.arriba - m.abajo);
  let svg = `<svg viewBox="0 0 ${ancho} ${alto}" role="img" aria-label="Megabytes por segundo en cada ronda">`;
  for (let v = 0; v <= tope; v += paso) {
    svg += `<line class="${v ? 'rejilla' : 'eje'}" x1="${m.izquierda}" x2="${ancho - m.derecha}" y1="${y(v)}" y2="${y(v)}"/>` +
      `<text x="${m.izquierda - 8}" y="${y(v) + 4}" text-anchor="end">${numero(v)}</text>`;
  }
  for (let i = 0; i < rondas; i++) {
    if (rondas <= 10 || i % Math.ceil(rondas / 10) === 0 || i === rondas - 1) svg += `<text x="${x(i)}" y="${alto - 10}" text-anchor="middle">${i + 1}</text>`;
  }
  svg += `<text x="${m.izquierda}" y="${alto - 10}" text-anchor="end" dx="-14">ronda</text>`;
  const finales = series.map((s) => ({ s, y: y(s.valores.at(-1)) })).sort((a, b) => a.y - b.y);
  if (finales.length === 2 && finales[1].y - finales[0].y < 14) finales[1].y = finales[0].y + 14;   // rótulos sin pisarse
  for (const s of series) {
    const puntos = s.valores.map((v, i) => `${x(i)},${y(v)}`).join(' ');
    svg += `<polyline fill="none" stroke="var(${s.color})" stroke-width="2" stroke-linejoin="round" points="${puntos}"/>`;
    svg += s.valores.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="var(${s.color})" stroke="var(--superficie)" stroke-width="2"/>`).join('');
  }
  for (const f of finales) {
    svg += `<text class="rotulo" x="${x(f.s.valores.length - 1) + 10}" y="${f.y + 4}">${html(f.s.corto)} ${numero(f.s.valores.at(-1))}</text>`;
  }
  svg += '<line class="mira" y1="0" y2="0" x1="0" x2="0" visibility="hidden"/></svg>';
  destino.querySelector('.lienzo').innerHTML = svg;

  // capa de exploración: la ronda más cercana al cursor, con los valores de las dos series
  const dibujo = destino.querySelector('svg');
  const mira = dibujo.querySelector('.mira');
  const globo = destino.querySelector('.globo');
  dibujo.onmousemove = (e) => {
    const caja = dibujo.getBoundingClientRect();
    const px = ((e.clientX - caja.left) / caja.width) * ancho;
    const i = Math.max(0, Math.min(series[0].valores.length - 1, Math.round(((px - m.izquierda) / (ancho - m.izquierda - m.derecha)) * (rondas - 1 || 1))));
    mira.setAttribute('x1', x(i)); mira.setAttribute('x2', x(i));
    mira.setAttribute('y1', m.arriba); mira.setAttribute('y2', alto - m.abajo);
    mira.setAttribute('visibility', 'visible');
    globo.hidden = false;
    globo.innerHTML = `Ronda ${i + 1}<br>` + series.map((s) => `${html(s.nombre)}: <b>${numero(s.valores[i], 1)}</b> MB/s`).join('<br>');
    const izquierda = (x(i) / ancho) * caja.width + dibujo.offsetLeft;
    globo.style.left = `${Math.min(izquierda + 12, caja.width - globo.offsetWidth)}px`;
    globo.style.top = `${dibujo.offsetTop + 8}px`;
  };
  dibujo.onmouseleave = () => { mira.setAttribute('visibility', 'hidden'); globo.hidden = true; };
}

function pintarComparacion(mediciones, rondas) {
  const propio = mediciones.map((x) => x.streaming);
  const base = mediciones.map((x) => x.dom);
  const ultima = mediciones.at(-1);
  if (base[0].error) {
    $('comparacion').innerHTML = `<p>La línea base no pudo compilar este esquema: ${html(base[0].error)}</p>`;
    return;
  }
  const [mbPropio, mbBase] = [mediana(propio.map((s) => s.mb_s)), mediana(base.map((s) => s.mb_s))];
  const cifra = (titulo, valor, nota) => `<div><dt>${titulo}</dt><dd>${valor}<small>${nota}</small></dd></div>`;
  $('comparacion').innerHTML = `
    <p class="vacio">Documento de ${tamanoLegible(ultima.bytes)}; ronda ${mediciones.length} de ${rondas}.
      ${ultima.dom.solo_parseo
    ? `La línea base solo construye el árbol del documento y no lo valida contra el XSD (${html(ultima.dom.estado)}); veredicto del autómata:
       <span class="estado ${ultima.streaming.estado}">${ESTADOS[ultima.streaming.estado]}</span>.`
    : `Los dos validadores dan el mismo veredicto: ${ultima.streaming.estado === ultima.dom.estado ? 'sí' : 'no'}
       (<span class="estado ${ultima.streaming.estado}">${ESTADOS[ultima.streaming.estado]}</span>).`}</p>
    <dl class="cifras">
      ${cifra('Autómata de pila', `${numero(mbPropio, 1)} MB/s`, `${numero(mediana(propio.map((s) => s.ciclos_por_byte)), 1)} ciclos por byte`)}
      ${cifra('Línea base', `${numero(mbBase, 1)} MB/s`, `${numero(mediana(base.map((s) => s.ciclos_por_byte)), 1)} ciclos por byte`)}
      ${cifra('Relación', `${numero(mbPropio / mbBase, 2)} ×`, mbPropio >= mbBase ? 'el autómata es más rápido' : 'la línea base es más rápida')}
      ${cifra('Reservas de memoria', numero(ultima.streaming.asignaciones), `frente a ${numero(ultima.dom.asignaciones)} de la línea base`)}
      ${cifra('Memoria usada', tamanoLegible(ultima.streaming.memoria_usada), `frente a ${tamanoLegible(ultima.dom.pico_heap)}; profundidad ${ultima.streaming.profundidad}`)}
    </dl>
    <div class="grafico">
      <h3>Velocidad en cada ronda</h3>
      <p class="nota">Megabytes por segundo; más alto es mejor.</p>
      <div class="leyenda"><span><i style="border-color: var(--serie-1)"></i>Autómata de pila (una pasada, sin árbol)</span>
        <span><i style="border-color: var(--serie-2)"></i>Línea base (${ultima.dom.solo_parseo ? 'solo construye el árbol' : 'árbol + validador genérico'})</span></div>
      <div class="lienzo"></div><div class="globo" hidden></div>
    </div>
    <details><summary>Ver las mediciones como tabla</summary>
    ${tabla(['Ronda', 'Autómata MB/s', 'Línea base MB/s', 'Solo construir el árbol MB/s', 'Autómata ciclos/byte', 'Línea base ciclos/byte'],
      mediciones.map((x) => [x.ronda, numero(x.streaming.mb_s, 1), numero(x.dom.mb_s, 1), numero(x.dom_parseo.mb_s, 1),
        numero(x.streaming.ciclos_por_byte, 2), numero(x.dom.ciclos_por_byte, 2)]), ['num', 'num', 'num', 'num', 'num', 'num'])}
    </details>`;
  graficoDeLineas($('comparacion').querySelector('.grafico'), [
    { nombre: 'Autómata de pila', corto: 'Autómata', color: '--serie-1', valores: propio.map((s) => s.mb_s) },
    { nombre: 'Línea base', corto: 'Base', color: '--serie-2', valores: base.map((s) => s.mb_s) },
  ], rondas);
}

async function comparar() {
  $('comparar').disabled = true;
  $('comparacion').innerHTML = '<p class="vacio">Midiendo la primera ronda…</p>';
  const rondas = Number($('rondas').value);
  let canal;
  try {
    canal = await conectar('/ws/comparar');
    canal.ws.send(JSON.stringify({ esquema: $('esquema').value, payload_b64: aBase64(bytesDelDocumento()), rondas }));
    const mediciones = [];
    for (;;) {
      const m = await canal.siguiente();
      if (m.tipo === 'medicion') {
        mediciones.push(m);
        pintarComparacion(mediciones, rondas);
      } else {
        if (m.tipo === 'error') $('comparacion').innerHTML = `<p>${html(m.esquema ? 'El esquema no compila: revisa la pestaña Análisis.' : m.mensaje)}</p>`;
        break;
      }
    }
  } catch (e) {
    $('comparacion').innerHTML = `<p>${html(e.message[0].toUpperCase() + e.message.slice(1))}.</p>`;
  } finally {
    canal?.ws.close();
    $('comparar').disabled = false;
  }
}

// =====================================================================
// Casos de prueba
// =====================================================================
function ponerDocumento(bytes) {
  try {
    $('documento').value = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    estado.bytesFijos = null;
    aviso('aviso-documento', '');
  } catch {
    $('documento').value = new TextDecoder().decode(bytes);
    estado.bytesFijos = bytes;
    aviso('aviso-documento', 'Este documento no es UTF-8 válido: se envía tal como está guardado mientras no lo edites.');
  }
}

async function cargarCaso(id) {
  const r = await api(`/api/casos/${id}`);
  if (!r.ok) return;
  $('esquema').value = r.datos.esquema;
  ponerDocumento(deBase64(r.datos.payload_b64));
  await compilar();
  await validar();
}

function filaDeCaso(c) {
  const u = c.ultimo;
  const m = u?.metricas;
  const medidas = !m ? '' : m.no_disponible ? 'sin medición' :
    `${numero(m.mb_s, 1)} MB/s${m.mb_s_dom ? ` (${numero(m.mb_s / m.mb_s_dom, 2)} × la línea base)` : ''}, ${numero(m.asignaciones)} reservas`;
  return [
    `<button class="enlace" data-cargar="${c._id}">${html(c.nombre)}</button>${c.lenguaje === 'xml' ? ' <span class="cat">XML</span>' : ''}
     <br><span class="metricas">${html(c.notas ?? '')}</span>`,
    tamanoLegible(c.bytes),
    c.esperado ? `<span class="estado ${c.esperado.estado}">${ESTADOS[c.esperado.estado]}</span>` : '',
    u ? `<span class="estado ${u.estado}">${ESTADOS[u.estado]}</span>${u.coincide === undefined ? '' : u.coincide ? ', coincide' : ', <b>no coincide</b>'}
         <br><span class="metricas">${medidas}</span>` : '<span class="metricas">sin ejecutar</span>',
    `<span class="acciones"><button data-ejecutar="${c._id}">Ejecutar</button> <button data-borrar="${c._id}">Borrar</button></span>`,
  ];
}

async function listarCasos() {
  const categoria = $('categoria').value;
  const r = await api(`/api/casos${categoria ? `?categoria=${encodeURIComponent(categoria)}` : ''}`);
  if (!r.ok) return;
  const generados = estado.generados.length ? `<h3>Generados desde el esquema</h3>
    ${tabla(['Clase', 'Resultado del autómata', 'Qué se cambió', ''], estado.generados.map((g, i) => [
      html(CATEGORIAS[g.clase] ?? g.clase), `<span class="estado ${g.estado}">${ESTADOS[g.estado]}</span>${g.violaciones ? `, ${violaciones(g.violaciones)}` : ''}`,
      html(g.descripcion), `<span class="acciones"><button data-generado="${i}">Cargar</button></span>`]))}` : '';
  if (!r.datos.length) {
    $('casos').innerHTML = `${generados}<p class="vacio">No hay casos guardados${categoria ? ' en esta categoría' : ''}. Guarda el esquema y el documento
      actuales con el formulario de arriba, o carga los de la propuesta con <code>python -m web.backend.sembrar</code>.</p>`;
    return;
  }
  let filas = '';
  let anterior = null;
  for (const c of r.datos) {
    if (c.categoria !== anterior) filas += `<tr class="grupo"><td colspan="5">${html(CATEGORIAS[c.categoria] ?? c.categoria)}</td></tr>`;
    anterior = c.categoria;
    filas += `<tr>${filaDeCaso(c).map((x, i) => `<td class="${i === 1 ? 'num' : ''}">${x}</td>`).join('')}</tr>`;
  }
  $('casos').innerHTML = `${generados}<h3>Guardados (${r.datos.length})</h3><table><thead><tr><th>Caso</th><th class="num">Tamaño</th>
    <th>Esperado</th><th>Última ejecución</th><th></th></tr></thead><tbody>${filas}</tbody></table>`;
}

$('casos').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.cargar) return cargarCaso(b.dataset.cargar);
  if (b.dataset.generado) {
    ponerDocumento(deBase64(estado.generados[Number(b.dataset.generado)].payload_b64));
    return validar();
  }
  if (b.dataset.ejecutar) {
    b.disabled = true;
    b.textContent = 'Ejecutando…';
    await api(`/api/casos/${b.dataset.ejecutar}/ejecutar`, {});
    return listarCasos();
  }
  if (b.dataset.borrar) {
    if (b.dataset.confirmado) {
      await api(`/api/casos/${b.dataset.borrar}`, undefined, 'DELETE');
      return listarCasos();
    }
    b.dataset.confirmado = '1';        // segundo clic para confirmar, sin diálogos del navegador
    b.textContent = 'Confirmar borrado';
  }
});

async function ejecutarTodos() {
  $('ejecutar-todos').disabled = true;
  const r = await api('/api/casos');
  for (const [i, c] of (r.datos ?? []).entries()) {
    $('ejecutar-todos').textContent = `Ejecutando ${i + 1} de ${r.datos.length}…`;
    await api(`/api/casos/${c._id}/ejecutar`, {});
  }
  $('ejecutar-todos').textContent = 'Ejecutar todos';
  $('ejecutar-todos').disabled = false;
  listarCasos();
}

async function generarCasos() {
  const r = await api('/api/generar', { esquema: $('esquema').value, validos: 4, invalidos: 4, malformados: 2, semilla: Date.now() % 100000 });
  if (!r.ok) {
    $('casos').innerHTML = `<p>${html(motivoDe(r))}</p>`;
    return;
  }
  estado.generados = r.datos;
  listarCasos();
}

$('guardar').addEventListener('submit', async (e) => {
  e.preventDefault();
  const cuerpo = cuerpoDeValidacion();
  const v = await api('/api/validar', cuerpo);
  if (!v.ok) return aviso('aviso-documento', motivoDe(v), true);
  const r = await api('/api/casos', {
    nombre: $('nombre-caso').value.trim(), categoria: $('categoria-caso').value, esquema: cuerpo.esquema,
    payload_b64: cuerpo.payload_b64, esperado: { estado: v.datos.estado, violaciones: v.datos.violaciones },
  });
  if (!r.ok) return aviso('aviso-documento', motivoDe(r), true);
  $('nombre-caso').value = '';
  listarCasos();
});

// =====================================================================
// Arranque
// =====================================================================
for (const area of [$('esquema'), $('documento')]) {
  area.addEventListener('keydown', (e) => {
    if (e.key === 'Tab' && !e.ctrlKey && !e.altKey) {       // tabulador como sangría, no como salto de foco
      e.preventDefault();
      area.setRangeText('  ', area.selectionStart, area.selectionEnd, 'end');
    } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      (area.id === 'esquema' ? compilar : validar)();
    }
  });
}
$('documento').addEventListener('input', () => { estado.bytesFijos = null; });
$('compilar').addEventListener('click', compilar);
$('validar').addEventListener('click', validar);
$('streaming').addEventListener('click', validarPorBloques);
$('wasm').addEventListener('click', validarConWasm);
$('derivar').addEventListener('click', () => derivar(false));
$('derivar-esquema').addEventListener('click', () => derivar(true));
$('comparar').addEventListener('click', comparar);
$('categoria').addEventListener('change', listarCasos);
$('ejecutar-todos').addEventListener('click', ejecutarTodos);
$('generar').addEventListener('click', generarCasos);

for (const [valor, nombre] of Object.entries(CATEGORIAS)) {
  $('categoria').add(new Option(nombre, valor));
  $('categoria-caso').add(new Option(nombre, valor));
}

/** Deja el selector de formato en «json» o «xml» y ofrece los ejemplos de ese formato. */
function elegirFormato(formato) {
  const lista = $('ejemplos');
  if (estado.formato === formato) return;
  estado.formato = formato;
  $('formato').value = formato;
  const actual = lista.value;
  lista.length = 1;                      // se conserva «Cargar un ejemplo…»
  for (const e of estado.ejemplos ?? []) if (e.lenguaje === formato) lista.add(new Option(e.nombre, e.nombre));
  lista.value = [...lista.options].some((o) => o.value === actual) ? actual : '';
}

async function arrancar() {
  try {
    const [salud, ejemplos] = await Promise.all([api('/api/salud'), api('/api/ejemplos')]);
    estado.salud = salud.datos;
    $('servidor').innerHTML = `Casos en ${salud.datos.almacen === 'mongodb' ? 'MongoDB' : 'archivos locales'} (${salud.datos.casos})<br>` +
      `Medición nativa: ${salud.datos.medicion ? 'disponible' : 'no disponible'}; WebAssembly: ${salud.datos.wasm ? 'disponible' : 'no disponible'}`;
    $('comparar').disabled = !salud.datos.medicion;
    estado.ejemplos = ejemplos.datos;
    elegirFormato('json');
    $('formato').addEventListener('change', () => {
      elegirFormato($('formato').value);
      $('ejemplos').value = estado.ejemplos.find((x) => x.lenguaje === $('formato').value)?.nombre ?? '';
      $('ejemplos').dispatchEvent(new Event('change'));
    });
    $('ejemplos').addEventListener('change', async () => {
      const e = ejemplos.datos.find((x) => x.nombre === $('ejemplos').value);
      if (!e) return;
      $('esquema').value = e.esquema;
      ponerDocumento(codificar(e.payload));
      await compilar();
      await validar();
      abrir('analisis');
    });
    $('ejemplos').value = ejemplos.datos[0].nombre;
    $('ejemplos').dispatchEvent(new Event('change'));
    listarCasos();
  } catch (e) {
    $('servidor').textContent = 'Sin conexión con el servidor.';
  }
}
arrancar();
