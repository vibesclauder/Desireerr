# Desireerr in a container. No registry: torrent-box builds this straight from the repo
#   docker build -t desireerr https://github.com/vibesclauder/desireerr.git#main
# and again every week, so a push to main is all a new version needs. Standard library only.
FROM python:3.14-slim
COPY desireerr.py /app/
COPY web /app/web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DESIREERR_DATA=/data DESIREERR_CACHE=/cache
EXPOSE 5056
# A checkout mounted at /code runs instead of the copy built in (to try changes without a
# build). Arguments go to desireerr.py: none for the app, "init" to set it up (README).
ENTRYPOINT ["/bin/sh", "-c", "exec python3 \"$([ -f /code/desireerr.py ] && echo /code || echo /app)/desireerr.py\" \"$@\"", "desireerr"]
