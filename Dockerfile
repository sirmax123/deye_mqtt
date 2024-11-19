FROM python:3.12-slim

RUN \
    mkdir /deye_exporter

WORKDIR /deye_exporter

COPY requirements.txt .
COPY deye ./deye
COPY deye_exporter.py .


RUN \
    pwd && \
    ls -lsa && \
    pip3 \
        install -r requirements.txt && \
    chmod +x deye_exporter.py


CMD /deye_exporter/deye_exporter.py