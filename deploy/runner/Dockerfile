# Pin base images by digest for production builds. Never copy host authentication.
ARG NODE_IMAGE=node:22-slim
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${NODE_IMAGE} AS harnesses
COPY forge/harnesses /opt/forge/harnesses
RUN node /opt/forge/harnesses/install.cjs
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git gh ca-certificates bubblewrap socat ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /work && chown 1000:1000 /work \
    && mkdir -p /opt/forge/claude-tools && mv /usr/bin/bwrap /opt/forge/claude-tools/bwrap
COPY --from=harnesses /usr/local/bin/node /usr/local/bin/node
COPY --from=harnesses /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY forge/harnesses /opt/forge/harnesses
RUN node /opt/forge/harnesses/install.cjs --links-only
COPY forge/factory/job.py forge/factory/harness.py forge/factory/probe.py /opt/forge/
USER 1000:1000
WORKDIR /work
