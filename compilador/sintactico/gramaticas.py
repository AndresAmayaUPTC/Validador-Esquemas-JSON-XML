"""Las dos gramáticas del proyecto para JSON (Fase 2): la de los payloads y la del metalenguaje de esquemas.

    python -m compilador.sintactico.gramaticas json       # producciones, PRIMEROS, SIGUIENTES y tabla
    python -m compilador.sintactico.gramaticas esquema
    python -m compilador.sintactico.gramaticas xml
"""
from ..lexico.esquema import PALABRAS_CLAVE
from .gramatica import Gramatica

# ---------------------------------------------------------------------------
# JSON (RFC 8259) sobre el alfabeto de tokens de la Fase 1. La recursión por la izquierda de las
# listas se sustituye por los no terminales mas_pares y mas_valores para que sea LL(1).
# ---------------------------------------------------------------------------
JSON = Gramatica('''
    json        → valor FIN
    valor       → objeto | arreglo | CADENA | NUMERO | TRUE | FALSE | NULL
    objeto      → LLAVE_ABRE miembros LLAVE_CIERRA
    miembros    → par mas_pares | ε
    mas_pares   → COMA par mas_pares | ε
    par         → CADENA DOS_PUNTOS valor
    arreglo     → CORCHETE_ABRE elementos CORCHETE_CIERRA
    elementos   → valor mas_valores | ε
    mas_valores → COMA valor mas_valores | ε
''')

# ---------------------------------------------------------------------------
# Metalenguaje de esquemas (JSON Schema Draft 2020-12).
#
# Un esquema es un documento JSON, pero cada palabra clave exige una forma concreta para su valor.
# La gramática fija esa forma; lo demás (que "type" nombre un tipo que existe, que un límite no sea
# negativo, que "$ref" apunte a algo) es análisis semántico (Fase 3).
#
# Cada palabra clave es un terminal propio, y el no terminal «cadena» admite además cualquier palabra
# clave: así "type" puede ser el nombre de una propiedad o un valor sin que la gramática deje de ser LL(1).
# ---------------------------------------------------------------------------
FORMAS = {  # no terminal → palabras clave cuyo valor tiene esa forma
    'kw_esquema': ('items', 'additionalProperties', 'not', 'if', 'then', 'else', 'contains', 'propertyNames',
                   'unevaluatedItems', 'unevaluatedProperties', 'contentSchema'),
    'kw_mapa': ('properties', 'patternProperties', '$defs', 'dependentSchemas'),
    'kw_lista': ('allOf', 'anyOf', 'oneOf', 'prefixItems'),
    'kw_numero': ('multipleOf', 'maximum', 'exclusiveMaximum', 'minimum', 'exclusiveMinimum', 'maxLength',
                  'minLength', 'maxItems', 'minItems', 'maxContains', 'minContains', 'maxProperties', 'minProperties'),
    'kw_cadena': ('$schema', '$id', '$ref', '$anchor', '$dynamicRef', '$dynamicAnchor', '$comment', 'pattern',
                  'title', 'description', 'format', 'contentEncoding', 'contentMediaType'),
    'kw_booleano': ('uniqueItems', 'deprecated', 'readOnly', 'writeOnly'),
    'kw_arreglo': ('enum', 'examples'),
    'kw_valor': ('const', 'default', '$vocabulary'),
}
SUELTAS = ('type', 'required', 'dependentRequired')  # tienen una forma propia

_alternativas = lambda palabras: ' | '.join(f'"{p}"' for p in palabras)
_grupos = '\n'.join(f'    {nt} → {_alternativas(palabras)}' for nt, palabras in FORMAS.items())

ESQUEMA = Gramatica(f'''
    documento       → esquema FIN
    esquema         → TRUE | FALSE | LLAVE_ABRE claves LLAVE_CIERRA
    claves          → clave mas_claves | ε
    mas_claves      → COMA clave mas_claves | ε
    clave           → kw_esquema DOS_PUNTOS esquema
                    | kw_mapa DOS_PUNTOS mapa
                    | kw_lista DOS_PUNTOS lista
                    | "type" DOS_PUNTOS tipo
                    | "required" DOS_PUNTOS cadenas
                    | "dependentRequired" DOS_PUNTOS mapa_cadenas
                    | kw_numero DOS_PUNTOS NUMERO
                    | kw_cadena DOS_PUNTOS cadena
                    | kw_booleano DOS_PUNTOS booleano
                    | kw_arreglo DOS_PUNTOS arreglo
                    | kw_valor DOS_PUNTOS valor
                    | CADENA DOS_PUNTOS valor
{_grupos}
    mapa            → LLAVE_ABRE entradas LLAVE_CIERRA
    entradas        → entrada mas_entradas | ε
    mas_entradas    → COMA entrada mas_entradas | ε
    entrada         → cadena DOS_PUNTOS esquema
    lista           → CORCHETE_ABRE esquema mas_esquemas CORCHETE_CIERRA
    mas_esquemas    → COMA esquema mas_esquemas | ε
    tipo            → cadena | cadenas
    cadenas         → CORCHETE_ABRE lista_cadenas CORCHETE_CIERRA
    lista_cadenas   → cadena mas_cadenas | ε
    mas_cadenas     → COMA cadena mas_cadenas | ε
    mapa_cadenas    → LLAVE_ABRE dependencias LLAVE_CIERRA
    dependencias    → dependencia mas_dependencias | ε
    mas_dependencias → COMA dependencia mas_dependencias | ε
    dependencia     → cadena DOS_PUNTOS cadenas
    booleano        → TRUE | FALSE
    cadena          → CADENA | {_alternativas(PALABRAS_CLAVE)}
    valor           → objeto | arreglo | cadena | NUMERO | TRUE | FALSE | NULL
    objeto          → LLAVE_ABRE miembros LLAVE_CIERRA
    miembros        → par mas_pares | ε
    mas_pares       → COMA par mas_pares | ε
    par             → cadena DOS_PUNTOS valor
    arreglo         → CORCHETE_ABRE elementos CORCHETE_CIERRA
    elementos       → valor mas_valores | ε
    mas_valores     → COMA valor mas_valores | ε
''')

# ---------------------------------------------------------------------------
# XML bien formado, sobre los tokens del lexer de XML. El lexer ya fusiona en un solo TEXTO todo el
# texto que hay entre dos etiquetas, así que entre dos elementos hay como mucho un TEXTO.
#
# Lo que esta gramática no puede expresar se comprueba al construir el árbol (xml_dom.py): que el
# nombre de la etiqueta de cierre sea el de la de apertura, que no se repitan atributos y que fuera
# del elemento raíz solo haya espacios. La coincidencia de nombres no es libre de contexto si los
# nombres son arbitrarios; con el esquema, que fija un conjunto finito de nombres, la resuelve la pila.
# ---------------------------------------------------------------------------
XML = Gramatica('''
    documento  → prologo elemento blancos FIN
    prologo    → DECL_XML blancos | blancos
    blancos    → TEXTO | ε
    elemento   → ETIQ_ABRE NOMBRE atributos cierre
    atributos  → NOMBRE IGUAL VALOR_ATRIB atributos | ε
    cierre     → ETIQ_AUTOCIERRE | ETIQ_FIN contenido ETIQ_CIERRE_ABRE NOMBRE ETIQ_FIN
    contenido  → elemento contenido | TEXTO contenido | ε
''')

GRAMATICAS = {'json': JSON, 'esquema': ESQUEMA, 'xml': XML}

if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    nombre = sys.argv[1] if len(sys.argv) > 1 else 'json'
    print(GRAMATICAS[nombre].informe())
