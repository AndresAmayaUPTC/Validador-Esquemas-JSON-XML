"""Valores de JSON en forma canónica: huellas de 64 bits y múltiplos exactos (Fase 4, estrategias de respaldo).

Huellas. «uniqueItems» y los «enum»/«const» con objetos o arreglos comparan valores completos. Guardar
los valores no cabe en memoria acotada; se guarda su huella: un resumen de 64 bits de la forma canónica
del valor, calculado en streaming mientras se leen los tokens. Dos valores iguales para JSON Schema tienen
la misma huella:

  - los números se comparan por su valor (1, 1.0 y 1e0 son el mismo): se resume ±0.DÍGITOS × 10^e;
  - las cadenas, por su texto ya decodificado ("a" y "\\u0061" son la misma);
  - en un objeto no importa el orden de las propiedades: sus pares se combinan con una suma, que es
    conmutativa; en un arreglo sí importa: sus elementos se encadenan.

Dos valores distintos tienen la misma huella con probabilidad del orden de 2^-64 por par: es el único
caso en que el validador puede equivocarse (vería un duplicado que no existe). Está documentado.

El motor de Rust (validador/src/validador.rs) calcula exactamente lo mismo; el compilador usa estas
funciones para las constantes del esquema y el intérprete para los documentos.

Múltiplos. «multipleOf» se decide con aritmética entera exacta sobre la forma canónica: con v = D·10^a y
m = M·10^b (D y M enteros sin ceros finales), v/m es entero si y solo si M divide a D·10^(a−b). El motor
de Rust hace la misma cuenta módulo M, por eso el divisor debe caber en 64 bits (hasta 18 dígitos).
"""
from decimal import Decimal

M64 = (1 << 64) - 1
FNV_INICIO, FNV_PRIMO = 0xCBF29CE484222325, 0x100000001B3
ORO = 0x9E3779B97F4A7C15
TOPE = 4_000_000_000_000_000_000   # el mismo tope de exponente que validador/src/decimal.rs
MAX_DIGITOS_DIVISOR = 18
CADENA, NUMERO, VERDADERO, FALSO, NULO, ARREGLO, OBJETO = 1, 2, 3, 4, 5, 6, 7


def mezclar(k: int) -> int:
    """Finalizador de MurmurHash3: dispersa todos los bits."""
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & M64
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & M64
    return k ^ (k >> 33)


def fnv(h: int, datos) -> int:
    for b in datos:
        h = ((h ^ b) * FNV_PRIMO) & M64
    return h


def canonico(lexema: str):
    """(negativo, exponente, dígitos) de un número: ±0.DÍGITOS × 10^exponente, sin ceros a los lados.
    El cero es (False, 0, ()). Reproduce validador/src/decimal.rs, incluido el tope de los exponentes."""
    negativo = lexema.startswith('-')
    sin_signo = lexema[negativo:]
    corte = next((i for i, c in enumerate(sin_signo) if c in 'eE'), len(sin_signo))
    mantisa, resto = sin_signo[:corte], sin_signo[corte:]
    escrito, exponente_negativo = 0, False
    for c in resto[1:]:
        if c == '-':
            exponente_negativo = True
        elif c != '+':
            escrito = min(escrito * 10 + int(c), TOPE)
    if exponente_negativo:
        escrito = -escrito
    enteros = mantisa.index('.') if '.' in mantisa else len(mantisa)
    ceros, inicio = 0, len(mantisa)
    for i, c in enumerate(mantisa):
        if c == '0':
            ceros += 1
        elif c != '.':
            inicio = i
            break
    digitos = [int(c) for c in mantisa[inicio:] if c != '.']
    while digitos and digitos[-1] == 0:
        digitos.pop()
    if not digitos:
        return False, 0, ()
    return negativo, max(-TOPE, min(TOPE, enteros - ceros + escrito)), tuple(digitos)


def canonico_decimal(d: Decimal):
    """La misma forma para una constante del esquema (como la guarda generador/tablas.py)."""
    signo, digitos, exp = d.as_tuple()
    digitos = list(digitos)
    while digitos and digitos[0] == 0:
        digitos.pop(0)
    exponente = len(digitos) + exp if digitos else 0
    while digitos and digitos[-1] == 0:
        digitos.pop()
    if not digitos:
        return False, 0, ()
    return bool(signo), exponente, tuple(digitos)


# ---------------------------------------------------------------------------
# Huellas
# ---------------------------------------------------------------------------
def de_cadena(datos: bytes) -> int:
    return mezclar(fnv(fnv(FNV_INICIO, (CADENA,)), datos))


def de_numero(lexema: str) -> int:
    negativo, exponente, digitos = canonico(lexema)
    return mezclar(fnv(FNV_INICIO, (NUMERO, int(negativo), *exponente.to_bytes(8, 'little', signed=True), *digitos)))


def de_literal(etiqueta: int) -> int:
    return mezclar(fnv(FNV_INICIO, (etiqueta,)))


def elemento(acumulado: int, huella: int) -> int:
    """Encadena un elemento de un arreglo (el orden importa)."""
    return mezclar((acumulado * ORO + huella) & M64)


def miembro(acumulado: int, clave: int, huella: int) -> int:
    """Suma un par clave-valor de un objeto (el orden no importa)."""
    return (acumulado + mezclar((clave * ORO + huella) & M64)) & M64


INICIO_ARREGLO, INICIO_OBJETO = ARREGLO, 0


def cerrar_arreglo(acumulado: int) -> int:
    return mezclar(acumulado ^ (ARREGLO << 56))


def cerrar_objeto(acumulado: int) -> int:
    return mezclar(acumulado ^ (OBJETO << 56))


def de_valor(valor) -> int:
    """Huella de un valor del AST del esquema (sintactico/ast_esquema.Valor)."""
    clase = valor.clase
    if clase == 'cadena':
        return de_cadena(valor.dato.encode('utf-8'))
    if clase == 'numero':
        return de_numero(valor.dato)
    if clase == 'booleano':
        return de_literal(VERDADERO if valor.dato else FALSO)
    if clase == 'nulo':
        return de_literal(NULO)
    if clase == 'arreglo':
        h = INICIO_ARREGLO
        for v in valor.dato:
            h = elemento(h, de_valor(v))
        return cerrar_arreglo(h)
    h = INICIO_OBJETO
    for k, v in valor.dato:
        h = miembro(h, de_cadena(k.encode('utf-8')), de_valor(v))
    return cerrar_objeto(h)


# ---------------------------------------------------------------------------
# Múltiplos
# ---------------------------------------------------------------------------
def divisor_admitido(d: Decimal) -> bool:
    """El motor de Rust trabaja módulo M en 64 bits: el divisor no puede tener más de 18 dígitos."""
    return 0 < len(canonico_decimal(d)[2]) <= MAX_DIGITOS_DIVISOR


def es_multiplo(valor, divisor) -> bool:
    """valor y divisor en forma canónica (canonico o canonico_decimal); el divisor es positivo."""
    _, e, digitos = valor
    _, f, digitos_m = divisor
    if not digitos:
        return True
    s = (e - len(digitos)) - (f - len(digitos_m))
    if s < 0:   # D no acaba en cero: no puede ser múltiplo de M·10^(−s)
        return False
    m = int(''.join(map(str, digitos_m)))
    resto = 0
    for d in digitos:
        resto = (resto * 10 + d) % m
    return resto * pow(10, s, m) % m == 0
