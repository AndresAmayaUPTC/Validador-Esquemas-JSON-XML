"""Análisis semántico del esquema (Fase 3): reúne la tabla de símbolos, el metaesquema, los ciclos y
la clasificación en un solo resultado.

    python -m compilador.semantico.analisis esquema.json     # informe completo

No se detiene en el primer problema: recoge todos los diagnósticos. Las fases siguientes solo
trabajan con un análisis sin errores (Analisis.ok).
"""
from dataclasses import dataclass, field

from ..sintactico.ast_esquema import Esquema, analizar_esquema
from . import ciclos, metaesquema
from .clasificacion import NIVELES, Uso, clasificar, nivel
from .simbolos import Diagnosticos, Tabla


@dataclass
class Analisis:
    raiz: Esquema
    tabla: Tabla
    diagnosticos: list
    tipos: dict            # id(esquema) → tipos de JSON que puede aceptar
    grupos: list           # grupos de esquemas mutuamente recursivos (listas de Esquema)
    usos: list             # clasificación: un Uso por cada palabra clave del esquema
    _grupo: dict = field(default_factory=dict, repr=False)

    @property
    def errores(self):
        return [d for d in self.diagnosticos if d.severidad == 'error']

    @property
    def avisos(self):
        return [d for d in self.diagnosticos if d.severidad == 'aviso']

    @property
    def ok(self):
        return not self.errores

    @property
    def nivel(self):
        """'a', 'b', 'c' o None: la categoría más exigente que usa el esquema."""
        return nivel(self.usos)

    def es_recursivo(self, esquema):
        return id(esquema) in self._grupo

    def es_llamada_recursiva(self, esquema):
        """True si el $ref de este esquema vuelve a su propio grupo recursivo: necesita la pila."""
        destino = self.tabla.referencias.get(id(esquema))
        return destino is not None and self._grupo.get(id(esquema), -1) == self._grupo.get(id(destino), -2)

    def informe(self, fuente: bytes = None) -> str:
        """Informe legible. Con el texto del esquema, las posiciones se dan como línea:columna."""
        def donde(pos):
            if fuente is None:
                return f'byte {pos}'
            return f'{fuente.count(bytes([10]), 0, pos) + 1}:{pos - fuente.rfind(bytes([10]), 0, pos)}'

        nombre = lambda ruta: ruta or '(raíz)'
        lineas = [f'DIAGNÓSTICOS: {len(self.errores)} errores, {len(self.avisos)} avisos']
        lineas += [f'  {d.severidad:5}  {donde(d.inicio)}  {d.codigo}: {d.mensaje}  [{nombre(d.ruta)}]'
                   for d in self.diagnosticos]
        lineas += ['', f'TABLA DE SÍMBOLOS: {len(self.tabla.simbolos)} esquemas']
        for ruta, e in self.tabla.simbolos.items():
            tipos = 'true' if e.booleano is True else 'false' if e.booleano is False else \
                ', '.join(sorted(self.tipos[id(e)])) or 'ninguno'
            lineas.append(f'  {nombre(ruta):40}  {donde(e.inicio):>9}  {"recursivo" if self.es_recursivo(e) else "":9}  tipos: {tipos}')
        lineas += ['', f'REFERENCIAS: {len(self.tabla.referencias)}']
        for e in self.tabla.esquemas:
            destino = self.tabla.referencias.get(id(e))
            if destino is not None:
                clase = 'llamada recursiva' if self.es_llamada_recursiva(e) else 'expansión'
                lineas.append(f'  {nombre(self.tabla.ruta(e)):40}  →  {nombre(self.tabla.ruta(destino)):24}  {clase}')
        lineas += ['', 'CLASIFICACIÓN DE PALABRAS CLAVE']
        resumen = {}
        for u in self.usos:
            resumen[u.palabra, u.categoria, u.estrategia] = resumen.get((u.palabra, u.categoria, u.estrategia), 0) + 1
        orden = ('a', 'b', 'c', 'anotacion', 'desconocida', 'no-soportada')
        for (palabra, categoria, estrategia), n in sorted(resumen.items(), key=lambda x: (orden.index(x[0][1]), x[0][0])):
            etiqueta = f'({categoria})' if len(categoria) == 1 else categoria
            lineas.append(f'  {etiqueta:12}  {palabra:22}  ×{n:<3}  {estrategia}')
        n = len(self.grupos)
        lineas += ['', f'NIVEL: {NIVELES[self.nivel]}',
                   f'RECURSIÓN: {n} grupo{"s" if n > 1 else ""} recursivo{"s" if n > 1 else ""}' if n
                   else 'RECURSIÓN: ninguna; la profundidad del documento la acota el propio esquema']
        return '\n'.join(lineas)


def analizar(raiz: Esquema) -> Analisis:
    diag = Diagnosticos()
    tabla = Tabla(raiz, diag)
    metaesquema.verificar(tabla, diag)

    for ciclo in ciclos.ciclos_no_productivos(tabla):
        # un ciclo así pasa siempre por algún $ref: el diagnóstico se ancla en el primero
        origen = next(e for e in ciclo if e.clave('$ref') is not None)
        rutas = ' → '.join(tabla.ruta(e) or '#' for e in ciclo)
        diag.error('ciclo-de-referencias', f'referencia circular que no consume entrada: {rutas}',
                   origen.clave('$ref'), tabla.ruta(origen))

    tipos = ciclos.calcular_tipos(tabla)
    grupos = ciclos.recursiones(tabla)
    grupo = {id(e): i for i, miembros in enumerate(grupos) for e in miembros}
    productivo = ciclos.calcular_productivos(tabla, tipos)
    en_ciclo_vacio = {id(e) for ciclo in ciclos.ciclos_no_productivos(tabla) for e in ciclo}
    for miembros in grupos:
        sin_instancias = [e for e in miembros if not productivo[id(e)]]
        if sin_instancias and not any(id(e) in en_ciclo_vacio for e in miembros):
            e = sin_instancias[0]
            diag.error('recursion-sin-caso-base', 'ningún documento finito puede cumplir este esquema recursivo',
                       e, tabla.ruta(e))

    usos = clasificar(tabla, tipos, grupo)
    diag.sort(key=lambda d: (d.inicio, d.severidad))
    return Analisis(raiz, tabla, list(diag), tipos, grupos, usos, grupo)


def analizar_fuente(fuente: bytes) -> Analisis:
    """Lexer + parser + análisis semántico. Lanza ErrorLexico o ErrorSintactico si el texto no es un esquema."""
    return analizar(analizar_esquema(fuente))


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    with open(sys.argv[1], 'rb') as f:
        contenido = f.read()
    resultado = analizar_fuente(contenido)
    print(resultado.informe(contenido))
    sys.exit(0 if resultado.ok else 1)
