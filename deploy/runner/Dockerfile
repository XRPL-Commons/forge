# Pin base images by digest for production builds. Never copy host authentication.
ARG NODE_IMAGE=node:22-slim
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${NODE_IMAGE} AS codex
ARG CODEX_VERSION=0.153.4
RUN npm install --global @openai/codex@${CODEX_VERSION}
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git gh ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /work && chown 1000:1000 /work
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
COPY forge/factory/job.py forge/factory/harness.py /opt/forge/
COPY deploy/runner/requirements.toml /etc/codex/requirements.toml
USER 1000:1000
WORKDIR /work
