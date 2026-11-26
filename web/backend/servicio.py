"""Operaciones de la aplicación web sobre el compilador (Fase 7).

Entran texto y bytes; salen diccionarios listos para convertir a JSON. No sabe nada de HTTP: lo usan
por igual las rutas REST, los WebSocket y las pruebas.

Todo lo que llega del navegador es dato, nunca código: el esquema se compila a tablas y el documento se
valida con el autómata. Los tamaños están acotados para que una petición no pueda ocupar el servidor.
"""
import base64
import io
import json
import tempfile
import zipfile
from collections import Counter
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from compilador import formatos
from compilador.conversion import ErrorDeConversion
from compilador.conversion.documentos import a_texto_json, json_a_xml, xml_a_json
from compilador.conversion.inferir_json import inferir_json
from compilador.conversion.inferir_xsd import inferir_xsd
from compilador.conversion.json_a_xsd import json_a_xsd
from compilador.conversion.xsd_a_json import leer_xsd, xsd_a_json
from compilador.automata.compilar import Compilado, ErrorDeEsquema, a_dot, describir, filas_delta
from compilador.automata.interprete import Resultado
from compilador.generador.rust import generar_crate
from compilador.generador.tablas import aplanar
from compilador.lexico.lexer import ErrorLexico
from compilador.semantico.clasificacion import NIVELES
from compilador.sintactico import arbol as derivacion
from compilador.sintactico.parser import ErrorSintactico

MAX_ESQUEMA = 256 * 1024            # bytes
MAX_PAYLOAD = 8 * 1024 * 1024
MAX_ESTADOS_DIBUJO = 90             # con más estados el diagrama no se lee: se muestra solo la tabla
MAX_FILAS_DELTA = 800
MAX_TOKENS_ARBOL = 250
MAX_DETALLE = 200

MENSAJES = {
    'no admitido': 'el valor no es de un tipo admitido aquí',
    'required': 'falta una propiedad obligatoria',
    'dependentRequired': 'falta una propiedad que otra exige',
    'minProperties': 'el objeto tiene menos propiedades de las exigidas',
    'maxProperties': 'el objeto tiene más propiedades de las permitidas',
    'minItems': 'el arreglo tiene menos elementos de los exigidos',
    'maxItems': 'el arreglo tiene más elementos de los permitidos',
    'entero': 'el número debe ser entero',
    'min': 'el número es menor que el mínimo',
    'max': 'el número es mayor que el máximo',
    'long_min': 'la cadena es más corta de lo exigido',
    'long_max': 'la cadena es más larga de lo permitido',
    'enum_num': 'el número no es uno de los valores permitidos',
    'enum_cad': 'la cadena no es uno de los valores permitidos',
    # ---- estrategias de respaldo ----
    'patron': 'la cadena no casa con la expresión regular (pattern)',
    'multiplo': 'el número no es múltiplo del exigido (multipleOf)',
    'enum_huella': 'el objeto o arreglo no es uno de los valores permitidos',
    'unicos': 'el arreglo tiene elementos repetidos (uniqueItems)',
    'contiene': 'el número de elementos que cumplen «contains» no está en el rango exigido',
    'anyOf': 'no cumple ninguna de las ramas del anyOf',
    'oneOf': 'no cumple exactamente una de las ramas del oneOf',
    'not': 'cumple el esquema del not',
    'if': 'no cumple el then (o el else) que le toca según el if',
    'dependentSchemas': 'tiene una propiedad cuyo esquema dependiente no cumple',
    'propertyNames': 'el nombre de la propiedad no cumple propertyNames',
    # ---- XML ----
    'xs_entero': 'el valor no es un entero (xs:integer)',
    'xs_decimal': 'el valor no es un número decimal (xs:decimal)',
    'xs_booleano': 'el valor no es un booleano (true, false, 1 o 0)',
    'elemento no permitido': 'el elemento no está permitido aquí; se salta entero',
    'atributo no declarado': 'el esquema no declara este atributo',
    'atributo requerido': 'falta un atributo obligatorio',
    'texto no permitido': 'aquí no puede haber texto',
    'contenido incompleto': 'el contenido del elemento está incompleto o vacío',
}


class EntradaInvalida(Exception):
    """La petición no se puede atender. codigo es el estado HTTP que le corresponde."""

    def __init__(self, mensaje, codigo=422, **datos):
        super().__init__(mensaje)
        self.codigo, self.datos = codigo, dict(datos, mensaje=mensaje)


def posicion(fuente: bytes, byte: int) -> dict:
    """Posición legible de un byte: línea y columna empezando en 1 (la columna cuenta bytes)."""
    byte = max(0, min(byte, len(fuente)))
    return {'byte': byte, 'linea': fuente.count(b'\n', 0, byte) + 1, 'columna': byte - fuente.rfind(b'\n', 0, byte)}


def _a_bytes(texto: str, maximo: int, que: str) -> bytes:
    datos = texto.encode('utf-8')
    if len(datos) > maximo:
        raise EntradaInvalida(f'{que} ocupa {len(datos)} bytes y el máximo es {maximo}', 413)
    return datos


def bytes_del_payload(payload: str | None, payload_b64: str | None) -> bytes:
    """El documento llega como texto o, si puede no ser UTF-8 válido, en base64."""
    if payload_b64 is not None:
        try:
            datos = base64.b64decode(payload_b64, validate=True)
        except ValueError:
            raise EntradaInvalida('payload_b64 no es base64 válido', 400)
    elif payload is not None:
        datos = payload.encode('utf-8')
    else:
        raise EntradaInvalida('falta el documento: envía «payload» o «payload_b64»', 400)
    if len(datos) > MAX_PAYLOAD:
        raise EntradaInvalida(f'el documento ocupa {len(datos)} bytes y el máximo es {MAX_PAYLOAD}', 413)
    return datos


@lru_cache(maxsize=64)
def _compilado(fuente: bytes) -> Compilado:
    return formatos.compilar_esquema(fuente)   # JSON Schema o XSD, según el primer carácter


def _error_de_esquema(e: Exception, fuente: bytes) -> dict:
    """Descripción uniforme de por qué un esquema no compila."""
    if isinstance(e, ErrorLexico):
        return {'ok': False, 'fase': 'léxico', 'mensaje': str(e), **posicion(fuente, e.pos)}
    if isinstance(e, ErrorSintactico):
        return {'ok': False, 'fase': 'sintáctico', 'mensaje': str(e), 'esperados': e.esperados[:12],
                **posicion(fuente, e.pos)}
    if isinstance(e, ErrorDeEsquema):
        return {'ok': False, 'fase': 'semántico', 'mensaje': f'el esquema tiene {len(e.analisis.errores)} errores',
                'diagnosticos': _diagnosticos(e.analisis, fuente)}
    return {'ok': False, 'fase': 'construcción', 'mensaje': str(e)}


def compilado_de(texto: str) -> Compilado:
    """El esquema compilado. Lanza EntradaInvalida, con el detalle del error, si no compila."""
    fuente = _a_bytes(texto, MAX_ESQUEMA, 'el esquema')
    try:
        return _compilado(fuente)
    except Exception as e:  # los errores del esquema son un resultado esperado, no un fallo del servidor
        raise EntradaInvalida('el esquema no compila', 422, esquema=_error_de_esquema(e, fuente))


def _diagnosticos(analisis, fuente):
    return [{'severidad': d.severidad, 'codigo': d.codigo, 'mensaje': d.mensaje, 'ruta': d.ruta,
             'fin': d.fin, **posicion(fuente, d.inicio)} for d in analisis.diagnosticos]


def compilar_esquema(texto: str) -> dict:
    """Todas las fases sobre un esquema: diagnósticos, tabla de símbolos, clasificación y autómata.
    Si el esquema no compila devuelve {'ok': False, 'fase', ...} en lugar de lanzar."""
    fuente = _a_bytes(texto, MAX_ESQUEMA, 'el esquema')
    try:
        c = _compilado(fuente)
    except Exception as e:
        return _error_de_esquema(e, fuente)
    an, a = c.analisis, c.automata
    if c.xml:
        descripcion = lambda t: t.descripcion() + ''.join(f'; @{n}{"" if x.requerido else "?"}' for n, x in t.atributos.items())
        simbolos = [{'ruta': f'elemento {n}', 'recursivo': id(t) in an.recursivos, **posicion(fuente, t.inicio),
                     'tipos': [t.nombre or 'tipo anónimo']} for n, t in an.elementos.items()]
        simbolos += [{'ruta': t.nombre or t.ruta, 'recursivo': id(t) in an.recursivos, **posicion(fuente, t.inicio),
                      'tipos': [descripcion(t)]} for t in an.tipos]
        referencias = [{'origen': origen, 'destino': destino, 'recursiva': recursiva} for origen, destino, recursiva in an.referencias]
        grupos = an.grupos
    else:
        simbolos = [{'ruta': ruta, 'recursivo': an.es_recursivo(e), **posicion(fuente, e.inicio),
                     'tipos': ('true' if e.booleano else 'false') if e.booleano is not None else sorted(an.tipos[id(e)])}
                    for ruta, e in an.tabla.simbolos.items()]
        referencias = [{'origen': an.tabla.ruta(e), 'destino': an.tabla.ruta(an.tabla.referencias[id(e)]),
                        'recursiva': an.es_llamada_recursiva(e)}
                       for e in an.tabla.esquemas if id(e) in an.tabla.referencias]
        grupos = len(an.grupos)
    usos = Counter((u.palabra, u.categoria, u.estrategia) for u in an.usos)
    orden = ('a', 'b', 'c', 'anotacion', 'desconocida', 'no-soportada')
    tablas = aplanar(a)
    return {
        'ok': True,
        'formato': 'xml' if c.xml else 'json',
        'diagnosticos': _diagnosticos(an, fuente),
        'nivel': an.nivel,
        'nivel_texto': NIVELES[an.nivel],
        'clasificacion': [{'palabra': p, 'categoria': c_, 'estrategia': e, 'usos': n}
                          for (p, c_, e), n in sorted(usos.items(), key=lambda x: (orden.index(x[0][1]), x[0][0]))],
        'simbolos': simbolos,
        'referencias': referencias,
        'grupos_recursivos': grupos,
        'automata': {
            'tamano': c.tamano, 'sin_minimizar': c.sin_minimizar,
            'diferidas': [{'ruta': ruta, 'palabra': palabra} for ruta, palabra in a.diferidas],
            'descripcion': describir(c, con_delta=False),
            'dot': a_dot(c) if len(a.estados) <= MAX_ESTADOS_DIBUJO else None,
            'dot_detallado': a_dot(c, agrupar_retornos=False) if len(a.estados) <= MAX_ESTADOS_DIBUJO else None,
            'delta': filas_delta(c, MAX_FILAS_DELTA),
            'delta_truncada': c.tamano['transiciones'] > MAX_FILAS_DELTA,
            'bytes_de_tablas': tablas.bytes_de_tabla(), 'palabras_de_mascara': tablas.palabras,
        },
    }


def resultado_a_dict(r: Resultado, datos: bytes) -> dict:
    return {
        'estado': r.estado, 'violaciones': r.violaciones, 'pendientes': r.pendientes, 'profundidad': r.profundidad,
        'ruta': r.ruta,
        'error': None if r.error is None else {**posicion(datos, r.error[0]), 'motivo': r.error[1]},
        'detalle': [{**posicion(datos, pos), 'motivo': motivo, 'mensaje': MENSAJES.get(motivo, motivo)}
                    for pos, motivo in r.detalle[:MAX_DETALLE]],
        'detalle_truncado': len(r.detalle) > MAX_DETALLE,
    }


def opciones_de_validacion(fallo_temprano=False, max_profundidad=128, max_bytes=None) -> dict:
    if not 1 <= max_profundidad <= 4096:
        raise EntradaInvalida('la profundidad máxima debe estar entre 1 y 4096', 400)
    return {'fallo_temprano': bool(fallo_temprano), 'max_profundidad': max_profundidad, 'max_bytes': max_bytes}


def validar_payload(texto_esquema: str, datos: bytes, **opciones) -> dict:
    """Valida un documento completo con el autómata del esquema."""
    c = compilado_de(texto_esquema)
    return resultado_a_dict(formatos.validar_documento(c, datos, **opciones_de_validacion(**opciones)), datos)


def validador_en_streaming(texto_esquema: str, **opciones):
    """Un validador al que se le van entregando bloques (lo usa el WebSocket)."""
    return formatos.validador_de(compilado_de(texto_esquema), **opciones_de_validacion(**opciones))


def arbol_de_derivacion(texto: str, es_esquema=False) -> dict:
    """Árbol de derivación de un documento (JSON o XML) o de un esquema, con su dibujo en DOT.
    Un XSD es un documento XML: se deriva con la gramática de XML."""
    datos = _a_bytes(texto, MAX_ESQUEMA, 'el texto')
    try:
        if formatos.es_xml(datos):
            raiz = derivacion.derivar(datos, xml=True)
        else:
            raiz = derivacion.derivar(datos, esquema=es_esquema, max_anidamiento=64)
    except (ErrorLexico, ErrorSintactico) as e:
        fase = 'léxico' if isinstance(e, ErrorLexico) else 'sintáctico'
        raise EntradaInvalida('el texto no se puede derivar', 422, fase=fase, detalle=str(e), **posicion(datos, e.pos))
    hojas = len(derivacion.hojas(raiz))
    if hojas > MAX_TOKENS_ARBOL:
        raise EntradaInvalida(f'el texto tiene {hojas} tokens; el árbol solo se dibuja hasta {MAX_TOKENS_ARBOL}', 413)
    return {'tokens': hojas, 'arbol': derivacion.a_dict(raiz, datos), 'dot': derivacion.a_dot(raiz, datos),
            'texto': derivacion.a_texto(raiz, datos)}


def caso_a_dict(caso) -> dict:
    try:
        texto = caso.payload.decode('utf-8')
    except UnicodeDecodeError:
        texto = None  # no es texto válido: solo se puede transportar en base64
    return {'clase': caso.clase, 'estado': caso.estado, 'violaciones': caso.violaciones, 'descripcion': caso.descripcion,
            'payload': texto, 'payload_b64': base64.b64encode(caso.payload).decode('ascii')}


def casos_generados(texto_esquema: str, validos=5, invalidos=5, malformados=3, semilla=0) -> list:
    """Documentos de prueba generados desde el esquema, con el resultado que da el intérprete."""
    if not all(0 <= n <= 50 for n in (validos, invalidos, malformados)):
        raise EntradaInvalida('se pueden pedir entre 0 y 50 casos de cada clase', 400)
    return [caso_a_dict(c) for c in formatos.generar_casos(compilado_de(texto_esquema), validos, invalidos, malformados, semilla)]


def crate_en_zip(texto_esquema: str, nombre='validador_generado') -> bytes:
    """El validador nativo del esquema como archivo .zip: crate de Rust autocontenido, con sus pruebas."""
    if not (nombre.isidentifier() and nombre.isascii() and nombre.islower()):
        raise EntradaInvalida('el nombre del crate debe ser un identificador en minúsculas, sin tildes', 400)
    c = compilado_de(texto_esquema)
    with tempfile.TemporaryDirectory(prefix='crate-') as carpeta:
        raiz = generar_crate(c, Path(carpeta) / nombre, nombre, casos=formatos.generar_casos(c, 6, 6, 3), autocontenido=True,
                             origen='un esquema enviado a la aplicación web')
        memoria = io.BytesIO()
        with zipfile.ZipFile(memoria, 'w', zipfile.ZIP_DEFLATED) as z:
            for archivo in sorted(raiz.rglob('*')):
                if archivo.is_file():
                    z.write(archivo, f'{nombre}/{archivo.relative_to(raiz).as_posix()}')
        return memoria.getvalue()


# ---------------------------------------------------------------------------
# Conversión de esquemas e inferencia (compilador/conversion)
# ---------------------------------------------------------------------------
MAX_EJEMPLOS = 50


def _conversion_a_dict(c, origen: str) -> dict:
    return {'ok': True, 'origen': origen, 'formato': c.formato, 'texto': c.texto,
            'avisos': [{'ruta': a.ruta, 'mensaje': a.mensaje} for a in c.avisos]}


def _error_de_conversion(e):
    return EntradaInvalida(str(e), 422, errores=e.errores)


def inferir_esquema(documentos: list) -> dict:
    """Esquema inferido a partir de documentos de ejemplo, todos JSON o todos XML."""
    if not 1 <= len(documentos) <= MAX_EJEMPLOS:
        raise EntradaInvalida(f'envía entre 1 y {MAX_EJEMPLOS} documentos de ejemplo', 400)
    datos = [_a_bytes(d, MAX_PAYLOAD, f'el ejemplo {i}') for i, d in enumerate(documentos, 1)]
    xml = {formatos.es_xml(d) for d in datos}
    if len(xml) > 1:
        raise EntradaInvalida('los ejemplos deben ser todos JSON o todos XML', 400)
    try:
        if xml == {True}:
            return _conversion_a_dict(inferir_xsd(datos), 'xml')
        return _conversion_a_dict(inferir_json(datos), 'json')
    except ErrorDeConversion as e:
        raise _error_de_conversion(e)


def convertir_esquema(texto: str) -> dict:
    """XSD → JSON Schema o JSON Schema → XSD, según el formato del esquema."""
    fuente = _a_bytes(texto, MAX_ESQUEMA, 'el esquema')
    try:
        if formatos.es_xml(fuente):
            return _conversion_a_dict(xsd_a_json(fuente), 'xsd')
        return _conversion_a_dict(json_a_xsd(fuente), 'json')
    except ErrorDeConversion as e:
        raise _error_de_conversion(e)


def convertir_documento(texto_esquema: str, texto_documento: str) -> dict:
    """Un documento XML a JSON o al revés, con la convención de los convertidores. El esquema dice los tipos
    y qué hijos se repiten; si es un JSON Schema, se usa su conversión a XSD."""
    fuente = _a_bytes(texto_esquema, MAX_ESQUEMA, 'el esquema')
    datos = _a_bytes(texto_documento, MAX_PAYLOAD, 'el documento')
    avisos = []
    try:
        if formatos.es_xml(fuente):
            analisis = leer_xsd(fuente)
        else:
            conversion = json_a_xsd(fuente)
            avisos = [{'ruta': a.ruta, 'mensaje': a.mensaje} for a in conversion.avisos]
            analisis = leer_xsd(conversion.texto.encode('utf-8'))
        if formatos.es_xml(datos):
            return {'ok': True, 'origen': 'xml', 'formato': 'json', 'avisos': avisos,
                    'texto': a_texto_json(xml_a_json(datos, analisis))}
        try:
            valor = json.loads(datos.decode('utf-8-sig'), parse_float=Decimal)
        except (ValueError, UnicodeDecodeError) as e:
            raise EntradaInvalida(f'el documento no es JSON válido: {e}', 422)
        return {'ok': True, 'origen': 'json', 'formato': 'xml', 'avisos': avisos, 'texto': json_a_xml(valor, analisis)}
    except ErrorDeConversion as e:
        raise _error_de_conversion(e)
