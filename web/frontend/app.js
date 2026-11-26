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
  sustentacion: 'Esquemas sencillos', valido: 'Válidos', invalido: 'Inválidos', malformado: 'Malformados', 'fuera-del-dpda': 'Fuera del DPDA puro',
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
  document.dispatchEvent(new Event('pestana-abierta'));   // los diagramas se reajustan al hacerse visibles
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
// Visor con zoom: se abre ajustado al ancho de la lámina; se arrastra para desplazarse, Ctrl + rueda (o los
// botones) acerca y aleja alrededor del puntero, y puede pasar a pantalla completa.
let viz;
const ESCALA_MIN = 0.02, ESCALA_MAX = 4, MARGEN = 12;

/**
 * Resaltado de un autómata dibujado por Graphviz: al elegir un estado se destacan sus transiciones (entrantes y
 * salientes) y sus vecinos, y se atenúa el resto. También sustituye los títulos de Graphviz («q2->q6») por textos
 * legibles, que el navegador muestra al pasar el puntero. Devuelve la función que atiende un clic.
 */
function prepararResaltado(svg) {
  const nodos = new Map();   // nombre en el DOT (q0, ini, ret) → <g>
  for (const g of svg.querySelectorAll('g.node')) {
    g.dataset.id = g.querySelector('title')?.textContent ?? '';
    nodos.set(g.dataset.id, g);
  }
  const aristas = [...svg.querySelectorAll('g.edge')].map((g) => {
    const [de, a] = (g.querySelector('title')?.textContent ?? '').split('->');
    return { g, de, a };
  });
  // el nombre del estado: un nombre largo de estados fusionados («A≡B≡C») viene partido en varias líneas
  const nombre = (id) => (id === 'ret' ? '↩'
    : [...(nodos.get(id)?.querySelectorAll('text') ?? [])].map((t) => t.textContent).join('') || id);
  for (const [id, g] of nodos) {
    const titulo = g.querySelector('title');
    if (!titulo) continue;
    const salen = aristas.filter((x) => x.de === id).length, entran = aristas.filter((x) => x.a === id).length;
    titulo.textContent = id === 'ini' ? 'Inicio'
      : id === 'ret' ? '↩ Retorno: se vuelve al estado que indica la cima de la pila'
      : `${nombre(id)}: ${salen} transiciones salientes, ${entran} entrantes`;
  }
  for (const x of aristas) {
    const titulo = x.g.querySelector('title');
    // la etiqueta visible o, si la arista no la tiene (las de violación), el tooltip que Graphviz pone en un <a>
    const enlace = x.g.querySelector('a');
    const etiqueta = [...x.g.querySelectorAll('text')].map((t) => t.textContent).join('\n')
      || enlace?.getAttributeNS('http://www.w3.org/1999/xlink', 'title') || enlace?.getAttribute('title') || '';
    // el <a> de Graphviz tiene su propio título; se quita para que el navegador muestre el nuestro
    enlace?.removeAttributeNS('http://www.w3.org/1999/xlink', 'title');
    enlace?.removeAttribute('title');
    if (titulo) titulo.textContent = `${nombre(x.de)} → ${nombre(x.a)}${etiqueta ? `\n${etiqueta}` : ''}`;
  }

  let actual = null;
  const resaltar = (id) => {
    actual = id;
    svg.classList.toggle('con-foco', id !== null);
    for (const g of nodos.values()) g.classList.remove('foco', 'vecino');
    for (const x of aristas) {
      const toca = id !== null && (x.de === id || x.a === id);
      x.g.classList.toggle('activa', toca);
      if (toca) { nodos.get(x.de)?.classList.add('vecino'); nodos.get(x.a)?.classList.add('vecino'); }
    }
    if (id !== null) nodos.get(id)?.classList.add('foco');
  };
  const alClic = (objetivo) => {
    const g = objetivo?.closest?.('g.node');
    const id = g && g.dataset.id !== 'ini' ? g.dataset.id : null;
    resaltar(id === actual ? null : id);   // otro clic en el mismo estado, en el fondo o Esc: se quita
  };

  // ---- recorrido de una validación: estado actual, visitados y última transición ----
  const porNombre = new Map([...nodos].filter(([id]) => id !== 'ini' && id !== 'ret').map(([id]) => [nombre(id), id]));
  let enCurso = null;   // id del estado actual
  const reiniciar = () => {
    enCurso = null;
    for (const g of nodos.values()) g.classList.remove('actual', 'visitado');
    for (const x of aristas) x.g.classList.remove('recorrida', 'ultima');
  };
  const inicial = [...nodos].find(([, g]) => g.classList.contains('inicial'))?.[0];
  /**
   * Marca una transición: el estado «hacia» pasa a ser el actual y se resalta la arista tomada desde «desde»
   * (directa, un lazo, o pasando por el punto de retorno ↩ si el diagrama lo agrupa). Sin «hacia» marca el
   * estado inicial; sin «desde» se parte del estado actual. Devuelve el <g> del estado, o null si no está.
   */
  const marcar = (hacia, desde) => {
    const id = hacia == null ? inicial : porNombre.get(hacia);
    if (id === undefined) return null;
    const origen = desde === undefined ? enCurso : porNombre.get(desde);
    for (const x of aristas) x.g.classList.remove('ultima');
    if (origen != null) {
      const directa = aristas.filter((x) => x.de === origen && x.a === id);
      const tomadas = directa.length ? directa
        : [...aristas.filter((x) => x.de === origen && x.a === 'ret'), ...aristas.filter((x) => x.de === 'ret' && x.a === id)];
      if (directa.length || tomadas.length === 2) for (const x of tomadas) x.g.classList.add('recorrida', 'ultima');
    }
    if (enCurso !== null && enCurso !== id) nodos.get(enCurso).classList.replace('actual', 'visitado');
    enCurso = id;
    nodos.get(id).classList.remove('visitado');
    nodos.get(id).classList.add('actual');
    return nodos.get(id);
  };
  return { alClic, marcar, reiniciar };
}

async function dibujar(dot, destino, opciones = {}) {
  try {
    viz ??= await graphviz();
    const svg = viz.renderSVGElement(dot);
    // Graphviz da el tamaño en puntos; se pasa a píxeles y desde aquí lo controla la escala
    const ancho = parseFloat(svg.getAttribute('width')) * 4 / 3;
    const alto = parseFloat(svg.getAttribute('height')) * 4 / 3;
    svg.removeAttribute('width');
    svg.removeAttribute('height');

    const crear = (etiqueta, clase, texto) => Object.assign(document.createElement(etiqueta), { className: clase, textContent: texto ?? '' });
    const marco = crear('div', 'diagrama');
    const barra = crear('div', 'sobre-lamina');
    const lamina = crear('div', 'lamina');
    lamina.tabIndex = 0;
    lamina.setAttribute('aria-label', 'Diagrama: arrastra para desplazarte; Ctrl + rueda o + y − para el zoom');
    lamina.append(svg);
    const boton = (texto, titulo) => Object.assign(crear('button', '', texto), { type: 'button', title: titulo });
    const ayuda = crear('span', 'ayuda', 'Arrastra para moverte · doble clic o Ctrl + rueda para el zoom');
    const bAncho = boton('Ajustar al ancho', 'Que el diagrama ocupe el ancho de la lámina');
    const bTodo = boton('Ver todo', 'Que quepa entero, a lo ancho y a lo alto');
    const bMenos = boton('−', 'Alejar');
    const bZoom = Object.assign(boton('100 %', 'Tamaño real'), { className: 'zoom' });
    const bMas = boton('+', 'Acercar');
    const bPantalla = boton('Pantalla completa', 'Ver el diagrama en toda la pantalla');
    barra.append(bAncho, bTodo, bMenos, bZoom, bMas);
    if (marco.requestFullscreen) barra.append(bPantalla);
    barra.append(ayuda);
    marco.append(barra, lamina);

    let escala = 1, modo = 'ancho';   // 'ancho', 'todo' o 'libre' (el usuario eligió un zoom)
    const aplicar = (nueva, ancla) => {
      const anterior = escala;
      escala = Math.min(ESCALA_MAX, Math.max(ESCALA_MIN, nueva));
      // el punto bajo el ancla (el puntero, o el centro) se queda donde estaba
      const ax = ancla?.x ?? lamina.clientWidth / 2, ay = ancla?.y ?? lamina.clientHeight / 2;
      const px = (lamina.scrollLeft + ax - MARGEN) / anterior, py = (lamina.scrollTop + ay - MARGEN) / anterior;
      svg.style.width = `${ancho * escala}px`;
      svg.style.height = `${alto * escala}px`;
      lamina.scrollLeft = px * escala + MARGEN - ax;
      lamina.scrollTop = py * escala + MARGEN - ay;
      bZoom.textContent = `${Math.round(escala * 100)} %`;
    };
    const ajustar = () => {
      // la lámina mide lo que el diagrama, hasta un tope (max-height); «ver todo» cuenta con ese tope
      const tope = document.fullscreenElement === marco ? lamina.clientHeight : parseFloat(getComputedStyle(lamina).maxHeight);
      const w = lamina.clientWidth - 2 * MARGEN, h = tope - 2 * MARGEN;
      if (w <= 0) return;   // la pestaña aún no está visible: se ajusta cuando aparezca
      const objetivo = modo === 'todo' ? Math.min(w / ancho, h / alto) : w / ancho;
      aplicar(Math.min(1, objetivo));
      lamina.scrollLeft = lamina.scrollTop = 0;
    };
    const zoom = (factor, ancla) => { modo = 'libre'; aplicar(escala * factor, ancla); };

    bAncho.addEventListener('click', () => { modo = 'ancho'; ajustar(); });
    bTodo.addEventListener('click', () => { modo = 'todo'; ajustar(); });
    bMenos.addEventListener('click', () => zoom(1 / 1.25));
    bMas.addEventListener('click', () => zoom(1.25));
    bZoom.addEventListener('click', () => { modo = 'libre'; aplicar(1); });
    bPantalla.addEventListener('click', () => (document.fullscreenElement ? document.exitFullscreen() : marco.requestFullscreen()));
    marco.addEventListener('fullscreenchange', () => {
      bPantalla.textContent = document.fullscreenElement === marco ? 'Salir de pantalla completa' : 'Pantalla completa';
    });
    lamina.addEventListener('keydown', (e) => {
      const accion = { '+': 1.25, '=': 1.25, '-': 1 / 1.25 }[e.key];
      if (accion) { e.preventDefault(); zoom(accion); }
      if (e.key === '0') { e.preventDefault(); modo = 'ancho'; ajustar(); }
    });
    lamina.addEventListener('wheel', (e) => {
      if (!e.ctrlKey) return;   // sin Ctrl, la rueda desplaza como siempre
      e.preventDefault();
      const caja = lamina.getBoundingClientRect();
      zoom(Math.exp(-e.deltaY * 0.0015), { x: e.clientX - caja.left, y: e.clientY - caja.top });
    }, { passive: false });

    // arrastrar para desplazarse (ratón, lápiz o dedo). Un arrastre corto cuenta como clic (para el resaltado)
    let arrastre = null, movido = false;
    lamina.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      arrastre = { x: e.clientX, y: e.clientY, izquierda: lamina.scrollLeft, arriba: lamina.scrollTop };
      movido = false;
      lamina.setPointerCapture(e.pointerId);
      lamina.classList.add('arrastrando');
    });
    lamina.addEventListener('pointermove', (e) => {
      if (!arrastre) return;
      if (Math.hypot(e.clientX - arrastre.x, e.clientY - arrastre.y) > 4) movido = true;
      lamina.scrollLeft = arrastre.izquierda - (e.clientX - arrastre.x);
      lamina.scrollTop = arrastre.arriba - (e.clientY - arrastre.y);
    });
    const soltar = () => { arrastre = null; lamina.classList.remove('arrastrando'); };
    lamina.addEventListener('pointerup', (e) => {
      // con la captura del puntero, el destino del evento es la lámina: se busca qué hay bajo el puntero
      if (arrastre && !movido && alClic) alClic(document.elementFromPoint(e.clientX, e.clientY));
      soltar();
    });
    lamina.addEventListener('pointercancel', soltar);
    const interaccion = opciones.resaltar ? prepararResaltado(svg) : null;
    const alClic = interaccion?.alClic;
    if (alClic) {
      lamina.classList.add('interactiva');
      lamina.addEventListener('keydown', (e) => { if (e.key === 'Escape') alClic(null); });
    }

    // doble clic: acerca al doble en ese punto (con Mayús, aleja); así se va de la vista general al detalle
    lamina.addEventListener('dblclick', (e) => {
      const caja = lamina.getBoundingClientRect();
      zoom(e.shiftKey ? 0.5 : 2, { x: e.clientX - caja.left, y: e.clientY - caja.top });
    });

    // se ajusta al aparecer (la pestaña puede estar oculta al dibujar) y al cambiar de tamaño, salvo que el
    // usuario haya elegido un zoom. El observador se guarda en la lámina: sin una referencia, el navegador
    // puede recogerlo como basura mientras la pestaña está oculta y ya no avisa al mostrarse
    lamina.observador = new ResizeObserver(() => { if (modo !== 'libre') ajustar(); });
    lamina.observador.observe(lamina);
    // además, al abrir la pestaña que lo contiene (no depende de que el navegador esté dibujando fotogramas)
    const alAbrir = () => {
      if (!lamina.isConnected) return document.removeEventListener('pestana-abierta', alAbrir);
      if (modo !== 'libre') ajustar();
    };
    document.addEventListener('pestana-abierta', alAbrir);
    destino.replaceChildren(marco);
    aplicar(1);
    ajustar();

    // seguir un recorrido: con un zoom en el que se lean las etiquetas, y moviendo la lámina hasta el estado
    // actual cuando se acerca al borde
    if (opciones.seguir && escala < 0.7) { modo = 'libre'; aplicar(0.7); }
    // desplazamiento instantáneo: con uno suave, el paso siguiente puede llegar mientras la animación sigue en
    // curso y la lámina se queda a medio camino, con el estado actual fuera de la vista
    const suave = 'auto';
    const centrar = (g) => {
      // la parte de la lámina que se ve de verdad: puede quedar cortada por el panel que la contiene o la ventana
      const r = lamina.getBoundingClientRect(), panel = lamina.closest('.panel')?.getBoundingClientRect();
      const vista = {
        left: Math.max(r.left, panel?.left ?? 0, 0), right: Math.min(r.right, panel?.right ?? innerWidth, innerWidth),
        top: Math.max(r.top, panel?.top ?? 0, 0), bottom: Math.min(r.bottom, panel?.bottom ?? innerHeight, innerHeight),
      };
      if (vista.bottom - vista.top < 40 || vista.right - vista.left < 40) return;   // la lámina no está a la vista
      const caja = g.getBoundingClientRect(), borde = 40;
      if (caja.left > vista.left + borde && caja.right < vista.right - borde
          && caja.top > vista.top + borde && caja.bottom < vista.bottom - borde) return;
      const limitar = (x, max) => Math.min(Math.max(0, x), max);
      const left = limitar(lamina.scrollLeft + caja.left + caja.width / 2 - (vista.left + vista.right) / 2, lamina.scrollWidth - lamina.clientWidth);
      const top = limitar(lamina.scrollTop + caja.top + caja.height / 2 - (vista.top + vista.bottom) / 2, lamina.scrollHeight - lamina.clientHeight);
      // dónde quedará el estado; si la lámina llegó a su borde y aun así no se ve, se desplaza el panel lo justo
      const arriba = caja.top - (top - lamina.scrollTop), abajo = arriba + caja.height;
      lamina.scrollTo({ left, top, behavior: suave });
      const falta = abajo > vista.bottom - borde ? abajo - (vista.bottom - borde) : arriba < vista.top + borde ? arriba - (vista.top + borde) : 0;
      if (falta) lamina.closest('.panel')?.scrollBy({ top: falta, behavior: suave });
    };
    return {
      marcar: (hacia, desde) => {
        const g = interaccion?.marcar(hacia, desde);
        if (g && opciones.seguir && !arrastre) centrar(g);   // si el usuario está arrastrando, no se le quita la vista
      },
      reiniciar: () => interaccion?.reiniciar(),
    };
  } catch (e) {
    destino.innerHTML = `<p class="vacio">No se pudo dibujar el diagrama: ${html(e.message)}</p>`;
    return null;
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
    estado.textoCompilado = texto;
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
    ${a.dot && a.dot_detallado ? `<div class="controles"><label>Retornos <select id="modo-retorno">
      <option value="dot">agrupados en ↩ según la cima de la pila</option>
      <option value="dot_detallado">hacia cada estado al que pueden volver</option></select></label></div>` : ''}
    <div id="diagrama">${a.dot ? '<p class="vacio">Dibujando…</p>'
      : `<p class="vacio">Con ${a.tamano.estados} estados el diagrama no se lee; la tabla de transiciones está debajo.</p>`}</div>
    ${a.dot ? `<div class="leyenda-diagrama" aria-label="Leyenda del diagrama">
      <span><i class="l-entrada">▷</i><i class="l-estado l-inicial"></i>estado inicial q₀</span>
      <span><i class="l-estado l-final"></i>estado de aceptación</span>
      <span><i class="l-linea"></i>lee un símbolo y avanza</span>
      <span><i class="l-linea l-violacion"></i>solo suma violaciones (w &gt; 0); el detalle, al pasar el puntero</span>
      <span><i class="l-linea l-retorno"></i>retorno: vuelve según la cima de la pila</span>
      <span><i class="l-rombo">↩</i>punto de retorno (tabla RETORNO)</span>
      <span class="ayuda">Clic en un estado para resaltar sus transiciones; clic en el fondo o Esc para quitarlo.</span>
    </div>` : ''}
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
  if (a.dot) dibujar(a.dot, $('diagrama'), { resaltar: true });
  $('modo-retorno')?.addEventListener('change', (e) => dibujar(a[e.target.value], $('diagrama'), { resaltar: true }));
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
  $('recorrido').hidden = true;
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

const decodificar = (trozo) => new TextDecoder().decode(trozo);

/**
 * La cinta de entrada. Sin token, la cabeza está en el byte «posicion»; con un token [inicio, fin) se resalta
 * entero (es lo que lee la transición que se está mostrando) y lo anterior queda como leído.
 */
function pintarCinta(bytes, posicion, token = null) {
  const inicio = token ? token.inicio : posicion;
  const fin = token ? Math.max(token.fin, token.inicio) : posicion;
  const desde = Math.max(0, inicio - 160);
  let marca, resto;
  if (token && fin > inicio) {
    marca = `<span class="token">${html(decodificar(bytes.subarray(inicio, fin)))}</span>`;
    resto = fin < bytes.length ? decodificar(bytes.subarray(fin, Math.min(bytes.length, fin + 260))) : '';
  } else {
    const cabeza = inicio < bytes.length ? decodificar(bytes.subarray(inicio, Math.min(bytes.length, inicio + 4))).slice(0, 1) : '';
    marca = `<span class="${token ? 'token' : 'cabeza'}">${html(cabeza || '⊣')}</span>`;
    resto = inicio < bytes.length ? decodificar(bytes.subarray(inicio, Math.min(bytes.length, inicio + 260))).slice(cabeza.length) : '';
  }
  $('cinta').innerHTML = `${desde ? '…' : ''}<span class="leido">${html(decodificar(bytes.subarray(desde, inicio)))}</span>` +
    `${marca}${html(resto)}${fin + 260 < bytes.length ? '…' : ''}`;
}

/** La pila, con la cima arriba. tramos: el nombre de la propiedad o elemento de cada marco, si se conoce. */
function pintarPila(simbolos, tramos = []) {
  const marcos = simbolos.map((simbolo, i) => ({ simbolo, tramo: i === 0 ? '' : tramos[i - 1] ?? '' })).reverse();
  const visibles = marcos.slice(0, 11);
  $('pila').innerHTML = visibles.map((m) => `<li class="${m.simbolo === 'Z0' ? 'fondo' : ''}">
      <span class="simbolo">${html(m.simbolo)}</span><span class="tramo">${html(m.tramo)}</span></li>`).join('') +
    (marcos.length > visibles.length ? `<li class="mas">… ${marcos.length - visibles.length} más</li>` : '');
}

function pintarContadores(p, bytes) {
  $('contadores').innerHTML = [
    ['Leído', `${numero(p.bytes)} de ${numero(bytes.length)} bytes`], ['Estado', p.estado_del_automata],
    ['Violaciones', numero(p.violaciones)], ['Profundidad', `${p.profundidad} (máxima ${p.profundidad_maxima})`],
  ].map(([k, v]) => `<div><dt>${k}</dt><dd>${html(v)}</dd></div>`).join('');
}

function pintarMaquina(p, bytes) {
  pintarCinta(bytes, p.bytes);
  pintarContadores(p, bytes);
  pintarPila(p.pila, p.ruta.split('/').slice(1));
}

/**
 * Diagrama de la pestaña Validación que sigue el recorrido del autómata. Se dibuja con el esquema compilado
 * (si el texto cambió desde la última compilación, se compila antes) y se reutiliza mientras no cambie.
 * Devuelve su control ({marcar, reiniciar}) o null si el autómata es demasiado grande para dibujarlo.
 */
async function prepararRecorrido() {
  if (estado.textoCompilado !== $('esquema').value) await compilar();
  const dot = estado.compilado?.automata?.dot;
  $('recorrido').hidden = !dot;
  if (!dot) return null;
  abrir('validacion');   // visible antes de dibujar, para que la lámina tenga medidas
  if (estado.recorrido?.dot !== dot) {
    estado.recorrido = { dot, control: await dibujar(dot, $('diagrama-recorrido'), { resaltar: true, seguir: true }) };
  }
  estado.recorrido.control?.reiniciar();
  return estado.recorrido.control;
}

// ---- ritmo del recorrido: por bloques (rápido) o una transición cada vez, con pausa o paso a paso ----
const ritmo = {
  pausado: false,
  saltar: false,       // se pulsó Siguiente: la transición que espera sale ya
  cancelado: false,
  despertar: null,     // hace que la espera en curso vuelva a mirar las condiciones
  pasos: 0,            // transiciones mostradas en esta validación
  lento: () => $('ritmo').value !== 'bloque',
  manual: () => $('ritmo').value === 'manual',
  avisar() { this.despertar?.(); },
};

/**
 * Espera el turno de la siguiente transición. Sale cuando pasa el tiempo elegido (contado desde que se mostró
 * la transición actual, así una pausa no lo reinicia), con Siguiente, al pasar a «por bloques» o al detener.
 * Cada botón solo despierta la espera, que vuelve a mirar el estado: no hay temporizadores sueltos.
 */
async function esperarTurno() {
  const inicio = performance.now();
  ritmo.saltar = false;
  for (;;) {
    if (ritmo.cancelado || !ritmo.lento() || ritmo.saltar) break;
    const automatico = !ritmo.manual() && !ritmo.pausado;
    const resta = Number($('ritmo').value) - (performance.now() - inicio);
    if (automatico && resta <= 0) break;
    let reloj = null;
    await new Promise((despertar) => {
      ritmo.despertar = despertar;
      if (automatico) reloj = setTimeout(despertar, resta);
    });
    clearTimeout(reloj);
    ritmo.despertar = null;
  }
  ritmo.saltar = false;
}

function actualizarBotonesDePaso() {
  const enCurso = !!cancelarStreaming;
  $('pausar').hidden = !enCurso || ritmo.manual() || !ritmo.lento();
  $('pausar').textContent = ritmo.pausado ? 'Continuar' : 'Pausar';
  $('siguiente').hidden = !enCurso || !ritmo.lento();
  $('siguiente').disabled = !(ritmo.manual() || ritmo.pausado);
}

const siguientePaso = () => { ritmo.saltar = true; ritmo.avisar(); };
$('pausar').addEventListener('click', () => {
  ritmo.pausado = !ritmo.pausado;
  actualizarBotonesDePaso();
  ritmo.avisar();
});
$('siguiente').addEventListener('click', siguientePaso);
$('ritmo').addEventListener('change', () => {
  // cambiar el ritmo a mitad de una validación vale enseguida (por ejemplo, «por bloques» para terminar rápido)
  if (!ritmo.manual()) ritmo.pausado = false;
  actualizarBotonesDePaso();
  ritmo.avisar();
});
document.addEventListener('keydown', (e) => {
  const escribiendo = ['TEXTAREA', 'INPUT', 'SELECT'].includes(document.activeElement?.tagName);
  if (e.key === 'ArrowRight' && !escribiendo && !$('siguiente').hidden && !$('siguiente').disabled) {
    e.preventDefault();
    siguientePaso();
  }
});

const textoDelToken = (bytes, d) => {
  if (d.token === 'FIN') return '⊣ (fin del documento)';
  const texto = decodificar(bytes.subarray(d.inicio, d.fin)).replace(/\s+/g, ' ');
  return texto.length > 48 ? `${texto.slice(0, 47)}…` : texto;
};

/** Muestra una transición: rótulo, token resaltado en la cinta, pila que deja e historial. */
function pintarPaso(bytes, desde, hacia, d) {
  ritmo.pasos += 1;
  const token = d ? textoDelToken(bytes, d) : '';
  const movimiento = desde === hacia
    ? `se queda en <span class="estado-dpda">${html(hacia)}</span>`
    : `<span class="estado-dpda">${html(desde)}</span><span class="flecha">→</span><span class="estado-dpda">${html(hacia)}</span>`;
  $('paso-actual').innerHTML = `<b>Paso ${numero(ritmo.pasos)}.</b> ` +
    (d ? `Lee <code>${html(token)}</code> <span class="tipo-token">${html(d.token)}${d.token === 'FIN' ? '' : `, bytes ${d.inicio}–${d.fin}`}</span> y ` : '') +
    `${movimiento}${d ? ` · pila: ${d.pila.length - 1 ? html(d.pila.slice(1).join(' · ')) + ' (cima a la derecha)' : 'solo Z0'}` : ''}`;
  $('historial').insertAdjacentHTML('afterbegin', `<li>${numero(ritmo.pasos)}. ${html(desde)} → ${html(hacia)}${d ? `  con ${html(token)}` : ''}</li>`);
  while ($('historial').children.length > 30) $('historial').lastElementChild.remove();
  if (!d) return;
  pintarCinta(bytes, d.inicio, d);
  pintarPila(d.pila);
  const estadoActual = $('contadores').querySelectorAll('dd')[1];
  if (estadoActual) estadoActual.textContent = hacia;
}

/**
 * Lleva el diagrama por las transiciones que trae un mensaje del servidor ([desde, hacia] cada una, y en
 * «detalles» el token y la pila de cada una). Por bloques, si un bloque provocó varias se ven una tras otra
 * repartiendo la pausa (como mucho 150 ms cada una). Con un ritmo lento, cada una espera su turno.
 */
async function recorrer(recorrido, mensaje, pausa, bytes) {
  if (!recorrido) return;
  const pasos = mensaje.pasos;
  if (!pasos) return recorrido.marcar(mensaje.estado_del_automata);   // un servidor sin la lista de pasos
  for (let i = 0; i < pasos.length && !ritmo.cancelado; i++) {
    // con un ritmo lento cada transición espera su turno antes de verse: en paso a paso se empieza viendo el
    // estado inicial, y la primera transición llega con el primer «Siguiente»
    if (ritmo.lento()) await esperarTurno();
    if (ritmo.cancelado) break;
    recorrido.marcar(pasos[i][1], pasos[i][0]);
    if (ritmo.lento()) {
      pintarPaso(bytes, pasos[i][0], pasos[i][1], mensaje.detalles?.[i]);
    } else {
      ritmo.pasos += 1;
      if (i < pasos.length - 1 && pausa) await esperar(Math.min(150, pausa / pasos.length));
    }
  }
}

let cancelarStreaming = null;
async function validarPorBloques() {
  if (cancelarStreaming) return cancelarStreaming();
  Object.assign(ritmo, { pausado: false, saltar: false, cancelado: false, pasos: 0 });
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
  cancelarStreaming = () => { cancelado = true; ritmo.cancelado = true; ritmo.avisar(); };
  $('streaming').textContent = 'Detener';
  actualizarBotonesDePaso();
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
    const recorrido = await prepararRecorrido();
    recorrido?.marcar(null);   // empieza en el estado inicial
    $('paso').hidden = !recorrido;
    $('historial').innerHTML = '';
    $('paso-actual').innerHTML = ritmo.lento()
      ? `En el estado inicial, antes de leer nada. ${ritmo.manual() ? 'Pulsa <b>Siguiente paso</b> (o la flecha derecha).' : ''}`
      : 'Por bloques: elige un recorrido lento o paso a paso para ver cada transición con su token.';
    let ultimo = null;   // el último progreso: al cerrar se repinta con el estado al que llevan los pasos finales
    for (let i = 0; i < bytes.length && !cancelado; i += tamano) {
      canal.ws.send(bytes.subarray(i, i + tamano));
      const p = await canal.siguiente();
      if (p.tipo !== 'progreso') return aviso('aviso-documento', p.mensaje ?? 'El servidor cerró la conexión.', true);
      ultimo = p;
      pintarMaquina(p, bytes);
      await recorrer(recorrido, p, pausa, bytes);
      for (const v of p.nuevas) {
        $('en-vivo').insertAdjacentHTML('beforeend', `<li><span class="donde">${enlaceA('documento', v, `byte ${v.byte}`)}</span>${html(v.mensaje)}</li>`);
      }
      aviso('aviso-documento', `Leyendo… ${numero(p.bytes)} de ${numero(bytes.length)} bytes.`);
      if (p.terminado) break;
      if (pausa && !ritmo.lento()) await esperar(pausa);   // con un ritmo lento manda la pausa de cada transición
    }
    if (cancelado) return aviso('aviso-documento', 'Validación detenida.');
    canal.ws.send(JSON.stringify({ tipo: 'fin' }));
    const final = await canal.siguiente();
    if (final.tipo === 'resultado') {
      await recorrer(recorrido, final, pausa, bytes);   // lo que solo se decide al cerrar: el último token y FIN → ACEPTA
      if (cancelado) return aviso('aviso-documento', 'Validación detenida.');
      // el contador «Estado» también: sin esto seguiría en el estado del último bloque (por ejemplo V1 con «3.8»)
      if (ultimo && final.pasos?.length) pintarMaquina({ ...ultimo, estado_del_automata: final.pasos.at(-1)[1] }, bytes);
      if (recorrido) {
        $('paso-actual').innerHTML = (ritmo.lento() ? $('paso-actual').innerHTML + ' ' : '') +
          `<b>Fin: ${html(ESTADOS[final.estado])}</b> tras ${numero(ritmo.pasos)} transiciones.`;
      }
      pintarVeredicto(final);
      aviso('aviso-documento', `${ESTADOS[final.estado]}: ${textoDeVeredicto(final)}.`, !['valido', 'pendiente'].includes(final.estado));
    }
  } finally {
    canal.ws.close();
    cancelarStreaming = null;
    $('streaming').textContent = 'Validar por bloques';
    actualizarBotonesDePaso();
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
  $('ejemplos').value = '';   // el editor ya no tiene un ejemplo: el selector vuelve a «Cargar un ejemplo…»
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
// Conversión de esquemas, inferencia y conversión de documentos
// =====================================================================
const NOMBRES_DE_FORMATO = { json: 'JSON Schema', xsd: 'XSD', xml: 'XML' };

/** Los documentos de ejemplo: el del editor y los del cuadro de abajo, separados por una línea con ---. */
function documentosDeEjemplo() {
  const extra = $('ejemplos-extra').value.split(/^\s*---\s*$/m).map((d) => d.trim()).filter(Boolean);
  return [$('documento').value, ...extra].filter((d) => d.trim());
}

function pintarConversion(titulo, explicacion, d, usarComo) {
  const avisos = d.avisos.length
    ? `<ul class="lista">${d.avisos.map((a) => `<li><span class="severidad-aviso">aviso</span>
        <span class="donde">${html(a.ruta)}</span>${html(a.mensaje)}</li>`).join('')}</ul>`
    : '<p class="vacio">Sin avisos: la conversión es exacta.</p>';
  $('conversion').innerHTML = `<h3>${html(titulo)}</h3><p class="vacio">${explicacion}</p>
    <div class="controles"><button class="principal" data-usar="${usarComo}">Usar como ${usarComo}</button>
    <button data-copiar>Copiar</button></div>
    <h3>Avisos (${d.avisos.length})</h3>${avisos}
    <h3>Resultado</h3><pre class="formal resultado">${html(d.texto)}</pre>`;
  estado.conversion = d.texto;
}

async function convertirCon(boton, ruta, cuerpo, pintar) {
  $(boton).disabled = true;
  $('conversion').innerHTML = '<p class="vacio">Convirtiendo…</p>';
  try {
    const r = await api(ruta, cuerpo);
    if (!r.ok) {
      const errores = r.datos?.errores ?? [];
      $('conversion').innerHTML = `<p>${html(motivoDe(r))}</p>` +
        (errores.length ? `<ul class="lista">${errores.map((e) => `<li>${html(e)}</li>`).join('')}</ul>` : '');
      return;
    }
    pintar(r.datos);
  } catch (e) {
    $('conversion').innerHTML = `<p>No se pudo contactar con el servidor: ${html(e.message)}</p>`;
  } finally {
    $(boton).disabled = false;
  }
}

$('convertir-esquema').addEventListener('click', () => convertirCon('convertir-esquema', '/api/convertir',
  { esquema: $('esquema').value }, (d) => pintarConversion(
    `${NOMBRES_DE_FORMATO[d.origen]} → ${NOMBRES_DE_FORMATO[d.formato]}`,
    'El esquema convertido está dentro del subconjunto que acepta el compilador: se puede usar y compilar enseguida.',
    d, 'esquema')));

$('inferir').addEventListener('click', () => {
  const documentos = documentosDeEjemplo();
  if (!documentos.length) {
    $('conversion').innerHTML = '<p>Escribe al menos un documento de ejemplo.</p>';
    return;
  }
  convertirCon('inferir', '/api/inferir', { documentos }, (d) => pintarConversion(
    `${NOMBRES_DE_FORMATO[d.formato]} inferido de ${documentos.length} ${documentos.length === 1 ? 'ejemplo' : 'ejemplos'}`,
    d.formato === 'json' ? 'Inferido con la biblioteca GenSON: required son las claves presentes en todos los ejemplos.'
      : 'Cada elemento tiene un tipo anónimo; minOccurs y maxOccurs salen de las veces que aparece en los ejemplos.',
    d, 'esquema'));
});

$('convertir-documento').addEventListener('click', () => convertirCon('convertir-documento', '/api/convertir-documento',
  { esquema: $('esquema').value, documento: $('documento').value }, (d) => pintarConversion(
    `Documento ${d.origen.toUpperCase()} → ${d.formato.toUpperCase()}`,
    'Los tipos de los valores y qué hijos se repiten salen del esquema. Para validar el resultado, usa también el '
      + 'esquema convertido.',
    d, 'documento')));

$('conversion').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b || estado.conversion === undefined) return;
  if (b.dataset.copiar !== undefined) {
    await navigator.clipboard?.writeText(estado.conversion);
    b.textContent = 'Copiado';
    return;
  }
  if (b.dataset.usar === 'esquema') {
    $('esquema').value = estado.conversion;
    $('ejemplos').value = '';
    await compilar();
    abrir('analisis');
  } else if (b.dataset.usar === 'documento') {
    ponerDocumento(codificar(estado.conversion));
    aviso('aviso-documento', 'Documento convertido. Para validarlo, el esquema también debe estar en su formato.');
  }
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
$('categoria-caso').value = 'valido';   // al guardar un caso nuevo, por defecto entre los válidos

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
    // al editar el esquema a mano ya no es el ejemplo: el selector vuelve a «Cargar un ejemplo…». «input» solo llega
    // con cambios del usuario, no cuando el programa asigna el texto al cargar un ejemplo o un caso
    $('esquema').addEventListener('input', () => { $('ejemplos').value = ''; });
    $('ejemplos').value = ejemplos.datos[0].nombre;
    $('ejemplos').dispatchEvent(new Event('change'));
    listarCasos();
  } catch (e) {
    $('servidor').textContent = 'Sin conexión con el servidor.';
  }
}
arrancar();
