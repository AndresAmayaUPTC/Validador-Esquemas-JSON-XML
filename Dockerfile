# Imagen de la aplicación web: backend en Python, frontend estático y las herramientas de Rust que
# usa en ejecución (el programa de medición y la compilación de validadores a WebAssembly).
#
#   docker build -t validador-web .
#   docker run -p 8000:8000 -e MONGODB_URI="mongodb+srv://..." validador-web
#
# Sin MONGODB_URI guarda los casos en archivos dentro del contenedor (se pierden al borrarlo).
FROM rust:1-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && rustup target add wasm32-unknown-unknown

WORKDIR /app
COPY web/requirements.txt web/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r web/requirements.txt

COPY compilador compilador
COPY validador validador
COPY bench bench
COPY web web

# La carpeta de compilación es /.cargo-target (dos niveles sobre validador/, como en el desarrollo local).
# Se deja compilado el programa de medición y, con un esquema vacío, el motor para WebAssembly:
# así la primera petición no espera a rustc.
RUN cd bench && cargo build --release \
 && cd /app && /venv/bin/python -c "from web.backend import nativo; nativo.wasm_de('{}')"

# El servicio no corre como root. Necesita escribir en la carpeta de compilación (para los .wasm),
# en la de cargo (su archivo de bloqueo) y en el almacén local de casos.
RUN useradd --create-home app \
 && mkdir -p /app/web/datos \
 && chown -R app /.cargo-target /usr/local/cargo /app/web/datos
USER app

EXPOSE 8000
CMD ["/venv/bin/python", "-m", "web.backend", "--host", "0.0.0.0", "--port", "8000"]
