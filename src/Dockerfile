# whatsapp-mauve image: one image for every profile (profiles/<name>/ is mounted at /data).
#   docker build -t whatsapp-mauve:local .      (or: wa build; `docker compose up` also builds it when missing)
# published: ghcr.io/3mro777o/whatsapp-mauve (multi-arch, built by .github/workflows/docker.yml on a v* tag)
FROM node:22-trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg poppler-utils ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir pillow 'whatsapp-chat-exporter[crypt15]'
ENV PATH=/opt/venv/bin:$PATH WA_HOME=/data WA_PHONE_DIR=/phone HOME=/tmp NODE_ENV=production
WORKDIR /app
COPY tools/wa-sync/package.json tools/wa-sync/package-lock.json tools/wa-sync/patch-baileys.mjs tools/wa-sync/
RUN cd tools/wa-sync && npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY tools tools
COPY docker/entrypoint.sh /entrypoint.sh
COPY VERSION /app/VERSION
# release builds (.github/workflows/docker.yml) override the labels via docker/metadata-action; VERSION is the app's own version
ARG VERSION=dev
LABEL org.opencontainers.image.title="whatsapp-mauve" \
      org.opencontainers.image.source="https://github.com/3mro777o/whatsapp-mauve" \
      org.opencontainers.image.version="$VERSION"
# not root by default (compose overrides with the host uid:gid)
USER node
ENTRYPOINT ["/entrypoint.sh"]
CMD ["viewer"]
