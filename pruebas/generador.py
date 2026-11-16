"""Generador de documentos de prueba a partir de un esquema (solo para las pruebas).

Produce valores que intentan cumplir el esquema, con errores aleatorios intercalados, de modo que salgan
tanto documentos válidos como inválidos. No pretende ser exhaustivo: el veredicto lo da el oráculo.
"""

BASURA = [None, True, False, 0, 1, -1, 2, 3, 5, 7, 10, 151, 1.0, 2.5, -0.5, '', 'a', 'ab', 'abc', 'admin', 'x', 'ñ',
          'abcdef', [], {}, [1], ['a', 'b'], {'a': 1}, {'x': None}]


def basura(rng, prof=0):
    v = rng.choice(BASURA)
    if prof < 3 and rng.random() < 0.2:
        return [basura(rng, prof + 1) for _ in range(rng.randint(0, 3))]
    if prof < 3 and rng.random() < 0.2:
        return {rng.choice(['a', 'b', 'c', 'h', 'x']): basura(rng, prof + 1) for _ in range(rng.randint(0, 3))}
    return v


def _resolver(raiz, ref):
    nodo = raiz
    for parte in ref.lstrip('#').split('/')[1:]:
        nodo = nodo[int(parte)] if isinstance(nodo, list) else nodo[parte.replace('~1', '/').replace('~0', '~')]
    return nodo


def generar(esq, raiz, rng, prof=0):
    if esq is True or esq is False or prof > 7 or rng.random() < 0.1:
        return basura(rng, prof)
    esq = dict(esq)
    for _ in range(4):  # absorber los aplicadores en un solo diccionario, de forma aproximada
        if '$ref' in esq:
            destino = _resolver(raiz, esq.pop('$ref'))
            esq = {**(destino if isinstance(destino, dict) else {}), **esq}
        for palabra in ('anyOf', 'oneOf'):
            if palabra in esq:
                rama = rng.choice(esq.pop(palabra))
                esq = {**esq, **(rama if isinstance(rama, dict) else {})}
        if 'allOf' in esq:
            for rama in esq.pop('allOf'):
                if isinstance(rama, dict):
                    props = {**esq.get('properties', {}), **rama.get('properties', {})}
                    esq = {**esq, **rama, **({'properties': props} if props else {})}
    if 'const' in esq:
        return esq['const']
    if 'enum' in esq and esq['enum']:
        return rng.choice(esq['enum'])
    tipo = esq.get('type')
    if isinstance(tipo, list):
        tipo = rng.choice(tipo)
    if tipo is None:
        tipo = rng.choice(['object'] if 'properties' in esq or 'required' in esq else
                          ['array'] if 'items' in esq or 'prefixItems' in esq else
                          ['integer', 'string', 'null', 'boolean', 'number', 'object', 'array'])
    if tipo == 'null':
        return None
    if tipo == 'boolean':
        return rng.random() < 0.5
    if tipo in ('integer', 'number'):
        base = esq.get('minimum', esq.get('exclusiveMinimum', esq.get('maximum', esq.get('exclusiveMaximum', 3))))
        n = base + rng.choice([-2, -1, 0, 0, 1, 1, 2, 5])
        if tipo == 'number' or rng.random() < 0.15:
            n = n + rng.choice([0.0, 0.5])
        return n
    if tipo == 'string':
        largo = esq.get('minLength', esq.get('maxLength', 2)) + rng.choice([-1, 0, 0, 1, 2])
        return ''.join(rng.choice('abñ€😀') for _ in range(max(0, largo)))
    if tipo == 'array':
        prefijo = esq.get('prefixItems', [])
        n = max(0, esq.get('minItems', esq.get('maxItems', len(prefijo) + 1)) + rng.choice([-1, 0, 0, 1, 2]))
        return [generar(prefijo[i] if i < len(prefijo) else esq.get('items', True), raiz, rng, prof + 1) for i in range(n)]
    propiedades = esq.get('properties', {})
    obj = {}
    for nombre in list(propiedades) + [r for r in esq.get('required', []) if r not in propiedades]:
        obligatoria = nombre in esq.get('required', [])
        if rng.random() < (0.9 if obligatoria else 0.6):
            obj[nombre] = generar(propiedades.get(nombre, esq.get('additionalProperties', True)), raiz, rng, prof + 1)
    for nombre, exige in esq.get('dependentRequired', {}).items():
        if nombre in obj and rng.random() < 0.7:
            for otra in exige:
                obj.setdefault(otra, basura(rng, prof + 1))
    if rng.random() < 0.25:
        obj[rng.choice(['extra', 'a', 'z'])] = generar(esq.get('additionalProperties', True), raiz, rng, prof + 1)
    return obj
