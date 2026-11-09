#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/shaddad/.Xauthority

PIKARAOKE_DIR=/home/shaddad/Documents/pikaraoke

# Run pre-flight smoke tests before starting karaoke
python3 "$PIKARAOKE_DIR/preflight_check.py"
if [ $? -ne 0 ]; then
  echo "Pre-flight checks failed. Fix the issues above and try again."
  exit 1
fi

python3 "$PIKARAOKE_DIR/app.py" \
  -y /home/shaddad/.local/bin/yt-dlp \
  --log-level 10 \
  --download-path /home/shaddad/Documents/pikaraoke-songs/ \
  --high-quality \
  --logo-path "$PIKARAOKE_DIR/myvikaraoke.png"
