FROM python:3.12-bookworm

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
