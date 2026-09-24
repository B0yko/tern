#!/bin/bash
# Fetch the 20-file demo-reel archive: 12 photos + 8 videos, all public domain or
# Creative Commons. Deliberately named like camera originals (DSC_/IMG_/A001_C003)
# so nothing in the filename can be what search matches on — see README.md.
#
# Requires: curl, ffprobe (ffmpeg). Idempotent; re-run to repair a partial fetch.

set -euo pipefail
cd "$(dirname "$0")"

UA="tern-demo-seed/1.0 (local demo workspace; https://github.com/B0yko/tern/issues)"

# fetch <outfile> <url> <label>
#
# Downloads to a .part file and renames only on success, so an interrupted run
# never leaves a truncated media file for the indexer to choke on.
fetch() {
  local out="$1" url="$2" label="$3"
  if [ -s "$out" ]; then
    echo "  = $out  (already there)"
    return 0
  fi
  echo "  > $out  <- $label"
  curl -fsSL --retry 3 --retry-delay 2 -H "User-Agent: $UA" -o "$out.part" "$url"
  mv "$out.part" "$out"
}

echo "Photos (Wikimedia Commons, 1920px renditions)"
fetch "DSC_4471.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Land_Rover_Range_Rover_Autobiography_L405_Santorini_Black_%281%29.jpg/1920px-Land_Rover_Range_Rover_Autobiography_L405_Santorini_Black_%281%29.jpg" \
  "black Range Rover, three-quarter front — CC BY-SA 4.0"
fetch "DSC_4102.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/9/90/Close-up_of_Woman_Holding_Coffee_Cup_at_Cafe_%2828217840617%29.jpg/1920px-Close-up_of_Woman_Holding_Coffee_Cup_at_Cafe_%2828217840617%29.jpg" \
  "hands round a coffee cup in a cafe — CC0"
fetch "DSC_4238.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/f/f8/Dog_playing3.jpg" \
  "dog mid-stride on grass — CC BY 2.5"
fetch "DSC_3987.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b6/Koh_Mak_%28island%29%2C_Thailand%2C_Sunset_on_the_beach_with_palms.jpg/1920px-Koh_Mak_%28island%29%2C_Thailand%2C_Sunset_on_the_beach_with_palms.jpg" \
  "palms over a beach at sunset — CC BY 4.0"
fetch "DSC_3844.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/6/63/Marmolada%2C_Italy.jpg/1920px-Marmolada%2C_Italy.jpg" \
  "snow-covered alpine peak — CC0"
fetch "DSC_5120.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/0/07/Times_Square_neon_2.jpg/1920px-Times_Square_neon_2.jpg" \
  "Times Square at night, dense neon signage — CC BY-SA 2.0"
fetch "IMG_2210.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cc/Maths_Tutor.jpg/1920px-Maths_Tutor.jpg" \
  "whiteboard covered in handwritten maths — CC BY 2.0"
fetch "DSC_5077.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/9/99/Outreachy_organizers_present_at_FOSDEM_2024_02.jpg/1920px-Outreachy_organizers_present_at_FOSDEM_2024_02.jpg" \
  "speakers on a conference stage with a projected slide — CC BY-SA 4.0"
fetch "IMG_2384.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/e/ed/Sushi_food_in_Tokyo%2C_Japan.jpg/1920px-Sushi_food_in_Tokyo%2C_Japan.jpg" \
  "sushi platter — CC BY-SA 4.0"
fetch "DSC_3910.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d6/D-AIRN_Lufthansa_Airbus_A321-131_takeoff_from_Polderbaan%2C_Schiphol_%28AMS_-_EHAM%29_at_sunset%2C_pic1.JPG/1920px-D-AIRN_Lufthansa_Airbus_A321-131_takeoff_from_Polderbaan%2C_Schiphol_%28AMS_-_EHAM%29_at_sunset%2C_pic1.JPG" \
  "airliner rotating off the runway at sunset — CC0"
fetch "IMG_2196.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8f/Bicycles_against_wall_in_23_Foster_St_car_park_%28Unsplash%29.jpg/1920px-Bicycles_against_wall_in_23_Foster_St_car_park_%28Unsplash%29.jpg" \
  "bicycles chained against a brick wall — CC0"
fetch "DSC_5203.jpg" \
  "https://upload.wikimedia.org/wikipedia/commons/thumb/1/13/Bay_Terrace_Shopping_Center_td_%282022-02-16%29_24_-_Old_Navy.jpg/1920px-Bay_Terrace_Shopping_Center_td_%282022-02-16%29_24_-_Old_Navy.jpg" \
  "shopping-centre storefronts, large shop signage — CC BY-SA 4.0"

echo ""
echo "Videos (NASA image library, public domain, ~mobile renditions)"
fetch "A001_C003_0731XB.mp4" \
  "https://images-assets.nasa.gov/video/Artemis%20I%20Launches%20to%20the%20Moon%20%28Official%20NASA%20Recap%29/Artemis%20I%20Launches%20to%20the%20Moon%20%28Official%20NASA%20Recap%29~mobile.mp4" \
  "Artemis I night launch recap, narrated, title cards"
fetch "A001_C007_0731XB.mp4" \
  "https://images-assets.nasa.gov/video/NHQ_2019_1018_all%20woman%20spacewalk/NHQ_2019_1018_all%20woman%20spacewalk~mobile.mp4" \
  "first all-woman spacewalk, narrated"
fetch "A002_C001_0802LM.mp4" \
  "https://images-assets.nasa.gov/video/GSFC_20150420_Orbit_m11858_2014/GSFC_20150420_Orbit_m11858_2014~mobile.mp4" \
  "Earth from orbit, music bed only, no speech"
fetch "A002_C004_0802LM.mp4" \
  "https://images-assets.nasa.gov/video/JPL-20210305-M2020f-0001Perseverance%20Lands%20on%20Mars%20UHD%20Master/JPL-20210305-M2020f-0001Perseverance%20Lands%20on%20Mars%20UHD%20Master~mobile.mp4" \
  "Perseverance touches down on Mars, mission-control audio"
fetch "B001_C002_0805RT.mp4" \
  "https://images-assets.nasa.gov/video/NHQ_2019_0906_Keeping%20an%20eye%20on%20Hurricane%20Dorian%20from%20Space%20on%20This%20Week%20%40NASA%20%E2%80%93%20September%206%2C%202019/NHQ_2019_0906_Keeping%20an%20eye%20on%20Hurricane%20Dorian%20from%20Space%20on%20This%20Week%20%40NASA%20%E2%80%93%20September%206%2C%202019~mobile.mp4" \
  "news-format weekly bulletin, anchor + lower-thirds"
fetch "B001_C006_0805RT.mp4" \
  "https://images-assets.nasa.gov/video/First%20Images%20from%20the%20James%20Webb%20Space%20Telescope%20%28Official%20NASA%20Highlights%29/First%20Images%20from%20the%20James%20Webb%20Space%20Telescope%20%28Official%20NASA%20Highlights%29~mobile.mp4" \
  "James Webb first-images highlights, narrated"
fetch "A002_C009_0802LM.mp4" \
  "https://images-assets.nasa.gov/video/NHQ20220915ARMD01/NHQ20220915ARMD01~mobile.mp4" \
  "quiet supersonic aircraft programme, hangar and lab footage"

echo ""
echo "Video (Blender Foundation, CC BY 3.0)"
fetch "C001_C001_0811KD.mp4" \
  "https://download.blender.org/durian/trailer/sintel_trailer-720p.mp4" \
  "Sintel trailer — scripted dialogue over fantasy animation, heavy title cards"

echo ""
echo "Verifying every file decodes and reporting durations:"
bad=0
for f in *.jpg *.mp4; do
  [ -e "$f" ] || continue
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null || true)
  if [ -z "$d" ] && [ "${f##*.}" = "mp4" ]; then
    echo "  FAIL $f — ffprobe cannot read it"; bad=1; continue
  fi
  sz=$(du -h "$f" | cut -f1)
  if [ -n "$d" ]; then
    printf "  ok   %-22s %6s  %s\n" "$f" "$sz" "$(printf %.0f "$d")s"
  else
    printf "  ok   %-22s %6s\n" "$f" "$sz"
  fi
done
[ "$bad" -eq 0 ] || { echo "One or more files are unreadable. Delete them and re-run."; exit 1; }

cat <<'EOF'

Next: index into the demo workspace. From the repository root, with ./run.sh
stopped (ChromaDB allows one process per workspace):
  cd service_pipeline
  uv run python -m tern.cli -w ../demo index ../demo/demo_reel -l en
Or run scripts/init_demo.sh to index everything under demo/ through the API.

README.md lists verified queries for this set.
EOF
