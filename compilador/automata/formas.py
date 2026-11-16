"""Forma de cada subesquema (Fase 4, primer paso): el AST reducido a las restricciones que el autómata
sabe comprobar, más la lista de las que quedan en verificación diferida.

Una Forma ya no guarda texto ni posiciones: guarda tipos, pruebas sobre valores escalares, la estructura
de objetos y arreglos, y los enlaces a otras formas ($ref, allOf, anyOf, oneOf).
"""
from dataclasses import dataclass, field

from ..semantico.ciclos import TODOS
from ..semantico.metaesquema import decimal

CLASES = ('null', 'boolean', 'number', 'string', 'object', 'array')

# Palabras clave que el autómata no verifica, y las clases de valor a las que afectan.
DIFERIDAS = {
    'pattern': ('string',), 'multipleOf': ('number',), 'uniqueItems': ('array',), 'contains': ('array',),
    'patternProperties': ('object',), 'propertyNames': ('object',), 'dependentSchemas': ('object',),
    'not': CLASES, 'if': CLASES,
}

# Costo estimado de cada prueba sobre un valor escalar. Las pruebas de una transición se ordenan de
# menor a mayor costo, para que en modo de fallo temprano se ejecuten primero las baratas.
COSTO_FIJO = {'long_min': 1, 'long_max': 1, 'entero': 2, 'enum_cad': 2, 'min': 3, 'max': 3}


def costo(prueba):
    if prueba[0] == 'enum_num':
        return 3 * len(prueba[1])  # una comparación decimal por valor
    return COSTO_FIJO[prueba[0]]


@dataclass(eq=False)
class Forma:
    ruta: str
    booleano: bool | None = None            # esquemas true y false
    posibles: frozenset = TODOS             # clases de valor que podría aceptar (cota superior de la Fase 3)
    tipos: frozenset | None = None          # nombres de "type"; None si no hay
    pruebas: dict = field(default_factory=lambda: {'number': [], 'string': []})
    conjuntos: list = field(default_factory=list)   # una lista de valores por cada "enum" o "const"
    # objetos
    propiedades: dict = field(default_factory=dict)  # nombre → Forma
    adicional: 'Forma | None' = None
    requeridas: tuple = ()
    dependientes: tuple = ()                # ((nombre, (nombres que exige)), ...)
    min_props: int | None = None
    max_props: int | None = None
    # arreglos
    prefijo: list = field(default_factory=list)
    elementos: 'Forma | None' = None
    min_items: int | None = None
    max_items: int | None = None
    # aplicadores sobre la misma instancia
    ref: 'Forma | None' = None
    todas: list = field(default_factory=list)       # allOf
    alguna: list | None = None                      # anyOf
    una: list | None = None                         # oneOf
    diferidas: dict = field(default_factory=lambda: {c: [] for c in CLASES})  # clase → palabras sin verificar

    def __repr__(self):
        return f'Forma({self.ruta or "#"})'


_LIMITES = {'minimum': ('min', False), 'exclusiveMinimum': ('min', True),
            'maximum': ('max', False), 'exclusiveMaximum': ('max', True)}
_ENTEROS = {'minProperties': 'min_props', 'maxProperties': 'max_props', 'minItems': 'min_items', 'maxItems': 'max_items'}


def construir_formas(analisis):
    """Devuelve la Forma de la raíz. Requiere un análisis semántico sin errores."""
    if not analisis.ok:
        raise ValueError('el esquema tiene errores semánticos: ' + '; '.join(d.mensaje for d in analisis.errores))
    tabla = analisis.tabla
    formas = {id(e): Forma(tabla.ruta(e), e.booleano, analisis.tipos[id(e)]) for e in tabla.esquemas}
    forma = lambda e: formas[id(e)]

    for e in tabla.esquemas:
        f, adicional, con_patrones = forma(e), None, False
        for c in e.claves:
            n, v = c.nombre, c.valor
            if n == 'type':
                f.tipos = frozenset([v.dato] if v.clase == 'cadena' else [x.dato for x in v.dato])
            elif n in _LIMITES:
                f.pruebas['number'].append((_LIMITES[n][0], decimal(v), _LIMITES[n][1]))
            elif n in ('minLength', 'maxLength'):
                f.pruebas['string'].append(('long_min' if n == 'minLength' else 'long_max', int(decimal(v))))
            elif n in _ENTEROS:
                setattr(f, _ENTEROS[n], int(decimal(v)))
            elif n == 'const':
                f.conjuntos.append([v])
            elif n == 'enum':
                f.conjuntos.append(list(v.dato))
            elif n == 'properties':
                f.propiedades = {entrada.nombre: forma(entrada.esquema) for entrada in v}
            elif n == 'additionalProperties':
                adicional = forma(v)
            elif n == 'required':
                f.requeridas = tuple(x.dato for x in v.dato)
            elif n == 'dependentRequired':
                f.dependientes = tuple((k, tuple(x.dato for x in lista.dato)) for k, lista in v.dato)
            elif n == 'prefixItems':
                f.prefijo = [forma(s) for s in v]
            elif n == 'items':
                f.elementos = forma(v)
            elif n == '$ref':
                f.ref = forma(tabla.referencias[id(e)])
            elif n == 'allOf':
                f.todas = [forma(s) for s in v]
            elif n == 'anyOf':
                f.alguna = [forma(s) for s in v]
            elif n == 'oneOf':
                f.una = [forma(s) for s in v]
            elif n in DIFERIDAS and not (n == 'uniqueItems' and v.dato is False):
                con_patrones |= n == 'patternProperties'
                for clase in DIFERIDAS[n]:
                    f.diferidas[clase].append(n)
        if adicional is not None and con_patrones:
            # sin evaluar los patrones no se sabe qué propiedades son «adicionales»
            f.diferidas['object'].append('additionalProperties')
        else:
            f.adicional = adicional
    return forma(analisis.raiz)
