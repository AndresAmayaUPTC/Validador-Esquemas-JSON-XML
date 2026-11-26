"""Un documento XML ↔ su equivalente en JSON, guiados por un XSD y con la convención de convencion.py.

    python -m compilador.conversion xml-a-json esquema.xsd documento.xml
    python -m compilador.conversion json-a-xml esquema.xsd documento.json

Con ellos se comprueba que las conversiones de esquemas conservan el significado: un documento válido
según el XSD, leído como JSON, cumple el JSON Schema convertido (y al revés). El XSD hace falta porque el
texto de un elemento no dice su tipo (¿«007» es un número o una cadena?) ni si un hijo que aparece una
vez podría repetirse (¿valor suelto o arreglo?).

No validan: un documento que no cumple el esquema se convierte igual, lo mejor posible, para que el
validador del otro lado lo rechace.
"""
import json
from decimal import Decimal, InvalidOperation

from ..lexico.lexer import ErrorLexico
from ..sintactico.parser import ErrorSintactico
from ..sintactico.xml_dom import analizar_xml
from . import ErrorDeConversion
from .convencion import ATRIBUTO, ELEMENTO_DE_ARREGLO, TEXTO, es_repetido, ocurrencias
from .json_a_xsd import decimal_xsd

_BLANCOS = ' \t\n\r'


# ---------------------------------------------------------------------------
# XML → JSON
# ---------------------------------------------------------------------------
def escalar(texto: str, simple):
    """Texto de un elemento o atributo → valor de JSON según su tipo simple (si no encaja, la cadena)."""
    if simple is None or simple.base == 'string':
        return texto
    limpio = texto.strip(_BLANCOS)
    if simple.base == 'boolean':
        return {'true': True, '1': True, 'false': False, '0': False}.get(limpio, texto)
    try:
        valor = Decimal(limpio)
    except InvalidOperation:
        return texto
    if not valor.is_finite() or any(c in limpio for c in 'eEnN'):
        return texto
    if simple.base == 'integer':
        return int(valor) if valor == valor.to_integral_value() and '.' not in limpio else texto
    return valor


def _generico(e):
    """Valor de un elemento de tipo xs:anyType: sin tipos, todo es cadena."""
    if not e.elementos() and not e.atributos:
        return e.texto()
    valor = {ATRIBUTO + n: a.valor for n, a in e.atributos.items() if not n.startswith('xmlns')}
    for h in e.elementos():
        if h.nombre in valor:
            if not isinstance(valor[h.nombre], list):
                valor[h.nombre] = [valor[h.nombre]]
            valor[h.nombre].append(_generico(h))
        else:
            valor[h.nombre] = _generico(h)
    if e.texto().strip(_BLANCOS):
        valor[TEXTO] = e.texto()
    return valor


def valor_xml(e, tipo):
    """Valor de JSON del elemento e, que tiene ese tipo del modelo de XSD."""
    if tipo is None or tipo.contenido == 'libre':
        return _generico(e)
    sobra = e.elementos() or any(not n.startswith('xmlns') for n in e.atributos)
    if tipo.contenido == 'simple' and not tipo.atributos:
        # con atributos o hijos que el tipo no admite, el valor es un objeto: el JSON Schema lo rechaza
        return _generico(e) if sobra else escalar(e.texto(), tipo.simple)
    valor = {}
    for nombre, a in e.atributos.items():
        if nombre.startswith('xmlns'):
            continue
        declarado = tipo.atributos.get(nombre)
        valor[ATRIBUTO + nombre] = escalar(a.valor, declarado.simple if declarado else None)
    if tipo.contenido == 'simple':
        valor[TEXTO] = escalar(e.texto(), tipo.simple)
        for h in e.elementos():
            valor[h.nombre] = _generico(h)
        return valor
    if e.texto().strip(_BLANCOS):
        valor[TEXTO] = e.texto()     # texto entre los hijos: no está declarado, así que no valida
    cuenta = ocurrencias(tipo.particula)
    for h in e.elementos():
        minimo, maximo, sub = cuenta.get(h.nombre, (0, 1, None))
        v = valor_xml(h, sub)
        if es_repetido(maximo):
            valor.setdefault(h.nombre, []).append(v)
        elif h.nombre in valor:   # aparece más veces de las permitidas: se conserva para que no valide
            if not isinstance(valor[h.nombre], list):
                valor[h.nombre] = [valor[h.nombre]]
            valor[h.nombre].append(v)
        else:
            valor[h.nombre] = v
    return valor


def xml_a_json(datos: bytes, analisis):
    """Documento XML → valor de JSON (con Decimal para los números), según el XSD analizado."""
    try:
        raiz = analizar_xml(datos)
    except (ErrorLexico, ErrorSintactico) as e:
        raise ErrorDeConversion(f'el documento no es XML bien formado: {e}', [str(e)]) from e
    tipo = analisis.elementos.get(raiz.nombre)
    if tipo is None:
        raise ErrorDeConversion(f'el XSD no declara el elemento raíz «{raiz.nombre}»')
    valor = valor_xml(raiz, tipo)
    return valor if len(analisis.elementos) == 1 else {raiz.nombre: valor}


def a_texto_json(v, nivel=0) -> str:
    """JSON con sangría que escribe los Decimal tal cual (json.dumps no los admite)."""
    dentro, fuera = '  ' * (nivel + 1), '  ' * nivel
    if isinstance(v, dict) and v:
        miembros = [f'{dentro}{json.dumps(k, ensure_ascii=False)}: {a_texto_json(x, nivel + 1)}' for k, x in v.items()]
        return '{\n' + ',\n'.join(miembros) + '\n' + fuera + '}'
    if isinstance(v, list) and v:
        return '[\n' + ',\n'.join(dentro + a_texto_json(x, nivel + 1) for x in v) + '\n' + fuera + ']'
    if isinstance(v, Decimal):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------------------
# JSON → XML
# ---------------------------------------------------------------------------
def _texto(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float, Decimal)):
        return decimal_xsd(Decimal(str(v)) if isinstance(v, float) else Decimal(v))
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def _escapar_texto(t: str) -> str:
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\r', '&#13;')


def _escapar_atributo(t: str) -> str:
    return (t.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;')
             .replace('\t', '&#9;').replace('\n', '&#10;').replace('\r', '&#13;'))


class _Escritor:
    def __init__(self):
        self.lineas = []

    def elemento(self, nombre, valor, tipo, sangria):
        if isinstance(valor, list):   # un arreglo que no es propiedad: hijos <elemento>
            valor = {ELEMENTO_DE_ARREGLO: valor}
        if isinstance(valor, dict):
            # en un tipo simple, un objeto se escribe con sus hijos: así el documento no valida
            simple = tipo is not None and tipo.contenido == 'simple' and not tipo.atributos
            self.objeto(nombre, valor, None if simple else tipo, sangria)
        else:
            self.lineas.append(f'{sangria}<{nombre}>{_escapar_texto(_texto(valor))}</{nombre}>')

    def objeto(self, nombre, valor, tipo, sangria):
        atributos = ''.join(f' {k[1:]}="{_escapar_atributo(_texto(v))}"' for k, v in valor.items()
                            if k.startswith(ATRIBUTO) and len(k) > 1)
        cuenta = ocurrencias(tipo.particula) if tipo is not None and tipo.contenido == 'elementos' else {}
        hijos = [k for k in cuenta if k in valor] + [k for k in valor if k not in cuenta and not k.startswith(ATRIBUTO)
                                                     and k != TEXTO]
        texto = valor.get(TEXTO)
        if not hijos:
            contenido = _escapar_texto(_texto(texto)) if texto is not None else ''
            self.lineas.append(f'{sangria}<{nombre}{atributos}>{contenido}</{nombre}>' if contenido
                               else f'{sangria}<{nombre}{atributos}/>')
            return
        self.lineas.append(f'{sangria}<{nombre}{atributos}>')
        for k in hijos:
            v = valor[k]
            sub = cuenta[k][2] if k in cuenta else None
            repetido = k not in cuenta or es_repetido(cuenta[k][1])
            for uno in (v if isinstance(v, list) and repetido else [v]):
                self.elemento(k, uno, sub, sangria + '  ')
        self.lineas.append(f'{sangria}</{nombre}>')


def json_a_xml(valor, analisis) -> str:
    """Valor de JSON → documento XML, con los hijos en el orden del XSD analizado."""
    if len(analisis.elementos) == 1:
        (nombre, tipo), = analisis.elementos.items()
    else:
        if not isinstance(valor, dict) or len(valor) != 1:
            raise ErrorDeConversion('el XSD declara varios elementos globales: el documento JSON debe ser un objeto '
                                    'con una sola propiedad, el nombre de la raíz')
        (nombre, valor), = valor.items()
        if nombre not in analisis.elementos:
            raise ErrorDeConversion(f'el XSD no declara el elemento raíz «{nombre}»')
        tipo = analisis.elementos[nombre]
    escritor = _Escritor()
    escritor.elemento(nombre, valor, tipo, '')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + '\n'.join(escritor.lineas) + '\n'
