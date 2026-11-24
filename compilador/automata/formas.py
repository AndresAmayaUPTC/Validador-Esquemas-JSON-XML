"""Forma de cada subesquema (Fase 4, primer paso): el AST reducido a las restricciones que el autómata
sabe comprobar, más la lista de las que quedan en verificación diferida.

Una Forma ya no guarda texto ni posiciones: guarda tipos, pruebas sobre valores escalares, la estructura
de objetos y arreglos, y los enlaces a otras formas ($ref, allOf, anyOf, oneOf, not, if...).

Las palabras clave de la categoría (c) se resuelven con estas estrategias de respaldo:

  pattern, patternProperties   AFD de la expresión regular (automata/expresiones.py)
  multipleOf                   aritmética entera exacta sobre la forma canónica del número
  uniqueItems, enum y const    huellas de 64 bits de la forma canónica de los valores
    con objetos o arreglos     (automata/valores.py)
  anyOf/oneOf ambiguos, not,   producto de autómatas en ejecución: sub-ejecuciones del mismo DPDA
    if/then/else, contains,    que leen el mismo valor en paralelo y cuyos pesos se combinan al
    propertyNames,             terminar el valor (automata/nodos.py)
    dependentSchemas

Solo quedan en verificación diferida las expresiones que no son regulares (referencias hacia atrás) o
que no se implementan (aserciones, \\b, \\p{...}), y los «multipleOf» con divisores de más de 18 dígitos.
"""
from dataclasses import dataclass, field

from ..semantico.ciclos import TODOS
from ..semantico.metaesquema import decimal
from . import expresiones, valores

CLASES = ('null', 'boolean', 'number', 'string', 'object', 'array')

# Palabras clave que pueden quedar sin verificar, y las clases de valor a las que afectan.
DIFERIDAS = {'pattern': ('string',), 'multipleOf': ('number',), 'patternProperties': ('object',)}

# Costo estimado de cada prueba sobre un valor escalar. Las pruebas de una transición se ordenan de
# menor a mayor costo, para que en modo de fallo temprano se ejecuten primero las baratas.
COSTO_FIJO = {'long_min': 1, 'long_max': 1, 'entero': 2, 'enum_cad': 2, 'min': 3, 'max': 3, 'patron': 4, 'multiplo': 4}


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
    patrones: list = field(default_factory=list)     # [(expresión, Forma)] de patternProperties
    adicional: 'Forma | None' = None
    requeridas: tuple = ()
    dependientes: tuple = ()                # ((nombre, (nombres que exige)), ...)
    esquemas_dependientes: list = field(default_factory=list)  # [(Forma «required: [nombre]», Forma)]
    nombres: 'Forma | None' = None          # propertyNames
    min_props: int | None = None
    max_props: int | None = None
    # arreglos
    prefijo: list = field(default_factory=list)
    elementos: 'Forma | None' = None
    min_items: int | None = None
    max_items: int | None = None
    unicos: bool = False                    # uniqueItems
    contiene: tuple | None = None           # (Forma, mínimo, máximo o None)
    solo_contiene: 'Forma | None' = None    # una Forma que solo tiene este «contains» (ver nodos.py)
    # aplicadores sobre la misma instancia
    ref: 'Forma | None' = None
    todas: list = field(default_factory=list)       # allOf
    alguna: list | None = None                      # anyOf
    una: list | None = None                         # oneOf
    no: 'Forma | None' = None                       # not
    si: 'Forma | None' = None                       # if
    entonces: 'Forma | None' = None                 # then
    sino: 'Forma | None' = None                     # else
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
        f, adicional, patrones_diferidos = forma(e), None, False
        contiene, min_contiene, max_contiene = None, 1, None
        si = entonces = sino = None
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
            elif n == 'patternProperties':
                if all(expresiones.regular(entrada.nombre) is None for entrada in v):
                    f.patrones = [(entrada.nombre, forma(entrada.esquema)) for entrada in v]
                else:
                    patrones_diferidos = True
                    f.diferidas['object'].append('patternProperties')
            elif n == 'additionalProperties':
                adicional = forma(v)
            elif n == 'required':
                f.requeridas = tuple(x.dato for x in v.dato)
            elif n == 'dependentRequired':
                f.dependientes = tuple((k, tuple(x.dato for x in lista.dato)) for k, lista in v.dato)
            elif n == 'dependentSchemas':
                f.esquemas_dependientes = [
                    (Forma(f'{f.ruta}/dependentSchemas/{entrada.nombre}', requeridas=(entrada.nombre,)),
                     forma(entrada.esquema)) for entrada in v]
            elif n == 'propertyNames':
                f.nombres = forma(v)
            elif n == 'prefixItems':
                f.prefijo = [forma(s) for s in v]
            elif n == 'items':
                f.elementos = forma(v)
            elif n == 'uniqueItems':
                f.unicos = v.dato is True
            elif n == 'contains':
                contiene = forma(v)
            elif n == 'minContains':
                min_contiene = int(decimal(v))
            elif n == 'maxContains':
                max_contiene = int(decimal(v))
            elif n == '$ref':
                f.ref = forma(tabla.referencias[id(e)])
            elif n == 'allOf':
                f.todas = [forma(s) for s in v]
            elif n == 'anyOf':
                f.alguna = [forma(s) for s in v]
            elif n == 'oneOf':
                f.una = [forma(s) for s in v]
            elif n == 'not':
                f.no = forma(v)
            elif n == 'if':
                si = forma(v)
            elif n == 'then':
                entonces = forma(v)
            elif n == 'else':
                sino = forma(v)
            elif n == 'pattern':
                if expresiones.regular(v.dato) is None:
                    f.pruebas['string'].append(('patron', v.dato))
                else:
                    f.diferidas['string'].append('pattern')
            elif n == 'multipleOf':
                d = decimal(v)
                if valores.divisor_admitido(d):
                    f.pruebas['number'].append(('multiplo', d))
                else:
                    f.diferidas['number'].append('multipleOf')
        if adicional is not None and patrones_diferidos:
            # sin evaluar los patrones no se sabe qué propiedades son «adicionales»
            f.diferidas['object'].append('additionalProperties')
        else:
            f.adicional = adicional
        if si is not None and (entonces is not None or sino is not None):  # then y else sin if no hacen nada
            f.si, f.entonces, f.sino = si, entonces, sino
        if contiene is not None and (min_contiene > 0 or max_contiene is not None):
            f.contiene = (contiene, min_contiene, max_contiene)
            f.solo_contiene = Forma(f'{f.ruta}/contains', posibles=frozenset(['array']), contiene=f.contiene)
    return forma(analisis.raiz)
