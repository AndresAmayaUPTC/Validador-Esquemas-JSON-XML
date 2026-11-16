"""Clasificación de las palabras clave del esquema (Fase 3).

  (a)  directas en el DPDA: se convierten en estados y transiciones.
  (b)  con registros acotados: necesitan una máscara de bits o un contador en el marco de pila.
  (c)  no deterministas o no libres de contexto: necesitan una estrategia de respaldo
       (despacho por el primer token, producto de autómatas, AFD de una expresión regular o
       verificación diferida).

Además: 'anotacion' (no afecta a la validación), 'no-soportada' y 'desconocida'.
La categoría de casi todas las palabras es fija; en unas pocas depende del uso concreto.
"""
from dataclasses import dataclass

A, B, C, ANOTACION, NO_SOPORTADA, DESCONOCIDA = 'a', 'b', 'c', 'anotacion', 'no-soportada', 'desconocida'

_LIMITE = 'comparación decimal sobre el lexema del número'
_CONTADOR = 'contador en el marco de pila'
_PRODUCTO = 'producto de autómatas'
_DESPACHO = 'despacho por el primer token; diferida si dos ramas admiten el mismo tipo'
_DIFERIDA = 'verificación diferida'
_SIN_EFECTO = 'sin efecto en la validación'

# palabra → (categoría, mecanismo)
CATEGORIAS = {
    # (a) directas
    'type': (A, 'transición según el tipo del token'),
    'properties': (A, 'AFD de los nombres de propiedad y un subautómata por propiedad'),
    'additionalProperties': (A, 'subautómata para las propiedades no declaradas'),
    'items': (A, 'subautómata repetido por cada elemento'),
    'prefixItems': (A, 'subautómatas en secuencia'),
    'enum': (A, 'AFD de los valores permitidos'),
    'const': (A, 'AFD del valor permitido'),
    'minimum': (A, _LIMITE), 'maximum': (A, _LIMITE),
    'exclusiveMinimum': (A, _LIMITE), 'exclusiveMaximum': (A, _LIMITE),
    '$ref': (A, 'expansión en línea'),
    '$defs': (A, 'tabla de símbolos; no genera transiciones'),
    # (b) con registros
    'required': (B, 'máscara de bits en el marco de pila'),
    'dependentRequired': (B, 'implicación sobre la máscara de bits al cerrar el objeto'),
    'minItems': (B, _CONTADOR), 'maxItems': (B, _CONTADOR),
    'minProperties': (B, _CONTADOR), 'maxProperties': (B, _CONTADOR),
    'minLength': (B, 'contador de caracteres'), 'maxLength': (B, 'contador de caracteres'),
    # (c) con estrategia de respaldo
    # (la estrategia es la que aplica hoy el autómata de la Fase 4)
    'allOf': (C, _PRODUCTO), 'anyOf': (C, _DESPACHO), 'oneOf': (C, _DESPACHO),
    'not': (C, _DIFERIDA), 'if': (C, _DIFERIDA), 'then': (C, _DIFERIDA), 'else': (C, _DIFERIDA),
    'dependentSchemas': (C, _DIFERIDA), 'contains': (C, _DIFERIDA), 'minContains': (C, _DIFERIDA),
    'maxContains': (C, _DIFERIDA), 'propertyNames': (C, _DIFERIDA),
    'pattern': (C, _DIFERIDA), 'patternProperties': (C, _DIFERIDA),
    'uniqueItems': (C, _DIFERIDA), 'multipleOf': (C, _DIFERIDA),
    # no soportadas
    '$dynamicRef': (NO_SOPORTADA, '—'), '$dynamicAnchor': (NO_SOPORTADA, '—'),
    'unevaluatedItems': (NO_SOPORTADA, '—'), 'unevaluatedProperties': (NO_SOPORTADA, '—'),
}
for _palabra in ('$schema', '$id', '$anchor', '$comment', '$vocabulary', 'title', 'description', 'default',
                 'deprecated', 'readOnly', 'writeOnly', 'examples', 'format', 'contentEncoding',
                 'contentMediaType', 'contentSchema'):
    CATEGORIAS[_palabra] = (ANOTACION, _SIN_EFECTO)

NIVELES = {
    None: 'no restringe nada: acepta cualquier documento JSON',
    A: 'DPDA puro: todas las palabras clave se convierten en estados y transiciones',
    B: 'DPDA con registros acotados (máscaras de bits y contadores)',
    C: 'requiere estrategias de respaldo fuera del DPDA puro',
}


@dataclass(frozen=True)
class Uso:
    """Una aparición de una palabra clave: su categoría y cómo se va a implementar."""
    palabra: str
    categoria: str
    estrategia: str
    ruta: str
    inicio: int
    fin: int


def _con_contenedores(clave):
    """True si algún valor permitido por un enum o un const es un objeto o un arreglo."""
    valores = clave.valor.dato if clave.nombre == 'enum' else [clave.valor]
    return any(v.clase in ('objeto', 'arreglo') for v in valores)


def _se_distinguen(ramas, tipos):
    """True si los tipos de las ramas son disjuntos dos a dos: el primer token decide la rama."""
    vistos = set()
    for rama in ramas:
        t = tipos[id(rama)]
        if t & vistos:
            return False
        vistos |= t
    return True


def clasificar(tabla, tipos, grupo):
    """Lista de Uso, en el orden del texto. grupo: id(esquema) → número de su grupo recursivo."""
    usos = []
    for esquema in tabla.esquemas:
        ruta = tabla.ruta(esquema)
        for c in esquema.claves:
            if not c.conocida:
                categoria, estrategia = DESCONOCIDA, 'se ignora'
            else:
                categoria, estrategia = CATEGORIAS[c.nombre]
                if c.nombre == '$ref':
                    destino = tabla.referencias.get(id(esquema))
                    if destino is not None and grupo.get(id(esquema)) is not None \
                            and grupo.get(id(esquema)) == grupo.get(id(destino)):
                        estrategia = 'llamada recursiva: apila el estado de retorno'
                elif c.nombre in ('anyOf', 'oneOf') and _se_distinguen(c.valor, tipos):
                    estrategia = 'despacho determinista por el primer token'
                elif c.nombre in ('enum', 'const') and _con_contenedores(c):
                    # los valores escalares se comprueban; comparar objetos o arreglos completos queda diferido
                    categoria, estrategia = C, _DIFERIDA
                elif c.nombre == 'uniqueItems' and c.valor.dato is False:
                    categoria, estrategia = ANOTACION, _SIN_EFECTO
            usos.append(Uso(c.nombre, categoria, estrategia, ruta, c.inicio, c.fin))
    return usos


def nivel(usos):
    """La categoría más exigente entre las que afectan a la validación: 'a', 'b', 'c' o None."""
    presentes = {u.categoria for u in usos} & {A, B, C}
    return max(presentes, default=None)
