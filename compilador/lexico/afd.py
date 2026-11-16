"""Especificación ejecutable de los AFD del tokenizador (Fase 1, sección 4).

Cada AFD es un transductor (máquina de Mealy): delta[estado][símbolo] = (siguiente, salidas, marca) o None (error).
Símbolos 0..255 = bytes, 256 = ⊣.  marca=True: el próximo token empieza en este byte.
Salidas (tipo, modo): 'prev' = [inicio, pos)   'close' = [inicio, pos+1)   'single' = [pos, pos+1)   'eof' = [pos, pos)
"""
EOF = 256
rng = lambda a, b: list(range(a, b + 1))
B = lambda s: [ord(c) for c in s]
DIG, DIG19 = rng(0x30, 0x39), rng(0x31, 0x39)
HEX = DIG + rng(0x41, 0x46) + rng(0x61, 0x66)


class AFD:
    def __init__(self, inicial):
        self.delta, self.inicial = {}, inicial

    def st(self, n):
        self.delta.setdefault(n, [None] * 257)
        return n

    def on(self, s, simbolos, dst, *out, mark=False, sobre=False):
        self.st(s); self.st(dst)
        for b in simbolos:
            if self.delta[s][b] is not None and not sobre:
                continue  # la primera definición tiene prioridad
            self.delta[s][b] = (dst, tuple(out), mark)

    def lex(self, data: bytes):
        s, ini, toks = self.inicial, 0, []
        for pos, sym in enumerate(list(data) + [EOF]):
            t = self.delta[s][sym]
            if t is None:
                return toks, pos
            nxt, outs, mark = t
            for tok, modo in outs:
                toks.append((tok, {'prev': (ini, pos), 'close': (ini, pos + 1),
                                   'single': (pos, pos + 1), 'eof': (pos, pos)}[modo]))
            if mark:
                ini = pos
            s = nxt
        return toks, None

    # ---------- análisis ----------
    def minimizar(self, op=lambda s, b: None):
        """Algoritmo de Moore sobre el transductor. Devuelve {estado: bloque}.
        op(s, b) es la acción de valor de la transición (ver lexer.py): dos estados solo se fusionan si también coinciden en ella."""
        est = list(self.delta)
        bloque = {s: tuple(None if t is None else (t[1], t[2], op(s, b)) for b, t in enumerate(self.delta[s])) for s in est}
        while True:
            firma = {s: (bloque[s], tuple(None if t is None else (bloque[t[0]], t[1], t[2]) for t in self.delta[s])) for s in est}
            ids = {f: i for i, f in enumerate(sorted(set(firma.values()), key=repr))}
            nuevo = {s: ids[firma[s]] for s in est}
            if len(set(nuevo.values())) == len(set(bloque.values())):
                return nuevo
            bloque = nuevo

    def clases(self):
        col = {}
        for b in range(256):
            col.setdefault(tuple(self.delta[s][b] for s in sorted(self.delta)), []).append(b)
        return list(col.values())

    def resumen(self):
        m = self.minimizar()
        grupos = {}
        for s, b in m.items():
            grupos.setdefault(b, []).append(s)
        return {'estados': len(self.delta), 'minimo': len(grupos), 'clases': len(self.clases()),
                'fusionados': [g for g in grupos.values() if len(g) > 1],
                'transiciones': sum(t is not None for s in self.delta for t in self.delta[s])}


def utf8(a: AFD, ctx, ret, fffe=False):
    """Estados pendientes del producto con M_UTF8 dentro del contexto ctx; al completar vuelven a ret.
    fffe=True excluye U+FFFE y U+FFFF (Char de XML)."""
    q = lambda n: f'{ctx}·{n}'
    c = rng(0x80, 0xBF)
    a.on(q('q1'), c, ret); a.on(q('q2'), c, q('q1')); a.on(q('q3'), c, q('q2'))
    a.on(q('qE0'), rng(0xA0, 0xBF), q('q1')); a.on(q('qED'), rng(0x80, 0x9F), q('q1'))
    a.on(q('qF0'), rng(0x90, 0xBF), q('q2')); a.on(q('qF4'), rng(0x80, 0x8F), q('q2'))
    if fffe:
        a.on(q('qEF'), rng(0x80, 0xBE), q('q1')); a.on(q('qEF'), [0xBF], q('qEFBF'))
        a.on(q('qEFBF'), rng(0x80, 0xBD), ret)


def lideres(a: AFD, s, ctx, *out, mark=False, fffe=False):
    """Transiciones desde s con un byte inicial de secuencia multibyte hacia los pendientes de ctx."""
    q = lambda n: f'{ctx}·{n}'
    a.on(s, rng(0xC2, 0xDF), q('q1'), *out, mark=mark); a.on(s, [0xE0], q('qE0'), *out, mark=mark)
    a.on(s, rng(0xE1, 0xEC) + [0xEE], q('q2'), *out, mark=mark)
    a.on(s, [0xEF], q('qEF') if fffe else q('q2'), *out, mark=mark)
    a.on(s, [0xED], q('qED'), *out, mark=mark); a.on(s, [0xF0], q('qF0'), *out, mark=mark)
    a.on(s, rng(0xF1, 0xF3), q('q3'), *out, mark=mark); a.on(s, [0xF4], q('qF4'), *out, mark=mark)


# =====================================================================
#                                JSON
# =====================================================================
def construir_json():
    a = AFD('S0')
    WS = [0x09, 0x0A, 0x0D, 0x20]
    EST = {ord('{'): 'LLAVE_ABRE', ord('}'): 'LLAVE_CIERRA', ord('['): 'CORCHETE_ABRE',
           ord(']'): 'CORCHETE_CIERRA', ord(':'): 'DOS_PUNTOS', ord(','): 'COMA'}

    def entre_tokens(s):
        a.on(s, WS, 'S')
        for b, t in EST.items():
            a.on(s, [b], 'S', (t, 'single'))
        a.on(s, [EOF], 'FIN', ('FIN', 'eof'))
        a.on(s, B('"'), 'C1', mark=True)
        a.on(s, B('-'), 'N1', mark=True); a.on(s, B('0'), 'N3', mark=True); a.on(s, DIG19, 'N2', mark=True)
        a.on(s, B('t'), 'T1', mark=True); a.on(s, B('f'), 'F1', mark=True); a.on(s, B('n'), 'L1', mark=True)
    a.on('S0', [0xEF], 'BOM1'); a.on('BOM1', [0xBB], 'BOM2'); a.on('BOM2', [0xBF], 'S')
    entre_tokens('S0'); entre_tokens('S'); a.st('FIN')

    def delimitado(s, tok):
        a.on(s, WS, 'S', (tok, 'prev'))
        for b, t in EST.items():
            a.on(s, [b], 'S', (tok, 'prev'), (t, 'single'))
        a.on(s, [EOF], 'FIN', (tok, 'prev'), ('FIN', 'eof'))

    E = B('eE')
    a.on('N1', B('0'), 'N3'); a.on('N1', DIG19, 'N2')
    a.on('N2', DIG, 'N2'); a.on('N2', B('.'), 'N4'); a.on('N2', E, 'N6'); delimitado('N2', 'NUMERO')
    a.on('N3', B('.'), 'N4'); a.on('N3', E, 'N6'); delimitado('N3', 'NUMERO')
    a.on('N4', DIG, 'N5')
    a.on('N5', DIG, 'N5'); a.on('N5', E, 'N6'); delimitado('N5', 'NUMERO')
    a.on('N6', B('+-'), 'N7'); a.on('N6', DIG, 'N8')
    a.on('N7', DIG, 'N8')
    a.on('N8', DIG, 'N8'); delimitado('N8', 'NUMERO')

    for pal, pref, tok in [('true', 'T', 'TRUE'), ('false', 'F', 'FALSE'), ('null', 'L', 'NULL')]:
        for i in range(1, len(pal)):
            a.on(f'{pref}{i}', B(pal[i]), f'{pref}{i+1}')
        delimitado(f'{pref}{len(pal)}', tok)

    a.on('C1', [0x20, 0x21] + rng(0x23, 0x5B) + rng(0x5D, 0x7F), 'C1')
    a.on('C1', B('"'), 'S', ('CADENA', 'close'))
    a.on('C1', B('\\'), 'C2')
    a.on('C2', B('"\\/bfnrt'), 'C1'); a.on('C2', B('u'), 'C3')
    a.on('C3', [h for h in HEX if h not in B('Dd')], 'H3'); a.on('C3', B('Dd'), 'C4')
    a.on('C4', rng(0x30, 0x37), 'H2'); a.on('C4', B('89ABab'), 'A2')
    a.on('H3', HEX, 'H2'); a.on('H2', HEX, 'H1'); a.on('H1', HEX, 'C1')
    a.on('A2', HEX, 'A1'); a.on('A1', HEX, 'B0')
    a.on('B0', B('\\'), 'B1'); a.on('B1', B('u'), 'B2'); a.on('B2', B('Dd'), 'B3')
    a.on('B3', rng(0x43, 0x46) + rng(0x63, 0x66), 'H2')
    lideres(a, 'C1', 'C'); utf8(a, 'C', 'C1')
    return a


# =====================================================================
#                                 XML
# =====================================================================
def construir_xml():
    a = AFD('X0')
    WS = [0x09, 0x0A, 0x0D, 0x20]
    CHAR = WS + rng(0x20, 0x7F)                       # parte ASCII de Char_X
    NSC = B(':_') + rng(0x41, 0x5A) + rng(0x61, 0x7A)  # parte ASCII de NameStartChar
    NC = NSC + B('-.') + DIG
    menos = lambda conj, ex: [b for b in conj if b not in B(ex)]

    def cadena(s, texto, dst, ci=False):
        """Cadena fija (opcionalmente sin distinguir mayúsculas) de s a dst."""
        cur = s
        for i, ch in enumerate(texto):
            nxt = dst if i == len(texto) - 1 else f'{s}/{texto[:i+1]}'
            a.on(cur, list({ord(ch), ord(ch.upper()), ord(ch.lower())}) if ci else [ord(ch)], nxt)
            cur = nxt

    def referencia(ctx, ret):
        """REF = &(lt|gt|amp|quot|apos); | &#DIG+; | &#xHEX+;  — al terminar vuelve a ret."""
        r = f'{ctx}&'
        for nombre in ['lt', 'gt', 'amp', 'quot', 'apos']:
            cadena(r, nombre + ';', ret)
        a.on(r, B('#'), f'{r}#'); a.on(f'{r}#', DIG, f'{r}#d'); a.on(f'{r}#d', DIG, f'{r}#d'); a.on(f'{r}#d', B(';'), ret)
        a.on(f'{r}#', B('x'), f'{r}#x'); a.on(f'{r}#x', HEX, f'{r}#xh'); a.on(f'{r}#xh', HEX, f'{r}#xh'); a.on(f'{r}#xh', B(';'), ret)
        return r

    # ---- modo CONTENIDO: texto y la marca de inicio de etiqueta ----
    rTX = referencia('TX', 'TX')
    def contenido(s, lt):
        a.on(s, [EOF], 'FIN', ('FIN', 'eof'))
        a.on(s, B('<'), lt, mark=True)
        a.on(s, B('&'), rTX, mark=True)
        a.on(s, B(']'), 'TX]', mark=True)
        a.on(s, menos(CHAR, '<&]'), 'TX', mark=True)
        lideres(a, s, 'TX', mark=True, fffe=True)
    contenido('CT', 'LT')
    # inicio del documento: BOM opcional y declaración <?xml ... ?> solo aquí
    a.on('X0', [0xEF], 'BOM1', mark=True)
    a.on('BOM1', [0xBB], 'BOM2'); a.on('BOM1', [0xBF], 'TX·qEFBF'); a.on('BOM1', rng(0x80, 0xBE), 'TX·q1')
    a.on('BOM2', [0xBF], 'X1'); a.on('BOM2', rng(0x80, 0xBE), 'TX')
    contenido('X0', 'XLT'); contenido('X1', 'XLT')
    for s in ['TX', 'TX]', 'TX]]']:
        a.on(s, B('<'), 'LT', ('TEXTO', 'prev'), mark=True)
        a.on(s, [EOF], 'FIN', ('TEXTO', 'prev'), ('FIN', 'eof'))
        a.on(s, B('&'), rTX)
        a.on(s, B(']'), 'TX]]' if s != 'TX' else 'TX]')
        if s == 'TX]]':
            a.on(s, menos(CHAR, '<&]>'), 'TX')  # «]]>» no se permite en el texto
        else:
            a.on(s, menos(CHAR, '<&]'), 'TX')
        lideres(a, s, 'TX', fffe=True)
    utf8(a, 'TX', 'TX', fffe=True)

    # ---- después de '<' ----
    for lt in ['LT', 'XLT']:
        a.on(lt, NSC, 'TN', ('ETIQ_ABRE', 'prev'), mark=True)
        lideres(a, lt, 'TN', ('ETIQ_ABRE', 'prev'), mark=True, fffe=True)
        a.on(lt, B('/'), 'CL0', ('ETIQ_CIERRE_ABRE', 'close'))
        a.on(lt, B('!'), f'{lt}!')
        cadena(f'{lt}!', '--', 'K1')
        cadena(f'{lt}!', '[CDATA[', 'D1')
    a.on('LT', B('?'), 'PI0'); a.on('XLT', B('?'), 'XPI0')

    # ---- modo ETIQUETA ----
    def nombre(s, ctx, fin):
        """Resto de un NOMBRE; fin(s) agrega las transiciones de salida."""
        a.on(s, NC, s); lideres(a, s, ctx, fffe=True); utf8(a, ctx, s, fffe=True); fin(s)
    def salida_etiqueta(s, *previo):
        a.on(s, B('>'), 'CT', *previo, ('ETIQ_FIN', 'single'))
        a.on(s, B('/'), 'SL', *previo, mark=True)
    def fin_tn(s):
        a.on(s, WS, 'TW', ('NOMBRE', 'prev')); salida_etiqueta(s, ('NOMBRE', 'prev'))
    nombre('TN', 'TN', fin_tn)
    a.on('TW', WS, 'TW'); salida_etiqueta('TW')
    a.on('TW', NSC, 'AN', mark=True); lideres(a, 'TW', 'AN', mark=True, fffe=True)
    a.on('SL', B('>'), 'CT', ('ETIQ_AUTOCIERRE', 'close'))
    def fin_an(s):
        a.on(s, WS, 'AW', ('NOMBRE', 'prev')); a.on(s, B('='), 'AQ', ('NOMBRE', 'prev'), ('IGUAL', 'single'))
    nombre('AN', 'AN', fin_an)
    a.on('AW', WS, 'AW'); a.on('AW', B('='), 'AQ', ('IGUAL', 'single'))
    a.on('AQ', WS, 'AQ')
    for q, s in [('"', 'AV"'), ("'", "AV'")]:
        a.on('AQ', B(q), s, mark=True)
        r = referencia(s, s)
        a.on(s, B(q), 'AF', ('VALOR_ATRIB', 'close'))
        a.on(s, B('&'), r)
        a.on(s, menos(CHAR, '<&' + q), s)
        lideres(a, s, s, fffe=True); utf8(a, s, s, fffe=True)
    a.on('AF', WS, 'TW'); salida_etiqueta('AF')  # tras un valor: espacio, '>' o '/>'
    # etiqueta de cierre
    a.on('CL0', NSC, 'CN', mark=True); lideres(a, 'CL0', 'CN', mark=True, fffe=True)
    def fin_cn(s):
        a.on(s, WS, 'CW', ('NOMBRE', 'prev')); a.on(s, B('>'), 'CT', ('NOMBRE', 'prev'), ('ETIQ_FIN', 'single'))
    nombre('CN', 'CN', fin_cn)
    a.on('CW', WS, 'CW'); a.on('CW', B('>'), 'CT', ('ETIQ_FIN', 'single'))

    # ---- comentario y CDATA (gramáticas K y D de la sección 3) ----
    a.on('K1', menos(CHAR, '-'), 'K1'); a.on('K1', B('-'), 'K2')
    a.on('K2', menos(CHAR, '-'), 'K1'); a.on('K2', B('-'), 'K3')
    a.on('K3', B('>'), 'CT')
    for s in ['K1', 'K2']:
        lideres(a, s, 'K', fffe=True)
    utf8(a, 'K', 'K1', fffe=True)
    a.on('D1', menos(CHAR, ']'), 'D1'); a.on('D1', B(']'), 'D2')
    a.on('D2', menos(CHAR, ']'), 'D1'); a.on('D2', B(']'), 'D3')
    a.on('D3', B(']'), 'D3'); a.on('D3', B('>'), 'CT', ('TEXTO', 'close')); a.on('D3', menos(CHAR, ']>'), 'D1')
    for s in ['D1', 'D2', 'D3']:
        lideres(a, s, 'D', fffe=True)
    utf8(a, 'D', 'D1', fffe=True)

    # ---- instrucciones de procesamiento (se descartan); destino ≠ xml sin distinguir mayúsculas ----
    def pi(p0):
        a.on(p0, B('xX'), f'{p0}x'); a.on(f'{p0}x', B('mM'), f'{p0}xm'); a.on(f'{p0}xm', B('lL'), f'{p0}xml')
        a.on(p0, NSC, 'PN'); lideres(a, p0, 'PN', fffe=True)
        for s in [f'{p0}x', f'{p0}xm']:
            a.on(s, NC, 'PN'); lideres(a, s, 'PN', fffe=True)
            a.on(s, WS, 'PC'); a.on(s, B('?'), 'PQ')
        a.on(f'{p0}xml', NC, 'PN'); lideres(a, f'{p0}xml', 'PN', fffe=True)  # «<?xml» + espacio o «?» = error
    # al inicio del documento, «<?xml» en minúsculas seguido de espacio es la declaración (XD1..XD3)
    a.on('XPI0', B('x'), 'XD1'); a.on('XD1', B('m'), 'XD2'); a.on('XD2', B('l'), 'XD3')
    a.on('XD1', B('M'), 'XPI0xm'); a.on('XD2', B('L'), 'XPI0xml')
    a.on('XD3', WS, 'DV')
    for s in ['XD1', 'XD2']:
        a.on(s, NC, 'PN'); lideres(a, s, 'PN', fffe=True); a.on(s, WS, 'PC'); a.on(s, B('?'), 'PQ')
    a.on('XD3', NC, 'PN'); lideres(a, 'XD3', 'PN', fffe=True)
    pi('PI0'); pi('XPI0')
    nombre('PN', 'PN', lambda s: (a.on(s, WS, 'PC'), a.on(s, B('?'), 'PQ')))
    a.on('PQ', B('>'), 'CT')
    a.on('PC', menos(CHAR, '?'), 'PC'); a.on('PC', B('?'), 'PC?')
    a.on('PC?', B('>'), 'CT'); a.on('PC?', B('?'), 'PC?'); a.on('PC?', menos(CHAR, '?>'), 'PC')
    for s in ['PC', 'PC?']:
        lideres(a, s, 'PC', fffe=True)
    utf8(a, 'PC', 'PC', fffe=True)

    # ---- declaración <?xml version EQ Q(1.0) (encoding ...)? (standalone ...)? ?> ----
    def atributo(s, nombre_attr, valores, dst, ci=False):
        cadena(s, nombre_attr, f'{s}{nombre_attr}=')
        e = f'{s}{nombre_attr}='
        a.on(e, WS, e); a.on(e, B('='), f'{e}='); a.on(f'{e}=', WS, f'{e}=')
        for q in '"\'':
            for v in valores:
                cadena(f'{e}={q}', v + q, dst, ci)
            a.on(f'{e}=', B(q), f'{e}={q}')
    a.on('DV', WS, 'DV')
    atributo('DV', 'version', ['1.0'], 'DV1')
    def tras_atributo(s, siguientes):
        a.on(s, WS, f'{s}_'); a.on(s, B('?'), 'DQ')
        a.on(f'{s}_', WS, f'{s}_'); a.on(f'{s}_', B('?'), 'DQ')
        for primera, (nom, vals, dst, ci) in siguientes.items():
            atributo(f'{s}_', nom, vals, dst, ci)
    std = ('standalone', ['yes', 'no'], 'DS1', False)
    tras_atributo('DV1', {'e': ('encoding', ['utf-8'], 'DE1', True), 's': std})
    tras_atributo('DE1', {'s': std})
    tras_atributo('DS1', {})
    a.on('DQ', B('>'), 'CT', ('DECL_XML', 'close'))  # empieza en el '<' marcado al inicio
    a.st('FIN')
    return a


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    # Los casos de prueba están en pruebas/test_afd.py; aquí solo se imprimen las cifras de la Tabla 17.
    for nom, afd in [('JSON', construir_json()), ('XML', construir_xml())]:
        print(nom, afd.resumen())
