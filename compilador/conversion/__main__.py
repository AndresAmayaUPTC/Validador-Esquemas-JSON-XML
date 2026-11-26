"""Conversión de esquemas desde la línea de órdenes. El resultado sale por la salida estándar y los avisos
por la de errores, así que se puede redirigir el esquema a un archivo y seguir viendo los avisos.

    python -m compilador.conversion inferir ejemplo1.json ejemplo2.json     # JSON Schema (o XSD, con .xml)
    python -m compilador.conversion xsd-a-json esquema.xsd
    python -m compilador.conversion json-a-xsd esquema.json
    python -m compilador.conversion xml-a-json esquema.xsd documento.xml    # el esquema puede ser JSON Schema
    python -m compilador.conversion json-a-xml esquema.xsd documento.json
"""
import argparse
import json
import sys
from decimal import Decimal

from ..formatos import es_xml
from . import ErrorDeConversion
from .documentos import a_texto_json, json_a_xml, xml_a_json
from .inferir_json import inferir_json
from .inferir_xsd import inferir_xsd
from .json_a_xsd import json_a_xsd
from .xsd_a_json import leer_xsd, xsd_a_json


def _leer(ruta):
    with open(ruta, 'rb') as f:
        return f.read()


def _analisis(ruta_esquema):
    fuente = _leer(ruta_esquema)
    return leer_xsd(fuente if es_xml(fuente) else json_a_xsd(fuente).texto.encode('utf-8'))


def main(argumentos=None):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(prog='python -m compilador.conversion', description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ordenes = p.add_subparsers(dest='orden', required=True)
    ordenes.add_parser('inferir', help='esquema a partir de documentos de ejemplo').add_argument('ejemplos', nargs='+')
    ordenes.add_parser('xsd-a-json', help='XSD → JSON Schema').add_argument('esquema')
    ordenes.add_parser('json-a-xsd', help='JSON Schema → XSD').add_argument('esquema')
    for nombre, ayuda in (('xml-a-json', 'documento XML → JSON'), ('json-a-xml', 'documento JSON → XML')):
        sub = ordenes.add_parser(nombre, help=ayuda)
        sub.add_argument('esquema')
        sub.add_argument('documento')
    args = p.parse_args(argumentos)
    try:
        if args.orden == 'inferir':
            ejemplos = [_leer(r) for r in args.ejemplos]
            resultado = (inferir_xsd if es_xml(ejemplos[0]) else inferir_json)(ejemplos)
        elif args.orden == 'xsd-a-json':
            resultado = xsd_a_json(_leer(args.esquema))
        elif args.orden == 'json-a-xsd':
            resultado = json_a_xsd(_leer(args.esquema))
        elif args.orden == 'xml-a-json':
            print(a_texto_json(xml_a_json(_leer(args.documento), _analisis(args.esquema))))
            return 0
        else:
            valor = json.loads(_leer(args.documento).decode('utf-8-sig'), parse_float=Decimal)
            print(json_a_xml(valor, _analisis(args.esquema)), end='')
            return 0
    except ErrorDeConversion as e:
        print(f'error: {e}', file=sys.stderr)
        for detalle in e.errores:
            print(f'  {detalle}', file=sys.stderr)
        return 1
    print(resultado.texto.rstrip('\n'))
    for a in resultado.avisos:
        print(f'aviso  {a.ruta}: {a.mensaje}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
