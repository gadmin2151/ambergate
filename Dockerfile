FROM nginx:1.28-alpine

LABEL org.opencontainers.image.title="Nginx Scale Gateway" \
      org.opencontainers.image.description="A self-hosted Nginx gateway with a web UI, multi-domain routing, load balancing and cache." \
      org.opencontainers.image.source="https://github.com/gadmin2151/nginx-scale-gw" \
      org.opencontainers.image.url="https://github.com/gadmin2151/nginx-scale-gw"

RUN apk add --no-cache python3 tini \
    && rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p /app /data /cache /run/gateway
WORKDIR /app
COPY gateway /app/gateway
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_DATA_DIR=/data \
    GATEWAY_CACHE_DIR=/cache \
    GATEWAY_RUN_DIR=/run/gateway
EXPOSE 80 8083
VOLUME ["/data", "/cache"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8083/healthz',timeout=2)"
STOPSIGNAL SIGTERM
ENTRYPOINT ["/sbin/tini", "--", "python3", "-m", "gateway.main"]
