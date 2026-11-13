FROM python:3.13-slim

LABEL org.opencontainers.image.title="PatchDeck" \
      org.opencontainers.image.description="clusters-prj の各 CT/VM のパッケージ・Docker をまとめて更新する管理ツール" \
      org.opencontainers.image.source="https://github.com/swmr71/patchdeck"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/config/settings.yaml \
    SSH_KEY_PATH=/ssh/id_ed25519 \
    SSH_KNOWN_HOSTS=/ssh/known_hosts \
    TZ=Asia/Tokyo

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config/settings.yaml /srv/defaults/settings.yaml
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/config", "/ssh"]
EXPOSE 8800
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8800/healthz',timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
