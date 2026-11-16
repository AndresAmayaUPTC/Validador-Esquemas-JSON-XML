"""Documentos XML compartidos por las pruebas (válidos e inválidos), mutantes y un generador aleatorio."""
import random

ENTRADAS_XML = [
    # estructura
    b'<r/>', b'<r></r>', b'<r a="1"/>', b"<r a='1' b=\"2\">x</r>", b'<a><b><c/></b><b/></a>', b'  <r/>\n',
    '<p id="7">Hola &amp; adiós</p>'.encode(), '<año mes="ene">x</año>'.encode(), b'<a-b.c _x="1"/>',
    # declaración y BOM
    b'<?xml version="1.0"?><r/>', b'<?xml version="1.0" encoding="UTF-8"?>\n<r/>',
    b"\xef\xbb\xbf<?xml version='1.0' standalone='yes'?><r/>", b'\xef\xbb\xbf<r/>',
    b' <?xml version="1.0"?><r/>', b'<?XML version="1.0"?><r/>', b'<?xml version="1.1"?><r/>',
    b'<?xml version="1.0" encoding="latin-1"?><r/>',
    # texto, CDATA, referencias, comentarios e instrucciones de procesamiento
    b'<r>a<!--c-->b<![CDATA[x]]y]]>&#65;</r>', b'<r><![CDATA[a<b&c]]></r>', b'<r><![CDATA[]]></r>', b'<r><![CDATA[]]]]></r>',
    b'<r>&lt;&gt;&amp;&quot;&apos;</r>', b'<r>&#x41;&#65;&#x1F600;</r>', b'<r a="&lt;&#9;&#10;"/>',
    b'<r>l1\r\nl2\rl3\n</r>', b'<r a="x\ty\r\nz"/>', b'<r><?pi dato?><!-- c --></r>', b'<!-- antes --><r/><!-- despues -->',
    b'<?hoja a?><r/>', b'<r>a]b]]c</r>', '<r>ñ€😀</r>'.encode(), b'<r> </r>', b'<r>\n  <a/>\n  <b/>\n</r>',
    # errores léxicos
    b'<!DOCTYPE r><r/>', b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>',
    b'<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><l>&b;</l>',
    b'<r>&nbsp;</r>', b'<r>a & b</r>', b'<r>&#0;</r>', b'<r>&#xFFFE;</r>', b'<r>&#1114112;</r>', b'<r>a]]>b</r>',
    b'<1a/>', b'<r a="<"/>', b'<r a="1"b="2"/>', b'<r a=1/>', b'<r a/>', b'<!-- a -- b --><r/>', b'<r>\x01</r>',
    b'<r>\xff</r>', b'<r>\xef\xbf\xbe</r>', b'<r', b'<r>', b'<r><![CDATA[x', b'<r>&amp', '<a×/>'.encode(), '<×a/>'.encode(),
    # errores sintácticos y de buena formación
    b'', b'texto', b'<r/><r/>', b'<a></b>', b'<a><b></a></b>', b'<r a="1" a="2"/>', b'<r/>texto', b'</r>', b'<r/> ',
]

_INTERESANTES = b'<>/="\'&;#x![]- \t\r\nab1\x00\x7f\x80\xbf\xc3\xe2\xef\xff'


def mutantes_xml(semilla=2026, por_entrada=20):
    """Variantes de ENTRADAS_XML con uno a tres cambios de un byte. Deterministas."""
    rng, salida = random.Random(semilla), []
    for base in ENTRADAS_XML:
        for _ in range(por_entrada):
            m = bytearray(base)
            for _ in range(rng.randint(1, 3)):
                i, b = rng.randint(0, len(m)), rng.choice(_INTERESANTES)
                operacion = rng.randrange(4)
                if operacion == 0 and i < len(m):
                    m[i] = b
                elif operacion == 1:
                    m.insert(i, b)
                elif operacion == 2 and i < len(m):
                    del m[i]
                elif operacion == 3:
                    del m[i:]
            salida.append(bytes(m))
    return salida


_NOMBRES = ['a', 'b', 'item', 'año', 'x-1', 'n.m', '_p', 'é']
_TEXTOS = ['hola', ' ', '\n  ', 'a&amp;b', '&lt;x&gt;', '&#65;&#x42;', 'ñ€😀', 'l1\r\nl2', 'fin\r', '<![CDATA[<&>]]>',
           '<![CDATA[a]]b]]>', '<!-- c -->', '<?pi x?>', 'a]b', '\t', '&quot;&apos;']
_VALORES = ['1', '', 'a b', 'x\ty', 'l1\r\nl2', 'a&amp;b', '&#10;&#9;', "it's" if False else 'its', 'ñ€', '&lt;']


def documento_aleatorio(rng, profundidad=4) -> bytes:
    """Documento XML bien formado, con variedad de construcciones léxicas."""
    def elemento(prof):
        nombre = rng.choice(_NOMBRES)
        atributos = ''
        for n in rng.sample(_NOMBRES, rng.randint(0, 3)):
            comilla = rng.choice('"\'')
            atributos += f'{rng.choice([" ", "  ", chr(10)])}{n}{rng.choice(["=", " = "])}{comilla}{rng.choice(_VALORES)}{comilla}'
        if prof == 0 or rng.random() < 0.25:
            return f'<{nombre}{atributos}{rng.choice(["", " "])}/>'
        cuerpo = ''.join(elemento(prof - 1) if rng.random() < 0.45 else rng.choice(_TEXTOS) for _ in range(rng.randint(0, 5)))
        return f'<{nombre}{atributos}>{cuerpo}</{nombre}{rng.choice(["", " "])}>'
    prologo = rng.choice(['', '<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-8"?>\n', '<!-- c -->', ' \n'])
    return (prologo + elemento(profundidad) + rng.choice(['', '\n', '<!-- fin -->'])).encode('utf-8')
