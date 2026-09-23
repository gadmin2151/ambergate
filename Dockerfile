FROM nginx:1.28-alpine

LABEL org.opencontainers.image.title="AmberGate" \
      org.opencontainers.image.description="A self-hosted Nginx gateway with a web UI, multi-domain routing, load balancing and cache." \
      org.opencontainers.image.source="https://github.com/gadmin2151/ambergate" \
      org.opencontainers.image.url="https://github.com/gadmin2151/ambergate"

RUN apk add --no-cache python3 tini certbot openssl \
    && rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p /app /data /cache /run/ambergate
WORKDIR /app
COPY ambergate /app/ambergate
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 80 443 8083
VOLUME ["/data", "/cache"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8083/healthz',timeout=2)"
STOPSIGNAL SIGTERM
ENTRYPOINT ["/sbin/tini", "--", "python3", "-m", "ambergate.main"]
