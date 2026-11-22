# Desireerr in a container. GitHub Actions (.github/workflows/image.yml) publishes it to
# ghcr.io/vibesclauder/desireerr: latest on a merge to main, dev on a push to dev.
# Standard library only.
FROM python:3.14-slim
LABEL org.opencontainers.image.source=https://github.com/vibesclauder/desireerr
COPY desireerr.py /app/
COPY web /app/web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DESIREERR_DATA=/data DESIREERR_CACHE=/cache
EXPOSE 5056
# A checkout mounted at /code runs instead of the copy built in (to try changes without a
# build). Arguments go to desireerr.py: none for the app, "init" to set it up (README).
ENTRYPOINT ["/bin/sh", "-c", "exec python3 \"$([ -f /code/desireerr.py ] && echo /code || echo /app)/desireerr.py\" \"$@\"", "desireerr"]
