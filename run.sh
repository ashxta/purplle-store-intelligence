# run.sh
#!/usr/bin/env bash
# run.sh — Process all CCTV clips and feed output into the API
# Usage: ./run.sh [path/to/clips/dir] [api_url]
set -e

CLIPS_DIR="${1:-../cctv_footage}"
API_URL="${2:-http://localhost:8000}"
STORE_ID="STORE_BLR_002"
LAYOUT="../store_layout.json"
POS="../pos_transactions.csv"
OUTPUT_DIR="../events"

mkdir -p "$OUTPUT_DIR"

# Camera ID mapping — adjust if your filenames differ
declare -A CAM_MAP=(
  ["CAM 1"]="CAM_ENTRY_01"
  ["CAM 2"]="CAM_FLOOR_02"
  ["CAM 3"]="CAM_BILLING_03"
  ["CAM 4"]="CAM_FLOOR_04"
  ["CAM 5"]="CAM_FLOOR_05"
)

echo "=== Purplle Store Intelligence — Detection Pipeline ==="
echo "Clips dir : $CLIPS_DIR"
echo "API URL   : $API_URL"
echo "Output    : $OUTPUT_DIR"
echo ""

for cam_name in "CAM 1" "CAM 2" "CAM 3" "CAM 4" "CAM 5"; do
  cam_id="${CAM_MAP[$cam_name]}"
  # Find the file (handles spaces in names)
  video_file=$(find "$CLIPS_DIR" -name "${cam_name}.mp4" 2>/dev/null | head -1)

  if [ -z "$video_file" ]; then
    echo "[SKIP] $cam_name — file not found in $CLIPS_DIR"
    continue
  fi

  output_file="$OUTPUT_DIR/${cam_id}.jsonl"
  echo "[RUN] $cam_name → $cam_id"
  echo "      Video : $video_file"
  echo "      Output: $output_file"

  python3 detect.py \
    --video "$video_file" \
    --camera-id "$cam_id" \
    --store-id "$STORE_ID" \
    --layout "$LAYOUT" \
    --pos "$POS" \
    --output "$output_file" \
    --api-url "$API_URL"

  echo "[DONE] $cam_id → events written to $output_file"
  echo ""
done

# Merge all events and bulk-ingest
echo "=== Merging and ingesting all events ==="
cat "$OUTPUT_DIR"/*.jsonl > "$OUTPUT_DIR/all_events.jsonl" 2>/dev/null || true
EVENT_COUNT=$(wc -l < "$OUTPUT_DIR/all_events.jsonl" | tr -d ' ')
echo "Total events: $EVENT_COUNT"

# Batch ingest in chunks of 500
if command -v python3 &>/dev/null; then
  python3 - <<'PYEOF'
import json, requests, sys
with open("../events/all_events.jsonl") as f:
    events = [json.loads(l) for l in f if l.strip()]

api = "http://localhost:8000"
batch_size = 500
for i in range(0, len(events), batch_size):
    batch = events[i:i+batch_size]
    try:
        r = requests.post(f"{api}/events/ingest", json={"events": batch}, timeout=30)
        print(f"Batch {i//batch_size+1}: {r.status_code} ({len(batch)} events)")
    except Exception as e:
        print(f"Batch {i//batch_size+1}: FAILED — {e}")
PYEOF
fi

echo "=== Pipeline complete ==="
echo "Check metrics at: $API_URL/stores/$STORE_ID/metrics"