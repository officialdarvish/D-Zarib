FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir psycopg2-binary

COPY src/xui_factor.py /app/src/xui_factor.py
COPY scripts/docker-entrypoint.sh /app/scripts/docker-entrypoint.sh

RUN chmod +x /app/src/xui_factor.py /app/scripts/docker-entrypoint.sh \
    && ln -sf /app/src/xui_factor.py /usr/local/bin/xui-factorctl \
    && ln -sf /app/src/xui_factor.py /usr/local/bin/xui-factor \
    && ln -sf /app/src/xui_factor.py /usr/local/bin/d-zarib

EXPOSE 19090

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["serve"]
