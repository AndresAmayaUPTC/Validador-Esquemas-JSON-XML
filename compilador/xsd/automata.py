"""Autómata de pila determinista ponderado para XML (Fase 4, XML).

Es el mismo M = (Q, Σ, Γ, δ, q0, Z0, F, w) de compilador/automata/dpda.py, con otros estados y otro alfabeto:

  Σ   los tokens del lexer de XML. Tres se refinan:
        NOMBRE   según dónde aparece: ('ELEM', n) al abrir un elemento, ('ATRIB', n) en un atributo y
                 ('CIERRE', n) en la etiqueta de cierre; (…, None) es «cualquier otro nombre».
                 ATRIB_NS son los atributos xmlns:* y xsi:*, que no forman parte del contenido.
        TEXTO    se separa en ESPACIOS (solo espacios en blanco) y TEXTO.
  Q   Un nodo es una pareja (nombre de elemento, tipo). Por cada nodo n:
        A      dentro de la etiqueta de apertura, esperando un atributo, «>» o «/>»
        =, VA  esperando «=» y el valor de un atributo (una pareja por atributo declarado)
        C.d    en el contenido, con el autómata del modelo de contenido en el estado d
        CO.d   tras «<», esperando el nombre del hijo
        T, TX  en un elemento de contenido simple: antes del texto, y después
        TO     tras «<» dentro de un contenido simple (ningún hijo está permitido)
      Compartidos: CN:nombre espera el nombre de la etiqueta de cierre, CF su «>», y D0, D1, D2, DO, DF,
      DF2, ACEPTA recorren lo que rodea al elemento raíz.
  Γ   Z0 y un símbolo E por cada (nodo, estado del modelo de contenido): dice a qué estado se vuelve
      cuando se cierra el hijo que está encima. Al abrir un hijo, el símbolo del padre se reescribe
      con el estado siguiente de su modelo de contenido.
  w   una violación por cada hijo no permitido (que se salta entero con el nodo libre), texto donde no
      puede haberlo, atributo no declarado u obligatorio que falta, contenido incompleto al cerrar y
      prueba de valor que no se cumple.

Registros de cada marco: la máscara de atributos vistos y el número de hijos (para la ruta del error).

Que el nombre de la etiqueta de cierre sea el de la apertura lo comprueba δ: el estado CN:nombre solo
tiene transición para ese nombre. En los elementos que el esquema no conoce (los que se saltan y los de
tipo anyType) el nombre es arbitrario y no puede estar en δ: ahí el marco guarda la longitud y una
huella de 64 bits del nombre de apertura, y la acción «cierre_libre» las compara.
"""
from ..automata.dpda import Automata, Transicion
from ..automata.formas import COSTO_FIJO, costo
from .modelo import AnalisisXSD, pruebas_lexicas

TOKENS_XML = ('DECL_XML', 'ETIQ_ABRE', 'ETIQ_CIERRE_ABRE', 'NOMBRE', 'IGUAL', 'VALOR_ATRIB', 'ETIQ_FIN',
              'ETIQ_AUTOCIERRE', 'TEXTO', 'ESPACIOS', 'ATRIB_NS', 'FIN')
ACCIONES_XML = {
    'atributo': '(bit): marca el atributo; repetido = mal formado',
    'cerrar_atributos': '(máscara requerida): suma los atributos obligatorios que faltan',
    'hijo': '(): cuenta un hijo elemento',
    'cierre_libre': '(): el nombre de cierre debe tener la huella del de apertura; si no, mal formado',
}
COSTO_FIJO.update(xs_entero=0, xs_decimal=0, xs_booleano=0)   # la forma léxica va siempre primero


def _pruebas(simple):
    return tuple(sorted(pruebas_lexicas(simple), key=costo))


def construir_xml(an: AnalisisXSD) -> Automata:
    from .interprete import fallos   # las pruebas del valor vacío se evalúan al compilar
    a = Automata()
    diferidas = set()
    z0 = a.pila_inicial = a.simbolo('Z0', 'Z')
    d0, d1, d2, do, df, df2 = (a.estado(n) for n in ('D0', 'D1', 'D2', 'DO', 'DF', 'DF2'))
    acepta, cf = a.estado('ACEPTA'), a.estado('CF')
    a.inicial, a.finales, a.retorno[z0] = d0, frozenset([acepta]), df
    a.delta[d0].update(DECL_XML=Transicion(d1), ESPACIOS=Transicion(d2), ETIQ_ABRE=Transicion(do))
    a.delta[d1].update(ESPACIOS=Transicion(d2), ETIQ_ABRE=Transicion(do))
    a.delta[d2].update(ETIQ_ABRE=Transicion(do))
    a.delta[df].update(ESPACIOS=Transicion(df2), FIN=Transicion(acepta))
    a.delta[df2].update(FIN=Transicion(acepta))
    a.delta[cf]['ETIQ_FIN'] = Transicion(desapila=True)

    cierres = {}

    def cierre(nombre):
        """Estado que espera el nombre de la etiqueta de cierre."""
        if nombre not in cierres:
            cierres[nombre] = q = a.estado('CN:' + (nombre if nombre is not None else '*'))
            a.delta[q]['CIERRE', nombre] = Transicion(cf, accion=() if nombre is not None else ('cierre_libre',))
        return cierres[nombre]

    # ---- nodos: (nombre, tipo) → estado A y símbolo de pila inicial ----
    nodos, pendientes, entrada, simbolo = {}, [], [], []

    def nodo(nombre, tipo):
        clave = (nombre, id(tipo))
        if clave not in nodos:
            nodos[clave] = i = len(entrada)
            entrada.append(a.estado(f'A{i}'))
            simbolo.append(None)
            pendientes.append((i, nombre, tipo))
        return nodos[clave]

    libre = nodo(None, an.libre)
    for nombre, tipo in an.elementos.items():
        nodo(nombre, tipo)
    for i, nombre, tipo in pendientes:   # la lista crece mientras se recorre
        es_libre = tipo.contenido == 'libre'
        A = entrada[i]
        otro_igual, otro_valor = a.estado(f'=A{i}'), a.estado(f'VA{i}')
        a.delta[otro_igual]['IGUAL'] = Transicion(otro_valor)
        a.delta[otro_valor]['VALOR_ATRIB'] = Transicion(A)
        a.delta[A]['ATRIB_NS'] = Transicion(otro_igual)
        a.delta[A]['ATRIB', None] = Transicion(otro_igual, peso=0 if es_libre else 1)
        requeridas = 0
        for bit, (nombre_a, atributo) in enumerate(sorted(tipo.atributos.items())):
            igual, valor = a.estado(f'=A{i}.{nombre_a}'), a.estado(f'VA{i}.{nombre_a}')
            requeridas |= atributo.requerido << bit
            diferidas.update((ruta, 'pattern') for ruta in atributo.simple.patrones)
            a.delta[A]['ATRIB', nombre_a] = Transicion(igual, accion=('atributo', bit))
            a.delta[igual]['IGUAL'] = Transicion(valor)
            a.delta[valor]['VALOR_ATRIB'] = Transicion(A, pruebas=_pruebas(atributo.simple),
                                                       pendientes=len(atributo.simple.patrones))
        cerrar = ('cerrar_atributos', requeridas) if requeridas else ()
        fin = cierre(nombre)

        if tipo.contenido == 'simple':
            pruebas, n_patrones = _pruebas(tipo.simple), len(tipo.simple.patrones)
            diferidas.update((ruta, 'pattern') for ruta in tipo.simple.patrones)
            vacio = len(fallos(pruebas, ''))
            T, TX, TO = a.estado(f'T{i}'), a.estado(f'TX{i}'), a.estado(f'TO{i}')
            simbolo[i] = a.simbolo(f'E{i}', 'E', TX)
            leer = Transicion(TX, pruebas=pruebas, pendientes=n_patrones)
            a.delta[T].update(TEXTO=leer, ESPACIOS=leer, ETIQ_ABRE=Transicion(TO),
                              ETIQ_CIERRE_ABRE=Transicion(fin, peso=vacio, pendientes=n_patrones))
            a.delta[TX].update(TEXTO=Transicion(TX), ESPACIOS=Transicion(TX), ETIQ_ABRE=Transicion(TO),
                               ETIQ_CIERRE_ABRE=Transicion(fin))
            a.delta[TO]['ELEM', None] = Transicion(entrada[libre], accion=('hijo',), peso=1, apila=simbolo[libre])
            a.delta[A].update(ETIQ_FIN=Transicion(T, accion=cerrar),
                              ETIQ_AUTOCIERRE=Transicion(desapila=True, accion=cerrar, peso=vacio, pendientes=n_patrones))
            continue

        # contenido de elementos (o libre: un solo estado que admite cualquier hijo)
        afd = tipo.afd
        estados = range(1 if es_libre else len(afd.acepta))
        C = [a.estado(f'C{i}.{d}') for d in estados]
        CO = [a.estado(f'CO{i}.{d}') for d in estados]
        E = [a.simbolo(f'E{i}.{d}', 'E', C[d]) for d in estados]
        simbolo[i] = E[0]
        sin_hijos = not es_libre and afd.vacio()
        for d in estados:
            completo = es_libre or afd.acepta[d]
            a.delta[C[d]].update(
                ESPACIOS=Transicion(C[d], peso=int(sin_hijos)),   # un elemento de contenido vacío no admite ni espacios
                TEXTO=Transicion(C[d], peso=0 if es_libre else 1),
                ETIQ_ABRE=Transicion(CO[d]),
                ETIQ_CIERRE_ABRE=Transicion(fin, peso=0 if completo else 1))
            a.delta[CO[d]]['ELEM', None] = Transicion(entrada[libre], accion=('hijo',), peso=0 if es_libre else 1,
                                                      apila=simbolo[libre])
            if not es_libre:
                for hijo, d2 in afd.delta[d].items():
                    h = nodo(hijo, afd.tipo_de[hijo])
                    a.delta[CO[d]]['ELEM', hijo] = Transicion(entrada[h], accion=('hijo',),
                                                              cima=E[d2] if d2 != d else None, apila=('nodo', h))
        completo = es_libre or afd.acepta[0]
        a.delta[A].update(ETIQ_FIN=Transicion(C[0], accion=cerrar),
                          ETIQ_AUTOCIERRE=Transicion(desapila=True, accion=cerrar, peso=0 if completo else 1))

    # los símbolos de pila de los hijos se conocen al final: se sustituyen las marcas ('nodo', h)
    from dataclasses import replace
    for transiciones in a.delta:
        for s, t in transiciones.items():
            if isinstance(t.apila, tuple):
                transiciones[s] = replace(t, apila=simbolo[t.apila[1]])
    for nombre, tipo in an.elementos.items():
        n = nodos[nombre, id(tipo)]
        a.delta[do]['ELEM', nombre] = Transicion(entrada[n], apila=simbolo[n])
    a.delta[do]['ELEM', None] = Transicion(entrada[libre], peso=1, apila=simbolo[libre])
    a.diferidas = sorted(diferidas)
    return a
