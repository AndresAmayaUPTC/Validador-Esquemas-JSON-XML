"""Generador de payloads a partir del esquema (Fase 5).

Produce documentos de tres clases para alimentar las pruebas del validador generado:

  válidos       se construyen recorriendo los nodos de validación y eligiendo, en cada uno, un valor
                que cumpla sus restricciones;
  inválidos     son un documento válido al que se le rompe una sola cosa (un tipo, un límite, una
                propiedad obligatoria...);
  malformados   son un documento válido con un byte quitado, añadido o cortado.

El resultado esperado de cada caso no se supone: lo calcula el intérprete de referencia, y solo se
conservan los documentos que quedan en la clase que se buscaba.
"""
import json
import random
from dataclasses import dataclass
from decimal import Decimal

from ..automata.compilar import Compilado
from ..automata.formas import construir_formas
from ..automata.interprete import falla, validar
from ..automata.nodos import Nodos
from ..lexico.lexer import Token

MAX_VALORES = 400       # valores por documento: corta las recursiones que no terminan
MAX_LONGITUD = 2000     # no se generan cadenas ni arreglos más largos que esto


@dataclass(frozen=True)
class Caso:
    clase: str          # 'valido', 'invalido' o 'malformado': lo que se buscaba
    payload: bytes
    estado: str         # veredicto del intérprete de referencia
    violaciones: int
    descripcion: str


class _Imposible(Exception):
    """No se encontró un valor que cumpla el nodo."""


SIN_DOCUMENTO = object()   # lo devuelve documento() cuando no encuentra nada; None es un documento válido (null)


def a_json(valor) -> str:
    """Serializa a JSON admitiendo Decimal, para no perder precisión en los números."""
    if isinstance(valor, dict):
        return '{' + ', '.join(f'{json.dumps(k, ensure_ascii=False)}: {a_json(v)}' for k, v in valor.items()) + '}'
    if isinstance(valor, list):
        return '[' + ', '.join(a_json(v) for v in valor) + ']'
    if isinstance(valor, Decimal):
        return str(valor)
    return json.dumps(valor, ensure_ascii=False)


class Generador:
    def __init__(self, compilado: Compilado, semilla=0):
        self.automata = compilado.automata
        self.nodos = Nodos(construir_formas(compilado.analisis))
        self.rng = random.Random(semilla)

    # ---------- valores válidos ----------
    def _numero(self, escalar):
        pruebas = escalar.pruebas
        permitidos = [p[1] for p in pruebas if p[0] == 'enum_num']
        if permitidos:
            candidatos = list(permitidos[0])
        else:
            minimos = [p[1] for p in pruebas if p[0] == 'min']
            maximos = [p[1] for p in pruebas if p[0] == 'max']
            bajo, alto = max(minimos, default=None), min(maximos, default=None)
            candidatos = [Decimal(x) for x in (0, 1, 2, 7, 42, -1, -30)] + [Decimal('0.5'), Decimal('12.25')]
            for limite in (bajo, alto):
                if limite is not None:
                    candidatos += [limite, limite + 1, limite - 1, limite + Decimal('0.5'), limite - Decimal('0.5'),
                                   limite.to_integral_value()]
            if bajo is not None and alto is not None:
                candidatos += [(bajo + alto) / 2, ((bajo + alto) / 2).to_integral_value()]
        validos = [c for c in candidatos if not any(falla(p, Token('NUMERO', 0, 0, str(c))) for p in pruebas)]
        if not validos:
            raise _Imposible
        return self.rng.choice(validos)

    def _cadena(self, escalar):
        pruebas = escalar.pruebas
        permitidas = [set(p[1]) for p in pruebas if p[0] == 'enum_cad']
        if permitidas:
            candidatas = sorted(set.intersection(*permitidas))
        else:
            bajo = max((p[1] for p in pruebas if p[0] == 'long_min'), default=0)
            alto = min((p[1] for p in pruebas if p[0] == 'long_max'), default=bajo + 6)
            if bajo > MAX_LONGITUD:
                raise _Imposible
            largos = {bajo, min(alto, bajo + 3), min(alto, MAX_LONGITUD)}
            candidatas = [''.join(self.rng.choice('abcxyz ñ€😀') for _ in range(n)) for n in sorted(largos) if n >= bajo]
        validas = [c for c in candidatas if not any(falla(p, Token('CADENA', 0, 0, c)) for p in pruebas)]
        if not validas:
            raise _Imposible
        return self.rng.choice(validas)

    def _objeto(self, o, prof):
        nombres = list(o.nombres)
        hijo = dict(zip(o.nombres, o.hijos))
        elegidas = {n for i, n in enumerate(nombres) if o.requeridas >> i & 1}
        opcionales = [n for n in nombres if n not in elegidas]
        if prof < 4:
            elegidas |= {n for n in opcionales if self.rng.random() < 0.5}
        cambio = True
        while cambio:  # cerrar las dependencias: si está la que activa, deben estar las exigidas
            cambio = False
            for bit, exige in o.dependencias:
                if nombres[bit] in elegidas:
                    faltan = {n for i, n in enumerate(nombres) if exige >> i & 1} - elegidas
                    elegidas |= faltan
                    cambio |= bool(faltan)
        minimo = o.min_props or 0
        extras = [n for n in opcionales if n not in elegidas]
        self.rng.shuffle(extras)
        while len(elegidas) < minimo and extras:
            elegidas.add(extras.pop())
        obligatorias = elegidas - set(opcionales)
        resultado = {}
        for n in nombres:
            if n in elegidas:
                try:
                    resultado[n] = self.valor(hijo[n], prof + 1)
                except _Imposible:
                    if n in obligatorias or o.dependencias:
                        raise   # sin ella el objeto no sería válido
        adicionales = 0
        while len(resultado) < minimo or (prof < 3 and adicionales == 0 and self.rng.random() < 0.2):
            if o.max_props is not None and len(resultado) >= o.max_props:
                break
            adicionales += 1
            try:
                resultado[f'extra{adicionales}'] = self.valor(o.otra, prof + 1)
            except _Imposible:
                if len(resultado) < minimo:
                    raise
                break   # el esquema no admite propiedades adicionales: se deja sin ellas
            if adicionales > MAX_LONGITUD:
                raise _Imposible
        return resultado

    def _arreglo(self, r, prof):
        minimo = r.min_items or 0
        if minimo > MAX_LONGITUD:
            raise _Imposible
        largo = minimo if prof >= 4 else minimo + self.rng.randint(0, 3)
        if r.max_items is not None:
            largo = min(largo, r.max_items)
        return [self.valor(r.prefijo[i] if i < len(r.prefijo) else r.resto, prof + 1) for i in range(largo)]

    def valor(self, nodo_id, prof=0):
        """Un valor de Python que cumple el nodo (los números son Decimal)."""
        self._presupuesto -= 1
        if self._presupuesto < 0:
            raise _Imposible
        nodo = self.nodos.lista[nodo_id]
        escalares = [t for t, e in nodo.escalar.items() if e.peso == 0]
        contenedores = [c for c, info in (('objeto', nodo.objeto), ('arreglo', nodo.arreglo)) if not info.prohibido]
        libre = nodo_id == self.nodos.libre
        if libre or (prof >= 5 and escalares):
            contenedores = [] if escalares else contenedores   # cerca del fondo se prefieren los valores simples
        opciones = escalares + contenedores * (1 if libre else 3)
        self.rng.shuffle(opciones)
        for opcion in opciones:
            try:
                if opcion == 'NUMERO':
                    return self._numero(nodo.escalar['NUMERO'])
                if opcion == 'CADENA':
                    return self._cadena(nodo.escalar['CADENA'])
                if opcion == 'objeto':
                    return self._objeto(nodo.objeto, prof)
                if opcion == 'arreglo':
                    return self._arreglo(nodo.arreglo, prof)
                return {'TRUE': True, 'FALSE': False, 'NULL': None}[opcion]
            except _Imposible:
                continue
        raise _Imposible

    def documento(self):
        """Un documento que busca ser válido, como objeto de Python, o SIN_DOCUMENTO si no se encontró."""
        for _ in range(30):
            self._presupuesto = MAX_VALORES
            try:
                return self.valor(self.nodos.raiz)
            except _Imposible:
                continue
        return SIN_DOCUMENTO

    # ---------- mutaciones ----------
    def _romper(self, documento):
        """Copia del documento con un solo cambio, y la descripción del cambio."""
        rng = self.rng
        copia = json.loads(a_json(documento), parse_float=Decimal, parse_int=Decimal)
        lugares = []  # (contenedor, clave, ruta)

        def recorrer(v, ruta):
            for k, x in (v.items() if isinstance(v, dict) else enumerate(v) if isinstance(v, list) else ()):
                lugares.append((v, k, f'{ruta}/{k}'))
                recorrer(x, f'{ruta}/{k}')
        raiz = [copia]
        lugares.append((raiz, 0, ''))
        recorrer(copia, '')
        contenedor, clave, ruta = rng.choice(lugares)
        v = contenedor[clave]
        cambios = [('otro tipo', lambda: rng.choice([x for x in (None, True, Decimal(7), 'zzz', [], {})
                                                     if type(x) is not type(v)]))]
        if isinstance(v, dict):
            if v:
                cambios.append(('sin una propiedad', lambda: {k: x for k, x in v.items() if k != quitada}))
                quitada = rng.choice(sorted(v))
            cambios.append(('con una propiedad de más', lambda: {**v, 'zz_no_declarada': Decimal(1)}))
        elif isinstance(v, list):
            if v:
                cambios.append(('sin el último elemento', lambda: v[:-1]))
            cambios.append(('con elementos de más', lambda: v + [None] * rng.randint(1, 4)))
        elif isinstance(v, Decimal):
            cambios += [('número mayor', lambda: v + 1000), ('número menor', lambda: v - 1000),
                        ('número con decimales', lambda: v + Decimal('0.5'))]
        elif isinstance(v, str):
            cambios += [('cadena más larga', lambda: v + 'x' * 25), ('cadena vacía', lambda: ''),
                        ('otra cadena', lambda: v + '_')]
        descripcion, hacer = rng.choice(cambios)
        contenedor[clave] = hacer()
        return raiz[0], f'{descripcion} en {ruta or "la raíz"}'

    def _deformar(self, datos: bytes):
        rng = self.rng
        m = bytearray(datos)
        i = rng.randint(0, len(m))
        operacion = rng.choice(['quitar', 'insertar', 'cortar', 'utf8'])
        if operacion == 'quitar' and i < len(m):
            del m[i]
            return bytes(m), f'sin el byte {i}'
        if operacion == 'insertar':
            b = rng.choice(b'{}[],:"')
            m.insert(i, b)
            return bytes(m), f'con «{chr(b)}» insertado en el byte {i}'
        if operacion == 'utf8':
            m.insert(i, 0xFF)
            return bytes(m), f'con el byte FF insertado en la posición {i}'
        return bytes(m[:i]), f'cortado en el byte {i}'

    # ---------- casos ----------
    def casos(self, validos=5, invalidos=5, malformados=3):
        """Lista de Caso. Puede devolver menos de los pedidos si el esquema no admite esa clase."""
        salida, vistos = [], set()

        def agregar(clase, payload, descripcion, aceptados):
            r = validar(self.automata, payload)
            if r.estado in aceptados and payload not in vistos:
                vistos.add(payload)
                salida.append(Caso(clase, payload, r.estado, r.violaciones, descripcion))
                return True
            return False

        # Se generan más documentos de los pedidos: los que sobran sirven de base para las mutaciones.
        bases, hechos = [], 0
        for _ in range(max(40, 6 * validos)):
            doc = self.documento()
            if doc is SIN_DOCUMENTO:
                break
            bases.append(doc)
            if hechos < validos:
                hechos += agregar('valido', a_json(doc).encode('utf-8'), 'generado desde el esquema', ('valido', 'pendiente'))
        if not bases:
            bases = [None]  # el esquema no admite nada: se parte de un documento cualquiera

        def roto(doc):
            cambiado, descripcion = self._romper(doc)
            return a_json(cambiado).encode('utf-8'), descripcion

        def deforme(doc):
            return self._deformar(a_json(doc).encode('utf-8'))

        for clase, pedidos, cambiar, aceptados in (('invalido', invalidos, roto, ('invalido',)),
                                                   ('malformado', malformados, deforme, ('malformado',))):
            hechos = 0
            for _ in range(60 * pedidos):
                if hechos >= pedidos:
                    break
                payload, descripcion = cambiar(self.rng.choice(bases))
                hechos += agregar(clase, payload, descripcion, aceptados)
        return salida


def generar_casos(compilado: Compilado, validos=5, invalidos=5, malformados=3, semilla=0):
    return Generador(compilado, semilla).casos(validos, invalidos, malformados)
