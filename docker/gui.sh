#!/bin/sh
# Runs the real window on a virtual display and serves it over noVNC; started by
# `docker compose up gui`. State lives in .gui/ (gitignored): a writable copy of
# the test comics, so removals can really be applied, and a home directory for
# the app's settings, cache and history, so they survive a restart.
set -eu

STATE=/workspace/.gui
LIBRARY=$STATE/library
export HOME=$STATE/home
mkdir -p "$HOME" "$LIBRARY"

if [ -z "$(ls -A "$LIBRARY")" ]; then
    echo "First run: copying the test comics from /comics into .gui/library ..."
    find /comics -maxdepth 1 -type f \( -iname '*.cbz' -o -iname '*.cbr' \
        -o -iname '*.cb7' -o -iname '*.zip' \) -exec cp -p {} "$LIBRARY/" \;
fi

export DISPLAY=:1
Xvfb "$DISPLAY" -screen 0 "${CC_GUI_SIZE:-1600x1000x24}" -nolisten tcp &
while [ ! -e /tmp/.X11-unix/X1 ]; do sleep 0.1; done
openbox &
x11vnc -display "$DISPLAY" -rfbport 5900 -localhost -forever -shared -nopw -quiet &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo
echo "Comic Cleaner is at http://localhost:${CC_GUI_PORT:-6080}/vnc.html?autoconnect=1&resize=scale"
echo "Close the window to restart it with the same library; Ctrl+C here to stop."
echo

unset QT_QPA_PLATFORM
while :; do
    python -m comiccleaner "$LIBRARY" || echo "Exited with status $?."
    echo "Window closed; starting it again in 2 s."
    sleep 2
done
