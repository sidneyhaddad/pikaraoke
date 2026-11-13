#!/bin/bash
cd /home/shaddad/Documents/pikaraoke-ws
python3 server.py \
  --pikaraoke-url http://localhost:5000 \
  --download-path /home/shaddad/Documents/pikaraoke-songs/ \
  --ytdlp-path /home/shaddad/.local/bin/yt-dlp \
  --db-path /home/shaddad/Documents/pikaraoke-songs/karaoke.db \
  --port 5001
