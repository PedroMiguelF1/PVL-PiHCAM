#!/bin/bash

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export QT_QPA_PLATFORM=xcb
: "${DISPLAY:=:0}"
export DISPLAY
[ -z "$XAUTHORITY" ] && [ -f "$HOME/.Xauthority" ] && export XAUTHORITY="$HOME/.Xauthority"

cd /home/pi/Documents
python3 handyload.py