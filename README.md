# duck-lecture

Smart two-microphone mixing for lecture and seminar recordings.

`duck-lecture` takes a video with two audio tracks, a speaker's lapel microphone and a room (shotgun) microphone, and mixes them automatically. When the audience speaks, the speaker is ducked and the room microphone is lifted. When the speaker talks, the room microphone is pulled right down to cut echo and room noise. The video stream is copied, not re-encoded.

## Features

- **Relative level detection** (room mic minus lapel mic), so the speaker's own voice does not trigger the duck.
- **Delay alignment** between the two microphones using cross-correlation, to reduce the "phasey" comb-filter effect.
- **Two-way ducking**: the speaker is ducked during audience speech, and the room mic is ducked during speaker speech.
- **Speech-band detection** (150-4000 Hz) for more reliable decisions.
- **Look-ahead**, so the first syllable of an audience question is not clipped.
- **Smooth gain changes** applied in the dB domain with separate attack and release times.
- **Low memory use** (cumulative-sum RMS, chunked gain application), suitable for long recordings.
- **Final loudness normalisation** (`loudnorm`) and a limiter.
- **Analyse mode** that prints level statistics and a threshold sweep table, to help you pick settings before rendering.

## Requirements

- Python 3.9 or later
- [FFmpeg](https://ffmpeg.org/download.html) available on your `PATH`
- Python packages:

```bash
pip install numpy scipy soundfile
```

### Input format

The input video must contain **two audio streams**:

| Stream | Content |
| ------ | ------- |
| `0:a:0` | Speaker (lapel microphone) |
| `0:a:1` | Audience / room (shotgun microphone) |

If your tracks are in a different order, change `TEACHER_MAP` and `AUDIENCE_MAP` at the top of `duck_lecture_v4.py`.

## Usage

### Direct Python usage

Analyse a recording first (nothing is written):

```bash
python duck_lecture_v4.py analyze lecture.mp4
```

Process it:

```bash
python duck_lecture_v4.py run lecture.mp4 output.mp4
```

### Choosing thresholds

Run `analyze` and look at the sweep table at the end. It shows, for several `--on` and `--floor-margin` values, what percentage of the recording is classed as audience speech and what percentage as speaker speech. Pick the row where audience activity falls roughly between 5% and 20%, then pass those values to `run`:

```bash
python duck_lecture_v4.py run lecture.mp4 output.mp4 --on 12 --off 7 --floor-margin 30
```

### Options

Shared by `analyze` and `run`:

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--on` | `-3.0` | Audience detection turn-on threshold (room dB minus lapel dB) |
| `--off` | `-8.0` | Audience detection turn-off threshold |
| `--floor-margin` | `10.0` | dB above the noise floor (10th percentile) that counts as active |
| `--min-on` | `0.3` | Minimum audience burst length (seconds) |
| `--min-off` | `0.6` | Gaps shorter than this are filled in (seconds) |
| `--teach-hold` | `1.5` | Seconds to keep the room mic closed after the speaker stops (echo) |
| `--no-align` | off | Disable delay alignment |
| `--delay-ms` | auto | Set the delay manually (ms, positive = room mic is late) |
| `--denoise` | off | Apply `afftdn` noise reduction to the room mic |

Additional options for `run`:

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--duck-db` | `-10` | Speaker reduction while the audience speaks (dB) |
| `--aud-duck-db` | `-40` | Room mic reduction while the speaker speaks (dB) |
| `--aud-boost-db` | `3` | Room mic boost while the audience speaks (dB) |
| `--attack` / `--release` | `0.12` / `0.5` | Speaker duck fall and recovery times (s) |
| `--aud-attack` / `--aud-release` | `0.15` / `0.5` | Room mic close and reopen times (s) |
| `--lookahead` | `0.15` | Look-ahead (s) |
| `--lufs` | `-16` | Target loudness |
| `--no-loudnorm` | off | Skip loudness normalisation |

## PowerShell wrapper (Windows)

`run_duckv4.ps1` is a convenience wrapper for batch-style use on Windows. It:

- finds the input file by name (`.mp4`, `.mkv` or `.mov`) inside a sub-folder of your video directory,
- writes the result next to it as `<name>_processed4.mp4`,
- detects your virtual environment and FFmpeg,
- checks that the required Python packages are installed,
- writes a timestamped log to `logs/`.

Before first use, edit the `$base` variable near the top of the script so it points at your own video folder.

```powershell
.\run_duckv4.ps1 -mode analyze -folder lectures -file "2026-10-27 15-03-05"
.\run_duckv4.ps1 -mode run     -folder lectures -file "2026-10-27 15-03-05"
```

Tuning parameters can be passed through:

```powershell
.\run_duckv4.ps1 -mode run -folder lectures -file "talk" -on 35 -off 30 -floorMargin 30 -duckDb -10 -audDuckDb -40 -teachHold 1.5 -denoise
```

If PowerShell refuses to run the script, allow it for the current session:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_duckv4.ps1 -mode run -folder lectures -file "talk"
```

## How it works

1. Both audio tracks are extracted with FFmpeg to 48 kHz mono float WAV files.
2. Each track is down-sampled to 16 kHz and band-pass filtered to the speech band.
3. The delay between the microphones is estimated from the loudest 20 seconds of the speaker track and corrected.
4. A 100 ms sliding RMS level is computed for both tracks.
5. Audience speech is detected with hysteresis on the level difference, then cleaned up (short gaps filled, short bursts removed). Speaker activity is detected separately.
6. Target gains are built for each track, shifted earlier by the look-ahead time and smoothed in the dB domain.
7. The gains are applied, the two tracks are summed, and FFmpeg muxes the mix back onto the original video with loudness normalisation and a limiter.

## Troubleshooting

- **`ffmpeg` not found**: install FFmpeg and add its `bin` folder to your `PATH`.
- **`ModuleNotFoundError`**: run `pip install numpy scipy soundfile` inside the environment you are using.
- **FFmpeg error about stream `0:a:1`**: the input has only one audio track. This tool needs two.
- **Audience detected far too often or never**: run `analyze` and choose `--on` / `--floor-margin` from the sweep table.
- **Phasey or hollow sound**: leave delay alignment on, or set `--delay-ms` manually.

## Limitations

- Detection is based on levels, not on content. Applause, laughter and loud room noise are treated like any other audience sound.
- The speaker and room microphones must be on separate tracks. A pre-mixed stereo track will not work.

## Licence

Add your preferred licence here (for example MIT) and include a `LICENSE` file.
