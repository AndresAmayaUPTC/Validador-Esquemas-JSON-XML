"""Tabla de símbolos del esquema y resolución de $ref (Fase 3).

Cada subesquema recibe un nombre único: su JSON Pointer (RFC 6901) desde la raíz, por ejemplo
/properties/edad o /$defs/nodo. La tabla asocia cada nombre con su nodo del AST, y con ella un $ref
se resuelve con una búsqueda. Solo se admiten referencias dentro del propio documento: una referencia
remota obligaría al compilador a abrir conexiones que decide quien escribe el esquema.
"""
import re
from dataclasses import dataclass
from urllib.parse import unquote

# Palabras clave cuyos subesquemas se aplican a la misma instancia, sin consumir entrada.
# Las demás (properties, items, ...) se aplican a un hijo: antes hay que leer al menos «{» o «[».
EN_SITIO = frozenset({'not', 'if', 'then', 'else', 'allOf', 'anyOf', 'oneOf', 'dependentSchemas'})
# Contienen esquemas que no se aplican a la instancia: solo se pueden alcanzar con $ref.
NO_SE_APLICAN = frozenset({'$defs', 'contentSchema'})

_ANCLA = re.compile(r'[A-Za-z_][-A-Za-z0-9._]*\Z')


@dataclass(frozen=True)
class Diagnostico:
    """Mensaje del análisis semántico. [inicio, fin) es la posición en bytes dentro del esquema y
    ruta el JSON Pointer del subesquema afectado. Un 'error' impide compilar; un 'aviso', no."""
    severidad: str
    codigo: str
    mensaje: str
    inicio: int
    fin: int
    ruta: str


class Diagnosticos(list):
    def error(self, codigo, mensaje, nodo, ruta):
        self.append(Diagnostico('error', codigo, mensaje, nodo.inicio, nodo.fin, ruta))

    def aviso(self, codigo, mensaje, nodo, ruta):
        self.append(Diagnostico('aviso', codigo, mensaje, nodo.inicio, nodo.fin, ruta))


def escapar(nombre):
    """Nombre de propiedad → segmento de JSON Pointer."""
    return nombre.replace('~', '~0').replace('/', '~1')


def hijos(esquema):
    """Subesquemas directos: (clave, segmento de ruta, subesquema), en el orden del texto."""
    for c in esquema.claves:
        if c.forma == 'esquema':
            yield c, f'/{c.nombre}', c.valor
        elif c.forma == 'mapa':
            for entrada in c.valor:
                yield c, f'/{c.nombre}/{escapar(entrada.nombre)}', entrada.esquema
        elif c.forma == 'lista':
            for i, sub in enumerate(c.valor):
                yield c, f'/{c.nombre}/{i}', sub


class Tabla:
    """Tabla de símbolos de un esquema.

    esquemas     todos los subesquemas, en el orden del texto (la raíz es el primero)
    simbolos     ruta → esquema
    rutas        id(esquema) → ruta
    anclas       nombre de $anchor → esquema
    referencias  id(esquema que tiene $ref) → esquema destino
    aristas      id(esquema) → [(esquema al que lleva, consume)]: el grafo sobre el que se buscan
                 ciclos. consume es False si el destino se aplica a la misma instancia.
    """

    def __init__(self, raiz, diag: Diagnosticos):
        self.raiz = raiz
        self.esquemas, self.simbolos, self.rutas = [], {}, {}
        self.anclas, self.referencias, self.aristas = {}, {}, {}
        identificador = raiz.clave('$id')
        self._id_raiz = identificador.valor.dato.partition('#')[0] if identificador else None
        self._registrar(raiz, '', diag)
        for esquema in self.esquemas:
            ref = esquema.clave('$ref')
            if ref is not None:
                destino = self._resolver(ref, self.rutas[id(esquema)], diag)
                if destino is not None:
                    self.referencias[id(esquema)] = destino
                    self.aristas[id(esquema)].append((destino, False))

    def ruta(self, esquema):
        return self.rutas[id(esquema)]

    def _registrar(self, esquema, ruta, diag):
        self.esquemas.append(esquema)
        self.rutas[id(esquema)] = ruta
        self.simbolos.setdefault(ruta, esquema)
        vistas = set()
        for c in esquema.claves:
            if c.nombre in vistas:
                diag.error('clave-duplicada', f'la clave «{c.nombre}» está repetida en el mismo esquema', c, ruta)
            vistas.add(c.nombre)
            if c.forma == 'mapa':
                nombres = set()
                for entrada in c.valor:
                    if entrada.nombre in nombres:
                        diag.error('entrada-duplicada', f'«{entrada.nombre}» está repetida en «{c.nombre}»', entrada, ruta)
                    nombres.add(entrada.nombre)
            elif c.nombre == '$anchor':
                nombre = c.valor.dato
                if not _ANCLA.match(nombre):
                    diag.error('ancla-invalida', f'«{nombre}» no es un nombre de ancla válido', c.valor, ruta)
                elif nombre in self.anclas:
                    diag.error('ancla-duplicada', f'el ancla «{nombre}» ya está definida', c.valor, ruta)
                else:
                    self.anclas[nombre] = esquema
        aristas = self.aristas[id(esquema)] = []
        for c, segmento, hijo in hijos(esquema):
            self._registrar(hijo, ruta + segmento, diag)
            if c.nombre not in NO_SE_APLICAN:
                aristas.append((hijo, c.nombre not in EN_SITIO))

    def _resolver(self, clave, ruta, diag):
        """Esquema al que apunta un $ref, o None (y un diagnóstico) si no se puede resolver."""
        ref = clave.valor.dato
        base, _, fragmento = ref.partition('#')
        if base and base != self._id_raiz:
            diag.error('ref-remota', f'«{ref}» apunta a otro documento; solo se admiten referencias locales (#...)',
                       clave.valor, ruta)
            return None
        fragmento = unquote(fragmento)
        if fragmento == '':
            return self.raiz
        if fragmento.startswith('/'):
            destino = self.simbolos.get(fragmento)
            pista = ' (en Draft 2020-12 las definiciones van en «$defs»)' if fragmento.startswith('/definitions/') else ''
        else:
            destino = self.anclas.get(fragmento)
            pista = ''
        if destino is None:
            diag.error('ref-no-resuelta', f'«{ref}» no apunta a ningún esquema del documento{pista}', clave.valor, ruta)
        return destino
