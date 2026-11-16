"""Validación del esquema contra el metaesquema de JSON Schema Draft 2020-12 (Fase 3).

La gramática de la Fase 2 ya garantiza la forma del valor de cada palabra clave (que "minItems" sea
un número, que "required" sea una lista de cadenas). Aquí se comprueba lo que una gramática libre de
contexto no puede expresar: que el nombre de un tipo exista, que un límite sea un entero no negativo,
que no haya elementos repetidos o que un intervalo no esté vacío.
"""
from decimal import Decimal, InvalidOperation

TIPOS = ('null', 'boolean', 'object', 'array', 'number', 'string', 'integer')
DIALECTO = 'https://json-schema.org/draft/2020-12/schema'
ENTEROS_NO_NEGATIVOS = ('maxLength', 'minLength', 'maxItems', 'minItems', 'maxContains', 'minContains',
                        'maxProperties', 'minProperties')
MAX_CONTADOR = 2 ** 32 - 2  # los contadores del validador son registros de 32 bits; 2^32 - 1 significa «sin límite»
NO_SOPORTADAS = ('$dynamicRef', '$dynamicAnchor', 'unevaluatedItems', 'unevaluatedProperties')
SOLO_EN_LA_RAIZ = ('$schema', '$id', '$vocabulary')
DE_OTROS_BORRADORES = {'definitions': '$defs', 'dependencies': 'dependentRequired o dependentSchemas',
                       'additionalItems': 'items', 'id': '$id'}
PARES_MIN_MAX = (('minLength', 'maxLength'), ('minItems', 'maxItems'), ('minProperties', 'maxProperties'),
                 ('minContains', 'maxContains'))


def decimal(valor):
    """Valor exacto de un número del AST, o None si su exponente no cabe en un Decimal."""
    try:
        return Decimal(valor.dato)
    except InvalidOperation:
        return None


def _repetidos(textos):
    vistos, repetidos = set(), []
    for t in textos:
        if t in vistos and t not in repetidos:
            repetidos.append(t)
        vistos.add(t)
    return repetidos


def _congelar(valor):
    """Forma comparable de un valor JSON: iguala 1 y 1.0, y los objetos con las mismas claves en otro orden."""
    if valor.clase == 'numero':
        d = decimal(valor)
        return ('numero', d.normalize() if d is not None else valor.dato)
    if valor.clase == 'arreglo':
        return ('arreglo', tuple(_congelar(v) for v in valor.dato))
    if valor.clase == 'objeto':
        return ('objeto', frozenset((k, _congelar(v)) for k, v in valor.dato))
    return (valor.clase, valor.dato)


def _verificar_clave(c, ruta, es_raiz, diag):
    nombre, v = c.nombre, c.valor
    if not c.conocida:
        actual = DE_OTROS_BORRADORES.get(nombre)
        pista = f'; en Draft 2020-12 es «{actual}»' if actual else ''
        diag.aviso('clave-desconocida', f'«{nombre}» no es una palabra clave de JSON Schema y se ignora{pista}', c, ruta)
    elif nombre in NO_SOPORTADAS:
        diag.error('no-soportada', f'«{nombre}» no está en el subconjunto soportado', c, ruta)
    elif nombre in SOLO_EN_LA_RAIZ and not es_raiz:
        diag.error('solo-en-la-raiz', f'«{nombre}» solo se admite en la raíz del esquema', c, ruta)
    elif nombre == '$schema':
        if v.dato.rstrip('#') != DIALECTO:
            diag.error('dialecto', f'solo se admite el dialecto {DIALECTO}', v, ruta)
    elif nombre == 'type':
        nombres = [v] if v.clase == 'cadena' else v.dato
        if not nombres:
            diag.error('tipo-vacio', '«type» no puede ser una lista vacía', v, ruta)
        for n in nombres:
            if n.dato not in TIPOS:
                diag.error('tipo-desconocido', f'«{n.dato}» no es un tipo; los tipos son: {", ".join(TIPOS)}', n, ruta)
        for r in _repetidos(n.dato for n in nombres):
            diag.error('repetido', f'el tipo «{r}» está repetido', v, ruta)
    elif nombre in ENTEROS_NO_NEGATIVOS:
        d = decimal(v)
        if d is None or d != d.to_integral_value() or d < 0:
            diag.error('limite-invalido', f'«{nombre}» debe ser un entero no negativo', v, ruta)
        elif d > MAX_CONTADOR:
            diag.error('limite-fuera-de-rango', f'«{nombre}» no puede pasar de {MAX_CONTADOR}', v, ruta)
    elif nombre == 'multipleOf':
        d = decimal(v)
        if d is None or d <= 0:
            diag.error('multiplo-invalido', '«multipleOf» debe ser un número mayor que cero', v, ruta)
    elif nombre in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'):
        if decimal(v) is None:
            diag.error('numero-fuera-de-rango', f'el exponente de «{nombre}» es demasiado grande', v, ruta)
    elif nombre == 'required':
        for r in _repetidos(n.dato for n in v.dato):
            diag.error('repetido', f'«{r}» está repetida en «required»', v, ruta)
    elif nombre == 'dependentRequired':
        for propiedad, lista in v.dato:
            for r in _repetidos(n.dato for n in lista.dato):
                diag.error('repetido', f'«{r}» está repetida en las dependencias de «{propiedad}»', lista, ruta)
        for r in _repetidos(k for k, _ in v.dato):
            diag.error('entrada-duplicada', f'«{r}» está repetida en «dependentRequired»', v, ruta)
    elif nombre == 'enum':
        if not v.dato:
            diag.aviso('enum-vacio', '«enum» está vacío: ningún valor lo cumple', v, ruta)
        elif len({_congelar(x) for x in v.dato}) < len(v.dato):
            diag.aviso('enum-repetido', '«enum» tiene valores repetidos', v, ruta)


def _verificar_intervalos(esquema, ruta, diag):
    """Avisa de los pares mínimo/máximo que dejan un intervalo vacío."""
    def numero(nombre):
        c = esquema.clave(nombre)
        return (c, decimal(c.valor)) if c is not None and c.valor.clase == 'numero' else (None, None)

    for minimo, maximo in PARES_MIN_MAX:
        (c, lo), (_, hi) = numero(minimo), numero(maximo)
        if lo is not None and hi is not None and lo > hi:
            diag.aviso('intervalo-vacio', f'«{minimo}» es mayor que «{maximo}»: ningún valor cumple los dos', c, ruta)

    (c_min, lo), (c_xmin, xlo) = numero('minimum'), numero('exclusiveMinimum')
    (_, hi), (_, xhi) = numero('maximum'), numero('exclusiveMaximum')
    # entre dos límites iguales gana el estricto: (d, True) es el mayor inferior y (d, False) el menor superior
    inferior = max(((d, estricto) for d, estricto in ((lo, False), (xlo, True)) if d is not None), default=None)
    superior = min(((d, incluido) for d, incluido in ((hi, True), (xhi, False)) if d is not None), default=None)
    if inferior is not None and superior is not None:
        (a, a_estricto), (b, b_incluido) = inferior, superior
        if a > b or (a == b and (a_estricto or not b_incluido)):
            diag.aviso('intervalo-vacio', 'los límites numéricos dejan un intervalo vacío', c_min or c_xmin, ruta)


def verificar(tabla, diag):
    for esquema in tabla.esquemas:
        ruta = tabla.ruta(esquema)
        for c in esquema.claves:
            _verificar_clave(c, ruta, esquema is tabla.raiz, diag)
        _verificar_intervalos(esquema, ruta, diag)
