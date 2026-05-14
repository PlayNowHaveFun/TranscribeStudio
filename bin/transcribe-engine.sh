#!/usr/bin/env bash
# transcribe-engine.sh — local audio + video transcription via whisper.cpp
#
# Generalized engine maintained inside Transcribe Studio. Reads ALL config
# from environment variables so the Python orchestrator can drive per-project
# parameters (model, language, translate, VAD, no-context, chunking, formats)
# without editing the script.
#
# Usage:
#   ./transcribe-engine.sh path/to/file.mp4
#   ./transcribe-engine.sh                   # transcribe every audio/video in pwd
#
# Bakes in the proven defaults from the audio-transcribe skill + our hard-won
# learnings on long therapy recordings:
#   - large-v3 multilingual by default (handles Hindi, Hinglish, English)
#   - VAD ON (silero) — cuts hallucinations, skips silence
#   - max-len 80 — breaks segments so any loop that survives stays shallow
#   - auto-chunk anything > CHUNK_THRESHOLD_MIN (default 10) into CHUNK_MIN-min
#     pieces; merges back into one .txt + .srt with correct timestamp offsets
#   - skips files already transcribed (idempotent on re-runs)
#
# Override any default with an env var:
#   WHISPER_MODEL=ggml-medium.bin                 # different model
#   WHISPER_TRANSLATE=0                           # keep source language
#   WHISPER_LANGUAGE=es                           # non-Hindi source
#   WHISPER_VAD=0                                 # disable VAD
#   WHISPER_NO_CONTEXT=1                          # disable cross-segment context
#                                                 # (best fix for hallucination loops)
#   WHISPER_CHUNK_THRESHOLD_MIN=20                # only chunk files > 20 min
#   WHISPER_CHUNK_MIN=3                           # use 3-min chunks
#   WHISPER_FORMATS=txt,srt,vtt                   # also emit .vtt
#   WHISPER_OUTPUT_DIR=/some/abs/path             # absolute output path
#                                                 # default: <input_dir>/transcriptions
#   WHISPER_MODEL_DIR=/path/to/models             # model search dir
#                                                 # default: ~/Documents/cowork-tools/whisper-models

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

MODEL_DIR="${WHISPER_MODEL_DIR:-$HOME/Documents/cowork-tools/whisper-models}"
MODEL_NAME="${WHISPER_MODEL:-ggml-large-v3.bin}"
MODEL_PATH="$MODEL_DIR/$MODEL_NAME"

LANGUAGE="${WHISPER_LANGUAGE:-hi}"
TRANSLATE="${WHISPER_TRANSLATE:-1}"
NO_CONTEXT="${WHISPER_NO_CONTEXT:-0}"

FORMATS="${WHISPER_FORMATS:-txt,srt}"
OUTPUT_DIR="${WHISPER_OUTPUT_DIR:-transcriptions}"
MAX_LEN="${WHISPER_MAX_LEN:-80}"

VAD="${WHISPER_VAD:-1}"
VAD_MODEL_NAME="${WHISPER_VAD_MODEL:-ggml-silero-v6.2.0.bin}"
VAD_MODEL_PATH="$MODEL_DIR/$VAD_MODEL_NAME"

CHUNK_THRESHOLD_MIN="${WHISPER_CHUNK_THRESHOLD_MIN:-10}"
CHUNK_MIN="${WHISPER_CHUNK_MIN:-5}"

# Emit a JSON-ish status line each major step so the orchestrator can parse it.
# Format: STATUS\t<phase>\t<key=value>...
emit() {
  printf 'STATUS\t%s\n' "$*" >&2
}

# =============================================================================
# Preflight
# =============================================================================

if command -v whisper-cli >/dev/null 2>&1; then
  WHISPER_BIN="whisper-cli"
elif command -v whisper-cpp >/dev/null 2>&1; then
  WHISPER_BIN="whisper-cpp"
else
  echo "Error: whisper-cli not found. Install with: brew install whisper-cpp" >&2
  exit 2
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Error: ffmpeg not found. Install with: brew install ffmpeg" >&2
  exit 2
fi

if [[ ! -f "$MODEL_PATH" ]]; then
  echo "Error: model not found at $MODEL_PATH" >&2
  echo "Download with:" >&2
  echo "  curl -L --fail -o \"$MODEL_PATH\" \\" >&2
  echo "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL_NAME" >&2
  exit 3
fi

if [[ "$VAD" == "1" && ! -f "$VAD_MODEL_PATH" ]]; then
  echo "Error: VAD enabled but VAD model not found at $VAD_MODEL_PATH" >&2
  echo "Download with:" >&2
  echo "  curl -L --fail -o \"$VAD_MODEL_PATH\" \\" >&2
  echo "    https://huggingface.co/ggml-org/whisper-vad/resolve/main/$VAD_MODEL_NAME" >&2
  exit 3
fi

# =============================================================================
# Helpers
# =============================================================================

build_format_flags() {
  local IFS=','
  for fmt in $FORMATS; do
    case "$fmt" in
      txt)  echo -n "-otxt " ;;
      srt)  echo -n "-osrt " ;;
      vtt)  echo -n "-ovtt " ;;
      json) echo -n "-oj " ;;
      csv)  echo -n "-ocsv " ;;
    esac
  done
}

is_video() {
  local lower
  lower="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    *.mp4|*.mov|*.webm|*.mkv|*.avi|*.m4v|*.flv|*.wmv) return 0 ;;
    *) return 1 ;;
  esac
}

get_duration_sec() {
  local d
  d=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$1" 2>/dev/null || echo 0)
  awk -v s="$d" 'BEGIN{printf "%d\n", s+0}'
}

whisper_args() {
  local input_wav="$1" out_base="$2"
  local args=(
    -m "$MODEL_PATH"
    -f "$input_wav"
    -of "$out_base"
    -pp
    -ml "$MAX_LEN"
  )
  for flag in $(build_format_flags); do
    args+=("$flag")
  done
  if [[ "$MODEL_NAME" != *.en.bin ]]; then
    args+=(-l "$LANGUAGE")
    [[ "$TRANSLATE" == "1" ]] && args+=(-tr)
  fi
  if [[ "$VAD" == "1" ]]; then
    args+=(--vad -vm "$VAD_MODEL_PATH")
  fi
  if [[ "$NO_CONTEXT" == "1" ]]; then
    args+=(--no-context)
  fi
  printf '%s\n' "${args[@]}"
}

resolve_out_dir() {
  local input_dir="$1"
  if [[ "$OUTPUT_DIR" = /* ]]; then
    echo "$OUTPUT_DIR"
  else
    echo "$input_dir/$OUTPUT_DIR"
  fi
}

# =============================================================================
# Standard (single-pass) transcription
# =============================================================================

transcribe_standard() {
  local input="$1"
  local in_dir in_name in_base out_dir out_base primary_output
  in_dir="$(dirname "$input")"
  in_name="$(basename "$input")"
  in_base="${in_name%.*}"
  out_dir="$(resolve_out_dir "$in_dir")"
  mkdir -p "$out_dir"
  out_base="$out_dir/$in_base"
  primary_output="${out_base}.txt"

  if [[ -f "$primary_output" ]]; then
    emit "skip file=$in_name reason=already_transcribed"
    return 0
  fi

  emit "phase=preflight file=$in_name mode=standard"

  local tmp_dir tmp_wav
  tmp_dir="$(mktemp -d)"
  tmp_wav="$tmp_dir/audio.wav"
  trap "rm -rf '$tmp_dir'" RETURN

  if ! ffprobe -v error -select_streams a -show_entries stream=codec_type \
       -of csv=p=0 "$input" 2>/dev/null | grep -q audio; then
    emit "phase=skip file=$in_name reason=no_audio_track"
    echo "(no audio track in source file)" > "$primary_output"
    return 0
  fi

  emit "phase=extract_audio file=$in_name"
  if ! ffmpeg -y -loglevel error -i "$input" -vn -ar 16000 -ac 1 -c:a pcm_s16le "$tmp_wav" 2>&1; then
    emit "phase=fail file=$in_name reason=ffmpeg_failed"
    return 1
  fi

  emit "phase=transcribe file=$in_name model=$MODEL_NAME vad=$VAD no_context=$NO_CONTEXT"
  local args=()
  while IFS= read -r line; do args+=("$line"); done < <(whisper_args "$tmp_wav" "$out_base")
  "$WHISPER_BIN" "${args[@]}" 2>&1

  if [[ -f "$primary_output" ]]; then
    emit "phase=done file=$in_name output=$primary_output"
  else
    emit "phase=fail file=$in_name reason=no_output"
    return 1
  fi
}

# =============================================================================
# Chunked transcription (for long files)
# =============================================================================

transcribe_chunked() {
  local input="$1"
  local in_dir in_name in_base out_dir out_base primary_output dur
  in_dir="$(dirname "$input")"
  in_name="$(basename "$input")"
  in_base="${in_name%.*}"
  out_dir="$(resolve_out_dir "$in_dir")"
  mkdir -p "$out_dir"
  out_base="$out_dir/$in_base"
  primary_output="${out_base}.txt"

  if [[ -f "$primary_output" ]]; then
    emit "skip file=$in_name reason=already_transcribed"
    return 0
  fi

  dur=$(get_duration_sec "$input")
  emit "phase=preflight file=$in_name mode=chunked duration_sec=$dur chunk_min=$CHUNK_MIN"

  local chunk_sec=$((CHUNK_MIN * 60))
  local work_dir="${TMPDIR:-/tmp}/transcribe_${in_base}_$$"
  mkdir -p "$work_dir"
  trap "rm -rf '$work_dir'" RETURN

  emit "phase=split file=$in_name"
  ffmpeg -y -loglevel error -i "$input" -vn -ar 16000 -ac 1 -c:a pcm_s16le \
    -f segment -segment_time "$chunk_sec" -reset_timestamps 1 \
    "$work_dir/chunk_%03d.wav"

  local chunks=()
  while IFS= read -r f; do chunks+=("$f"); done < <(ls "$work_dir"/chunk_*.wav 2>/dev/null | sort)
  local num_chunks=${#chunks[@]}
  emit "phase=split_done file=$in_name chunks=$num_chunks"

  local idx=0
  for chunk in "${chunks[@]}"; do
    idx=$((idx + 1))
    local cbase="${chunk%.wav}"
    if [[ -f "${cbase}.txt" ]]; then
      emit "phase=chunk_skip file=$in_name chunk=$idx of=$num_chunks"
      continue
    fi
    emit "phase=chunk file=$in_name chunk=$idx of=$num_chunks"
    local args=()
    while IFS= read -r line; do args+=("$line"); done < <(whisper_args "$chunk" "$cbase")
    if ! "$WHISPER_BIN" "${args[@]}" 2>&1; then
      emit "phase=chunk_fail file=$in_name chunk=$idx of=$num_chunks"
    fi
  done

  emit "phase=merge file=$in_name"
  python3 - "$work_dir" "$out_base" "$chunk_sec" <<'PY'
import sys, glob, re, os
work_dir, out_base, chunk_sec = sys.argv[1], sys.argv[2], int(sys.argv[3])
txts = sorted(glob.glob(os.path.join(work_dir, "chunk_*.txt")))
srts = sorted(glob.glob(os.path.join(work_dir, "chunk_*.srt")))
def fmt(s): return f"{s // 60:02d}:{s % 60:02d}"
with open(out_base + ".txt", "w") as out:
    for i, p in enumerate(txts):
        out.write(f"\n# === chunk {i:03d} (starts at {fmt(i * chunk_sec)}) ===\n")
        with open(p) as f: out.write(f.read().rstrip() + "\n")
def parse_ts(s):
    h, m, rest = s.split(":"); sec, ms = rest.split(",")
    return int(h)*3600000 + int(m)*60000 + int(sec)*1000 + int(ms)
def fmt_ts(ms):
    h = ms // 3600000; m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000; msr = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{msr:03d}"
seg_num = 1
with open(out_base + ".srt", "w") as out:
    for i, p in enumerate(srts):
        offset_ms = i * chunk_sec * 1000
        try:
            with open(p) as f: content = f.read().strip()
        except FileNotFoundError: continue
        if not content: continue
        for block in re.split(r'\n\s*\n', content):
            lines = [l for l in block.strip().split('\n') if l.strip()]
            if len(lines) < 3: continue
            ts = re.match(r'(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)', lines[1])
            if not ts: continue
            sm = parse_ts(ts.group(1)) + offset_ms
            em = parse_ts(ts.group(2)) + offset_ms
            text = '\n'.join(lines[2:])
            out.write(f"{seg_num}\n{fmt_ts(sm)} --> {fmt_ts(em)}\n{text}\n\n")
            seg_num += 1
PY

  if [[ -f "$primary_output" ]]; then
    emit "phase=done file=$in_name output=$primary_output"
  else
    emit "phase=fail file=$in_name reason=merge_failed"
    return 1
  fi
}

# =============================================================================
# Dispatch
# =============================================================================

transcribe_one() {
  local input="$1"
  local dur
  dur=$(get_duration_sec "$input")
  if (( dur > CHUNK_THRESHOLD_MIN * 60 )); then
    transcribe_chunked "$input"
  else
    transcribe_standard "$input"
  fi
}

if [[ $# -gt 0 ]]; then
  for f in "$@"; do
    transcribe_one "$f"
  done
else
  cd "$(dirname "$0")"
  shopt -s nullglob nocaseglob
  for f in *.m4a *.mp3 *.wav *.aac *.flac *.ogg \
           *.mp4 *.mov *.webm *.mkv *.avi *.m4v *.flv *.wmv; do
    transcribe_one "$f"
  done
fi
