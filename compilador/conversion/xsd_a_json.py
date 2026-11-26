"""XSD → JSON Schema (2020-12), con la convención de convencion.py.

    python -m compilador.conversion xsd-a-json esquema.xsd

Se parte del modelo que construye el análisis semántico del XSD (compilador/xsd/modelo.py): los tipos ya
tienen resueltas las referencias, las facetas acumuladas y los tipos predefinidos convertidos en rangos
(xs:byte es un entero entre -128 y 127). Cada pieza se traduce así:

    tipo complejo con nombre        $defs/<nombre> y "$ref"
    atributo                        propiedad "@nombre"; use="required" → required
    modelo de contenido             una propiedad por nombre de hijo, con las veces que puede aparecer:
                                    mínimo ≥ 1 → required; máximo > 1 → arreglo con minItems/maxItems
    choice que aparece una vez      además, dos ramas no pueden estar a la vez (not/anyOf de required)
    contenido simple con atributos  propiedad "$" con el texto
    xs:integer, xs:decimal...       "integer", "number", "boolean", "string"
    facetas                         minimum, exclusiveMinimum, minLength, enum, pattern (traducido)...

Lo que se pierde (y se avisa): el orden de los hijos y, en los modelos con repeticiones, qué combinación
exacta de cantidades es válida; el esquema de JSON queda entonces algo más permisivo que el XSD.
"""
import json
from decimal import Decimal

from ..lexico.lexer import ErrorLexico
from ..semantico.metaesquema import DIALECTO
from ..sintactico.parser import ErrorSintactico
from ..xsd.modelo import analizar_xsd
from . import Aviso, Avisos, Conversion, ErrorDeConversion
from .convencion import ATRIBUTO, TEXTO, es_repetido, ocurrencias, tipo_json
from .patrones import NoTraducible, xsd_a_ecma

MAX_PARES = 64     # pares de nombres excluyentes de un choice que se escriben como restricción


def leer_xsd(fuente: bytes):
    """Análisis del XSD; ErrorDeConversion si no es XML bien formado o tiene errores."""
    try:
        analisis = analizar_xsd(fuente)
    except (ErrorLexico, ErrorSintactico) as e:
        raise ErrorDeConversion(f'el XSD no es XML bien formado: {e}', [str(e)]) from e
    if not analisis.ok:
        raise ErrorDeConversion('el XSD tiene errores', [f'{d.codigo}: {d.mensaje} [{d.ruta}]' for d in analisis.errores])
    return analisis


def numero(valor: Decimal, avisos=None, ruta=''):
    """Decimal → int o float de Python (lo que json.dumps escribe como número)."""
    if valor == valor.to_integral_value():
        return int(valor)
    flotante = float(valor)
    if avisos is not None and Decimal(repr(flotante)) != valor:
        avisos.agregar(ruta, f'el número {valor} se escribe como {flotante!r}: JSON con doble precisión no lo representa exacto')
    return flotante


def _largo_minimo(p):
    """Mínimo de elementos que deja la partícula: 0 si puede no tener ninguno."""
    if p.clase == 'elemento':
        return p.minimo
    medidas = [_largo_minimo(h) for h in p.hijos]
    if p.clase == 'secuencia':
        return p.minimo * sum(medidas)
    return p.minimo * min(medidas, default=0)


def _apariciones(p, cuenta):
    if p.clase == 'elemento':
        cuenta[p.nombre] = cuenta.get(p.nombre, 0) + 1
    for h in p.hijos:
        _apariciones(h, cuenta)
    return cuenta


class _AJson:
    def __init__(self, analisis):
        self.analisis = analisis
        self.avisos = Avisos()
        self.nombres = {}        # id(tipo) → nombre en $defs
        self.defs = {}
        self.sin_orden = []      # tipos con varios hijos distintos: su orden no pasa a JSON
        usos = {}
        for t in [*analisis.elementos.values(), *(e for t in analisis.tipos for e in t.hijos())]:
            usos[id(t)] = usos.get(id(t), 0) + 1
        ocupados = set()
        for t in analisis.tipos:
            if t.nombre:
                self.nombres[id(t)] = t.nombre
                ocupados.add(t.nombre)
        for nombre, t in analisis.elementos.items():
            if id(t) not in self.nombres and t in analisis.tipos and usos.get(id(t), 0) > 1:
                propio = nombre if nombre not in ocupados else nombre + '_elemento'
                self.nombres[id(t)] = propio
                ocupados.add(propio)

    # ---- tipos ----
    def esquema_de(self, tipo, ruta):
        """Esquema del valor de un elemento de ese tipo. ruta: dónde se usa (para los avisos)."""
        nombre = self.nombres.get(id(tipo))
        if nombre is None:
            return self.cuerpo(tipo, ruta)
        if nombre not in self.defs:
            self.defs[nombre] = None          # reservado: un tipo recursivo se encuentra a sí mismo
            self.defs[nombre] = self.cuerpo(tipo, ruta)
        return {'$ref': f'#/$defs/{nombre}'}

    def cuerpo(self, t, ruta):
        if t in self.analisis.tipos:
            ruta = t.ruta
        if t.contenido == 'libre':
            return {}
        if t.contenido == 'simple' and not t.atributos:
            return self.simple(t.simple, ruta)
        propiedades, requeridas = {}, []
        for a in t.atributos.values():
            propiedades[ATRIBUTO + a.nombre] = self.simple(a.simple, f'{ruta}/@{a.nombre}')
            if a.requerido:
                requeridas.append(ATRIBUTO + a.nombre)
        restricciones = []
        if t.contenido == 'simple':
            propiedades[TEXTO] = self.simple(t.simple, ruta)
            requeridas.append(TEXTO)
        elif t.particula is not None:
            cuenta = ocurrencias(t.particula)
            for nombre, (minimo, maximo, tipo) in cuenta.items():
                if maximo == 0:
                    continue
                valor = self.esquema_de(tipo, f'{ruta}/{nombre}')
                if es_repetido(maximo):
                    valor = {'type': 'array', 'items': valor, 'minItems': max(minimo, 1)}
                    if maximo is not None:
                        valor['maxItems'] = maximo
                propiedades[nombre] = valor
                if minimo >= 1:
                    requeridas.append(nombre)
            if len(cuenta) > 1:
                self.sin_orden.append(ruta)
            self._opciones(t, t.particula, True, True, _apariciones(t.particula, {}), restricciones)
        esquema = {'type': 'object'}
        if propiedades:
            esquema['properties'] = propiedades
        if requeridas:
            esquema['required'] = requeridas
        esquema['additionalProperties'] = False
        if restricciones:
            esquema['allOf'] = restricciones
        return esquema

    def _opciones(self, t, p, una_vez, siempre, apariciones, salida):
        """Restricciones de los choice: «a lo sumo una rama» si el choice aparece una vez como mucho y
        sus nombres son solo suyos, y «al menos una rama» si aparece siempre y ninguna rama es vacía."""
        if p.clase == 'elemento':
            return
        una_vez = una_vez and p.maximo == 1
        siempre = siempre and p.minimo >= 1
        if p.clase == 'opcion' and len(p.hijos) > 1:
            ramas = [[n for n, (_, maximo, _) in ocurrencias(h).items() if maximo != 0] for h in p.hijos]
            propios = _apariciones(p, {})
            solo_suyos = all(apariciones[n] == propios[n] for n in propios)
            pares = [[x, y] for i, a in enumerate(ramas) for b in ramas[i + 1:] for x in a for y in b]
            if una_vez and solo_suyos and len({n for r in ramas for n in r}) == sum(len(r) for r in ramas):
                if len(pares) <= MAX_PARES:
                    if pares:
                        salida.append({'not': {'anyOf': [{'required': par} for par in pares]}})
                else:
                    self.avisos.agregar(t.ruta, 'choice con demasiadas ramas: no se exige que solo haya una')
            elif p.maximo is not None:
                self.avisos.agregar(t.ruta, 'choice repetido o con nombres compartidos: se comprueba cuántas veces '
                                            'aparece cada hijo, no qué combinación de ramas forman')
            if siempre and solo_suyos and all(_largo_minimo(h) > 0 for h in p.hijos):
                salida.append({'anyOf': [{'required': [n]} for n in dict.fromkeys(n for r in ramas for n in r)]})
            siempre = False
        elif p.clase == 'secuencia' and p.maximo not in (0, 1) and len(ocurrencias(p)) > 1:
            self.avisos.agregar(t.ruta, 'secuencia repetida: se comprueba cuántas veces aparece cada hijo, no que '
                                        'formen grupos completos')
        for h in p.hijos:
            self._opciones(t, h, una_vez, siempre, apariciones, salida)

    def simple(self, s, ruta):
        esquema = {'type': tipo_json(s.base)}
        minimos, maximos, patrones = [], [], []
        largo_min, largo_max, enumerados = None, None, None
        for prueba in s.pruebas:
            clase = prueba[0]
            if clase == 'min':
                minimos.append((prueba[1], prueba[2]))
            elif clase == 'max':
                maximos.append((prueba[1], prueba[2]))
            elif clase == 'long_min':
                largo_min = max(largo_min or 0, prueba[1])
            elif clase == 'long_max':
                largo_max = prueba[1] if largo_max is None else min(largo_max, prueba[1])
            elif clase in ('enum_num', 'enum_cad'):
                valores = set(prueba[1])
                enumerados = valores if enumerados is None else enumerados & valores
            elif clase == 'patron':
                _, textos, _ = prueba
                if s.base != 'string':
                    self.avisos.agregar(ruta, f'pattern sobre xs:{s.base} se omite: en JSON el valor es un número '
                                              'o un booleano y pattern solo se aplica a cadenas')
                    continue
                try:
                    patron, aproximado = xsd_a_ecma(textos)
                except NoTraducible as e:
                    self.avisos.agregar(ruta, f'pattern {" | ".join(textos)!r} no se pudo traducir ({e}): se omite')
                    continue
                if aproximado:
                    self.avisos.agregar(ruta, '\\d de XSD admite dígitos de cualquier alfabeto; se tradujo como [0-9]')
                patrones.append(patron)
        for _ in s.patrones:
            self.avisos.agregar(ruta, 'pattern no regular (en verificación diferida en el XSD): se omite')
        if minimos:
            valor, exclusivo = max(minimos, key=lambda m: (m[0], m[1]))
            esquema['exclusiveMinimum' if exclusivo else 'minimum'] = numero(valor, self.avisos, ruta)
        if maximos:
            valor, exclusivo = min(maximos, key=lambda m: (m[0], not m[1]))
            esquema['exclusiveMaximum' if exclusivo else 'maximum'] = numero(valor, self.avisos, ruta)
        if largo_min:
            esquema['minLength'] = largo_min
        if largo_max is not None:
            esquema['maxLength'] = largo_max
        if enumerados is not None:
            esquema['enum'] = sorted(numero(v, self.avisos, ruta) if isinstance(v, Decimal) else v for v in enumerados)
        if len(patrones) == 1:
            esquema['pattern'] = patrones[0]
        elif patrones:
            esquema['allOf'] = [{'pattern': p} for p in patrones]
        return esquema

    # ---- documento ----
    def convertir(self):
        raices = self.analisis.elementos
        esquema = {'$schema': DIALECTO}
        if len(raices) == 1:
            (nombre, tipo), = raices.items()
            esquema['title'] = nombre
            esquema.update(self.esquema_de(tipo, f'/{nombre}'))
        else:
            self.avisos.agregar('/schema', f'el XSD declara {len(raices)} elementos globales: el documento JSON es un '
                                           'objeto con una sola propiedad, el nombre de su raíz')
            esquema.update({'type': 'object', 'properties': {n: self.esquema_de(t, f'/{n}') for n, t in raices.items()},
                            'minProperties': 1, 'maxProperties': 1, 'additionalProperties': False})
        if self.defs:
            esquema['$defs'] = self.defs
        if self.sin_orden:
            self.avisos.insert(0, Aviso('/schema', 'el orden de los elementos hijos no pasa a JSON (un objeto no tiene '
                                                   f'orden); afecta a {len(self.sin_orden)} tipo(s): '
                                                   + ', '.join(self.sin_orden)))
        return esquema


def xsd_a_json(fuente: bytes) -> Conversion:
    """Convierte un XSD (del subconjunto soportado) en un JSON Schema equivalente según la convención."""
    conversion = _AJson(leer_xsd(fuente))
    esquema = conversion.convertir()
    return Conversion(json.dumps(esquema, indent=2, ensure_ascii=False), 'json', list(conversion.avisos))
