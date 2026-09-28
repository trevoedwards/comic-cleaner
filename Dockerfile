FROM python:3.12-bookworm AS dev

WORKDIR /workspace
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# The xcb-util libraries are for Qt's xcb plugin: PyInstaller bundles what it
# finds, so without them here a Linux build would leave them out, and many
# desktops lack them (libxcb-cursor0 above all).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 \
      libegl1 \
      libxkbcommon0 \
      libxkbcommon-x11-0 \
      libdbus-1-3 \
      libglib2.0-0 \
      libxcb1 \
      libxcb-cursor0 \
      libxcb-icccm4 \
      libxcb-image0 \
      libxcb-keysyms1 \
      libxcb-render-util0 \
      libxcb-shape0 \
      libxcb-util1 \
      libxcb-xkb1 \
      xz-utils \
    && rm -rf /var/lib/apt/lists/*

# The official 7-Zip, which reads .cbr: Debian's p7zip-full and 7zip are built
# without the RAR codec. Pinned, and checked against the release's digest.
ARG SEVENZIP_VERSION=26.03
ARG SEVENZIP_SHA256=dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695
RUN tag=$(echo "$SEVENZIP_VERSION" | tr -d .) \
    && curl -fsSL -o /tmp/7z.tar.xz \
       "https://github.com/ip7z/7zip/releases/download/$SEVENZIP_VERSION/7z$tag-linux-x64.tar.xz" \
    && echo "$SEVENZIP_SHA256  /tmp/7z.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/7z.tar.xz -C /usr/local/bin 7zz \
    && rm /tmp/7z.tar.xz \
    && 7zz i | grep -q Rar5

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
      libxcb-randr0 \
      libxcb-xinerama0 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
