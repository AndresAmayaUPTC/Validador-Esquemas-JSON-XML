"""API de la aplicación web (Fase 7): rutas REST, dos WebSocket y los archivos del frontend.

    python -m web.backend              # http://127.0.0.1:8000

REST
  GET    /api/salud                 qué ofrece este servidor
  GET    /api/ejemplos              esquemas y documentos para empezar
  POST   /api/compilar              todas las fases sobre un esquema
  POST   /api/arbol                 árbol de derivación de un documento o de un esquema
  POST   /api/validar               valida un documento completo
  POST   /api/comparar              mide el validador y la línea base sobre un documento
  POST   /api/generar               casos de prueba generados desde el esquema
  POST   /api/crate                 el validador nativo como .zip (crate de Rust)
  POST   /api/wasm                  el validador nativo compilado a WebAssembly
  GET    /api/casos                 casos guardados   (POST crea, PUT reemplaza, DELETE borra)
  POST   /api/casos/{id}/ejecutar   ejecuta un caso y guarda el resultado y las métricas

WebSocket
  /ws/validar    validación en streaming: llegan bloques, salen el progreso y el veredicto
  /ws/comparar   comparación en vivo: una medición por ronda, a medida que termina
"""
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from compilador import formatos

from . import nativo, repositorio as almacenes, sembrar, servicio

FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'
MAX_BLOQUE = 1024 * 1024


class Esquema(BaseModel):
    esquema: str


class Validacion(Esquema):
    payload: str | None = None
    payload_b64: str | None = None
    fallo_temprano: bool = False
    max_profundidad: int = 128
    max_bytes: int | None = Field(default=None, ge=0)


class Arbol(BaseModel):
    texto: str
    es_esquema: bool = False


class Generacion(Esquema):
    validos: int = 5
    invalidos: int = 5
    malformados: int = 3
    semilla: int = 0


class Crate(Esquema):
    nombre: str = 'validador_generado'


class Caso(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    categoria: str
    esquema: str
    payload_b64: str
    notas: str = ''
    esperado: dict | None = None


def crear_app(repositorio=None) -> FastAPI:
    app = FastAPI(title='Validación de esquemas JSON y XML con autómatas de pila ponderados', docs_url='/api/docs',
                  openapi_url='/api/openapi.json')
    repo = repositorio if repositorio is not None else almacenes.abrir()

    @app.exception_handler(servicio.EntradaInvalida)
    async def entrada_invalida(_, e: servicio.EntradaInvalida):
        return JSONResponse(e.datos, status_code=e.codigo)

    @app.exception_handler(nativo.NoDisponible)
    async def no_disponible(_, e: nativo.NoDisponible):
        return JSONResponse({'mensaje': str(e)}, status_code=503)

    # ---------------- compilador ----------------
    @app.get('/api/salud')
    def salud():
        return {'ok': True, 'almacen': repo.nombre, 'casos': len(repo.listar()), **nativo.disponibilidad()}

    @app.get('/api/ejemplos')
    def ejemplos():
        de_json = [{'nombre': nombre, 'lenguaje': 'json', 'esquema': json.dumps(esquema, ensure_ascii=False, indent=2),
                    'payload': json.dumps(payload, ensure_ascii=False, indent=2)}
                   for nombre, (esquema, payload) in sembrar.EJEMPLOS.items()]
        return de_json + [{'nombre': nombre, 'lenguaje': 'xml', 'esquema': esquema, 'payload': documento}
                          for nombre, (esquema, documento) in sembrar.EJEMPLOS_XML.items()]

    @app.post('/api/compilar')
    def compilar(e: Esquema):
        return servicio.compilar_esquema(e.esquema)

    @app.post('/api/arbol')
    def arbol(a: Arbol):
        return servicio.arbol_de_derivacion(a.texto, a.es_esquema)

    @app.post('/api/validar')
    def validar(v: Validacion):
        datos = servicio.bytes_del_payload(v.payload, v.payload_b64)
        return servicio.validar_payload(v.esquema, datos, fallo_temprano=v.fallo_temprano,
                                        max_profundidad=v.max_profundidad, max_bytes=v.max_bytes)

    @app.post('/api/comparar')
    def comparar(v: Validacion):
        return nativo.medir(v.esquema, servicio.bytes_del_payload(v.payload, v.payload_b64))

    @app.post('/api/generar')
    def generar(g: Generacion):
        return servicio.casos_generados(g.esquema, g.validos, g.invalidos, g.malformados, g.semilla)

    @app.post('/api/crate')
    def crate(c: Crate):
        contenido = servicio.crate_en_zip(c.esquema, c.nombre)
        return Response(contenido, media_type='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="{c.nombre}.zip"'})

    @app.post('/api/wasm')
    def wasm(e: Esquema):
        return Response(nativo.wasm_de(e.esquema), media_type='application/wasm')

    # ---------------- casos de prueba ----------------
    def _caso_o_404(id_):
        caso = repo.obtener(id_)
        if caso is None:
            raise HTTPException(404, 'no existe un caso con ese identificador')
        return caso

    def _revisar(caso: Caso):
        if caso.categoria not in almacenes.CATEGORIAS:
            raise servicio.EntradaInvalida(f'la categoría debe ser una de: {", ".join(almacenes.CATEGORIAS)}', 400)
        servicio.bytes_del_payload(None, caso.payload_b64)
        return caso.model_dump() | {'lenguaje': 'xml' if formatos.es_xml(caso.esquema.encode('utf-8')) else 'json'}

    @app.get('/api/casos')
    def listar_casos(categoria: str | None = None):
        # la lista no lleva los textos completos: se piden al abrir cada caso
        tamano = lambda b64: len(b64) * 3 // 4 - b64[-2:].count('=')
        return [{k: v for k, v in c.items() if k not in ('esquema', 'payload_b64')} | {'bytes': tamano(c['payload_b64'])}
                for c in repo.listar(categoria)]

    @app.get('/api/casos/{id_}')
    def obtener_caso(id_: str):
        return _caso_o_404(id_)

    @app.post('/api/casos', status_code=201)
    def crear_caso(caso: Caso):
        return {'_id': repo.guardar(_revisar(caso))}

    @app.put('/api/casos/{id_}')
    def reemplazar_caso(id_: str, caso: Caso):
        _caso_o_404(id_)
        repo.guardar(_revisar(caso) | {'_id': id_})
        return {'_id': id_}

    @app.delete('/api/casos/{id_}', status_code=204)
    def borrar_caso(id_: str):
        if not repo.borrar(id_):
            raise HTTPException(404, 'no existe un caso con ese identificador')
        return Response(status_code=204)   # sin cuerpo ni tipo de contenido: no hay nada que leer

    @app.post('/api/casos/{id_}/ejecutar')
    def ejecutar_caso(id_: str):
        """Valida el caso, lo mide si el servidor puede, y guarda el resultado junto al caso."""
        caso = _caso_o_404(id_)
        datos = base64.b64decode(caso['payload_b64'])
        r = servicio.validar_payload(caso['esquema'], datos)
        ultimo = {'estado': r['estado'], 'violaciones': r['violaciones'], 'ruta': r['ruta'], 'profundidad': r['profundidad'],
                  'fecha': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        esperado = caso.get('esperado')
        if esperado:
            ultimo['coincide'] = (r['estado'], r['violaciones']) == (esperado.get('estado'), esperado.get('violaciones', r['violaciones']))
        try:
            m = nativo.medir(caso['esquema'], datos, ms=40)
            ultimo['metricas'] = {
                'bytes': m['bytes'], 'mb_s': m['streaming']['mb_s'], 'mb_s_dom': m['dom'].get('mb_s'),
                'asignaciones': m['streaming']['asignaciones'], 'asignaciones_dom': m['dom'].get('asignaciones'),
                'memoria': m['streaming']['memoria_usada'], 'memoria_dom': m['dom'].get('pico_heap'),
                'estados': m['automata']['estados'], 'transiciones': m['automata']['transiciones']}
        except (nativo.NoDisponible, servicio.EntradaInvalida) as e:
            ultimo['metricas'] = {'no_disponible': str(e)}
        repo.guardar(caso | {'ultimo': ultimo})
        return {'resultado': r, 'ultimo': ultimo}

    # ---------------- WebSocket ----------------
    @app.websocket('/ws/validar')
    async def ws_validar(ws: WebSocket):
        """Protocolo: el cliente envía {"tipo": "inicio", "esquema", ...opciones}; después bloques binarios; al
        final {"tipo": "fin"}. El servidor responde "listo", un "progreso" por bloque y el "resultado"."""
        await ws.accept()
        try:
            inicio = await ws.receive_json()
            try:
                validador = servicio.validador_en_streaming(
                    inicio['esquema'], fallo_temprano=inicio.get('fallo_temprano', False),
                    max_profundidad=int(inicio.get('max_profundidad', 128)), max_bytes=inicio.get('max_bytes'))
            except servicio.EntradaInvalida as e:
                await ws.send_json({'tipo': 'error', **e.datos})
                return await ws.close()
            await ws.send_json({'tipo': 'listo'})
            recibido, vistas, total = bytearray(), 0, 0
            # cada transición de la ejecución principal, con los nombres de los estados: un bloque puede provocar
            # varias (un número solo se cierra al llegar el carácter siguiente) y las últimas llegan con «fin»
            validador.recorrido = []
            nombres = validador.a.estados

            def pasos():
                hechos = [[nombres[desde], nombres[hacia]] for desde, hacia in validador.recorrido]
                validador.recorrido.clear()
                return hechos
            while True:
                mensaje = await ws.receive()
                if mensaje['type'] == 'websocket.disconnect':
                    return
                if mensaje.get('bytes') is not None:
                    bloque = mensaje['bytes']
                    total += len(bloque)
                    if len(bloque) > MAX_BLOQUE or total > servicio.MAX_PAYLOAD:
                        await ws.send_json({'tipo': 'error', 'mensaje': 'el documento supera el tamaño máximo'})
                        return await ws.close()
                    if len(recibido) < 65536:          # solo se guarda el principio, para dar línea y columna
                        recibido += bloque
                    validador.feed(bloque)
                    r = validador._r
                    nuevas = [{'byte': p, 'motivo': m, 'mensaje': servicio.MENSAJES.get(m, m)} for p, m in r.detalle[vistas:vistas + 50]]
                    vistas = len(r.detalle)
                    await ws.send_json({'tipo': 'progreso', **validador.progreso(), 'nuevas': nuevas, 'pasos': pasos()})
                elif json.loads(mensaje.get('text') or '{}').get('tipo') == 'fin':
                    r = validador.finish()
                    await ws.send_json({'tipo': 'resultado', **servicio.resultado_a_dict(r, bytes(recibido)), 'pasos': pasos()})
                    return await ws.close()
        except WebSocketDisconnect:
            return

    @app.websocket('/ws/comparar')
    async def ws_comparar(ws: WebSocket):
        """El cliente envía {"esquema", "payload" o "payload_b64", "rondas"}; el servidor, una "medicion" por
        ronda en cuanto termina, y "fin"."""
        await ws.accept()
        try:
            pedido = await ws.receive_json()
            try:
                datos = servicio.bytes_del_payload(pedido.get('payload'), pedido.get('payload_b64'))
                rondas = max(1, min(int(pedido.get('rondas', 8)), 30))
                for ronda in range(1, rondas + 1):
                    m = await run_in_threadpool(nativo.medir, pedido['esquema'], datos, 60)
                    await ws.send_json({'tipo': 'medicion', 'ronda': ronda, 'rondas': rondas, **m})
                await ws.send_json({'tipo': 'fin'})
            except servicio.EntradaInvalida as e:
                await ws.send_json({'tipo': 'error', **e.datos})
            except nativo.NoDisponible as e:
                await ws.send_json({'tipo': 'error', 'mensaje': str(e)})
            await ws.close()
        except WebSocketDisconnect:
            return

    # el frontend se sirve desde la misma dirección: así no hace falta configurar CORS
    if FRONTEND.exists():
        app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')
    return app


app = crear_app()
