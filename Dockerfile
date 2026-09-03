# n8n 2.31.x ships as a Docker Hardened Image: Alpine 3.24 with the package
# manager REMOVED. `apk add ffmpeg python3` fails with "apk: not found".
# Both dependencies are therefore staged in from other images.
#
# Pinned, not :latest — this image reuses the pre-existing n8n_data volume and
# n8n DB migrations are one-way, so a floating tag could pull a build older
# than the one that wrote the DB and refuse to start.

# --- python3 ---------------------------------------------------------------
# Same Alpine 3.24 base as n8n, so the musl ABI matches. The interpreter needs
# only libpython + musl (verified with ldd), so staging these three paths is
# enough — no library conflicts with the base image.
FROM alpine:3.24 AS pybuild
RUN apk add --no-cache python3 && \
    mkdir -p /stage/usr/bin /stage/usr/lib && \
    cp -a /usr/bin/python3*      /stage/usr/bin/ && \
    cp -a /usr/lib/libpython3*   /stage/usr/lib/ && \
    cp -a /usr/lib/python3.*     /stage/usr/lib/

# --- ffmpeg ----------------------------------------------------------------
# Statically linked, so it drops in as two self-contained binaries. Do NOT
# switch to Alpine's ffmpeg package: it drags in 116 shared libraries
# including libssl/libcrypto/zlib, which would overwrite the ones Node links
# against in the base image.
FROM mwader/static-ffmpeg:7.1 AS ffmpeg

# --- final -----------------------------------------------------------------
FROM n8nio/n8n:2.31.7
USER root
COPY --from=pybuild /stage/ /
COPY --from=ffmpeg  /ffmpeg  /usr/local/bin/ffmpeg
COPY --from=ffmpeg  /ffprobe /usr/local/bin/ffprobe
RUN python3 -c "import json,subprocess,base64,os,sys" && \
    ffprobe -version >/dev/null && \
    ffmpeg -hide_banner -filters 2>/dev/null | grep -q showinfo
USER node
