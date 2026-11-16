"""python -m compilador.generador esquema.json|esquema.xsd [--crate CARPETA] [--validos N] [--invalidos N] [--malformados N]

Sin --crate, escribe los casos de prueba generados, uno por línea, en JSON (el payload va en base64
porque los malformados pueden no ser texto válido). Con --crate, genera además el validador nativo en
esa carpeta, con los casos como pruebas.
"""
import argparse
import base64
import json
import sys

from ..automata.compilar import ErrorDeEsquema
from ..formatos import compilar_esquema, generar_casos
from .rust import generar_crate

sys.stdout.reconfigure(encoding='utf-8')
p = argparse.ArgumentParser(prog='python -m compilador.generador', description='Genera casos de prueba y el validador nativo.')
p.add_argument('esquema')
p.add_argument('--crate', metavar='CARPETA', help='carpeta donde escribir el crate de Rust del validador')
p.add_argument('--nombre', default='validador_generado', help='nombre del crate')
p.add_argument('--autocontenido', action='store_true', help='copiar el motor dentro del crate')
p.add_argument('--validos', type=int, default=5)
p.add_argument('--invalidos', type=int, default=5)
p.add_argument('--malformados', type=int, default=3)
p.add_argument('--semilla', type=int, default=0)
args = p.parse_args()

with open(args.esquema, 'rb') as f:
    fuente = f.read()
try:
    compilado = compilar_esquema(fuente)
except ErrorDeEsquema as e:
    print(e.analisis.informe(fuente))
    sys.exit(1)

casos = generar_casos(compilado, args.validos, args.invalidos, args.malformados, args.semilla)
if args.crate:
    carpeta = generar_crate(compilado, args.crate, args.nombre, casos=casos, autocontenido=args.autocontenido,
                            origen=args.esquema)
    print(f'Crate generado en {carpeta} con {len(casos)} casos de prueba.')
    print('  cargo test                                                    # prueba el validador nativo')
    print('  cargo build --release                                         # biblioteca dinámica con la API en C')
    print('  cargo build --release --target wasm32-unknown-unknown         # módulo WebAssembly')
else:
    for c in casos:
        print(json.dumps({'clase': c.clase, 'estado': c.estado, 'violaciones': c.violaciones,
                          'descripcion': c.descripcion, 'payload_b64': base64.b64encode(c.payload).decode('ascii')},
                         ensure_ascii=False))
