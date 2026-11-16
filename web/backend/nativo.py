"""Código nativo al servicio de la aplicación web (Fase 7): medición y compilación a WebAssembly.

Dos decisiones de seguridad:

  Medición. El servidor nunca compila ni ejecuta código que dependa de lo que envía el usuario. El
  programa de medición ya está compilado y recibe el autómata como tablas de datos; lo mismo la línea
  base, que recibe el esquema como JSON.

  WebAssembly. El validador nativo de un esquema se compila a un módulo .wasm y se ejecuta en el
  navegador de quien lo pidió. Un módulo WebAssembly no puede abrir archivos, usar la red ni leer
  memoria ajena: ese es el sandbox. El código que se compila son las tablas numéricas que escribe el
  propio compilador; del esquema no pasa ningún texto al código fuente.

Las dos funciones tienen límite de tiempo y, si faltan las herramientas (cargo, el destino wasm32),
lanzan NoDisponible en lugar de fallar.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from bench import correr as bench
from compilador.generador.rust import generar_crate
from compilador.generador.tablas import aplanar, serializar

from .servicio import EntradaInvalida, compilado_de

PLAZO_DE_MEDICION = 60        # segundos
PLAZO_DE_COMPILACION = 300
PALABRAS_DEL_MEDIDOR = 4      # capacidad con la que está compilado bench (W en bench/src/main.rs)
DESTINO_WASM = bench.DESTINO / 'web'

_cerrojo_wasm = threading.Lock()
_cache_wasm = {}
_ejecutable = None


class NoDisponible(Exception):
    """Falta una herramienta nativa en este servidor."""


def _cargo():
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(Path.home() / '.cargo' / 'bin'))
    if cargo is None:
        raise NoDisponible('cargo no está instalado en el servidor')
    return cargo


def _entorno():
    return dict(os.environ, PATH=str(Path.home() / '.cargo' / 'bin') + os.pathsep + os.environ['PATH'])


def _medidor():
    global _ejecutable
    if _ejecutable is None:
        try:
            _ejecutable = bench.preparar()
        except RuntimeError as e:
            raise NoDisponible(str(e))
    return _ejecutable


def disponibilidad() -> dict:
    """Qué funciones nativas ofrece este servidor. No compila nada: solo mira lo que ya hay."""
    try:
        _cargo()
        cargo = True
    except NoDisponible:
        cargo = False
    medidor = shutil.which('bench', path=str(bench.DESTINO / 'bench' / 'release')) is not None
    wasm = False
    if cargo:
        r = subprocess.run(['rustup', 'target', 'list', '--installed'], env=_entorno(), capture_output=True, text=True)
        wasm = 'wasm32-unknown-unknown' in r.stdout
    return {'cargo': cargo, 'medicion': medidor or cargo, 'wasm': wasm}


def medir(texto_esquema: str, datos: bytes, ms=60, bloque=65536) -> dict:
    """Mide el validador en streaming y la línea base (DOM + validador genérico) sobre un documento."""
    tablas = aplanar(compilado_de(texto_esquema).automata)
    if tablas.palabras > PALABRAS_DEL_MEDIDOR:
        raise EntradaInvalida(f'el medidor admite hasta {64 * PALABRAS_DEL_MEDIDOR} propiedades (o atributos) declaradas', 422)
    ejecutable = _medidor()
    with tempfile.TemporaryDirectory(prefix='medir-') as carpeta:
        base = Path(carpeta)
        (base / 'tablas').write_text(serializar(tablas), encoding='ascii')
        (base / 'esquema').write_text(texto_esquema, encoding='utf-8')
        (base / 'payload').write_bytes(datos)
        # con un XSD la línea base solo construye el árbol del documento: ver bench/src/main.rs
        orden = [ejecutable, str(base / 'tablas'), str(base / 'esquema'), str(base / 'payload'),
                 '--ms', str(int(ms)), '--bloque', str(int(bloque))] + (['--xml'] if tablas.xml else [])
        try:
            r = subprocess.run(orden, capture_output=True, timeout=PLAZO_DE_MEDICION)
        except subprocess.TimeoutExpired:
            raise EntradaInvalida('la medición superó el tiempo máximo', 408)
    if r.returncode != 0:
        raise EntradaInvalida('el programa de medición no pudo procesar el documento', 422,
                              detalle=r.stderr.decode('utf-8', 'replace')[-500:])
    return json.loads(r.stdout.decode('utf-8'))


def wasm_de(texto_esquema: str) -> bytes:
    """El validador del esquema compilado a WebAssembly (misma API en C que la biblioteca nativa)."""
    compilado = compilado_de(texto_esquema)
    clave = hashlib.sha256(texto_esquema.encode('utf-8')).hexdigest()
    if clave in _cache_wasm:
        return _cache_wasm[clave]
    cargo = _cargo()
    with _cerrojo_wasm:  # una compilación cada vez: comparten carpeta de salida y nombre de crate
        with tempfile.TemporaryDirectory(prefix='wasm-') as carpeta:
            generar_crate(compilado, carpeta, 'validador_web', origen='un esquema enviado a la aplicación web')
            entorno = dict(_entorno(), CARGO_TARGET_DIR=str(DESTINO_WASM))
            try:
                r = subprocess.run([cargo, 'build', '--quiet', '--release', '--target', 'wasm32-unknown-unknown'],
                                   cwd=carpeta, env=entorno, capture_output=True, timeout=PLAZO_DE_COMPILACION)
            except subprocess.TimeoutExpired:
                raise EntradaInvalida('la compilación a WebAssembly superó el tiempo máximo', 408)
            if r.returncode != 0:
                error = r.stderr.decode('utf-8', 'replace')
                if 'wasm32-unknown-unknown' in error and 'not installed' in error:
                    raise NoDisponible('falta el destino wasm32: «rustup target add wasm32-unknown-unknown»')
                raise NoDisponible('no se pudo compilar a WebAssembly: ' + error[-400:])
            modulo = (DESTINO_WASM / 'wasm32-unknown-unknown' / 'release' / 'validador_web.wasm').read_bytes()
    if len(_cache_wasm) >= 16:
        _cache_wasm.pop(next(iter(_cache_wasm)))
    _cache_wasm[clave] = modulo
    return modulo
