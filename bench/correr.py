"""Mediciones de la Fase 6: el validador en streaming frente a la línea base DOM + validador genérico.

    .venv\\Scripts\\python bench/correr.py              # serie completa; deja los CSV en bench/resultados/
    .venv\\Scripts\\python bench/correr.py --rapido     # tamaños pequeños, para comprobar que todo funciona

Series:
  comparacion   cargas típicas: registros válidos e inválidos, números y cadenas largas
  bombeo        documentos u·vᵏ·x·yᵏ·z con anidamiento creciente k: tiempo, pila y memoria
  arreglos      arreglos cada vez más largos: el tiempo crece con n y la memoria no
  sostenida     el mismo documento una y otra vez durante unos segundos
  xml           pedidos en XML contra un XSD, válidos y con errores
  xml-bombeo    elementos XML anidados k veces

La línea base de JSON es `serde_json` (construye el árbol del documento) más `jsonschema` (lo valida).
En XML la línea base solo construye el árbol con `roxmltree`: no hay en Rust un validador de XSD
comparable, así que la comparación favorece a la línea base, que hace menos trabajo.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CODIGO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODIGO))

from compilador.formatos import compilar_esquema  # noqa: E402
from compilador.generador.tablas import aplanar, serializar  # noqa: E402

BENCH = CODIGO / 'bench'
DESTINO = CODIGO.parent / '.cargo-target'
RESULTADOS = BENCH / 'resultados'

PERSONA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer', 'minimum': 0},
        'nombre': {'type': 'string', 'minLength': 1, 'maxLength': 40},
        'correo': {'type': 'string', 'maxLength': 80},
        'edad': {'type': 'integer', 'minimum': 0, 'maximum': 150},
        'activo': {'type': 'boolean'},
        'rol': {'enum': ['admin', 'editor', 'lector']},
        'saldo': {'type': 'number'},
        'etiquetas': {'type': 'array', 'items': {'type': 'string', 'maxLength': 12}, 'maxItems': 8},
        'direccion': {'type': 'object', 'required': ['ciudad'], 'additionalProperties': False, 'properties': {
            'ciudad': {'type': 'string'}, 'codigo': {'type': 'string', 'maxLength': 10}}},
    },
    'required': ['id', 'nombre', 'edad', 'rol'],
    'additionalProperties': False,
}
REGISTROS = {'type': 'array', 'items': PERSONA}
ARBOL = {'$defs': {'nodo': {'type': 'object', 'required': ['v'], 'additionalProperties': False, 'properties': {
    'v': {'type': 'integer'}, 'h': {'type': 'array', 'items': {'$ref': '#/$defs/nodo'}, 'maxItems': 2}}}},
    '$ref': '#/$defs/nodo'}
NUMEROS = {'type': 'array', 'items': {'type': 'number', 'minimum': 0, 'maximum': 1000000}}
CADENAS = {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}


_XSD = '<?xml version="1.0" encoding="UTF-8"?>\n<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">{}</xs:schema>\n'
PEDIDOS_XSD = _XSD.format('''
  <xs:element name="pedidos">
    <xs:complexType><xs:sequence><xs:element name="pedido" type="Pedido" minOccurs="0" maxOccurs="unbounded"/></xs:sequence></xs:complexType>
  </xs:element>
  <xs:complexType name="Pedido">
    <xs:sequence>
      <xs:element name="cliente"><xs:simpleType><xs:restriction base="xs:string"><xs:minLength value="1"/><xs:maxLength value="40"/></xs:restriction></xs:simpleType></xs:element>
      <xs:element name="correo" type="xs:string" minOccurs="0"/>
      <xs:element name="linea" maxOccurs="unbounded">
        <xs:complexType>
          <xs:sequence><xs:element name="producto" type="xs:string"/><xs:element name="precio" type="Precio"/></xs:sequence>
          <xs:attribute name="cantidad" type="xs:positiveInteger" use="required"/>
        </xs:complexType>
      </xs:element>
    </xs:sequence>
    <xs:attribute name="id" type="xs:nonNegativeInteger" use="required"/>
    <xs:attribute name="estado" use="required">
      <xs:simpleType><xs:restriction base="xs:string"><xs:enumeration value="nuevo"/><xs:enumeration value="pagado"/><xs:enumeration value="enviado"/></xs:restriction></xs:simpleType>
    </xs:attribute>
  </xs:complexType>
  <xs:complexType name="Precio">
    <xs:simpleContent><xs:extension base="Importe"><xs:attribute name="moneda" type="xs:string" use="required"/></xs:extension></xs:simpleContent>
  </xs:complexType>
  <xs:simpleType name="Importe"><xs:restriction base="xs:decimal"><xs:minInclusive value="0"/></xs:restriction></xs:simpleType>
''').encode('utf-8')
NODOS_XSD = _XSD.format('''
  <xs:element name="nodo" type="Nodo"/>
  <xs:complexType name="Nodo">
    <xs:sequence><xs:element name="v" type="xs:int"/><xs:element name="nodo" type="Nodo" minOccurs="0" maxOccurs="2"/></xs:sequence>
  </xs:complexType>
''').encode('utf-8')


def pedidos_xml(n, invalidos=False):
    """n pedidos en XML; con invalidos=True, uno de cada diez tiene una cantidad negativa."""
    partes = ['<?xml version="1.0" encoding="UTF-8"?>\n<pedidos>\n']
    for i in range(n):
        cantidad = -1 if invalidos and i % 10 == 0 else 1 + i % 5
        partes.append(
            f'  <pedido id="{i}" estado="{("nuevo", "pagado", "enviado")[i % 3]}">\n'
            f'    <cliente>Cliente número {i}</cliente>\n    <correo>cliente{i}@ejemplo.org</correo>\n'
            f'    <linea cantidad="{cantidad}"><producto>Lápiz &amp; papel</producto><precio moneda="COP">{i * 1.25:.2f}</precio></linea>\n'
            f'    <linea cantidad="2"><producto>Cuaderno</producto><precio moneda="COP">8500</precio></linea>\n'
            '  </pedido>\n')
    partes.append('</pedidos>\n')
    return ''.join(partes).encode('utf-8')


def nodos_xml(k):
    """u·vᵏ·x·yᵏ·z en XML: k elementos <nodo> anidados alrededor de una hoja."""
    return b'<nodo><v>1</v>' * k + b'<nodo><v>0</v></nodo>' + b'</nodo>' * k


def registros(n, invalidos=False):
    """n registros de persona; con invalidos=True, uno de cada diez rompe una restricción."""
    filas = []
    for i in range(n):
        fila = {'id': i, 'nombre': f'Persona número {i}', 'correo': f'persona{i}@ejemplo.org', 'edad': 18 + i % 60,
                'activo': i % 3 == 0, 'rol': ('admin', 'editor', 'lector')[i % 3], 'saldo': i * 1.25,
                'etiquetas': [f'e{j}' for j in range(i % 5)], 'direccion': {'ciudad': 'Sogamoso', 'codigo': '152210'}}
        if invalidos and i % 10 == 0:
            fila['edad'] = -1
        filas.append(fila)
    return json.dumps(filas, ensure_ascii=False).encode('utf-8')


def arbol(k):
    """u·vᵏ·x·yᵏ·z: k niveles de objeto dentro de arreglo."""
    return b'{"v": 1, "h": [' * k + b'{"v": 0}' + b']}' * k


def preparar():
    """Compila el programa de medición y devuelve la ruta del ejecutable. Lanza RuntimeError si no se puede."""
    home = Path.home() / '.cargo' / 'bin'
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(home))
    if cargo is None:
        raise RuntimeError('cargo no está instalado')
    entorno = dict(os.environ, PATH=str(home) + os.pathsep + os.environ['PATH'])
    if sys.platform == 'win32':
        # Las dependencias de la línea base necesitan «dlltool» y la variante GNU de Rust no trae uno que
        # funcione; llvm-ar hace ese papel si se le llama llvm-dlltool.
        dlltool = DESTINO / 'herramientas' / 'llvm-dlltool.exe'
        if not dlltool.exists():
            raiz = subprocess.run([cargo.replace('cargo', 'rustc'), '--print', 'sysroot'], env=entorno, capture_output=True, text=True).stdout.strip()
            llvm_ar = next(iter(Path(raiz).glob('lib/rustlib/*/bin/llvm-ar.exe')), None)
            if llvm_ar is None:
                raise RuntimeError('falta llvm-ar: instálalo con «rustup component add llvm-tools»')
            dlltool.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(llvm_ar, dlltool)
        entorno['CARGO_ENCODED_RUSTFLAGS'] = f'-C\x1fdlltool={dlltool.as_posix()}'
    r = subprocess.run([cargo, 'build', '--release', '--quiet'], cwd=BENCH, env=entorno, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError('no se pudo compilar bench:\n' + r.stderr.decode('utf-8', 'replace')[-2000:])
    ejecutable = shutil.which('bench', path=str(DESTINO / 'bench' / 'release'))
    if ejecutable is None:
        raise RuntimeError('no se encontró el ejecutable de bench')
    return ejecutable


class Medidor:
    """Lanza el programa de medición. Compila cada esquema una sola vez."""

    def __init__(self, ms=200):
        self.ejecutable, self.ms = preparar(), ms
        self._carpeta = tempfile.TemporaryDirectory(prefix='bench-')
        self._esquemas = {}

    def _archivos_del_esquema(self, esquema):
        """(tablas, fuente, es XML). esquema es un diccionario (JSON Schema) o los bytes de un XSD."""
        clave = esquema if isinstance(esquema, bytes) else json.dumps(esquema, sort_keys=True).encode('utf-8')
        if clave not in self._esquemas:
            base = Path(self._carpeta.name) / f'esquema{len(self._esquemas)}'
            tablas = aplanar(compilar_esquema(clave).automata)
            base.with_suffix('.fuente').write_bytes(clave)
            base.with_suffix('.tablas').write_text(serializar(tablas), encoding='ascii')
            self._esquemas[clave] = (base, tablas.xml)
        base, xml = self._esquemas[clave]
        return str(base.with_suffix('.tablas')), str(base.with_suffix('.fuente')), xml

    def medir(self, etiqueta, esquema, payload: bytes, bloque=65536, sostenida=0):
        """Diccionario con las mediciones del payload (ver bench/src/main.rs)."""
        tablas, fuente, xml = self._archivos_del_esquema(esquema)
        ruta = Path(self._carpeta.name) / 'payload'
        ruta.write_bytes(payload)
        orden = [self.ejecutable, tablas, fuente, str(ruta), '--bloque', str(bloque), '--ms', str(self.ms),
                 '--etiqueta', etiqueta] + (['--sostenida', str(sostenida)] if sostenida else []) + (['--xml'] if xml else [])
        r = subprocess.run(orden, capture_output=True, check=True)
        return json.loads(r.stdout.decode('utf-8'))

    def cerrar(self):
        self._carpeta.cleanup()


def fila(serie, parametro, m):
    """Aplana una medición en una fila de CSV."""
    s, d = m['streaming'], m['dom']
    ns_byte = lambda x: round(x['ns'] / max(1, m['bytes']), 3)
    return {
        'serie': serie, 'caso': m['etiqueta'], 'parametro': parametro, 'bytes': m['bytes'],
        'estado': s['estado'], 'estado_dom': d.get('estado', 'error'), 'violaciones': s['violaciones'],
        'mb_s': s['mb_s'], 'mb_s_dom': d.get('mb_s'), 'mb_s_dom_solo_parseo': m['dom_parseo']['mb_s'],
        'aceleracion': round(s['mb_s'] / d['mb_s'], 2) if d.get('mb_s') else None,
        'ns_por_byte': ns_byte(s), 'ns_por_byte_dom': ns_byte(d) if 'ns' in d else None,
        'ciclos_por_byte': s['ciclos_por_byte'], 'ciclos_por_byte_dom': d.get('ciclos_por_byte'),
        'asignaciones': s['asignaciones'], 'asignaciones_dom': d.get('asignaciones'),
        'memoria_dinamica': s['pico_heap'], 'memoria_dinamica_dom': d.get('pico_heap'),
        'profundidad': s['profundidad'], 'memoria_del_validador': s['memoria_usada'],
        'estados': m['automata']['estados'], 'transiciones': m['automata']['transiciones'],
    }


def correr(rapido=False):
    """Ejecuta todas las series y devuelve (filas, medición sostenida)."""
    medidor = Medidor(ms=30 if rapido else 300)
    n = 300 if rapido else 20000
    filas = []
    try:
        for etiqueta, esquema, payload in (
                ('registros válidos', REGISTROS, registros(n)),
                ('registros con errores', REGISTROS, registros(n, invalidos=True)),
                ('números', NUMEROS, json.dumps([i * 0.5 for i in range(n * 10)]).encode()),
                ('cadenas largas', CADENAS, json.dumps(['texto de relleno ' * 600] * (n // 100 + 1)).encode())):
            filas.append(fila('comparacion', len(payload), medidor.medir(etiqueta, esquema, payload)))
        for k in ((1, 10, 50) if rapido else (1, 10, 100, 500, 1000, 2000)):
            filas.append(fila('bombeo', k, medidor.medir(f'árbol k={k}', ARBOL, arbol(k))))
        for largo in ((100, 1000) if rapido else (1000, 10000, 100000, 1000000)):
            payload = json.dumps(list(range(largo))).encode()
            filas.append(fila('arreglos', largo, medidor.medir(f'arreglo n={largo}', NUMEROS, payload)))
        for etiqueta, payload in (('pedidos XML válidos', pedidos_xml(n)), ('pedidos XML con errores', pedidos_xml(n, True))):
            filas.append(fila('xml', len(payload), medidor.medir(etiqueta, PEDIDOS_XSD, payload)))
        for k in ((1, 10, 50) if rapido else (1, 10, 100, 500, 1000, 2000)):
            filas.append(fila('xml-bombeo', k, medidor.medir(f'nodos XML k={k}', NODOS_XSD, nodos_xml(k))))
        sostenida = medidor.medir('carga sostenida', REGISTROS, registros(n // 20), sostenida=0.5 if rapido else 3)
    finally:
        medidor.cerrar()
    return filas, sostenida


def tabla(filas, columnas):
    anchos = [max(len(c), *(len(str(f[c])) for f in filas)) for c in columnas]
    lineas = ['  '.join(c.ljust(a) for c, a in zip(columnas, anchos))]
    lineas += ['  '.join(str(f[c]).ljust(a) for c, a in zip(columnas, anchos)) for f in filas]
    return '\n'.join(lineas)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    rapido = '--rapido' in sys.argv
    filas, sostenida = correr(rapido)
    if not rapido:
        RESULTADOS.mkdir(exist_ok=True)
        with open(RESULTADOS / 'mediciones.csv', 'w', newline='', encoding='utf-8') as f:
            escritor = csv.DictWriter(f, fieldnames=list(filas[0]))
            escritor.writeheader()
            escritor.writerows(filas)
        (RESULTADOS / 'sostenida.json').write_text(json.dumps(sostenida, ensure_ascii=False, indent=1), encoding='utf-8')
    de = lambda serie: [f for f in filas if f['serie'] == serie]
    print('COMPARACIÓN: streaming (autómata de pila) frente a DOM + validador genérico\n')
    print(tabla(de('comparacion'), ['caso', 'bytes', 'estado', 'mb_s', 'mb_s_dom', 'aceleracion', 'ciclos_por_byte',
                                    'ciclos_por_byte_dom', 'asignaciones', 'asignaciones_dom', 'memoria_del_validador',
                                    'memoria_dinamica_dom']))
    print('\nBOMBEO: anidamiento creciente (parametro = k)\n')
    print(tabla(de('bombeo'), ['parametro', 'bytes', 'profundidad', 'ns_por_byte', 'ns_por_byte_dom', 'asignaciones',
                               'memoria_del_validador', 'asignaciones_dom', 'memoria_dinamica_dom']))
    print('\nARREGLOS: longitud creciente (parametro = n)\n')
    print(tabla(de('arreglos'), ['parametro', 'bytes', 'profundidad', 'ns_por_byte', 'ns_por_byte_dom', 'asignaciones',
                                 'memoria_del_validador', 'asignaciones_dom', 'memoria_dinamica_dom']))
    print('\nXML: el autómata valida contra el XSD; la línea base (roxmltree) solo construye el árbol\n')
    print(tabla(de('xml') + de('xml-bombeo'), ['caso', 'bytes', 'estado', 'violaciones', 'profundidad', 'mb_s', 'mb_s_dom',
                                                'aceleracion', 'asignaciones', 'memoria_del_validador', 'asignaciones_dom',
                                                'memoria_dinamica_dom']))
    s = sostenida['sostenida']
    print(f"\nCARGA SOSTENIDA: {s['documentos']} documentos en {s['segundos']} s; "
          f"MB/s mínimo {s['mb_s_min']}, mediana {s['mb_s_mediana']}, máximo {s['mb_s_max']}; "
          f"asignaciones del validador: {s['asignaciones']}")
    if not rapido:
        print(f'\nResultados guardados en {RESULTADOS}')
