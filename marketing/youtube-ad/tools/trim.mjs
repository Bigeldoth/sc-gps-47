// Cut an OBS take down to the exact ad and encode it for YouTube.
//
//   node tools/trim.mjs "C:\Users\patri\Videos\take.mp4" [out.mp4] [seconds]
//
// The page flashes a few pure-WHITE frames immediately before the ad's frame 0
// (see #cue in the ad HTML). SpaceDrive's ads are near-black throughout, so a
// black cue would be invisible — the cue is inverted here versus the Family
// Heroes pipeline. We take the LAST white run in the head of the take and start
// on the frame right after it, so the cue never reaches the final file.
import { spawn } from 'node:child_process';
import { basename } from 'node:path';

const SRC = process.argv[2];
const OUT = process.argv[3] || 'spacedrive-ad.mp4';
const DURATION = parseFloat(process.argv[4] || '30');
const SCAN_TO = 14;     // seconds of take head to inspect
const WHITE_Y = 200;    // luma above this counts as the white cue
const FPS = 60;

if (!SRC) { console.error('usage: node tools/trim.mjs <take.mp4> [out.mp4] [seconds]'); process.exit(1); }

const run = (cmd, args) => new Promise(resolve => {
  const p = spawn(cmd, args);
  let buf = '';
  p.stdout.on('data', d => buf += d);
  p.stderr.on('data', d => buf += d);
  p.on('close', () => resolve(buf));
});

// ── 1. Per-frame luminance of the whole frame ────────────────────────
const log = await run('ffmpeg', [
  '-hide_banner', '-v', 'info', '-t', String(SCAN_TO), '-i', SRC,
  '-vf', 'scale=160:90,signalstats,metadata=print:key=lavfi.signalstats.YAVG',
  '-an', '-f', 'null', '-'
]);

const frames = [];
let pending = null;
for (const line of log.split(/\r?\n/)) {
  const t = line.match(/pts_time:([0-9.]+)/);
  if (t) { pending = parseFloat(t[1]); continue; }
  const y = line.match(/YAVG=([0-9.]+)/);
  if (y && pending !== null) { frames.push({ t: pending, y: parseFloat(y[1]) }); pending = null; }
}
if (!frames.length) { console.error('no frames analysed — check the path'); process.exit(1); }

// ── 2. Last run of white frames = the sync cue ───────────────────────
const runs = [];
for (const f of frames) {
  const white = f.y > WHITE_Y;
  const last = runs[runs.length - 1];
  // pts_time is rounded in ffmpeg's log, so match the frame step loosely.
  if (white && last && Math.abs(f.t - last.end - 1 / FPS) < 0.005) last.end = f.t;
  else if (white) runs.push({ start: f.t, end: f.t });
}
// Keep runs of 2+ frames that are followed by picture.
const cues = runs.filter(r => r.end - r.start >= 1 / FPS && r.end < frames[frames.length - 1].t - 0.5);
if (!cues.length) {
  console.error('no white sync cue found — is the take from a page with #cue?');
  console.error('white runs seen:', runs.map(r => `${r.start.toFixed(2)}-${r.end.toFixed(2)}`).join(' ') || 'none');
  process.exit(1);
}
const cue = cues[cues.length - 1];
const startAt = cue.end + 1 / FPS;

console.log(`sync cue ${cue.start.toFixed(3)}-${cue.end.toFixed(3)}s`);
console.log(`ad starts at ${startAt.toFixed(6)}s (frame ${Math.round(startAt * FPS)})`);

// ── 3. Encode exactly DURATION seconds, silent, YouTube-friendly ─────
await run('ffmpeg', [
  '-y', '-hide_banner', '-v', 'error', '-stats',
  '-ss', startAt.toFixed(6), '-i', SRC, '-t', String(DURATION), '-an',
  '-c:v', 'libx264', '-crf', '18', '-preset', 'slow', '-profile:v', 'high',
  '-pix_fmt', 'yuv420p', '-r', String(FPS), '-movflags', '+faststart', OUT
]);

const probe = await run('ffprobe', [
  '-v', 'error', '-select_streams', 'v:0',
  '-show_entries', 'stream=width,height,nb_frames', '-show_entries', 'format=duration',
  '-of', 'default=noprint_wrappers=1', OUT
]);
console.log(`${basename(OUT)}\n${probe.trim()}`);
