"""Casos de prueba de la sección 4 de la propuesta, y su carga en la base de datos.

    python -m web.backend.sembrar        # los inserta (o actualiza) en el almacén configurado

El resultado esperado de cada caso lo calcula el intérprete de referencia en el momento de sembrar.
Los identificadores salen del nombre, así que sembrar dos veces no duplica nada. Los casos sembrados llevan
"origen": "propuesta"; al sembrar se borran los de ese origen que ya no están en la lista (y los de
RETIRADOS, sembrados antes de que existiera la marca). Los casos creados por la API no se tocan.
"""
import base64
import hashlib
import json

from compilador.formatos import compilar_esquema, validar_documento

from .repositorio import abrir

PERSONA = {
    'type': 'object',
    'properties': {
        'nombre': {'type': 'string', 'minLength': 1, 'maxLength': 40},
        'edad': {'type': 'integer', 'minimum': 0, 'maximum': 150},
        'rol': {'enum': ['admin', 'editor', 'lector']},
        'activo': {'type': 'boolean'},
        'etiquetas': {'type': 'array', 'items': {'type': 'string', 'maxLength': 12}, 'maxItems': 4},
    },
    'required': ['nombre', 'edad'],
    'additionalProperties': False,
}
PEDIDO = {
    'type': 'object',
    'required': ['id', 'cliente', 'lineas'],
    'properties': {
        'id': {'type': 'integer', 'minimum': 1},
        'cliente': {'$ref': '#/$defs/cliente'},
        'lineas': {'type': 'array', 'minItems': 1, 'items': {'$ref': '#/$defs/linea'}},
        'entrega': {'anyOf': [{'type': 'string', 'maxLength': 60}, {'type': 'null'}]},
    },
    '$defs': {
        'cliente': {'type': 'object', 'required': ['nombre'], 'properties': {
            'nombre': {'type': 'string'}, 'correo': {'type': 'string'}}, 'dependentRequired': {'correo': ['nombre']}},
        'linea': {'type': 'object', 'required': ['producto', 'cantidad'], 'additionalProperties': False, 'properties': {
            'producto': {'type': 'string', 'minLength': 1}, 'cantidad': {'type': 'integer', 'minimum': 1},
            'precio': {'type': 'number', 'exclusiveMinimum': 0}}},
    },
}
ARBOL = {
    '$defs': {'nodo': {'type': 'object', 'required': ['v'], 'additionalProperties': False, 'properties': {
        'v': {'type': 'integer'}, 'h': {'type': 'array', 'items': {'$ref': '#/$defs/nodo'}, 'maxItems': 2}}}},
    '$ref': '#/$defs/nodo',
}
FUERA_DEL_DPDA = {   # cada palabra clave con su estrategia de respaldo
    'type': 'object',
    'properties': {
        'codigo': {'type': 'string', 'pattern': '^[A-Z]{3}-[0-9]+$', 'maxLength': 12},     # AFD de la expresión
        'medida': {'anyOf': [{'type': 'integer'}, {'type': 'number', 'minimum': 0.5}]},  # sub-ejecuciones en paralelo
        'lotes': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'integer'}},   # huellas de los elementos
        'firma': {'type': 'string', 'pattern': '^(..)\\1$'},   # no regular (referencia hacia atrás): diferida
    },
}
NUMEROS = {'type': 'array', 'items': {'type': 'number', 'minimum': 0}}

EJEMPLOS = {   # lo que ofrece la interfaz para empezar: esquema y un documento
    'Persona': (PERSONA, {'nombre': 'Ana', 'edad': 30, 'rol': 'admin', 'etiquetas': ['uno', 'dos']}),
    'Pedido con referencias': (PEDIDO, {'id': 7, 'cliente': {'nombre': 'Luis'}, 'entrega': None,
                                        'lineas': [{'producto': 'lápiz', 'cantidad': 3, 'precio': 1.5}]}),
    'Árbol recursivo': (ARBOL, {'v': 1, 'h': [{'v': 2, 'h': [{'v': 4}]}, {'v': 3}]}),
    'Fuera del DPDA puro': (FUERA_DEL_DPDA, {'codigo': 'ABC-12', 'medida': 3, 'lotes': [1, 2, 3]}),
}

j = lambda v: json.dumps(v, ensure_ascii=False).encode('utf-8')
arbol = lambda k: b'{"v": 1, "h": [' * k + b'{"v": 0}' + b']}' * k

# (nombre, categoría, esquema, payload en bytes, notas)
CASOS = [
    ('Persona completa', 'valido', PERSONA, j({'nombre': 'Ana', 'edad': 30, 'rol': 'admin', 'activo': True, 'etiquetas': ['a']}), 'esquema simple'),
    ('Persona mínima', 'valido', PERSONA, j({'nombre': 'Luis', 'edad': 0}), 'solo las propiedades obligatorias'),
    ('Pedido con referencias', 'valido', PEDIDO, j(EJEMPLOS['Pedido con referencias'][1]), 'esquema anidado con $ref y anyOf'),
    ('Árbol de tres niveles', 'valido', ARBOL, j(EJEMPLOS['Árbol recursivo'][1]), 'esquema recursivo'),
    ('Edad como texto', 'invalido', PERSONA, j({'nombre': 'Ana', 'edad': 'treinta'}), 'tipo incorrecto'),
    ('Falta la edad', 'invalido', PERSONA, j({'nombre': 'Ana'}), 'campo obligatorio faltante'),
    ('Propiedad no declarada', 'invalido', PERSONA, j({'nombre': 'Ana', 'edad': 30, 'apodo': 'ani'}), 'propiedad adicional'),
    ('Rol desconocido', 'invalido', PERSONA, j({'nombre': 'Ana', 'edad': 30, 'rol': 'jefe'}), 'violación de enum'),
    ('Edad fuera de rango', 'invalido', PERSONA, j({'nombre': 'Ana', 'edad': 200}), 'violación de rango'),
    ('Pedido sin líneas', 'invalido', PEDIDO, j({'id': 1, 'cliente': {'nombre': 'Luis'}, 'lineas': []}), 'minItems'),
    ('Varias violaciones', 'invalido', PERSONA, j({'nombre': '', 'edad': -1.5, 'rol': 3, 'etiquetas': ['a', 'b', 'c', 'd', 'e']}),
     'el peso cuenta todas las violaciones'),
    ('Coma sobrante', 'malformado', PERSONA, b'{"nombre": "Ana", "edad": 30,}', 'sintaxis incorrecta'),
    ('Llave sin cerrar', 'malformado', PERSONA, b'{"nombre": "Ana", "edad": 30', 'documento truncado'),
    ('UTF-8 inválido', 'malformado', PERSONA, b'{"nombre": "A\xffna", "edad": 30}', 'byte FF dentro de una cadena'),
    ('Clave duplicada', 'malformado', PERSONA, b'{"nombre": "Ana", "edad": 30, "edad": 31}', 'clave declarada repetida'),
    ('Número con cero inicial', 'malformado', PERSONA, b'{"nombre": "Ana", "edad": 030}', 'error léxico'),
    ('Patrón que no casa', 'fuera-del-dpda', FUERA_DEL_DPDA, j({'codigo': 'abc'}),
     'pattern: AFD de la expresión regular, en la misma pasada'),
    ('anyOf ambiguo', 'fuera-del-dpda', FUERA_DEL_DPDA, j({'medida': 0.25}),
     'dos ramas admiten un número: sub-ejecuciones en paralelo, y no cumple ninguna'),
    ('uniqueItems con repetidos', 'fuera-del-dpda', FUERA_DEL_DPDA, j({'lotes': [1, 2, 2.0]}),
     'huellas de los elementos: 2 y 2.0 son el mismo valor'),
    ('Patrón no regular', 'fuera-del-dpda', FUERA_DEL_DPDA, j({'firma': 'abab'}),
     'referencia hacia atrás: no es un lenguaje regular y queda en verificación diferida'),
    ('Árbol con k = 10', 'bombeo', ARBOL, arbol(10), 'u·vᵏ·x·yᵏ·z: profundidad 21'),
    ('Árbol con k = 60', 'bombeo', ARBOL, arbol(60), 'profundidad 121, cerca del límite de 128'),
    ('Arreglo de 20 000 números', 'bombeo', NUMEROS, j(list(range(20000))), 'arreglo muy largo: la pila no crece'),
    ('Profundidad sobre el límite', 'extremo', ARBOL, arbol(70), 'pide 141 niveles y el máximo es 128'),
    ('Números enormes', 'extremo', NUMEROS, b'[1e400, 123456789012345678901234567890.5, 1e-400, -1e999]', 'comparación decimal exacta'),
    ('Documento de 150 KB', 'extremo', {'type': 'array', 'items': PERSONA},
     j([{'nombre': f'Persona {i}', 'edad': i % 90, 'rol': 'lector'} for i in range(3000)]), 'payload grande en streaming'),
]

# ---------------------------------------------------------------------------
# XML: los mismos casos de la sección 4, con esquemas XSD
# ---------------------------------------------------------------------------
_XSD = '<?xml version="1.0" encoding="UTF-8"?>\n<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n{}\n</xs:schema>\n'
PEDIDO_XSD = '''<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="pedido">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="cliente" type="xs:string"/>
        <xs:element name="linea" type="Linea" maxOccurs="unbounded"/>
        <xs:element name="nota" type="xs:string" minOccurs="0"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:positiveInteger" use="required"/>
      <xs:attribute name="estado" type="Estado"/>
    </xs:complexType>
  </xs:element>
  <xs:complexType name="Linea">
    <xs:sequence>
      <xs:element name="producto">
        <xs:simpleType>
          <xs:restriction base="xs:string"><xs:minLength value="1"/><xs:maxLength value="40"/></xs:restriction>
        </xs:simpleType>
      </xs:element>
      <xs:element name="precio" type="Precio"/>
    </xs:sequence>
    <xs:attribute name="cantidad" type="xs:positiveInteger" use="required"/>
  </xs:complexType>
  <xs:complexType name="Precio">
    <xs:simpleContent>
      <xs:extension base="Importe"><xs:attribute name="moneda" type="xs:string" use="required"/></xs:extension>
    </xs:simpleContent>
  </xs:complexType>
  <xs:simpleType name="Importe">
    <xs:restriction base="xs:decimal"><xs:minInclusive value="0"/><xs:maxExclusive value="1000000"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="Estado">
    <xs:restriction base="xs:string">
      <xs:enumeration value="nuevo"/><xs:enumeration value="pagado"/><xs:enumeration value="enviado"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>
'''
NODO_XSD = _XSD.format('''  <xs:element name="nodo" type="Nodo"/>
  <xs:complexType name="Nodo">
    <xs:sequence>
      <xs:element name="v" type="xs:int"/>
      <xs:element name="nodo" type="Nodo" minOccurs="0" maxOccurs="2"/>
    </xs:sequence>
  </xs:complexType>''')
USUARIO_XSD = _XSD.format('''  <xs:element name="usuario">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="codigo">
          <xs:simpleType>
            <xs:restriction base="xs:string"><xs:pattern value="[A-Z]{3}-[0-9]+"/><xs:maxLength value="12"/></xs:restriction>
          </xs:simpleType>
        </xs:element>
        <xs:element name="edad" type="xs:unsignedByte"/>
        <xs:element name="alias" minOccurs="0">
          <xs:simpleType><xs:restriction base="xs:string"><xs:pattern value="\\p{IsBasicLatin}+"/></xs:restriction></xs:simpleType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>''')
MEDIDAS_XSD = _XSD.format('''  <xs:element name="medidas">
    <xs:complexType>
      <xs:sequence><xs:element name="m" type="Medida" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>
    </xs:complexType>
  </xs:element>
  <xs:simpleType name="Medida"><xs:restriction base="xs:decimal"><xs:minInclusive value="0"/></xs:restriction></xs:simpleType>''')

PEDIDO_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<pedido id="7" estado="pagado">
  <cliente>Ana &amp; Cía</cliente>
  <linea cantidad="2">
    <producto>Lápiz</producto>
    <precio moneda="COP">1500.50</precio>
  </linea>
  <linea cantidad="1">
    <producto>Cuaderno</producto>
    <precio moneda="COP">8500</precio>
  </linea>
  <nota>Entregar en la mañana</nota>
</pedido>
'''
_PEDIDO_MINIMO = '<pedido id="1"><cliente>Luis</cliente><linea cantidad="1"><producto>Lápiz</producto><precio moneda="COP">900</precio></linea></pedido>'
NODO_XML = '<nodo>\n  <v>1</v>\n  <nodo><v>2</v><nodo><v>4</v></nodo></nodo>\n  <nodo><v>3</v></nodo>\n</nodo>\n'
USUARIO_XML = '<usuario>\n  <codigo>ABC-12</codigo>\n  <edad>30</edad>\n</usuario>\n'

EJEMPLOS_XML = {   # nombre → (esquema XSD, documento XML), los dos como texto
    'XML · Pedido (XSD)': (PEDIDO_XSD, PEDIDO_XML),
    'XML · Nodo recursivo (XSD)': (NODO_XSD, NODO_XML),
    'XML · Fuera del DPDA puro (pattern)': (USUARIO_XSD, USUARIO_XML),
}

x = lambda texto: texto.encode('utf-8')
nodos = lambda k: b'<nodo><v>1</v>' * k + b'<nodo><v>0</v></nodo>' + b'</nodo>' * k
_cambiar = lambda viejo, nuevo: x(_PEDIDO_MINIMO.replace(viejo, nuevo))
assert all(viejo in _PEDIDO_MINIMO for viejo in ('cantidad="1"', ' id="1"', '</linea>', '900', '<cliente>Luis</cliente>', 'Luis'))

# (nombre, categoría, esquema XSD, documento en bytes, notas)
CASOS_XML = [
    ('XML: pedido completo', 'valido', PEDIDO_XSD, x(PEDIDO_XML), 'esquema anidado con tipos con nombre y atributos'),
    ('XML: pedido mínimo', 'valido', PEDIDO_XSD, x(_PEDIDO_MINIMO), 'solo lo obligatorio'),
    ('XML: nodo de tres niveles', 'valido', NODO_XSD, x(NODO_XML), 'esquema recursivo'),
    ('XML: cantidad como texto', 'invalido', PEDIDO_XSD, _cambiar('cantidad="1"', 'cantidad="una"'), 'tipo incorrecto'),
    ('XML: falta el atributo id', 'invalido', PEDIDO_XSD, _cambiar(' id="1"', ''), 'atributo obligatorio faltante'),
    ('XML: elemento no declarado', 'invalido', PEDIDO_XSD, _cambiar('</linea>', '<descuento>5</descuento></linea>'), 'hijo que el esquema no admite'),
    ('XML: estado desconocido', 'invalido', PEDIDO_XSD, _cambiar(' id="1"', ' id="1" estado="perdido"'), 'violación de enumeration'),
    ('XML: precio fuera de rango', 'invalido', PEDIDO_XSD, _cambiar('900', '-900'), 'violación de minInclusive'),
    ('XML: sin el cliente', 'invalido', PEDIDO_XSD, _cambiar('<cliente>Luis</cliente>', ''), 'falta un elemento de la secuencia'),
    ('XML: varias violaciones', 'invalido', PEDIDO_XSD,
     x('<pedido estado="x" sobra="1"><cliente>c</cliente><linea cantidad="-2"><producto></producto><precio>abc</precio></linea>texto</pedido>'),
     'el peso cuenta todas las violaciones'),
    ('XML: etiqueta sin cerrar', 'malformado', PEDIDO_XSD, x(_PEDIDO_MINIMO[:-9]), 'documento truncado'),
    ('XML: cierre con otro nombre', 'malformado', PEDIDO_XSD, _cambiar('</cliente>', '</client>'), 'la etiqueta de cierre no coincide'),
    ('XML: DOCTYPE con entidad externa (XXE)', 'malformado', PEDIDO_XSD,
     x('<!DOCTYPE pedido [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + _PEDIDO_MINIMO.replace('Luis', '&xxe;')),
     'se rechaza todo DOCTYPE: no se lee ningún archivo'),
    ('XML: expansión de entidades', 'malformado', PEDIDO_XSD,
     x('<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
       '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>' + _PEDIDO_MINIMO.replace('Luis', '&lol3;')),
     'ataque «billion laughs»: se rechaza en el byte 2, sin expandir nada'),
    ('XML: atributo duplicado', 'malformado', PEDIDO_XSD, _cambiar(' id="1"', ' id="1" id="2"'), 'atributo declarado repetido'),
    ('XML: UTF-8 inválido', 'malformado', PEDIDO_XSD, x(_PEDIDO_MINIMO).replace(b'Luis', b'Lu\xffis'), 'byte FF dentro del texto'),
    ('XML: entidad no definida', 'malformado', PEDIDO_XSD, _cambiar('Luis', 'Luis&nbsp;'), 'sin DTD solo existen las cinco entidades predefinidas'),
    ('XML: pattern que no casa', 'fuera-del-dpda', USUARIO_XSD, x('<usuario><codigo>abc</codigo><edad>30</edad></usuario>'),
     'xs:pattern: AFD de la expresión, anclada a todo el texto'),
    ('XML: pattern no regular', 'fuera-del-dpda', USUARIO_XSD,
     x('<usuario><codigo>ABC-1</codigo><edad>30</edad><alias>ana</alias></usuario>'),
     'bloque Unicode (\\p{IsBasicLatin}): queda en verificación diferida'),
    ('XML: nodos con k = 60', 'bombeo', NODO_XSD, nodos(60), 'u·vᵏ·x·yᵏ·z: profundidad 62'),
    ('XML: 5 000 medidas', 'bombeo', MEDIDAS_XSD, x('<medidas>' + ''.join(f'<m>{i}.5</m>' for i in range(5000)) + '</medidas>'),
     'secuencia muy larga: la pila no crece'),
    ('XML: profundidad sobre el límite', 'extremo', NODO_XSD, nodos(140), 'pide 142 niveles y el máximo es 128'),
    ('XML: decimales enormes', 'extremo', MEDIDAS_XSD,
     x('<medidas><m>123456789012345678901234567890.5</m><m>0.000000000000000000000000000001</m><m> +007.50 </m><m>-0.0</m></medidas>'),
     'comparación decimal exacta sobre el texto'),
]


# casos que existieron con otro nombre: ya no se siembran y se borran del almacén
RETIRADOS = ('Patrón sin verificar', 'uniqueItems sin verificar', 'Sin comprobaciones diferidas',
             'XML: pattern sin verificar', 'XML: lo demás sí se verifica')
ORIGEN = 'propuesta'


def identificador(nombre: str) -> str:
    return hashlib.sha256(nombre.encode('utf-8')).hexdigest()[:24]


def casos() -> list:
    """Los casos como documentos listos para guardar, con el resultado esperado ya calculado."""
    documentos, compilados = [], {}
    for nombre, categoria, esquema, payload, notas in CASOS + CASOS_XML:
        texto = esquema if isinstance(esquema, str) else json.dumps(esquema, ensure_ascii=False, indent=2)
        if texto not in compilados:
            compilados[texto] = compilar_esquema(texto.encode('utf-8'))
        r = validar_documento(compilados[texto], payload)
        documentos.append({
            '_id': identificador(nombre), 'origen': ORIGEN,
            'nombre': nombre, 'categoria': categoria, 'lenguaje': 'xml' if compilados[texto].xml else 'json',
            'notas': notas, 'esquema': texto,
            'payload_b64': base64.b64encode(payload).decode('ascii'),
            'esperado': {'estado': r.estado, 'violaciones': r.violaciones},
        })
    return documentos


def sembrar(repositorio) -> tuple:
    """Guarda los casos y borra los sembrados que ya no existen. Devuelve (guardados, borrados)."""
    vigentes = set()
    for caso in casos():
        anterior = repositorio.obtener(caso['_id'])
        if anterior and 'ultimo' in anterior:
            caso['ultimo'] = anterior['ultimo']   # conservar las métricas ya registradas
        repositorio.guardar(caso)
        vigentes.add(caso['_id'])
    sobrantes = {identificador(n) for n in RETIRADOS} | {c['_id'] for c in repositorio.listar() if c.get('origen') == ORIGEN}
    borrados = sum(repositorio.borrar(i) for i in sobrantes - vigentes)
    return len(vigentes), borrados


if __name__ == '__main__':
    from .repositorio import cargar_env
    cargar_env()
    almacen = abrir()
    guardados, borrados = sembrar(almacen)
    print(f'{guardados} casos guardados en el almacén «{almacen.nombre}»; {borrados} casos antiguos borrados.')
