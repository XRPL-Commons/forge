# Pin base images by digest for production builds. Never copy host authentication.
ARG NODE_IMAGE=node:24-slim
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${NODE_IMAGE} AS harnesses
COPY --chmod=0755 forge/harnesses /opt/forge/harnesses
RUN node /opt/forge/harnesses/install.cjs
RUN npm install --global corepack@0.34.0 playwright-core@1.55.0 \
    && COREPACK_HOME=/opt/forge/corepack corepack prepare pnpm@10.23.0 --activate
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git gh ca-certificates bubblewrap socat ripgrep build-essential chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /work && chown 1000:1000 /work \
    && mkdir -p /opt/forge/claude-tools && mv /usr/bin/bwrap /opt/forge/claude-tools/bwrap
COPY --from=harnesses /usr/local/bin/node /usr/local/bin/node
COPY --from=harnesses /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=harnesses /usr/local/lib/forge-harnesses /usr/local/lib/forge-harnesses
COPY --from=harnesses /usr/local/include/node /usr/local/include/node
COPY --from=harnesses /opt/forge/corepack /opt/forge/corepack
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && corepack enable
COPY --chmod=0755 forge/harnesses /opt/forge/harnesses
RUN node /opt/forge/harnesses/install.cjs --links-only \
    && python3 /opt/forge/harnesses/claude/prepare_launch.py
# Updates run with a private umask; image permissions must not inherit it.
COPY --chmod=0644 forge/factory/job.py forge/factory/harness.py forge/factory/probe.py /opt/forge/
COPY --chmod=0755 forge/factory/build_environment.py /opt/forge/build_environment.py
COPY --chmod=0644 deploy/runner/browser-canary.cjs /opt/forge/browser-canary.cjs
RUN ln -s /opt/forge/build_environment.py /usr/local/bin/safe-build
LABEL org.forge.build-environment="1"
USER 1000:1000
WORKDIR /work
