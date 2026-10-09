#!/bin/sh
# Spoken briefings: Kokoro TTS (82M, Apache-2.0) on the Pi. Creates /opt/newsrelay/tts/venv from the
# hash-pinned requirements-tts.lock, downloads the model files (verified by SHA-256) and enables
# newsrelay-audio.timer, which renders MP3s + word timings in the background after each run.
# Idempotent. Run as root from the current release: sudo sh /opt/newsrelay/current/src/scripts/setup-tts.sh
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
SRC=/opt/newsrelay/current/src
TTS=/opt/newsrelay/tts
REL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1
mkdir -p "$TTS/models"
if [ ! -x "$TTS/venv/bin/python" ]; then python3 -m venv "$TTS/venv"; fi
PIP_CONFIG_FILE=/dev/null "$TTS/venv/bin/pip" install -q --require-hashes --no-deps \
  -r "$SRC/requirements.lock" -r "$SRC/requirements-tts.lock"
fetch() {  # name sha256
  f="$TTS/models/$1"
  if [ -f "$f" ] && echo "$2  $f" | sha256sum -c --status; then return 0; fi
  curl -fsSL -o "$f.part" "$REL/$1"
  echo "$2  $f.part" | sha256sum -c --status || { rm -f "$f.part"; echo "checksum mismatch: $1" >&2; exit 1; }
  mv "$f.part" "$f"
}
fetch kokoro-v1.0.onnx beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a
fetch voices-v1.0.bin bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
chmod -R a+rX "$TTS"
for u in newsrelay-audio.service newsrelay-audio.timer; do
  install -m 0644 -o root -g root "$SRC/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now --quiet newsrelay-audio.timer
echo "spoken briefings enabled; first render: sudo systemctl start newsrelay-audio.service (runs in the background)"
