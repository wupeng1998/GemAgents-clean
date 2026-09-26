FROM python:3.11.15-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS builder

WORKDIR /build
RUN python -m pip install --no-cache-dir build==1.6.1 hatchling==1.32.3
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --no-isolation --outdir /wheel

FROM python:3.11.15-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

WORKDIR /app
COPY constraints/py311-all-extras.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY --from=builder /wheel /wheel
RUN python -m pip install --no-cache-dir --no-deps /wheel/gemagents-*.whl
ENTRYPOINT ["gemagents"]
