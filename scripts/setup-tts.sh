#!/bin/sh
# Spoken briefings: Kokoro TTS (82M, Apache-2.0) on the Pi. Creates /opt/newsrelay/tts/venv from the
# hash-pinned requirements-tts.lock, downloads the model files (verified by SHA-256) and enables
# newsrelay-audio.timer, which renders MP3s + word timings in the background after each run.
# German edition for the website: Argos Translate en->de model and the Piper voice
# de_DE-thorsten-medium (Thorsten-Voice dataset, CC0), patched once so it reports phoneme durations (word timings).
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
fetch() {  # name sha256 [url]
  f="$TTS/models/$1"
  if [ -f "$f" ] && echo "$2  $f" | sha256sum -c --status; then return 0; fi
  curl -fsSL -o "$f.part" "${3:-$REL/$1}"
  echo "$2  $f.part" | sha256sum -c --status || { rm -f "$f.part"; echo "checksum mismatch: $1" >&2; exit 1; }
  mv "$f.part" "$f"
}
fetch kokoro-v1.0.onnx beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a
fetch voices-v1.0.bin bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
# German edition
ARGOS=translate-en_de-1_3
fetch "$ARGOS.argosmodel" 6cd847f0c06c9c66013e6b0932e07fd54a6d90894659c02bf6c5247b72fb25b1   "https://argos-net.com/v1/$ARGOS.argosmodel"
if [ ! -f "$TTS/models/$ARGOS/model/model.bin" ]; then
  "$TTS/venv/bin/python" -I -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); [z.extract(n, sys.argv[2]) for n in z.namelist() if n.startswith(sys.argv[3] + '/model/') or n == sys.argv[3] + '/sentencepiece.model']"     "$TTS/models/$ARGOS.argosmodel" "$TTS/models" "$ARGOS"
fi
PIPER=https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium
VOICE=de_DE-thorsten-medium
fetch "$VOICE.orig.onnx" 7e64762d8e5118bb578f2eea6207e1a35a8e0c30595010b666f983fc87bb7819 "$PIPER/$VOICE.onnx"
fetch "$VOICE.onnx.json" 974adee790533adb273a1ac88f49027d2a1b8f0f2cf4905954a4791e79264e85 "$PIPER/$VOICE.onnx.json"
if [ ! -f "$TTS/models/$VOICE.onnx" ] || [ "$TTS/models/$VOICE.orig.onnx" -nt "$TTS/models/$VOICE.onnx" ]; then
  "$TTS/venv/bin/python" -I -m piper.patch_voice_with_alignment "$TTS/models/$VOICE.orig.onnx"     --output "$TTS/models/$VOICE.onnx.part" >/dev/null
  mv "$TTS/models/$VOICE.onnx.part" "$TTS/models/$VOICE.onnx"
fi
chmod -R a+rX "$TTS"
for u in newsrelay-audio.service newsrelay-audio.timer; do
  install -m 0644 -o root -g root "$SRC/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now --quiet newsrelay-audio.timer
echo "spoken briefings enabled; first render: sudo systemctl start newsrelay-audio.service (runs in the background)"
