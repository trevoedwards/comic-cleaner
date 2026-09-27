FROM python:3.12-bookworm AS dev

WORKDIR /workspace
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# unrar lives in non-free; Debian's 7z has no RAR codec, so without it no .cbr opens.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
      libgl1 \
      libegl1 \
      libxkbcommon0 \
      libdbus-1-3 \
      libglib2.0-0 \
      libxcb1 \
      p7zip-full \
      unrar \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[dev,build]"

# The real window on a virtual display, served to a browser by noVNC, for checking
# the GUI by hand on a machine with no desktop session. See docs/development.md.
FROM dev AS gui
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb \
      x11vnc \
      openbox \
      xdotool \
      novnc \
      websockify \
      fonts-dejavu-core \
      libxkbcommon-x11-0 \
      libxcb-cursor0 \
      libxcb-icccm4 \
      libxcb-image0 \
      libxcb-keysyms1 \
      libxcb-randr0 \
      libxcb-render-util0 \
      libxcb-shape0 \
      libxcb-xinerama0 \
      libxcb-xkb1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
