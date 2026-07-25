# SpaceDrive GPS — ads

Self-playing HTML ads, **silent**, rendered to MP4 by driving OBS over its websocket.
Everything is drawn in the page — there is no gameplay footage and no screen recording:
the MFD readout in the ads is a faithful CSS mock of the real overlay
(`src/main.py`, PADEK tokens from `assets/padek-theme.qss`).

| File | Format | Role |
|---|---|---|
| `ad-30s.html` | 1920×1080 | **30 s** YouTube cut — pain, product, overlay working, reassurance, CTA. |
| `short-1-lost.html` | 1080×1920 | **30 s** Short — "the spot you'll never find again". |
| `short-2-howto.html` | 1080×1920 | **30 s** Short — "Save it. Target it. Fly to it." (3-step listicle). |
| `spacedrive-*.mp4` | — | Rendered takes. Not committed. |
| `tools/capture.mjs` | — | Drives OBS and records a take. |
| `tools/trim.mjs` | — | Finds the exact first frame via the sync cue and cuts to length. |
| `tools/preview.mjs` | — | Renders one scene through OBS and saves a PNG — review without recording. |
| `tools/obs.mjs` | — | ~40-line obs-websocket v5 client (no dependency, Node 22+). |

## Copy rules — read before editing

- **The CTA is `spacedrive.padek-interactive.tech`** (the SpaceDrive Community Hub), never the
  GitHub repo: that site is where the installer and the community POIs live.
- **Never claim EAC endorsement or "ban-proof".** What the ads may say is what the app does:
  no memory reading, no injection, 100% screenshot-based. The word "safe" is only ever used
  about *how it runs*, not as a guarantee from Cloud Imperium Games.
- Every cut carries the disclaimer *"Unofficial tool · not affiliated with Cloud Imperium Games"*.
- The overlay mock must stay truthful: the readout format (`X: … Y: … / Z: …`, `▶ TARGET`,
  distance + heading), the freshness colours (emerald LIVE → copper AGING → red LOST) and the
  `DR` tag while the filter coasts are the app's real behaviours.

## Run a page by hand

Open it in Chrome and press the ▶ button (or <kbd>Space</kbd>).

| Key / param | Effect |
|---|---|
| <kbd>Space</kbd> / <kbd>Enter</kbd> / ▶ | Start |
| <kbd>R</kbd> | Replay from 0 |
| <kbd>F</kbd> | Fullscreen |
| `?auto=1` | Start on its own (for capture, no click needed) |
| `?countdown=3` | 3-2-1 lead-in before the ad |
| `?scene=N` | Jump to one scene (0…4) to review it |
| `?dev=1` | Thin progress bar at the bottom |

`window.ad.start() / .stop() / .reset() / .enterScene(i)` is exposed for scripted review.

## Timelines (30.0 s each)

### `ad-30s.html` — landscape
| # | Scene | In → out |
|---|---|---|
| 0 | Hook — the coordinates you wrote down, two days later | 0.0 → 6.2 |
| 1 | Brand — wordmark, tagline, three claims | 6.2 → 10.6 |
| 2 | The overlay working — 12.4 km → 17 m, one AGING/DR beat | 10.6 → 19.2 |
| 3 | Proof — three tiles | 19.2 → 24.8 |
| 4 | CTA | 24.8 → 30.0 |

### `short-1-lost.html` — vertical
| # | Scene | In → out |
|---|---|---|
| 0 | Hook — "You found the perfect spot" → no marker, no waypoint | 0.0 → 6.6 |
| 1 | Brand | 6.6 → 11.2 |
| 2 | The overlay working (approach run + DR beat) | 11.2 → 20.4 |
| 3 | Proof — three rows | 20.4 → 25.2 |
| 4 | CTA | 25.2 → 30.0 |

### `short-2-howto.html` — vertical
| # | Scene | In → out |
|---|---|---|
| 0 | Hook — three spots the game does not mark | 0.0 → 5.6 |
| 1 | 1 · Save it — Shift+F3, the save dialog | 5.6 → 12.4 |
| 2 | 2 · Target it — the POI list and its categories | 12.4 → 18.0 |
| 3 | 3 · Fly to it — approach run to arrival | 18.0 → 25.2 |
| 4 | CTA | 25.2 → 30.0 |

The clock is a single `requestAnimationFrame` loop (`AD.SCENES` in each page), so every replay
renders the same beats. The MFD readout steps off a per-scene cue table (`LIVE_CUES` /
`FLY_CUES`) — change a distance there, not in the markup.

## Exporting to MP4

Prerequisites: OBS running, **Tools → WebSocket Server Settings** with the server enabled on
port 4455 and authentication **off** (the client sends no password). Node 22+ and ffmpeg on PATH.

```powershell
node tools/capture.mjs short-1-lost.html 39 1080x1920
node tools/capture.mjs ad-30s.html 39 1920x1080
```

It snapshots the current canvas and program scene, switches to the requested size @60 fps,
creates a `SpaceDrive-Ad` scene with a browser source on `<page>?auto=1&countdown=3`, records,
then **restores the canvas and scene** (waiting for the recording to finalize first — video
settings are locked while an output is active). It prints the take's path.

```powershell
node tools/trim.mjs "C:\Users\patri\Videos\<take>.mp4" spacedrive-short-1-lost.mp4 30
```

The page flashes a few **pure-white frames** immediately before frame 0 (`#cue` in the HTML).
The trimmer takes the last white run in the head of the take and starts on the next frame, then
encodes exactly N seconds of silent H.264. The cue never reaches the final file.

**The cue is white here, not black** (the Family Heroes pipeline uses black): these ads are
near-black from end to end, so a black cue would be invisible to the detector.

**Restores are chained, not absolute.** Each run restores whatever it saw at start, so a series
of takes that ends on a vertical one leaves the canvas vertical. Check
Settings → Video after a session, or run one landscape take last.

**No colour emoji in the pages.** OBS's embedded browser (CEF) has no emoji font and renders
them as tofu. Text glyphs such as ▶ ● ▲ ◆ ★ ✓ are fine — they come from the regular font.

**Don't rotate a text triangle to show a heading.** `transform: rotate()` on `▲` reads as the
wrong direction at intermediate angles; the heading arrow is an inline SVG (`ARROW_SVG`) for
exactly this reason.

**Reviewing without recording**: `node tools/preview.mjs short-1-lost.html 0 1 2 3 4` writes
`scene-N.png` for each scene, rendered by the same browser that records. `HOLD=8600` (ms)
captures a later beat of a scene; `PAGE_WH=1920x1080` previews the landscape cut. It puts the
ad scene on program while it works — a browser source does not render otherwise, and the
screenshot comes back empty.

Verify a render by tiling frames, e.g.:

```powershell
ffmpeg -y -i spacedrive-short-1-lost.mp4 -vf "select='eq(n\,180)+eq(n\,560)+eq(n\,880)+eq(n\,1150)+eq(n\,1400)+eq(n\,1700)',scale=280:-1,tile=6x1" -frames:v 1 -fps_mode passthrough check.png
```

`-an` / silence is on purpose: the ads are built to read with the sound off. Add music in an
editor if a soundtrack is wanted for YouTube.

Note: DaVinci Resolve on this machine is the **free** edition, which does not expose the
external scripting API — automation goes through ffmpeg.

## Publishing

- **YouTube Shorts / TikTok**: the two 1080×1920 files, as they are (≤ 60 s, 9:16).
- **YouTube in-stream / community post**: `spacedrive-ad-30s.mp4`.
- Suggested description line: *SpaceDrive GPS — a GPS overlay for Star Citizen. Free, open
  source, screenshot-based. spacedrive.padek-interactive.tech*
