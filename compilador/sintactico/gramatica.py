"""Gramáticas libres de contexto y análisis LL(1) (Fase 2).

Una gramática se escribe como texto, una regla por línea:

    valor  → objeto | arreglo | CADENA
    objeto → LLAVE_ABRE miembros LLAVE_CIERRA
    miembros → par mas_pares | ε

Convenciones: los terminales van en MAYÚSCULAS (tipos de token) o entre comillas ("type", una palabra
clave); todo lo demás es un no terminal. El símbolo inicial es la cabeza de la primera regla. Una línea
que empieza por «|» continúa la regla anterior.

El fin de entrada no se añade aparte: el lexer entrega el token FIN, así que la primera regla de cada
gramática termina en FIN.
"""
EPSILON = 'ε'


class ConflictoLL1(Exception):
    """La gramática no es LL(1). conflictos: lista de (no terminal, terminal, [producciones])."""

    def __init__(self, gramatica, conflictos):
        lineas = [f'M[{A}, {a}]: ' + '  /  '.join(gramatica.texto_produccion(i) for i in ps)
                  for A, a, ps in conflictos]
        super().__init__('la gramática no es LL(1):\n' + '\n'.join(lineas))
        self.conflictos = conflictos


def es_terminal(simbolo):
    return simbolo.startswith('"') or simbolo.isupper()


class Gramatica:
    def __init__(self, texto):
        self.producciones = []  # (cabeza, cuerpo); el cuerpo vacío es ε
        cabeza = None
        for linea in texto.splitlines():
            linea = linea.strip()
            if not linea:
                continue
            if linea.startswith('|'):
                assert cabeza, 'una regla no puede empezar por «|»'
                alternativas = linea[1:]
            else:
                cabeza, alternativas = (p.strip() for p in linea.replace('->', '→').split('→'))
            for alternativa in alternativas.split('|'):
                simbolos = alternativa.split()
                if not simbolos:
                    raise ValueError(f'alternativa vacía en la regla de {cabeza}: use {EPSILON}')
                self.producciones.append((cabeza, tuple(s for s in simbolos if s != EPSILON)))

        self.no_terminales = list(dict.fromkeys(c for c, _ in self.producciones))
        self.inicial = self.no_terminales[0]
        cabezas = set(self.no_terminales)
        self.terminales = sorted({s for _, cuerpo in self.producciones for s in cuerpo if s not in cabezas})
        for s in self.no_terminales + self.terminales:
            if (s in cabezas) == es_terminal(s):
                raise ValueError(f'«{s}» no tiene producciones' if s not in cabezas else f'«{s}» parece un terminal')
        self._nt = cabezas
        self.primeros = self._calcular_primeros()
        self.siguientes = self._calcular_siguientes()

    # ---------- PRIMEROS y SIGUIENTES ----------
    def primeros_de(self, secuencia):
        """PRIMEROS de una cadena de símbolos; contiene ε si toda la cadena puede derivar ε."""
        resultado = set()
        for s in secuencia:
            if s not in self._nt:
                resultado.add(s)
                return resultado
            resultado |= self.primeros[s] - {EPSILON}
            if EPSILON not in self.primeros[s]:
                return resultado
        resultado.add(EPSILON)
        return resultado

    def _calcular_primeros(self):
        self.primeros = {A: set() for A in self.no_terminales}
        cambio = True
        while cambio:  # punto fijo
            cambio = False
            for A, cuerpo in self.producciones:
                nuevos = self.primeros_de(cuerpo) - self.primeros[A]
                if nuevos:
                    self.primeros[A] |= nuevos
                    cambio = True
        return self.primeros

    def _calcular_siguientes(self):
        siguientes = {A: set() for A in self.no_terminales}
        cambio = True
        while cambio:
            cambio = False
            for A, cuerpo in self.producciones:
                for i, B in enumerate(cuerpo):
                    if B not in self._nt:
                        continue
                    resto = self.primeros_de(cuerpo[i + 1:])
                    nuevos = resto - {EPSILON}
                    if EPSILON in resto:  # lo que sigue a A también sigue a B
                        nuevos |= siguientes[A]
                    if not nuevos <= siguientes[B]:
                        siguientes[B] |= nuevos
                        cambio = True
        return siguientes

    # ---------- tabla LL(1) ----------
    def celdas(self):
        """{(A, a): [producciones]}. La producción A → α va en M[A, a] si a ∈ PRIMEROS(α), o si
        α puede derivar ε y a ∈ SIGUIENTES(A)."""
        celdas = {}
        for i, (A, cuerpo) in enumerate(self.producciones):
            primeros = self.primeros_de(cuerpo)
            terminales = primeros - {EPSILON}
            if EPSILON in primeros:
                terminales |= self.siguientes[A]
            for a in terminales:
                celdas.setdefault((A, a), []).append(i)
        return celdas

    def conflictos(self):
        """Celdas con más de una producción; la gramática es LL(1) si y solo si no hay ninguna."""
        return sorted((A, a, ps) for (A, a), ps in self.celdas().items() if len(ps) > 1)

    def tabla_ll1(self):
        """{(A, a): producción}. Lanza ConflictoLL1 si la gramática no es LL(1)."""
        conflictos = self.conflictos()
        if conflictos:
            raise ConflictoLL1(self, conflictos)
        return {celda: ps[0] for celda, ps in self.celdas().items()}

    # ---------- texto ----------
    def texto_produccion(self, i):
        cabeza, cuerpo = self.producciones[i]
        return f'{cabeza} → {" ".join(cuerpo) or EPSILON}'

    def informe(self):
        """Producciones numeradas, PRIMEROS, SIGUIENTES y tabla LL(1), como texto para la documentación."""
        conjunto = lambda c: '{ ' + ', '.join(sorted(c)) + ' }' if c else '∅'
        ancho = max(map(len, self.no_terminales))
        lineas = ['PRODUCCIONES']
        lineas += [f'  {i:3}. {self.texto_produccion(i)}' for i in range(len(self.producciones))]
        lineas += ['', 'PRIMEROS']
        lineas += [f'  {A:{ancho}}  {conjunto(self.primeros[A])}' for A in self.no_terminales]
        lineas += ['', 'SIGUIENTES']
        lineas += [f'  {A:{ancho}}  {conjunto(self.siguientes[A])}' for A in self.no_terminales]
        lineas += ['', 'TABLA LL(1)   M[no terminal, terminal] = producción']
        celdas = self.celdas()
        for A in self.no_terminales:
            por_produccion = {}
            for (B, a), ps in celdas.items():
                if B == A:
                    por_produccion.setdefault(tuple(ps), []).append(a)
            for ps, terminales in sorted(por_produccion.items()):
                lineas.append(f'  {A:{ancho}}  {" / ".join(map(str, ps)):>3}  con {conjunto(terminales)}')
        conflictos = self.conflictos()
        lineas += ['', f'{len(self.no_terminales)} no terminales, {len(self.terminales)} terminales, '
                   f'{len(self.producciones)} producciones, {len(celdas)} celdas ocupadas. '
                   + ('Es LL(1): ninguna celda tiene más de una producción.' if not conflictos
                      else f'NO es LL(1): {len(conflictos)} conflictos.')]
        return '\n'.join(lineas)
