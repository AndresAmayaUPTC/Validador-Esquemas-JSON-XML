"""JSON Schema → XSD, con la convención de convencion.py.

    python -m compilador.conversion json-a-xsd esquema.json

El documento JSON es el valor del elemento raíz, que se llama como el «title» del esquema (si es un
nombre de XML válido) o «raiz». Cada pieza se traduce así:

    "type": "object"               complexType con una sequence: un elemento por propiedad, en el orden
                                   de «properties»; las que no están en required llevan minOccurs="0"
    propiedad "@nombre"            atributo (use="required" si está en required)
    propiedad "$"                  el texto del elemento (simpleContent) cuando no hay elementos hijos
    propiedad de tipo arreglo      el elemento repetido: minOccurs/maxOccurs salen de minItems/maxItems
    string, integer, number...     xs:string, xs:integer, xs:decimal, xs:boolean
    minimum, maxLength, enum...    facetas de una restricción; pattern se traduce de ECMA-262 a XSD
    $defs + $ref                   tipos con nombre (también los recursivos)

Lo que el subconjunto de XSD no puede expresar se avisa: combinaciones (anyOf, oneOf, not, if), tipos
unión, propiedades adicionales o con patrón, uniqueItems, contains, multipleOf... El XSD resultante queda
más permisivo (si se ignora una restricción) o más estricto (si se rechaza lo no declarado), y el aviso
dice cuál de las dos cosas pasa.
"""
import json
import math
from decimal import Decimal
from urllib.parse import unquote

from ..semantico.analisis import analizar_fuente
from ..lexico.lexer import ErrorLexico
from ..sintactico.parser import ErrorSintactico
from ..xsd.modelo import XS, analizar_xsd
from . import Aviso, Avisos, Conversion, ErrorDeConversion
from .convencion import ATRIBUTO, ELEMENTO_DE_ARREGLO, TEXTO, es_nombre_xml
from .patrones import NoTraducible, ecma_a_xsd

ESCALARES = {'string': 'xs:string', 'integer': 'xs:integer', 'number': 'xs:decimal', 'boolean': 'xs:boolean'}
ANOTACIONES = {'$schema', '$id', '$comment', '$defs', 'definitions', 'title', 'description', 'default', 'examples',
               'deprecated', 'readOnly', 'writeOnly', '$anchor', '$vocabulary'}
_PALABRAS_DE = {
    'object': ('properties', 'required', 'additionalProperties', 'patternProperties', 'propertyNames', 'minProperties',
               'maxProperties', 'dependentRequired', 'dependentSchemas', 'unevaluatedProperties'),
    'array': ('items', 'prefixItems', 'minItems', 'maxItems', 'contains', 'minContains', 'maxContains', 'uniqueItems',
              'unevaluatedItems'),
    'string': ('minLength', 'maxLength', 'pattern', 'format', 'contentEncoding', 'contentMediaType'),
    'number': ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'),
}
# palabras que el XSD no puede expresar: (mensaje, el XSD queda más permisivo o más estricto)
_IGNORADAS = {
    'patternProperties': 'propiedades con patrón', 'propertyNames': 'propertyNames', 'minProperties': 'minProperties',
    'maxProperties': 'maxProperties', 'dependentRequired': 'dependentRequired', 'dependentSchemas': 'dependentSchemas',
    'unevaluatedProperties': 'unevaluatedProperties', 'uniqueItems': 'uniqueItems', 'contains': 'contains',
    'minContains': 'minContains', 'maxContains': 'maxContains', 'unevaluatedItems': 'unevaluatedItems',
    'not': 'not', 'if': 'if/then/else', 'then': None, 'else': None, 'format': 'format',
    'contentEncoding': 'contentEncoding', 'contentMediaType': 'contentMediaType',
}
_MAS_PERMISIVO = 'el XSD queda más permisivo'
MAX_PROFUNDIDAD = 64     # anidamiento de tipos escritos en su sitio (corta los ciclos que no pasan por un nombre)


class NodoXsd:
    """Elemento del XSD que se está escribiendo (xs:nombre)."""

    def __init__(self, nombre, atributos=None, hijos=None):
        self.nombre, self.atributos, self.hijos = nombre, dict(atributos or {}), list(hijos or [])

    def escribir(self, salida, sangria):
        atributos = ''.join(f' {k}="{escapar_atributo(str(v))}"' for k, v in self.atributos.items())
        if not self.hijos:
            salida.append(f'{sangria}<xs:{self.nombre}{atributos}/>')
            return
        salida.append(f'{sangria}<xs:{self.nombre}{atributos}>')
        for h in self.hijos:
            h.escribir(salida, sangria + '  ')
        salida.append(f'{sangria}</xs:{self.nombre}>')


def escapar_atributo(valor: str) -> str:
    """Texto de un valor de atributo de XML. Tabuladores y saltos de línea van como referencias: escritos
    tal cual, la normalización de atributos los convertiría en espacios."""
    salida = []
    for c in valor:
        if c in '&<>"':
            salida.append({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])
        elif ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in '  ':
            salida.append(f'&#x{ord(c):X};')
        else:
            salida.append(c)
    return ''.join(salida)


def decimal_xsd(valor: Decimal) -> str:
    """Un número en la forma léxica de xs:decimal (sin exponente)."""
    texto = format(valor, 'f')
    if '.' in texto:
        texto = texto.rstrip('0').rstrip('.')
    return texto if texto not in ('-0', '') else '0'


def _es_numero(v):
    return isinstance(v, (int, Decimal)) and not isinstance(v, bool)


def _tipo_de_valor(v):
    if isinstance(v, bool):
        return 'boolean'
    if isinstance(v, int):
        return 'integer'
    if isinstance(v, Decimal):
        return 'integer' if v == v.to_integral_value() else 'number'
    if isinstance(v, str):
        return 'string'
    if v is None:
        return 'null'
    return 'array' if isinstance(v, list) else 'object'


class _AXsd:
    def __init__(self, raiz):
        self.raiz = raiz
        self.avisos = Avisos()
        self.globales = []                 # tipos con nombre, en el orden en que se crean
        self.con_nombre = {}               # referencia ('#/$defs/x') → nombre del tipo en el XSD
        self.ocupados = set()
        self.ordenados = []                # objetos con varios elementos: el XSD fija su orden
        self.profundidad = 0

    # ---- referencias ----
    def destino(self, ref, ruta):
        """El subesquema al que apunta un $ref local, o None (con aviso) si no se puede seguir."""
        if ref == '#':
            return self.raiz
        if not ref.startswith('#/'):
            self.avisos.agregar(ruta, f'$ref «{ref}» no apunta dentro del documento: se admite cualquier contenido')
            return None
        actual = self.raiz
        for parte in ref[2:].split('/'):
            parte = unquote(parte).replace('~1', '/').replace('~0', '~')
            if isinstance(actual, dict) and parte in actual:
                actual = actual[parte]
            elif isinstance(actual, list) and parte.isdigit() and int(parte) < len(actual):
                actual = actual[int(parte)]
            else:
                self.avisos.agregar(ruta, f'$ref «{ref}» no se encuentra: se admite cualquier contenido')
                return None
        return actual

    def resolver(self, esquema, ruta):
        """Sigue los $ref (sin ciclos) y combina allOf: el esquema efectivo, un dict o un booleano."""
        vistos = set()
        while isinstance(esquema, dict) and '$ref' in esquema:
            ref = esquema['$ref']
            hermanos = [k for k in esquema if k != '$ref' and k not in ANOTACIONES]
            if hermanos:
                self.avisos.agregar(ruta, f'junto a $ref se ignoran {", ".join(hermanos)}: {_MAS_PERMISIVO}')
            if ref in vistos:
                return True
            vistos.add(ref)
            destino = self.destino(ref, ruta)
            if destino is None:
                return True
            esquema = destino
        if isinstance(esquema, dict) and 'allOf' in esquema:
            esquema = self.fusionar(esquema, ruta)
        return esquema

    def fusionar(self, esquema, ruta):
        """allOf → un solo esquema con la unión de properties y required. Es exacto si las ramas no
        restringen lo mismo; si chocan, gana la primera y se avisa."""
        combinado = {k: v for k, v in esquema.items() if k != 'allOf'}
        for i, rama in enumerate(esquema['allOf']):
            rama = self.resolver(rama, f'{ruta}/allOf/{i}')
            if rama is True:
                continue
            if rama is False:
                self.avisos.agregar(ruta, 'allOf con una rama false: ningún documento cumple el esquema')
                continue
            for k, v in rama.items():
                if k == 'properties':
                    destino = combinado.setdefault('properties', {})
                    for nombre, sub in v.items():
                        if nombre in destino and destino[nombre] != sub:
                            self.avisos.agregar(ruta, f'allOf restringe dos veces «{nombre}»: se usa la primera')
                        destino.setdefault(nombre, sub)
                elif k == 'required':
                    combinado['required'] = list(dict.fromkeys([*combinado.get('required', []), *v]))
                elif k not in combinado:
                    combinado[k] = v
                elif combinado[k] != v and k not in ANOTACIONES:
                    self.avisos.agregar(ruta, f'allOf da dos valores a «{k}»: se usa el primero')
        return combinado

    # ---- tipos ----
    def tipos(self, esquema, ruta):
        """Tipos de JSON que admite el esquema efectivo (None = cualquiera) y si admite null aparte."""
        if esquema is True:
            return None, False
        if esquema is False:
            return set(), False
        anulable = False
        for clave in ('anyOf', 'oneOf'):
            if clave in esquema:
                ramas = [self.resolver(r, f'{ruta}/{clave}/{i}') for i, r in enumerate(esquema[clave])]
                nulas = [r for r in ramas if isinstance(r, dict) and r.get('type') == 'null' and len(r) == 1]
                if nulas and len(ramas) - len(nulas) == 1:
                    anulable = True
                else:
                    self.avisos.agregar(ruta, f'{clave} no tiene equivalente en el subconjunto de XSD: se admite '
                                              f'cualquier contenido ({_MAS_PERMISIVO})')
                    return None, False
        tipo = esquema.get('type')
        if isinstance(tipo, str):
            tipos = {tipo}
        elif isinstance(tipo, list):
            tipos = set(tipo)
        elif 'enum' in esquema or 'const' in esquema:
            tipos = {_tipo_de_valor(v) for v in (esquema['enum'] if 'enum' in esquema else [esquema['const']])}
        else:
            tipos = {t for t, palabras in _PALABRAS_DE.items() if any(p in esquema for p in palabras)} or None
        if tipos is not None:
            if 'null' in tipos and len(tipos) > 1:
                tipos, anulable = tipos - {'null'}, True
            if 'integer' in tipos and 'number' in tipos:
                tipos.discard('integer')
        return tipos, anulable

    def rama_de(self, esquema, ruta, resolver=True):
        """De un anyOf/oneOf anulable (X o null), la rama X. Si la rama es solo un $ref y no hay nada más,
        con resolver=False se devuelve tal cual: así un tipo recursivo se usa por su nombre."""
        if not isinstance(esquema, dict):
            return esquema
        for clave in ('anyOf', 'oneOf'):
            if clave in esquema:
                for i, r in enumerate(esquema[clave]):
                    r2 = self.resolver(r, f'{ruta}/{clave}/{i}')
                    if not (isinstance(r2, dict) and r2.get('type') == 'null' and len(r2) == 1):
                        resto = {k: v for k, v in esquema.items() if k != clave}
                        if not resolver and not set(resto) - ANOTACIONES:
                            return r
                        r2 = r2 if isinstance(r2, dict) else {}
                        return self.resolver({**resto, **{k: v for k, v in r2.items() if k not in resto}}, ruta)
        return esquema

    def avisar_ignoradas(self, esquema, ruta):
        for clave, que in _IGNORADAS.items():
            if que and clave in esquema:
                if clave == 'format':
                    self.avisos.agregar(ruta, f'format «{esquema[clave]}» no se comprueba en XSD (es una anotación)')
                else:
                    self.avisos.agregar(ruta, f'{que} no tiene equivalente en el subconjunto de XSD: se ignora '
                                              f'({_MAS_PERMISIVO})')

    def valor(self, esquema, ruta):
        """Tipo del elemento cuyo valor describe el esquema: un nombre de tipo ('xs:string', 'Nodo') o un
        nodo complexType/simpleType anónimo."""
        if self.profundidad > MAX_PROFUNDIDAD:
            self.avisos.agregar(ruta, 'referencias circulares a través de arreglos: se admite cualquier contenido')
            return 'xs:anyType'
        self.profundidad += 1
        try:
            return self._valor(esquema, ruta)
        finally:
            self.profundidad -= 1

    def _valor(self, esquema, ruta):
        if isinstance(esquema, dict) and '$ref' in esquema and set(esquema) - ANOTACIONES <= {'$ref'}:
            nombre = self.tipo_con_nombre(esquema['$ref'], ruta)
            if nombre is not None:
                return nombre
        esquema = self.resolver(esquema, ruta)
        if esquema is False:
            self.avisos.agregar(ruta, 'el esquema false no admite ningún valor: se admite cualquier contenido')
            return 'xs:anyType'
        tipos, anulable = self.tipos(esquema, ruta)
        if anulable and any(c in esquema for c in ('anyOf', 'oneOf')):
            return self.valor(self.rama_de(esquema, ruta, resolver=False), ruta)   # null lo trata quien declara el elemento
        if isinstance(esquema, dict):
            self.avisar_ignoradas(esquema, ruta)
        if esquema is True or tipos is None:
            return 'xs:anyType'
        if not tipos:
            self.avisos.agregar(ruta, 'ningún valor cumple este esquema: se admite cualquier contenido')
            return 'xs:anyType'
        if tipos == {'null'}:
            self.avisos.agregar(ruta, 'null se escribe como un elemento vacío (en JSON sería {})')
            return NodoXsd('complexType')
        if len(tipos) > 1:
            if tipos <= set(ESCALARES):
                self.avisos.agregar(ruta, f'tipo unión ({", ".join(sorted(tipos))}): se admite cualquier texto '
                                          f'({_MAS_PERMISIVO})')
                return self.simple(esquema, 'string', ruta, solo_enum=True)
            self.avisos.agregar(ruta, f'tipo unión ({", ".join(sorted(tipos))}): se admite cualquier contenido '
                                      f'({_MAS_PERMISIVO})')
            return 'xs:anyType'
        tipo, = tipos
        if tipo == 'object':
            return self.complejo(esquema, ruta)
        if tipo == 'array':
            self.avisos.agregar(ruta, f'un arreglo que no es el valor de una propiedad se escribe como elementos '
                                      f'<{ELEMENTO_DE_ARREGLO}> repetidos')
            return NodoXsd('complexType', hijos=[NodoXsd('sequence', hijos=[
                self.elemento(ELEMENTO_DE_ARREGLO, esquema, True, ruta, arreglo=True)])])
        return self.simple(esquema, tipo, ruta)

    def tipo_con_nombre(self, ref, ruta):
        """Nombre del tipo del XSD para un $ref, creándolo la primera vez. None si el destino no puede ser
        un tipo con nombre (un arreglo, por ejemplo): entonces se escribe en su sitio."""
        if ref in self.con_nombre:
            return self.con_nombre[ref]
        destino = self.destino(ref, ruta)
        if destino is None:
            return 'xs:anyType'
        efectivo = self.resolver(destino, ref[1:] or '/')
        tipos, anulable = self.tipos(efectivo, ref[1:] or '/')
        if efectivo is True or tipos is None:
            return 'xs:anyType'
        if len(tipos) != 1 or anulable or next(iter(tipos)) not in ('object', *ESCALARES):
            return None
        base = ref.rstrip('/').rpartition('/')[2] if ref != '#' else 'Raiz'
        nombre = ''.join(c if c.isalnum() or c in '_-.' else '_' for c in unquote(base)) or 'Tipo'
        if not es_nombre_xml(nombre):
            nombre = 'T_' + nombre
        while nombre in self.ocupados:
            nombre += '_'
        self.ocupados.add(nombre)
        self.con_nombre[ref] = nombre
        tipo, = tipos
        ruta_def = ref[1:] or '/'
        if tipo == 'object':
            nodo = self.complejo(efectivo, ruta_def)
        else:
            self.avisar_ignoradas(efectivo, ruta_def)
            nodo = self.simple(efectivo, tipo, ruta_def, con_nombre=True)
        nodo.atributos = {'name': nombre, **nodo.atributos}
        self.globales.append(nodo)
        return nombre

    def complejo(self, esquema, ruta):
        propiedades = esquema.get('properties', {})
        requeridas = set(esquema.get('required', []))
        atributos, elementos, texto = [], [], None
        for nombre, sub in propiedades.items():
            sub_ruta = f'{ruta}/properties/{nombre.replace("~", "~0").replace("/", "~1")}'
            if sub is False:
                continue        # prohibida: basta con no declararla
            if nombre == TEXTO:
                texto = sub
            elif nombre.startswith(ATRIBUTO):
                if not es_nombre_xml(nombre[1:]):
                    self.avisos.agregar(sub_ruta, f'«{nombre[1:]}» no es un nombre de atributo de XML válido: '
                                                  'la propiedad se omite')
                    continue
                atributos.append(self.atributo(nombre[1:], sub, nombre in requeridas, sub_ruta))
            elif not es_nombre_xml(nombre):
                self.avisos.agregar(sub_ruta, f'«{nombre}» no es un nombre de elemento de XML válido: la propiedad '
                                              'se omite')
            else:
                elemento = self.elemento(nombre, sub, nombre in requeridas, sub_ruta)
                if elemento is not None:
                    elementos.append(elemento)
        faltan = [n for n in requeridas if n not in propiedades]
        if faltan:
            self.avisos.agregar(ruta, f'required nombra propiedades sin declarar ({", ".join(sorted(faltan))}): '
                                      'en XSD no se pueden exigir')
        adicionales = esquema.get('additionalProperties', True)
        if adicionales is not False:
            self.avisos.agregar(ruta, 'las propiedades no declaradas no tienen equivalente (el subconjunto de XSD no '
                                      'tiene xs:any): el XSD las rechaza')
        if len(elementos) > 1:
            self.ordenados.append(ruta or '/')
        if texto is not None and elementos:
            self.avisos.agregar(f'{ruta}/properties/$', 'el contenido mixto (texto y elementos) no está en el '
                                                         'subconjunto de XSD: «$» se omite')
            texto = None
        if texto is not None:
            base = self.valor_simple_con_nombre(texto, f'{ruta}/properties/$')
            return NodoXsd('complexType', hijos=[NodoXsd('simpleContent', hijos=[
                NodoXsd('extension', {'base': base}, atributos)])])
        hijos = [NodoXsd('sequence', hijos=elementos)] if elementos else []
        return NodoXsd('complexType', hijos=hijos + atributos)

    def valor_simple_con_nombre(self, esquema, ruta):
        """El tipo del texto de un simpleContent: xs:extension necesita un nombre de tipo, así que una
        restricción con facetas se declara como simpleType global."""
        tipo = self.valor(esquema, ruta)
        if isinstance(tipo, str) and tipo != 'xs:anyType' and (tipo.startswith('xs:') or self.es_simple(tipo)):
            return tipo
        if isinstance(tipo, NodoXsd) and tipo.nombre == 'simpleType':
            nombre = 'Texto'
            while nombre in self.ocupados:
                nombre += '_'
            self.ocupados.add(nombre)
            tipo.atributos = {'name': nombre}
            self.globales.append(tipo)
            return nombre
        self.avisos.agregar(ruta, '«$» debe ser un valor simple (cadena, número o booleano): se admite cualquier texto')
        return 'xs:string'

    def es_simple(self, nombre):
        return any(n.atributos.get('name') == nombre and n.nombre == 'simpleType' for n in self.globales)

    def atributo(self, nombre, esquema, requerido, ruta):
        tipo = self.valor(esquema, ruta)
        if isinstance(tipo, NodoXsd) and tipo.nombre != 'simpleType' or tipo == 'xs:anyType' or \
                isinstance(tipo, str) and not tipo.startswith('xs:') and not self.es_simple(tipo):
            self.avisos.agregar(ruta, 'un atributo solo puede tener un valor simple: se admite cualquier texto')
            tipo = 'xs:string'
        nodo = NodoXsd('attribute', {'name': nombre})
        if isinstance(tipo, str):
            nodo.atributos['type'] = tipo
        else:
            nodo.hijos.append(tipo)
        if requerido:
            nodo.atributos['use'] = 'required'
        return nodo

    def elemento(self, nombre, esquema, requerido, ruta, arreglo=False):
        """Declaración de un hijo. Si su valor es un arreglo, el elemento se repite."""
        efectivo = self.resolver(esquema, ruta)
        tipos, anulable = self.tipos(efectivo, ruta)
        minimo, maximo = (1 if requerido else 0), 1
        if arreglo or tipos == {'array'}:
            efectivo = self.rama_de(efectivo, ruta)
            if 'prefixItems' in efectivo:
                self.avisos.agregar(ruta, f'prefixItems no tiene equivalente: {_MAS_PERMISIVO}')
            self.avisar_ignoradas(efectivo, ruta)
            n_min, n_max = efectivo.get('minItems', 0), efectivo.get('maxItems')
            if not requerido and not arreglo and n_min > 0:
                self.avisos.agregar(ruta, f'«puede faltar o tener al menos {n_min}» no se puede expresar: se admiten de '
                                          f'0 en adelante ({_MAS_PERMISIVO})')
            minimo = int(n_min) if requerido or arreglo else 0
            maximo = int(n_max) if n_max is not None else None
            esquema = efectivo.get('items', True)
            ruta = f'{ruta}/items'
            interno, _ = self.tipos(self.resolver(esquema, ruta), ruta)
            if interno == {'array'}:
                self.avisos.agregar(ruta, f'un arreglo dentro de otro no tiene equivalente: se admite cualquier '
                                          f'contenido ({_MAS_PERMISIVO})')
                esquema = True
        elif anulable:
            self.avisos.agregar(ruta, 'null se representa con la ausencia del elemento (minOccurs="0")')
            minimo = 0
        nodo = NodoXsd('element', {'name': nombre})
        tipo = self.valor(esquema, ruta)
        if isinstance(tipo, str):
            nodo.atributos['type'] = tipo
        else:
            nodo.hijos.append(tipo)
        if minimo != 1:
            nodo.atributos['minOccurs'] = minimo
        if maximo != 1:
            nodo.atributos['maxOccurs'] = 'unbounded' if maximo is None else maximo
        return nodo

    def simple(self, esquema, tipo, ruta, con_nombre=False, solo_enum=False):
        """Tipo simple: el predefinido o, si hay facetas, una restricción."""
        facetas = []
        base = ESCALARES[tipo]
        multiplo = esquema.get('multipleOf')
        if multiplo is not None and not solo_enum:
            if tipo in ('number', 'integer') and _es_numero(multiplo) and Decimal(multiplo) == 1:
                base, tipo = 'xs:integer', 'integer'
            elif tipo == 'integer' and _es_numero(multiplo) and Decimal(multiplo) == Decimal(multiplo).to_integral_value():
                self.avisos.agregar(ruta, f'multipleOf {multiplo} no tiene faceta en XSD: {_MAS_PERMISIVO}')
            else:
                self.avisos.agregar(ruta, f'multipleOf {multiplo} no tiene faceta en XSD: {_MAS_PERMISIVO}')
        if tipo == 'string' and not solo_enum:
            for clave, faceta in (('minLength', 'minLength'), ('maxLength', 'maxLength')):
                if clave in esquema:
                    facetas.append(NodoXsd(faceta, {'value': int(esquema[clave])}))
            if 'pattern' in esquema:
                try:
                    facetas.append(NodoXsd('pattern', {'value': ecma_a_xsd(esquema['pattern'])}))
                except NoTraducible as e:
                    self.avisos.agregar(ruta, f'pattern {esquema["pattern"]!r} no se pudo traducir ({e}): se omite')
        if tipo in ('number', 'integer') and not solo_enum:
            for clave, faceta, exclusiva in (('minimum', 'minInclusive', False), ('exclusiveMinimum', 'minExclusive', True),
                                             ('maximum', 'maxInclusive', False), ('exclusiveMaximum', 'maxExclusive', True)):
                if clave not in esquema or not _es_numero(esquema[clave]):
                    continue
                valor = Decimal(esquema[clave])
                if tipo == 'integer' and valor != valor.to_integral_value():
                    # un entero mayor que 2.5 es un entero ≥ 3: la faceta pasa a ser inclusiva y entera
                    valor = Decimal(math.ceil(valor)) if faceta.startswith('min') else Decimal(math.floor(valor))
                    faceta = 'minInclusive' if faceta.startswith('min') else 'maxInclusive'
                facetas.append(NodoXsd(faceta, {'value': decimal_xsd(valor)}))
        if 'enum' in esquema or 'const' in esquema:
            valores = esquema['enum'] if 'enum' in esquema else [esquema['const']]
            facetas += self.enumeracion(valores, tipo, ruta, solo_enum)
        if not facetas and not con_nombre:
            return base
        return NodoXsd('simpleType', hijos=[NodoXsd('restriction', {'base': base}, facetas)])

    def enumeracion(self, valores, tipo, ruta, como_texto):
        if tipo == 'boolean':
            permitidos = {v for v in valores if isinstance(v, bool)}
            if len(permitidos) == 1:   # xs:boolean no admite enumeration; un pattern sí
                return [NodoXsd('pattern', {'value': 'true|1' if True in permitidos else 'false|0'})]
            return []
        textos = []
        for v in valores:
            if isinstance(v, (list, dict)) or v is None:
                self.avisos.agregar(ruta, 'enum con arreglos, objetos o null: esos valores se omiten')
            elif como_texto:
                textos.append(('true' if v else 'false') if isinstance(v, bool) else
                              decimal_xsd(Decimal(v)) if _es_numero(v) else v)
            elif tipo == 'string' and isinstance(v, str):
                textos.append(v)
            elif tipo in ('number', 'integer') and _es_numero(v):
                if tipo == 'integer' and Decimal(v) != Decimal(v).to_integral_value():
                    continue
                textos.append(decimal_xsd(Decimal(v)))
        if not textos:
            self.avisos.agregar(ruta, 'enum sin valores del tipo del elemento: ningún valor lo cumple')
            return [NodoXsd('pattern', {'value': '[^\\s\\S]'})]
        return [NodoXsd('enumeration', {'value': t}) for t in dict.fromkeys(textos)]

    # ---- documento ----
    def convertir(self):
        titulo = self.raiz.get('title') if isinstance(self.raiz, dict) else None
        nombre = titulo if isinstance(titulo, str) and es_nombre_xml(titulo) else 'raiz'
        texto = json.dumps(self.raiz, default=str)
        if '"#"' in texto:     # el esquema se usa a sí mismo: la raíz necesita un tipo con nombre
            tipo = self.tipo_con_nombre('#', '/') or self.valor(self.raiz, '')
        else:
            tipo = self.valor(self.raiz, '')
        raiz = NodoXsd('element', {'name': nombre})
        if isinstance(tipo, str):
            raiz.atributos['type'] = tipo
        else:
            raiz.hijos.append(tipo)
        lineas = ['<?xml version="1.0" encoding="UTF-8"?>', f'<xs:schema xmlns:xs="{XS}">']
        for nodo in [raiz, *self.globales]:
            nodo.escribir(lineas, '  ')
        lineas.append('</xs:schema>')
        if self.ordenados:
            donde = ', '.join('la raíz' if r == '/' else r for r in self.ordenados)
            self.avisos.insert(0, Aviso('/', f'el XSD exige los elementos hijos en el orden de «properties» ({donde})'))
        return '\n'.join(lineas) + '\n'


def leer_esquema_json(fuente: bytes):
    """El esquema como objeto de Python (números exactos con Decimal); ErrorDeConversion si no es válido."""
    try:
        analisis = analizar_fuente(fuente)
    except (ErrorLexico, ErrorSintactico) as e:
        raise ErrorDeConversion(f'el esquema no es JSON válido: {e}', [str(e)]) from e
    if not analisis.ok:
        raise ErrorDeConversion('el esquema tiene errores', [f'{d.codigo}: {d.mensaje} [{d.ruta}]' for d in analisis.errores])
    return json.loads(fuente.decode('utf-8-sig'), parse_float=Decimal)


def json_a_xsd(fuente: bytes) -> Conversion:
    """Convierte un JSON Schema en un XSD del subconjunto soportado, según la convención."""
    conversion = _AXsd(leer_esquema_json(fuente))
    texto = conversion.convertir()
    analisis = analizar_xsd(texto.encode('utf-8'))
    if not analisis.ok:   # sería un defecto del convertidor
        raise AssertionError('el XSD generado no compila: ' + '; '.join(d.mensaje for d in analisis.errores))
    return Conversion(texto, 'xsd', list(conversion.avisos))
