"""python -m compilador.automata esquema.json [payload.json] [--dot] [--sin-delta]"""
import argparse
import sys

from .compilar import ErrorDeEsquema, a_dot, compilar, describir
from .interprete import validar

sys.stdout.reconfigure(encoding='utf-8')
p = argparse.ArgumentParser(prog='python -m compilador.automata',
                            description='Compila un esquema a su autómata de pila ponderado.')
p.add_argument('esquema')
p.add_argument('payload', nargs='?', help='documento JSON que se valida con el autómata')
p.add_argument('--dot', action='store_true', help='escribir el diagrama en Graphviz en lugar de la descripción')
p.add_argument('--sin-delta', action='store_true', help='omitir la tabla de transiciones')
args = p.parse_args()

with open(args.esquema, 'rb') as f:
    fuente = f.read()
try:
    compilado = compilar(fuente)
except ErrorDeEsquema as e:
    print(e.analisis.informe(fuente))
    sys.exit(1)

print(a_dot(compilado) if args.dot else describir(compilado, con_delta=not args.sin_delta))
if args.payload:
    with open(args.payload, 'rb') as f:
        r = validar(compilado.automata, f.read())
    print(f'\nRESULTADO: {r.estado}; violaciones (peso) = {r.violaciones}; diferidas = {r.pendientes}; '
          f'profundidad máxima = {r.profundidad}')
    for pos, motivo in r.detalle:
        print(f'  byte {pos}: {motivo}')
    if r.error:
        print(f'  byte {r.error[0]}: {r.error[1]}')
    sys.exit(0 if r.estado in ('valido', 'pendiente') else 2)
