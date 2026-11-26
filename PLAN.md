# Plan de código

Compilador (esquema → DPDA ponderado) en **Python**. Validadores en **Rust**. Base: `Fase1_Analisis_Lexico.docx` y `afd.py`.

## Estructura

```
Código/
├─ compilador/   paquete Python: lexico, sintactico, semantico, automata, generador, xsd, conversion
├─ validador/    crate de Rust: motor fijo (lexer + DPDA) y tablas generadas
├─ pruebas/      casos: esquema, payload y resultado esperado
└─ web/          backend y frontend (Fase 7)
```

El compilador emite tablas; el motor de Rust las ejecuta. Así el motor se escribe una sola vez.

## Pasos

Orden: primero JSON de la fase 1 a la 6, después XML/XSD, y la web al final.

### 0. Preparación
- [x] Instalar Rust con rustup. Toolchain por defecto: `stable-x86_64-pc-windows-gnu` (la variante MSVC no enlaza porque falta el SDK de Windows). Su enlazador no abre rutas con tilde, así que la compilación sale a `Proyecto/.cargo-target` (ver `validador/.cargo/config.toml`)
- [x] Mover `fase1/afd.py` a `compilador/lexico/` y actualizar la ruta que cita el docx (sección 4)
- [x] Pasar sus 38 casos a `pytest` (`pruebas/test_afd.py`)
- [x] Crear el crate `validador` con `cargo`

Para ejecutar las pruebas, desde `Código/`:

```
.venv\Scripts\python -m pytest          # compilador (Python)
cargo test --manifest-path validador/Cargo.toml   # validador (Rust)
```

### 1. Análisis léxico
- [x] Exportar a Rust las tablas de `afd.py`: byte → clase, δ y salidas (`compilador/lexico/tablas.py` → `validador/src/tablas_json.rs`)
- [x] Lexer en streaming en Rust: `feed(bloque)` y `finish()`, con el estado conservado entre bloques (`validador/src/lexer.rs`)
- [x] Valores de los tokens: escapes, pares de sustitutos, número como texto en búfer acotado
- [x] Lexer del esquema en Python: lexer de JSON más las palabras clave (`compilador/lexico/lexer.py` y `esquema.py`)
- [x] Prueba: cualquier partición en bloques, incluida byte a byte, da los mismos tokens y el mismo byte de error

Si cambia `afd.py`, hay que regenerar las tablas: `.venv\Scripts\python -m compilador.lexico.tablas`.

### 2. Análisis sintáctico
- [x] Gramáticas de JSON y del metalenguaje de esquemas (`compilador/sintactico/gramaticas.py`)
- [x] PRIMEROS, SIGUIENTES y tabla LL(1), con una prueba que falla si hay conflictos (`gramatica.py`)
- [x] Parser descendente del esquema → AST con posiciones (`parser.py` y `ast_esquema.py`)
- [x] Árboles de derivación de payloads de ejemplo (`arbol.py`)

Para la documentación, desde `Código/`:

```
.venv\Scripts\python -m compilador.sintactico.gramaticas json       # producciones, PRIMEROS, SIGUIENTES y tabla
.venv\Scripts\python -m compilador.sintactico.gramaticas esquema
.venv\Scripts\python -m compilador.sintactico.arbol payload.json    # árbol de derivación (--formato dot | json, --esquema)
```

### 3. Análisis semántico
- [x] Validación contra el metaesquema (`compilador/semantico/metaesquema.py`)
- [x] Tabla de símbolos de `$defs`, resolución de `$ref` y detección de ciclos no productivos (`simbolos.py` y `ciclos.py`)
- [x] Clasificación de palabras clave: (a) directas, (b) con registros, (c) con estrategia de respaldo (`clasificacion.py`)

Informe completo de un esquema (diagnósticos, tabla de símbolos, referencias y clasificación), desde `Código/`:

```
.venv\Scripts\python -m compilador.semantico.analisis esquema.json
```

### 4. Autómata ponderado
- [x] Estructura de M = (Q, Σ, Γ, δ, q₀, Z₀, F, w) y construcción por nodo del esquema (`compilador/automata/formas.py`, `nodos.py` y `dpda.py`)
- [x] Pesos: costo para ordenar comprobaciones y conteo de violaciones
- [x] Verificación de determinismo y minimización (`verificacion.py`)
- [x] Intérprete en Python, como referencia para probar el de Rust (`interprete.py`)

Las palabras clave fuera del DPDA puro se verifican con las estrategias de respaldo del paso 9. Solo quedan en
verificación diferida las expresiones regulares que no son regulares o que no se implementan (referencias hacia atrás,
aserciones, `\b`, `\p{...}`) y los `multipleOf` con un divisor de más de 18 dígitos.

Definición formal y tabla δ de un esquema, y validación de un payload, desde `Código/`:

```
.venv\Scripts\python -m compilador.automata esquema.json                # --dot para Graphviz, --sin-delta para resumir
.venv\Scripts\python -m compilador.automata esquema.json payload.json
```

### 5. Generación de código
- [x] Emitir las tablas del DPDA como código Rust (`compilador/generador/tablas.py`)
- [x] Motor en Rust con pila de profundidad fija y sin asignación dinámica (`validador/src/validador.rs`, `automata.rs` y `decimal.rs`; el crate es `no_std`)
- [x] API FFI y WebAssembly: un crate por esquema con API en C (`compilador/generador/rust.py`)
- [x] Generador de payloads válidos, inválidos y malformados (`compilador/generador/payloads.py`)

Generar casos de prueba y el validador nativo de un esquema, desde `Código/`:

```
.venv\Scripts\python -m compilador.generador esquema.json                           # casos, uno por línea
.venv\Scripts\python -m compilador.generador esquema.json --crate salida --autocontenido
cd salida
cargo test                                                    # el validador nativo contra sus casos
cargo build --release                                         # biblioteca dinámica con la API en C
cargo build --release --target wasm32-unknown-unknown         # módulo WebAssembly
```

Si la carpeta de salida tiene tildes en su ruta, hay que compilar con `CARGO_TARGET_DIR` apuntando a una ruta sin ellas.

### 6. Validación en tiempo real
- [x] Error con JSON Pointer y posición en bytes; límites de profundidad, tamaño y tiempo (`interprete.py` y `validador/src/validador.rs`)
- [x] Métricas: MB/s, ciclos por byte, memoria, asignaciones, profundidad de pila, tamaño del autómata (`bench/src/main.rs`)
- [x] Línea base: `serde_json` + `jsonschema` (misma medición, en el mismo programa)
- [x] Pruebas de bombeo con anidamiento creciente, arreglos largos y carga sostenida (`bench/correr.py`)

Mediciones, desde `Código/` (la primera vez compila la línea base, alrededor de un minuto):

```
.venv\Scripts\python bench/correr.py              # serie completa → bench/resultados/mediciones.csv
.venv\Scripts\python bench/correr.py --rapido     # comprobación en unos segundos
```

En Windows, las dependencias de la línea base necesitan `dlltool`; `correr.py` lo resuelve solo copiando `llvm-ar` como `llvm-dlltool` (requiere `rustup component add llvm-tools`, ya instalado). Para compilar `bench` a mano hay que pasar esa ruta con `CARGO_ENCODED_RUSTFLAGS`, como hace el guion.

### 7. Aplicación web
- [x] Backend en Python (REST + WebSocket) que usa el compilador directamente (`web/backend/`)
- [x] Frontend: editor de esquema, autómata, árbol de derivación, panel de comparación (`web/frontend/`, sin paso de compilación)
- [x] Sandbox: el validador nativo se compila a WebAssembly y se ejecuta en el navegador; el servidor solo ejecuta tablas de datos
- [x] Recorrido lento de la validación por bloques (pestaña Validación, «Recorrido»): 0,5, 1 o 2 s por transición, o paso a
  paso con «Siguiente» (o la flecha derecha), con pausa. Cada transición muestra el token que la provoca, resaltado en la
  cinta, y la pila que deja: el WebSocket envía «detalles» (token, posición y pila) en paralelo a «pasos»
- [x] Base de datos NoSQL con los casos de prueba: MongoDB si existe `MONGODB_URI`, archivos locales si no (`web/backend/repositorio.py`)
- [x] MongoDB Atlas conectado: los 26 casos están en la base `validador`, colección `casos`, con el resultado y las métricas de su última ejecución (la cadena de conexión no se guarda en el repositorio: va en `MONGODB_URI`)
- [ ] Desplegar en la nube: los archivos están listos (`Dockerfile`, `docker-compose.yml`), falta ejecutarlo con una cuenta de AWS o Azure

Arrancar en local, desde `Código/`:

```
.venv\Scripts\python -m web.backend.sembrar      # carga los 26 casos de la sección 4 de la propuesta
.venv\Scripts\python -m web.backend              # http://127.0.0.1:8000  (API documentada en /api/docs)
```

La conexión con MongoDB Atlas está en `Código/.env` (`MONGODB_URI=...`), que ambos comandos leen solos. Ese archivo
tiene la contraseña: no se sube al repositorio ni se copia a la imagen Docker. Sin él, los casos se guardan en
`web/datos/casos`. Una variable `MONGODB_URI` puesta a mano en la terminal tiene prioridad sobre el archivo.

Con Docker (probado en local el 2026-11-13: la imagen pesa 1,7 GB y dentro funcionan la medición, WebAssembly, los WebSocket y la conexión con Atlas; el servicio corre como usuario sin privilegios):

```
docker build -t validador-web .
docker run -p 8000:8000 --env-file .env validador-web
```

O con una base de datos MongoDB en otro contenedor, sin Atlas (el archivo es válido, pero esta variante no se ha ejecutado):

```
docker compose up --build -d
docker compose run --rm app /venv/bin/python -m web.backend.sembrar
```

## Cómo se verifica

Tres resultados deben coincidir en cada caso: el intérprete de Python, el validador de Rust y la biblioteca `jsonschema`.

### 8. XML y XSD

Misma tubería que JSON, reutilizando el autómata de pila (`compilador/automata/dpda.py`), los pesos, la verificación,
el motor de Rust, la aplicación web y Atlas. Oráculo de las pruebas: la biblioteca `xmlschema` de Python.

**Subconjunto de XSD.** Elementos globales y locales; `complexType` con `sequence` y `choice` anidables;
`minOccurs`/`maxOccurs` (incluido `unbounded`); atributos `use="required|optional"`; contenido simple con atributos
(`simpleContent`/`extension`); tipos simples `xs:string`, `xs:integer`, `xs:decimal`, `xs:boolean` y `simpleType` con
`restriction` y las facetas `minInclusive`, `maxInclusive`, `minExclusive`, `maxExclusive`, `minLength`, `maxLength`,
`length`, `enumeration`; tipos con nombre (`type="..."`) y `ref="..."`, incluidos los recursivos, y `pattern` (paso 9).
Todo lo demás (`all`, `any`, `anyAttribute`, grupos, sustituciones, espacios de nombres propios,
`import`/`include`, contenido mixto, `key`/`keyref`, `default`/`fixed`) es error de compilación «no soportada».

**Léxico** (`compilador/lexico/lexer_xml.py`). El AFD de `afd.construir_xml()` más las acciones de valor, por bloques
(`feed`/`finish`), con los tokens de la Tabla 10 del documento de la Fase 1. Reglas de valor:
- NOMBRE: el nombre; los caracteres no ASCII se comprueban contra NameStartChar/NameChar (sección 1.6 del docx).
- VALOR_ATRIB: referencias resueltas y normalización de XML 1.0 §3.3.3 (tab, CR y LF literales → espacio).
- TEXTO: datos de carácter, CDATA y referencias entre dos etiquetas forman **un solo** token aunque haya comentarios o
  instrucciones de procesamiento en medio; se emite al llegar la siguiente etiqueta o FIN. Fines de línea normalizados
  (CRLF y CR literales → LF, XML 1.0 §2.11). Nunca se emite un TEXTO vacío. `solo_espacios` si solo tiene espacio, tab, CR, LF.
- Referencias de carácter: el valor debe ser un Char de XML; si no, error léxico.
- Se rechaza todo DOCTYPE (sin DTD no hay entidades propias: ni XXE ni expansión de entidades).

**Sintáctico.** Gramática LL(1) de XML bien formado sobre esos tokens (`gramaticas.py`) y un constructor de árbol
(`compilador/sintactico/xml_dom.py`) que además comprueba lo que no es libre de contexto: que la etiqueta de cierre
coincida con la de apertura, atributos sin repetir y una sola raíz. Con él se leen los XSD y se dibujan árboles de derivación.

**Semántico** (`compilador/xsd/`). Del árbol del XSD a un modelo con tabla de símbolos (tipos y elementos con nombre),
referencias resueltas, ciclos, diagnósticos con posición y clasificación: (a) directas: elementos, `sequence`, `choice`,
ocurrencias, tipos y facetas; (b) con registros: atributos obligatorios (máscara de bits) y repetidos; (c) `pattern`, AFD anclado.

**Autómata.** Un nodo por pareja (nombre de elemento, tipo). El modelo de contenido de cada tipo es una expresión regular
sobre nombres de hijos: Glushkov → determinización por subconjuntos (con cota) → minimización; las ocurrencias acotadas se
expanden. Estados por elemento: atributos, posición del modelo de contenido, texto y cierre. La pila guarda la posición de
retorno en el padre; el marco lleva la máscara de atributos y el número de hijos. Un hijo no permitido cuenta una violación
y se salta entero. Que el cierre coincida con la apertura es de buena formación: si no, el documento está mal formado.
El texto de tipos numéricos y booleanos se recorta (whiteSpace = collapse) antes de comprobarlo.
Ruta del error: `/raiz/hijo[k]/@atributo`, con k = posición entre los hijos elemento del padre (desde 1).

Tareas:
- [x] Lexer de XML por bloques con valores, gramática LL(1), árbol y árboles de derivación (Python)
- [x] Lectura de XSD, modelo, diagnósticos y clasificación
- [x] Autómata de XML, intérprete de referencia y pruebas contra `xmlschema`
- [x] Generador de documentos XML válidos, inválidos y malformados
- [x] Lexer de XML en Rust (tablas generadas) con pruebas diferenciales
- [x] Motor de Rust para el autómata de XML, crate generado, API en C y WebAssembly
- [x] Aplicación web con selector JSON/XML, casos de prueba de XML (incluidos DOCTYPE/XXE) y medición

**Cómo quedó.** El formato se reconoce por el primer carácter del esquema («<» = XSD), en `compilador/formatos.py`: todo lo
demás (línea de órdenes, aplicación web, crate generado, medición) usa el mismo camino para los dos.

```
.venv\Scripts\python -m compilador.xsd.modelo esquema.xsd               # análisis: símbolos, clasificación, diagnósticos
.venv\Scripts\python -m compilador.xsd esquema.xsd documento.xml        # autómata y validación con el intérprete
.venv\Scripts\python -m compilador.sintactico.arbol documento.xml --xml # árbol de derivación
.venv\Scripts\python -m compilador.generador esquema.xsd --crate CARPETA  # validador nativo (Rust, C y WebAssembly)
.venv\Scripts\python -m compilador.lexico.tablas_xml                    # regenera validador/src/tablas_xml.rs
```

Código nuevo: `compilador/lexico/lexer_xml.py` y `tablas_xml.py`, `compilador/sintactico/xml_dom.py`, `compilador/xsd/`
(`modelo.py`, `contenido.py`, `automata.py`, `interprete.py`, `compilar.py`), `compilador/generador/documentos_xml.py`,
`validador/src/xml.rs`, `tablas_xml.rs` y `validador_xml.rs`. En la aplicación web hay 3 ejemplos y 23 casos de XML
(`web/backend/sembrar.py`), con las mismas categorías que los de JSON.

**Verificación.** El intérprete da el mismo veredicto que `xmlschema` en 1200 documentos generados y alterados (5 esquemas);
el lexer y el motor de Rust dan exactamente lo mismo que los de Python (tokens, estado, violaciones, ruta y límites) en más
de 11 000 casos; el crate generado se prueba con `cargo test`, por la API en C y como WebAssembly.

**Límites conocidos de XML** (están documentados en `compilador/xsd/interprete.py`):
- Los atributos repetidos se detectan solo en los atributos declarados (con la máscara de bits). En los que el esquema no
  declara no hay dónde recordarlos con memoria acotada; el documento ya es inválido salvo dentro de un `xs:anyType`.
- En los elementos que el esquema no conoce (los que se saltan y los de `xs:anyType`), el nombre de cierre se compara por
  longitud y huella de 64 bits (FNV-1a), no carácter a carácter. En los elementos declarados la comparación es exacta.
- Un número de más de 256 bytes que haya que comparar con un límite o una enumeración detiene la validación (`limite`).
- No se interpretan `xsi:type` ni `xsi:nil`: los atributos `xmlns:*` y `xsi:*` se ignoran.
- La línea base de la medición (`roxmltree`) solo construye el árbol: no existe en Rust un validador de XSD comparable.

### 9. Estrategias de respaldo (fuera del DPDA puro)

Lo que antes quedaba en verificación diferida en JSON se comprueba ahora en la misma pasada, con memoria acotada:

| Palabras clave | Estrategia | Dónde |
|---|---|---|
| `pattern`, `patternProperties` | AFD de la expresión regular (ECMA-262): Thompson sobre bytes UTF-8 → subconjuntos → Moore. Búsqueda Σ*·R·Σ* sobre ⊢w⊣. Sin retroceso: lineal, sin ReDoS | `compilador/automata/expresiones.py` |
| `patternProperties` con `properties` | Producto del trie de los nombres y los AFD de los patrones: cada conjunto de patrones con el que puede casar una clave da su nodo | `nodos.py`, `generador/tablas.py` |
| `multipleOf` | Aritmética entera exacta sobre la forma canónica: M divide a D·10^(a−b), módulo M en 64 bits | `compilador/automata/valores.py`, `validador/src/decimal.rs` |
| `uniqueItems`, `enum`/`const` con objetos o arreglos | Huellas de 64 bits de la forma canónica (1 = 1.0, objetos sin orden), calculadas en streaming; las de los elementos se guardan en un fondo de 1024 | `valores.py`, `validador/src/huellas.rs` |
| `anyOf`/`oneOf` ambiguos, `not`, `if`/`then`/`else`, `dependentSchemas`, `contains` (con `minContains`/`maxContains`), `propertyNames` | Producto de autómatas en la ejecución: sub-ejecuciones del mismo DPDA (pila propia con fondo Zh, terminan en FIN_HILO) que leen el mismo valor; sus pesos se combinan al terminar (anyOf = mínimo del semianillo tropical). Hasta 16 a la vez | `nodos.py`, `dpda.py`, `interprete.py`, `validador/src/validador.rs` |

Una rama con comprobaciones diferidas da «quizá»: los grupos combinan con lógica de tres valores (cumple, falla, quizá).
Nuevos límites, que el intérprete y el motor aplican igual: más de 16 sub-ejecuciones vivas → `limite` (`hilos`); más
de 1024 huellas de elementos a la vez → esa comprobación de `uniqueItems` queda pendiente. Dos valores distintos con la
misma huella (probabilidad ~2⁻⁶⁴ por par) darían un falso «repetido».

El validador de Rust tiene dos parámetros más: `E` (ejecuciones) y `U` (huellas), y `C` (niveles de la pila de huellas).
Por omisión valen 1, 0 y 0: un esquema que no usa estas estrategias ocupa y tarda lo mismo que antes (medido: 126 MB/s
en registros, igual que la versión anterior en el mismo equipo). El crate generado los fija según el esquema.

Pruebas: `pruebas/test_expresiones.py` (contra `re`, rangos UTF-8, ReDoS, huellas y múltiplos contra `fractions`),
`RESPALDO` en `pruebas/test_automata.py` (mismo veredicto que `jsonschema`) y `pruebas/test_motor_rust.py` (Rust =
Python también en patrones partidos en cualquier byte, múltiplos, huellas y los dos límites nuevos).

**`xs:pattern` de XSD.** El mismo AFD con la sintaxis del apéndice F de XSD Parte 2 (`_AnalizadorXSD` en
`expresiones.py`): anclada a todo el valor, `^` y `$` como caracteres normales, `\i` y `\c` (nombres de XML 1.0, quinta
edición), `\d`, `\w` y `\p{..}` con las categorías de Unicode (tabla de `unicodedata`) y resta de clases
`[a-z-[aeiou]]`. Varios `pattern` de un mismo paso de restricción se combinan con «o» (una sola prueba, el AFD de la
unión); los de pasos de derivación distintos, con «y». En `xs:string` se aplica al texto tal cual; en los tipos numéricos
y `xs:boolean`, sin los espacios de los lados (el motor no le pasa al AFD los bytes en blanco: si hay uno en medio, ya
falla la forma léxica, que va antes). Solo quedan diferidos los bloques Unicode (`\p{IsBasicLatin}`).
Diferencias conocidas con el oráculo (`elementpath`): fuera de una clase deja `\w` y `\s` a `re` de Python (`_` cuenta
en `\w`) y en `\i`/`\c` no incluye el plano astral; aquí se sigue la especificación.

### 10. Conversión de esquemas e inferencia

Paquete `compilador/conversion/`. Todo lo que sale está dentro del subconjunto que compila el compilador; lo que el otro
lenguaje no puede expresar no se calla: queda en los **avisos** de la conversión, con la ruta donde estaba.

| Conversión | Cómo | Módulo |
|---|---|---|
| JSON → JSON Schema | Biblioteca **GenSON** (`genson`): fusiona varios ejemplos; `required` = claves presentes en todos | `inferir_json.py` |
| XML → XSD | A mano. Un tipo anónimo por ruta de elemento. Si los hijos siguen un orden común en todos los ejemplos (cada nombre en un solo tramo, orden topológico sin ciclos), `sequence` con `minOccurs`/`maxOccurs` vistos; si no, `choice` repetido. Texto: `xs:boolean` ⊂ `xs:integer` ⊂ `xs:decimal` ⊂ `xs:string` | `inferir_xsd.py` |
| XSD → JSON Schema | A mano, sobre el modelo ya resuelto de `compilador/xsd/modelo.py`. Cuántas veces puede aparecer cada hijo (suma en `sequence`, mín./máx. en `choice`, por las ocurrencias de la partícula) → `required`, arreglo con `minItems`/`maxItems`. Un `choice` que aparece una vez: «a lo sumo una rama» (`not`/`anyOf` de pares en `required`) y, si aparece siempre, «al menos una» | `xsd_a_json.py` |
| JSON Schema → XSD | A mano. Objeto → `complexType`/`sequence` en el orden de `properties`; `$defs` + `$ref` → tipos con nombre (también recursivos); `allOf` se combina; `X o null` → `minOccurs="0"` | `json_a_xsd.py` |
| `pattern` ↔ `xs:pattern` | Se vuelve a imprimir el árbol que ya construye `automata/expresiones.py` con la sintaxis del otro lado, con los conjuntos como rangos explícitos. Anclaje: XSD → `^(?:R)$`; ECMA → se quitan `^`/`$` de los extremos y se añade `[\s\S]*` donde no están | `patrones.py` |
| Documento XML ↔ JSON | Guiado por el XSD (tipos y qué hijos se repiten), para probar las conversiones y para la web | `documentos.py` |

**Convención XML ↔ JSON** (`convencion.py`): un elemento de tipo simple sin atributos es su valor (entero, número,
booleano o cadena según el tipo); con atributos o hijos es un objeto, con `"@atributo"`, un miembro por hijo y `"$"` para
el texto junto a atributos; un hijo que el modelo deja repetir es siempre un arreglo. El documento es el valor de la raíz
(su nombre va en `title`); con varios elementos globales, un objeto con una sola propiedad. Sin equivalente: el orden de
los hijos y los contenedores vacíos (`{}` se escribe como un elemento vacío, igual que `""`; `[]`, como ninguno).

Lo que no se traduce y se avisa: en XSD → JSON, el orden y los `pattern` sobre tipos numéricos; `\d` de XSD (dígitos de
todos los alfabetos) se escribe `\d` de ECMA ([0-9]). En JSON → XSD, `anyOf`/`oneOf` (salvo «X o null»), `not`, `if`,
tipos unión, `additionalProperties` distinto de `false` (sin `xs:any` el XSD rechaza lo no declarado), `patternProperties`,
`propertyNames`, `min/maxProperties`, `dependent*`, `uniqueItems`, `contains`, `prefixItems`, `multipleOf` (salvo 1, que da
`xs:integer`), y los arreglos que no son valor de una propiedad (se escriben con hijos `<elemento>`).

Desde `Código/`:

```
.venv\Scripts\python -m compilador.conversion inferir ej1.json ej2.json     # o .xml → XSD
.venv\Scripts\python -m compilador.conversion xsd-a-json esquema.xsd
.venv\Scripts\python -m compilador.conversion json-a-xsd esquema.json
.venv\Scripts\python -m compilador.conversion xml-a-json esquema.xsd documento.xml
.venv\Scripts\python -m compilador.conversion json-a-xml esquema.xsd documento.json
```

Web: pestaña **Conversión** y rutas `POST /api/inferir`, `/api/convertir`, `/api/convertir-documento`.

Pruebas (`pruebas/test_conversion.py`): las expresiones traducidas reconocen las mismas cadenas (con los AFD del
proyecto); todo documento válido según un XSD, pasado a JSON, cumple el JSON Schema convertido (oráculo `jsonschema`) y
al menos el 85 % de los inválidos lo incumple (los demás solo cambian el orden); en los esquemas JSON que se traducen sin
avisos, `jsonschema` y `xmlschema` dan el mismo veredicto sobre cada documento y su versión XML; ida y vuelta
XSD → JSON → XSD; los ejemplos cumplen siempre el esquema inferido.
