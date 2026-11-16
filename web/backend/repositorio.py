"""Almacén de casos de prueba (Fase 7): documentos en una base de datos NoSQL.

Un caso es un documento como este (el payload va en base64 porque los malformados pueden no ser texto):

    {
      "_id": "…", "nombre": "…", "categoria": "valido | invalido | malformado | fuera-del-dpda | bombeo | extremo",
      "lenguaje": "json", "esquema": "<texto del esquema>", "payload_b64": "…", "notas": "…",
      "esperado": {"estado": "…", "violaciones": 0},
      "ultimo":   {"estado": "…", "violaciones": 0, "ruta": "…", "coincide": true, "metricas": {…}, "fecha": "…"}
    }

Hay dos almacenes con la misma interfaz:

  RepositorioMongo     MongoDB (Atlas en la nube o un servidor local). Se usa si existe la variable de
                       entorno MONGODB_URI.
  RepositorioArchivos  Un archivo JSON por caso en una carpeta. Es el modo sin configuración, para
                       desarrollar y para las pruebas.
"""
import json
import os
import uuid
from pathlib import Path

CATEGORIAS = ('valido', 'invalido', 'malformado', 'fuera-del-dpda', 'bombeo', 'extremo')
CARPETA_POR_DEFECTO = Path(__file__).resolve().parents[1] / 'datos' / 'casos'
ENV = Path(__file__).resolve().parents[2] / '.env'


def cargar_env(ruta=ENV):
    """Lee Código/.env (una variable por línea, CLAVE=valor) y define las que aún no existen en el
    entorno: una variable puesta a mano en la terminal tiene prioridad sobre el archivo."""
    ruta = Path(ruta)
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding='utf-8').splitlines():
        linea = linea.strip()
        if not linea or linea.startswith('#') or '=' not in linea:
            continue
        clave, _, valor = linea.partition('=')
        os.environ.setdefault(clave.strip(), valor.strip().strip('"\''))


class RepositorioArchivos:
    nombre = 'archivos'

    def __init__(self, carpeta=CARPETA_POR_DEFECTO):
        self.carpeta = Path(carpeta)
        self.carpeta.mkdir(parents=True, exist_ok=True)

    def _ruta(self, id_):
        if not (isinstance(id_, str) and id_.isalnum()):   # el id viene de fuera: nunca se usa como ruta sin revisar
            raise KeyError(id_)
        return self.carpeta / f'{id_}.json'

    def listar(self, categoria=None) -> list:
        casos = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(self.carpeta.glob('*.json'))]
        casos = [c for c in casos if categoria is None or c.get('categoria') == categoria]
        return sorted(casos, key=lambda c: (CATEGORIAS.index(c['categoria']) if c.get('categoria') in CATEGORIAS else 99,
                                            c.get('nombre', '')))

    def obtener(self, id_):
        try:
            return json.loads(self._ruta(id_).read_text(encoding='utf-8'))
        except (KeyError, FileNotFoundError):
            return None

    def guardar(self, caso: dict) -> str:
        """Inserta o reemplaza. Si el caso no trae _id, se le asigna uno. Devuelve el _id."""
        caso = dict(caso)
        caso.setdefault('_id', uuid.uuid4().hex)
        self._ruta(caso['_id']).write_text(json.dumps(caso, ensure_ascii=False, indent=1), encoding='utf-8')
        return caso['_id']

    def borrar(self, id_) -> bool:
        try:
            self._ruta(id_).unlink()
            return True
        except (KeyError, FileNotFoundError):
            return False


class RepositorioMongo:
    nombre = 'mongodb'

    def __init__(self, coleccion):
        self.coleccion = coleccion

    def listar(self, categoria=None) -> list:
        filtro = {} if categoria is None else {'categoria': categoria}
        casos = list(self.coleccion.find(filtro))
        return sorted(casos, key=lambda c: (CATEGORIAS.index(c['categoria']) if c.get('categoria') in CATEGORIAS else 99,
                                            c.get('nombre', '')))

    def obtener(self, id_):
        return self.coleccion.find_one({'_id': id_})

    def guardar(self, caso: dict) -> str:
        caso = dict(caso)
        caso.setdefault('_id', uuid.uuid4().hex)
        self.coleccion.replace_one({'_id': caso['_id']}, caso, upsert=True)
        return caso['_id']

    def borrar(self, id_) -> bool:
        return self.coleccion.delete_one({'_id': id_}).deleted_count == 1


def abrir():
    """El almacén configurado: MongoDB si hay MONGODB_URI; si no, archivos locales."""
    uri = os.environ.get('MONGODB_URI')
    if not uri:
        return RepositorioArchivos(os.environ.get('CASOS_DIR', CARPETA_POR_DEFECTO))
    from pymongo import MongoClient
    cliente = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return RepositorioMongo(cliente[os.environ.get('MONGODB_DB', 'validador')]['casos'])
