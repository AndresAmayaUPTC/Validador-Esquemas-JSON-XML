"""python -m web.backend [--host 127.0.0.1] [--port 8000]

Si existe Código/.env, toma de ahí MONGODB_URI y guarda los casos en esa base de datos.
"""
import argparse
import os

import uvicorn

from .repositorio import cargar_env

p = argparse.ArgumentParser(prog='python -m web.backend', description='Servidor de la aplicación web.')
p.add_argument('--host', default='127.0.0.1', help='usa 0.0.0.0 para aceptar conexiones de otras máquinas')
p.add_argument('--port', type=int, default=8000)
args = p.parse_args()
cargar_env()
print(f'Aplicación en http://{args.host}:{args.port}   (documentación de la API en /api/docs)')
print('Casos de prueba en ' + ('MongoDB' if os.environ.get('MONGODB_URI') else 'archivos locales (web/datos/casos)'))
uvicorn.run('web.backend.app:app', host=args.host, port=args.port, log_level='warning')
