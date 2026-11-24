"""Del texto del esquema al autómata verificado y minimizado, y su presentación (Fase 4).

    python -m compilador.automata esquema.json                 # definición formal de M y tabla δ
    python -m compilador.automata esquema.json --dot           # diagrama en Graphviz
    python -m compilador.automata esquema.json payload.json    # además valida el payload
"""
from dataclasses import dataclass

from ..semantico.analisis import Analisis, analizar_fuente
from .dpda import RETORNO, Automata, construir
from .verificacion import Verificacion, minimizar, verificar


class ErrorDeEsquema(Exception):
    """El esquema tiene errores semánticos. analisis conserva todos los diagnósticos."""

    def __init__(self, analisis):
        super().__init__('; '.join(f'{d.codigo}: {d.mensaje}' for d in analisis.errores))
        self.analisis = analisis


@dataclass
class Compilado:
    automata: Automata           # minimizado
    verificacion: Verificacion   # del autómata minimizado
    analisis: Analisis
    sin_minimizar: dict          # tamaño del autómata antes de minimizar
    tamano: dict                 # tamaño después, con las transiciones de δ expandida

    @property
    def xml(self) -> bool:
        """True si el autómata es de XML (viene de un esquema XSD)."""
        return any('ETIQ_ABRE' in transiciones for transiciones in self.automata.delta)


def compilar(fuente: bytes) -> Compilado:
    """Lexer → parser → análisis semántico → construcción → verificación → minimización.
    Lanza ErrorLexico, ErrorSintactico o ErrorDeEsquema."""
    analisis = analizar_fuente(fuente)
    if not analisis.ok:
        raise ErrorDeEsquema(analisis)
    return ensamblar(analisis, construir(analisis))


def ensamblar(analisis, bruto: Automata) -> Compilado:
    """Verifica y minimiza el autómata recién construido (de JSON o de XML)."""
    v = verificar(bruto)
    if not v.ok:  # sería un defecto del compilador, no del esquema
        raise AssertionError('el autómata construido no es un DPDA válido: ' + '; '.join(v.problemas))
    automata = minimizar(bruto)
    v = verificar(automata)
    assert v.ok, v.problemas
    antes = dict(bruto.tamano(), alcanzables=len(verificar(bruto).alcanzables))
    return Compilado(automata, v, analisis, antes, dict(automata.tamano(), transiciones_expandidas=v.transiciones_expandidas))


_REFINADO = {'CLAVE': ('clave', 'otra clave'), 'ELEM': ('elemento', 'otro elemento'),
             'ATRIB': ('atributo', 'otro atributo'), 'CIERRE': ('cierre', 'cierre de otro nombre')}


# cómo se muestra cada grupo de sub-ejecuciones
NOMBRE_GRUPO = {'alguna': 'anyOf', 'una': 'oneOf', 'no': 'not', 'si': 'if/then/else', 'dependiente': 'dependentSchemas',
                'nombres': 'propertyNames', 'contiene': 'contains', 'cumple': 'contains'}


def _simbolo(s):
    if isinstance(s, str):
        return s
    nombre, otro = _REFINADO[s[0]]
    if isinstance(s[1], frozenset):
        return 'otra clave que casa con ' + ', '.join(f'/{p}/' for p in sorted(s[1]))
    return otro if s[1] is None else f'{nombre} "{s[1]}"'


def _prueba(p):
    clase = p[0]
    if clase == 'entero':
        return 'es entero'
    if clase.startswith('xs_'):
        return 'tiene forma de xs:' + {'xs_entero': 'integer', 'xs_decimal': 'decimal', 'xs_booleano': 'boolean'}[clase]
    if clase in ('min', 'max'):
        return {('min', False): '≥', ('min', True): '>', ('max', False): '≤', ('max', True): '<'}[clase, p[2]] + f' {p[1]}'
    if clase in ('long_min', 'long_max'):
        return f'longitud {"≥" if clase == "long_min" else "≤"} {p[1]}'
    if clase == 'patron' and isinstance(p[1], tuple):   # XSD: el valor completo, alguna de las alternativas
        return 'es ' + ' o '.join(f'/{x}/' for x in p[1]) + (' (sin espacios a los lados)' if p[2] else '')
    if clase == 'patron':
        return f'casa con /{p[1]}/'
    if clase == 'multiplo':
        return f'múltiplo de {p[1]}'
    if clase == 'enum_huella':
        return f'huella ∈ {{{len(p[1])} valores}}'
    if clase == 'unicos':
        return 'elementos únicos'
    if clase == 'contiene':
        return f'contains entre {p[1]} y {"∞" if p[2] is None else p[2]}'
    return '∈ {' + ', '.join(repr(x) if isinstance(x, str) else str(x) for x in p[1]) + '}'


def _accion(accion):
    clase, *p = accion
    if clase == 'clave':
        partes = ([f'bit {p[0]}'] if p[0] is not None else []) + ([f'máx {p[1]}'] if p[1] is not None else [])
    elif clase == 'cerrar_objeto':
        partes = ([f'requiere {p[0]:#b}'] if p[0] else []) + [f'bit {b} exige {m:#b}' for b, m in p[1]] \
                 + ([f'mín {p[2]}'] if p[2] is not None else [])
    elif clase == 'atributo':
        partes = [f'bit {p[0]}']
    elif clase == 'cerrar_atributos':
        partes = [f'requiere {p[0]:#b}']
    elif clase in ('hijo', 'cierre_libre'):
        partes = []
    else:
        partes = [f'{"máx" if clase == "elemento" else "mín"} {p[0]}'] if p[0] is not None else []
    return f'{clase}({", ".join(partes)})'


def _efecto(a, t):
    """Texto de una transición: destino, operación de pila, acción, peso y pruebas."""
    partes = ['↩ retorno' if t.destino is RETORNO else a.estados[t.destino]]
    pila = []
    if t.desapila:
        pila.append('desapila')
    if t.cima is not None:
        pila.append(f'cima:={a.simbolos_pila[t.cima]}')
    if t.apila is not None:
        pila.append(f'apila {a.simbolos_pila[t.apila]}')
    partes.append(', '.join(pila) or '—')
    extra = []
    if t.accion:
        extra.append(_accion(t.accion))
    if t.peso:
        extra.append(f'w={t.peso}')
    extra += [_prueba(p) for p in t.pruebas]
    if t.pendientes:
        extra.append(f'diferidas={t.pendientes}')
    for clase, inicios in t.hilos:
        ramas = ', '.join('—' if q is None else a.estados[q] for q in inicios)
        extra.append(f'nacen {NOMBRE_GRUPO[clase]}({ramas})')
    if t.recoger:
        extra.append('guarda huellas')
    return partes, '; '.join(extra)


def filas_delta(c: Compilado, limite=None) -> list:
    """La tabla δ como filas {estado, simbolo, destino, pila, efecto}, para mostrarla en una interfaz."""
    a, filas = c.automata, []
    for q, transiciones in enumerate(a.delta):
        for s, tr in transiciones.items():
            if limite is not None and len(filas) >= limite:
                return filas
            (destino, pila), extra = _efecto(a, tr)
            filas.append({'estado': a.estados[q], 'simbolo': _simbolo(s), 'destino': destino, 'pila': pila, 'efecto': extra})
    return filas


def describir(c: Compilado, con_delta=True) -> str:
    a, t = c.automata, c.tamano
    antes = c.sin_minimizar
    lineas = [
        'M = (Q, Σ, Γ, δ, q0, Z0, F, w)',
        f'  |Q| = {t["estados"]} estados   (antes de minimizar: {antes["estados"]}, de ellos {antes["alcanzables"]} alcanzables)',
        f'  |Σ| = {t["simbolos_de_entrada"]} símbolos de entrada',
        f'  |Γ| = {t["simbolos_de_pila"]} símbolos de pila   (antes: {antes["simbolos_de_pila"]})',
        f'  |δ| = {t["transiciones"]} transiciones; {t["transiciones_expandidas"]} en la forma δ(q, a, Z)',
        f'  q0 = {a.estados[a.inicial]},  Z0 = {a.simbolos_pila[a.pila_inicial]},  '
        f'F = {{{", ".join(a.estados[q] for q in sorted(a.finales))}}}',
        '  w: violaciones que aporta cada transición (semianillo tropical: se suman a lo largo de la ejecución)',
        '  Determinista y sin transiciones ε. ' + ('Verificado.' if c.verificacion.ok else 'CON PROBLEMAS.'),
    ]
    if a.diferidas:
        lineas += ['', 'VERIFICACIÓN DIFERIDA (el autómata no comprueba estas palabras clave)']
        lineas += [f'  {palabra:22} en {ruta or "(raíz)"}' for ruta, palabra in a.diferidas]
    lineas += ['', 'RETORNO: estado al que se vuelve según la cima de la pila']
    lineas += [f'  {a.simbolos_pila[z]:8} → {a.estados[q] if q is not None else "—"}' for z, q in enumerate(a.retorno)]
    if con_delta:
        lineas += ['', 'δ: estado, símbolo de entrada → destino | pila | acción, peso y pruebas']
        for q, transiciones in enumerate(a.delta):
            for s, tr in transiciones.items():
                (destino, pila), extra = _efecto(a, tr)
                lineas.append(f'  {a.estados[q]:8} {_simbolo(s):22} → {destino:10} | {pila:22} | {extra}')
    return '\n'.join(lineas)


# Colores del diagrama: identidad UPTC sobre una lámina clara (negro institucional, amarillo para el estado inicial)
_TINTA, _SUAVE, _CLARO, _AMARILLO, _PAPEL = '#1a1a1a', '#6b675d', '#a39e8f', '#ffc72c', '#faf9f4'


def _escapar_dot(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _etiqueta_dot(pares) -> str:
    """Une en una línea los símbolos con el mismo efecto («CADENA, NUMERO, NULL [w=1]») y alinea las líneas a la
    izquierda (\\l de Graphviz)."""
    grupos = {}
    for simbolo, sufijo in pares:
        grupos.setdefault(sufijo, []).append(simbolo)
    return ''.join(_escapar_dot(', '.join(simbolos) + sufijo) + '\\l' for sufijo, simbolos in grupos.items())


def a_dot(c: Compilado, agrupar_retornos=True) -> str:
    """Diagrama del autómata en DOT. Las aristas de retorno van discontinuas. Con agrupar_retornos, todas llegan a
    un único nodo «↩» del que sale una arista por cada cima de la pila (la tabla RETORNO); sin él, cada retorno se
    dibuja hacia todos los estados a los que puede volver. Los atributos class sirven a la interfaz web."""
    a, v = c.automata, c.verificacion
    lineas = ['digraph M {',
              '  rankdir=LR; bgcolor="transparent"; pad=0.3; nodesep=0.32; ranksep=0.55;',
              f'  node [fontname="Helvetica", fontsize=10, shape=circle, style=filled, fillcolor="white", '
              f'color="{_TINTA}", fontcolor="{_TINTA}", penwidth=1.3, width=0.5, margin=0.03];',
              f'  edge [fontname="Helvetica", fontsize=8, color="{_SUAVE}", fontcolor="#3d3a33", arrowsize=0.6];',
              f'  ini [shape=point, width=0.09, class="inicio"];',
              f'  ini -> q{a.inicial} [color="{_TINTA}", penwidth=1.3, class="inicio"];']
    for q, nombre in enumerate(a.estados):
        atributos, clases = [f'label="{_escapar_dot(nombre)}"'], ['estado']
        if q in a.finales:
            atributos.append('shape=doublecircle')
            clases.append('final')
        if q == a.inicial:
            atributos.append(f'fillcolor="{_AMARILLO}"')
            clases.append('inicial')
        lineas.append(f'  q{q} [{", ".join(atributos)}, class="{" ".join(clases)}"];')

    # Los retornos y las transiciones que solo suman violaciones (w > 0) no cuentan para la disposición
    # (constraint=false): así el esqueleto del esquema —lo que se recorre con un documento válido— queda ordenado
    # de izquierda a derecha, y los caminos de error no lo enredan.
    retorno_dashed = f'style=dashed, color="{_CLARO}", fontcolor="{_SUAVE}", constraint=false, class="retorno"'
    cimas_usadas = set()
    for q, transiciones in enumerate(a.delta):
        agrupadas = {}  # (destino, ¿retorno?) → [(símbolo, sufijo)]
        con_peso = {}   # (destino, ¿retorno?) → ¿todas sus transiciones suman violaciones?
        for s, t in transiciones.items():
            (_, pila), extra = _efecto(a, t)
            par = (_simbolo(s), ('' if pila == '—' else f' / {pila}') + (f' [{extra}]' if extra else ''))
            if t.destino is not RETORNO:
                claves = [(f'q{t.destino}', False)]
            else:
                cimas = v.cimas[q] if not t.desapila else set().union(*(v.debajo[z] for z in v.cimas[q]))
                cimas = {z for z in cimas if a.retorno[z] is not None}
                cimas_usadas |= cimas
                claves = ([('ret', True)] if agrupar_retornos
                          else [(f'q{destino}', True) for destino in sorted({a.retorno[z] for z in cimas})])
            for clave in claves:
                agrupadas.setdefault(clave, []).append(par)
                con_peso[clave] = con_peso.get(clave, True) and bool(t.peso)
        for clave, pares in agrupadas.items():
            destino, es_retorno = clave
            if not es_retorno and con_peso[clave]:
                # caminos de error: sin etiqueta visible (el color ya lo dice); el detalle va en el tooltip
                detalle = _escapar_dot('\n'.join(s + sufijo for s, sufijo in pares))
                lineas.append(f'  q{q} -> {destino} [tooltip="{detalle}", color="#d3a29d", constraint=false, '
                              'class="violacion"];')
                continue
            estilo = retorno_dashed if es_retorno else 'class="avanza"'
            lineas.append(f'  q{q} -> {destino} [label="{_etiqueta_dot(pares)}", {estilo}];')

    if agrupar_retornos and cimas_usadas:
        lineas.append(f'  ret [label="↩", shape=diamond, width=0.36, height=0.36, fixedsize=true, fontsize=12, '
                      f'fillcolor="{_PAPEL}", color="{_SUAVE}", fontcolor="{_SUAVE}", class="punto-retorno"];')
        por_destino = {}
        for z in sorted(cimas_usadas):
            por_destino.setdefault(a.retorno[z], []).append(a.simbolos_pila[z])
        for destino, cimas in por_destino.items():
            lineas.append(f'  ret -> q{destino} [label="{_escapar_dot("cima " + ", ".join(cimas))}", {retorno_dashed}];')
    return '\n'.join(lineas + ['}'])
