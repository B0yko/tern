# Tern demo — the reel

Twenty files (12 photos, 8 videos) used as a search test set.

Every file is named the way a camera names things: `DSC_4471.jpg`,
`A001_C003_0731XB.mp4`. That is deliberate. Tern has a filename channel that
boosts hits whose basename contains a query token, and on a set named
`sushi.jpg` / `range_rover.jpg` that channel does all the work while the three
real channels take the credit. Camera names take the shortcut away: nothing
here can be found except by what was said, what was on screen, or what was in
the frame.

Media is **not committed** (see the repo `.gitignore`); `./download.sh` fetches
it. The index lives in `../db/` alongside the rest of the workspace.

---

## Verified queries

With the folder downloaded and indexed (see [Reproduce](#reproduce)), start
the backend from the repository root and open it:

```bash
./run.sh
```

Every query below was run through `/api/search` on 24 Sep 2026, on the
author's MacBook Pro, with the app's default limit of 30, against the demo
workspace indexed on 15 Aug 2026:
this folder plus `real_photos` and `real_videos`, and eight synthetic podcast
episodes kept outside the repository (37 files in all). The stated result is what
came back. `n` is the number of hits returned.

### Two channels on one photo

| Query | Result |
|---|---|
| `range rover` | `DSC_4471.jpg` at #1, `ocr+visual`, n=2 |

The file is called `DSC_4471.jpg`, so there is nothing in the name to match.
Apple Vision read `RANGE ROVER` off the bonnet badge, SigLIP matched the shape
of the car, and the result badge shows both channels.

### Found by the picture alone

| Query | #1 result | n |
|---|---|---|
| `dog running on grass` | `DSC_4238.jpg` | 4 |
| `a snowy alpine peak` | `DSC_3844.jpg` | 5 |
| `sushi` | `IMG_2384.jpg` | 1 |
| `bicycle` | `IMG_2196.jpg` | 1 |
| `coffee cup` | `DSC_4102.jpg` | 1 |
| `neon signs at night` | `DSC_5120.jpg` | 4 |
| `rocket launch at night` | `A001_C003_0731XB.mp4` @ 01:01 | 7 |
| `spacewalk outside the station` | `A001_C007_0731XB.mp4` @ 00:13 | 25 |
| `astronaut in a spacesuit` | `A001_C007_0731XB.mp4` @ 01:38 | 17 |
| `entry descent and landing` | `A002_C004_0802LM.mp4` @ 00:48 | 30 |

For the videos, the long tail of `n` is weaker visual matches from other
clips; the top hit is the one that matters.

### Speech, and all three channels agreeing

| Query | #1 result | Channels | n |
|---|---|---|---|
| `hurricane` | `B001_C002_0805RT.mp4` @ 00:01 | `ocr+transcript+visual` | 10 |
| `astronaut` | `B001_C002_0805RT.mp4` @ 02:01 | `ocr+transcript+visual` | 28 |
| `galaxies` | `B001_C006_0805RT.mp4` @ 00:43 | `transcript+visual` | 17 |
| `telescope` | `B001_C006_0805RT.mp4` @ 01:05 | `ocr+transcript+visual` | 4 |
| `International Space Station` | `B001_C002_0805RT.mp4` @ 00:01 | `transcript` | 2 |
| `Jessica` | `A001_C007_0731XB.mp4` @ 00:03 | `transcript` | 5 |

### Known ranking quirks

The older `../real_photos/picsum_*.jpg` fixtures are named after content they
do not contain (`picsum_office.jpg` is an aerial shot of mountains;
`picsum_beach.jpg` is a hazy city skyline). The filename channel scores a flat
0.50 for a name match, several times any real SigLIP cosine, so those files
take #1 and the correct photo drops to #2:

| Query | #1 result | The photo that shows it (#2) |
|---|---|---|
| `an airliner lifting off the runway` | `picsum_office.jpg`, because the token `off` matched inside `office` | `DSC_3910.jpg` |
| `sunset over a tropical beach` | `picsum_beach.jpg`, a city skyline | `DSC_3987.jpg` |
| `handwritten equations on a whiteboard` | `picsum_whiteboard.jpg`, a random photo | `IMG_2210.jpg` |

The phrasings in the tables above avoid these collisions. `snowy mountain
peak` collides with `picsum_mountain.jpg` the same way; `a snowy alpine peak`
does not.

Deleting the three fixtures removes the collisions. From the repository root,
with `./run.sh` running:

```bash
rm demo/real_photos/picsum_{office,beach,whiteboard}.jpg
curl -X POST http://127.0.0.1:18765/api/folders/remove \
  -H 'Content-Type: application/json' \
  -d "{\"folder\": \"$PWD/demo/real_photos\"}"
```

(That drops all seven `real_photos` entries from the index; re-index the
folder to get the other four back.)

### Two that return nothing, and why

- `black SUV`. The cosine is real but lands under the noise floor, so the
  ranker drops it. `range rover` is the query that works.
- `Jessica Meir`. Whisper Q5 transcribed the surname as `Me ir`, two tokens,
  so the phrase never matches. `Jessica` alone works. The same word-splitting
  shows up elsewhere in these transcripts (`Per se ver ance`, `Artem is`), so
  a proper noun can miss on speech even when it was said clearly.

---

## What is in here

### Photos (Wikimedia Commons)

| File | Shows | Licence | Credit |
|---|---|---|---|
| `DSC_4471.jpg` | black Range Rover in a car park, badge legible on the bonnet | CC BY-SA 4.0 | Damian B Oh |
| `DSC_4102.jpg` | hands round a coffee cup in a café | CC0 | Chevanon Photography |
| `DSC_4238.jpg` | brindle dog mid-stride on a playing field | CC BY 2.5 | Jlantzy (Jamie Lantzy) |
| `DSC_3987.jpg` | palms over a beach at sunset, Koh Mak | CC BY 4.0 | Vyacheslav Argenberg |
| `DSC_3844.jpg` | Marmolada, snow-covered peak | CC0 | Marco Bonomo |
| `DSC_5120.jpg` | Times Square at night, dense neon signage | CC BY-SA 2.0 | The All-Nite Images |
| `IMG_2210.jpg` | whiteboard covered in handwritten maths | CC BY 2.0 | Preply.com Images |
| `DSC_5077.jpg` | speakers on a conference stage, projected slide behind | CC BY-SA 4.0 | Jwf |
| `IMG_2384.jpg` | sushi platter, Tokyo | CC BY-SA 4.0 | Joli Rumi |
| `DSC_3910.jpg` | Lufthansa A321 rotating off the runway at sunset | CC0 | Alf van Beem |
| `IMG_2196.jpg` | bicycles against a brick wall | CC0 | Myles Tan |
| `DSC_5203.jpg` | shopping-centre storefronts, large shop signage | CC BY-SA 4.0 | Tdorante10 |

Fetched as Wikimedia's 1920 px renditions rather than full resolution,
because the indexer downscales anyway and the originals run to 40 MP.
`DSC_4238.jpg` is fetched as the original, which is smaller.

### Videos (NASA and Blender Foundation)

| File | Shows | Length | Licence |
|---|---|---|---|
| `A001_C003_0731XB.mp4` | Artemis I night launch recap, narrated, title cards | 2:32 | Public domain (NASA) |
| `A001_C007_0731XB.mp4` | first all-woman spacewalk, narrated | 2:04 | Public domain (NASA) |
| `A002_C001_0802LM.mp4` | Earth from orbit, music bed only, no speech | 3:11 | Public domain (NASA) |
| `A002_C004_0802LM.mp4` | Perseverance entry, descent and landing; mission-control audio | 2:11 | Public domain (NASA) |
| `A002_C009_0802LM.mp4` | quiet supersonic aircraft programme, hangar and lab | 2:50 | Public domain (NASA) |
| `B001_C002_0805RT.mp4` | weekly news bulletin: anchor, lower-thirds, three stories | 2:57 | Public domain (NASA) |
| `B001_C006_0805RT.mp4` | James Webb first images, narrated over on-screen captions | 3:10 | Public domain (NASA) |
| `C001_C001_0811KD.mp4` | Sintel trailer, scripted dialogue over heavy title cards | 0:52 | CC BY 3.0, Blender Foundation |

Two of the eight (`A002_C001`, `A002_C009`) have a music bed and no speech.
Whisper writes only music markers for them (`* music *`, `Music playing`), so
`music` finds both on the transcript channel. Both also carry on-screen
captions, and any other text hit on them comes from those
(`sonic boom` → `A002_C009` on `ocr+visual`).

Total 19:47 of video, about 60 MB on disk with the photos.

### Licensing

The media is fetched to the local machine for testing; this repository does
not redistribute it. NASA material is US public domain. The Sintel trailer
and the CC BY photographs need attribution if you pass them on. Five files
are **share-alike**: `DSC_4471`, `DSC_5120`, `DSC_5077`, `IMG_2384` and
`DSC_5203`. `scripts/prepare_bundle.sh` copies all of `demo/` into an app
bundle, so a bundle built while this folder is populated carries them:
exclude the folder, or swap those five for CC0 equivalents, before you share
such a bundle.

---

## Reproduce

From the repository root:

```bash
demo/demo_reel/download.sh
```

Then index it, with the backend **stopped**. ChromaDB's `PersistentClient`
is single-process, and a second client against a live server corrupts the
collection (`Error executing plan: Internal error: Error finding id`), which
only a full re-index recovers from:

```bash
cd service_pipeline
uv run python -m tern.cli -w ../demo index ../demo/demo_reel -l en
```

Or index the folder from the sidebar with the app running, which routes
through the one process that already owns the collection, or run
`scripts/init_demo.sh`, which indexes everything under `demo/` through the
API.

Rebuilt from the three download scripts alone (29 files, about 40 minutes of
media), `scripts/init_demo.sh` takes about two minutes, backend start
included.
