ARG BUILD_FROM
FROM ${BUILD_FROM}

# chromaprint provides `fpcalc` (only needed for --fingerprint; harmless otherwise).
RUN apk add --no-cache python3 py3-pip chromaprint \
    && ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /opt/music-tagger
COPY pyproject.toml requirements.txt README.md ./
COPY music_tagger ./music_tagger
# Installs deps (beets, mutagen, anthropic) and the `music-tagger` console script.
RUN pip3 install --no-cache-dir --break-system-packages .

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
