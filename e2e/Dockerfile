# Hermetic end-to-end image for vboxfront.
#
# Everything the suite needs and nothing it does not: Python, PyQt6 and the
# handful of system libraries Qt dlopens even under the offscreen platform.
# VirtualBox is deliberately absent — e2e/fake_vboxmanage.py stands in for it,
# so a run depends on no host state, no display, no network and no /dev/vboxdrv.
FROM python:3.12-slim-bookworm

# Qt loads these at runtime whatever the platform plugin; without them the
# offscreen plugin fails with a bare "could not load the Qt platform plugin".
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libegl1 \
      libgl1 \
      libglib2.0-0 \
      libxkbcommon0 \
      libxkbcommon-x11-0 \
      libdbus-1-3 \
      libfontconfig1 \
      libfreetype6 \
 && rm -rf /var/lib/apt/lists/*

# Not root. Root ignores directory permission bits, so a suite running as
# root silently skips past every "this folder cannot be written" branch —
# which is exactly how the trash's fallback-to-the-user-directory test came
# to pass locally and fail only in the container.
RUN useradd --create-home --shell /bin/sh vboxfront

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY vboxfront.py ./
COPY tests/ ./tests/
COPY e2e/ ./e2e/

# The packaging inputs are not built here, but `tests/test_args.py::VersionTest`
# reads them: it is the guard that keeps APP_VERSION, the changelog's top
# stanza, the spec and the build scripts from drifting apart. Leaving them out
# turned that guard into three errors instead of a check.
COPY Makefile vboxfront.spec ./
COPY scripts/ ./scripts/

# Offscreen Qt, and a config home inside the container so QSettings has
# somewhere writable that dies with it.
ENV QT_QPA_PLATFORM=offscreen \
    HOME=/home/vboxfront \
    XDG_CONFIG_HOME=/tmp/vboxfront-config \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER vboxfront

# Both suites: the unit tests prove the commands are built right, the e2e
# tests prove the workflows built out of them work.
CMD ["sh", "-c", "python -m unittest discover -s tests -t . -v && python -m unittest discover -s e2e -t . -v"]
