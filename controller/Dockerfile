FROM python:3.12-slim

# The controller is standard-library only, so there is nothing to install.
WORKDIR /app
COPY controller/ledger.py controller/metrics.py controller/scheduler.py \
     controller/status.py controller/runletd.py controller/kubernetes_runtime.py \
     ./

RUN useradd --create-home --shell /bin/sh runlet \
 && mkdir -p /var/lib/runlet \
 && chown -R runlet:runlet /app /var/lib/runlet

USER runlet
EXPOSE 9187 8787

# The ledger lives on a volume: state survives a restart.
ENTRYPOINT ["python3", "/app/runletd.py"]
