import argparse
import sys

from ..automata.compilar import ErrorDeEsquema, a_dot, describir
from .compilar import compilar_xsd
from .interprete import validar_xml

sys.stdout.reconfigure(encoding='utf-8')
p = argparse.ArgumentParser(prog='python -m compilador.xsd',
                            description='Compila un esquema XSD a un autómata de pila ponderado.')
p.add_argument('esquema')
p.add_argument('documento', nargs='?')
p.add_argument('--dot', action='store_true', help='imprime el diagrama del autómata en Graphviz')
args = p.parse_args()
with open(args.esquema, 'rb') as f:
    fuente = f.read()
try:
    c = compilar_xsd(fuente)
except ErrorDeEsquema as e:
    print(e.analisis.informe(fuente))
    sys.exit(1)
print(a_dot(c) if args.dot else describir(c))
if args.documento:
    with open(args.documento, 'rb') as f:
        r = validar_xml(c.automata, f.read())
    print(f'\n{r.estado}: {r.violaciones} violaciones, {r.pendientes} pendientes' + (f', en {r.ruta}' if r.ruta else '')
          + (f'; {r.error[1]} en el byte {r.error[0]}' if r.error else ''))
    for pos, motivo in r.detalle[:20]:
        print(f'  byte {pos}: {motivo}')
    sys.exit(0 if r.estado in ('valido', 'pendiente') else 2)
