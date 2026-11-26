"""La convención con la que un documento XML se lee como JSON (y al revés). La usan los dos convertidores
de esquemas y la conversión de documentos, para que los tres digan lo mismo.

    <pedido id="7">                         {"@id": 7,
      <cliente>Ana</cliente>                 "cliente": "Ana",
      <linea>2</linea>                       "linea": [2, 5],
      <linea>5</linea>                       "nota": {"@lang": "es", "$": "urgente"}}
      <nota lang="es">urgente</nota>
    </pedido>

  · El valor de un elemento de tipo simple y sin atributos es su texto, convertido según el tipo:
    xs:integer y derivados → entero, xs:decimal → número, xs:boolean → true/false, lo demás → cadena.
  · Un elemento con atributos o con elementos hijos es un objeto. Cada atributo es la propiedad "@nombre";
    cada hijo, la propiedad con su nombre; el texto de un elemento con contenido simple y atributos, "$".
  · Un hijo que el modelo de contenido deja aparecer más de una vez es siempre un arreglo, aunque en el
    documento aparezca una sola vez; si no aparece, la propiedad no está.
  · Un elemento de tipo xs:anyType admite cualquier valor de JSON.
  · El documento es el valor de su elemento raíz. Si el esquema declara varios elementos globales, el
    documento es un objeto con una sola propiedad: el nombre de la raíz.

Lo que no tiene equivalente: el orden de los hijos (un objeto de JSON no tiene orden; al escribir XML se
usa el del esquema), los comentarios o instrucciones de procesamiento, y los contenedores vacíos: un
objeto vacío {} se escribe como un elemento vacío, igual que la cadena "", y un arreglo vacío [] no
produce ningún elemento, igual que una propiedad ausente.
"""
import re

ATRIBUTO = '@'
TEXTO = '$'
ELEMENTO_DE_ARREGLO = 'elemento'   # nombre de los hijos con los que se escribe un arreglo que no es una propiedad
NOMBRE_XML = re.compile(r'[A-Za-z_À-ÖØ-öø-˿Ͱ-ͽͿ-῿‌‍'
                        r'⁰-↏Ⰰ-⿯、-퟿豈-﷏ﷰ-�]'
                        r'[-.0-9A-Za-z_·À-ÖØ-öø-ͽͿ-῿‌‍'
                        r'‿⁀⁰-↏Ⰰ-⿯、-퟿豈-﷏ﷰ-�]*\Z')
_TIPO_JSON = {'string': 'string', 'integer': 'integer', 'decimal': 'number', 'boolean': 'boolean'}


def es_nombre_xml(nombre: str) -> bool:
    """True si el nombre sirve como nombre de elemento o atributo sin prefijo (un NCName)."""
    return bool(NOMBRE_XML.match(nombre)) and not nombre.lower().startswith('xml')


def tipo_json(base: str) -> str:
    """Tipo primitivo del modelo de XSD ('string', 'integer', 'decimal', 'boolean') → tipo de JSON."""
    return _TIPO_JSON[base]


def _por(a, b):
    """Producto de dos cotas (None = sin límite)."""
    if a == 0 or b == 0:
        return 0
    return None if a is None or b is None else a * b


def ocurrencias(particula) -> dict:
    """Cuántas veces puede aparecer cada nombre de hijo en el modelo de contenido:
    nombre → [mínimo, máximo o None, tipo], en el orden en que aparecen en el esquema."""
    if particula is None:
        return {}
    if particula.clase == 'elemento':
        cuenta = {particula.nombre: [1, 1, particula.tipo]}
    elif particula.clase == 'secuencia':
        cuenta = {}
        for h in particula.hijos:
            for nombre, (minimo, maximo, tipo) in ocurrencias(h).items():
                if nombre in cuenta:
                    viejo = cuenta[nombre]
                    viejo[0] += minimo
                    viejo[1] = None if viejo[1] is None or maximo is None else viejo[1] + maximo
                else:
                    cuenta[nombre] = [minimo, maximo, tipo]
    else:   # opción: en cada rama, lo suyo; un nombre que falta en una rama puede no aparecer
        ramas = [ocurrencias(h) for h in particula.hijos]
        cuenta = {}
        for rama in ramas:
            for nombre, (minimo, maximo, tipo) in rama.items():
                if nombre not in cuenta:
                    cuenta[nombre] = [minimo, maximo, tipo]
                else:
                    viejo = cuenta[nombre]
                    viejo[0] = min(viejo[0], minimo)
                    viejo[1] = None if viejo[1] is None or maximo is None else max(viejo[1], maximo)
        for nombre in cuenta:
            if any(nombre not in rama for rama in ramas):
                cuenta[nombre][0] = 0
    return {nombre: [minimo * particula.minimo, _por(maximo, particula.maximo), tipo]
            for nombre, (minimo, maximo, tipo) in cuenta.items()}


def es_repetido(maximo) -> bool:
    return maximo is None or maximo > 1
