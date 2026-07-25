// Render each scene of an ad through OBS's browser source and save a PNG.
// This is exactly what gets recorded, so it catches CEF-only issues (missing
// glyphs, fonts that never loaded) before a take is spent on them.
//
//   node tools/preview.mjs short-1-lost.html 0 1 2 3 4
//   PAGE_WH=1920x1080 node tools/preview.mjs ad-30s.html 0 1 2
//   HOLD=8600 node tools/preview.mjs short-1-lost.html 2      (late beat)
import { connect, sleep } from './obs.mjs';
import { writeFileSync } from 'node:fs';

const PAGE = process.argv[2] || 'short-1-lost.html';
const SCENES = process.argv.slice(3).map(Number);
const SCENE = 'SpaceDrive-Ad';
const INPUT = 'sd-ad-page';
const BASE = 'file:///C:/Users/patri/.cursor/projects/spaceDrive/marketing/youtube-ad/';
const [PW, PH] = (process.env.PAGE_WH || '1080x1920').split('x').map(Number);
const SHOT_W = PW < PH ? 540 : 960; // keep vertical previews from getting huge
// How long to let the scene run before grabbing the frame.
const HOLD_MS = parseInt(process.env.HOLD || '2600', 10);

const obs = await connect();

// Bootstrap the scene + source so a preview can run before the first take.
const scenes = (await obs.call('GetSceneList')).scenes.map(s => s.sceneName);
if (!scenes.includes(SCENE)) await obs.call('CreateScene', { sceneName: SCENE });
const inputs = (await obs.call('GetInputList')).inputs.map(i => i.inputName);
if (!inputs.includes(INPUT)) {
  await obs.call('CreateInput', {
    sceneName: SCENE, inputName: INPUT, inputKind: 'browser_source',
    inputSettings: { url: `${BASE}${PAGE}`, width: PW, height: PH, fps_custom: true, fps: 60, shutdown: false }
  });
}

const prev = await obs.call('GetInputSettings', { inputName: INPUT });
// A browser source only renders while its scene is on program, otherwise
// GetSourceScreenshot returns an empty frame.
const prevScene = (await obs.call('GetSceneList')).currentProgramSceneName;
await obs.call('SetCurrentProgramScene', { sceneName: SCENE });

for (const n of SCENES) {
  await obs.call('SetInputSettings', {
    inputName: INPUT,
    inputSettings: { url: `${BASE}${PAGE}?scene=${n}`, width: PW, height: PH, fps_custom: true, fps: 60 }
  });
  await obs.call('PressInputPropertiesButton', { inputName: INPUT, propertyName: 'refreshnocache' });
  await sleep(HOLD_MS);
  const shot = await obs.call('GetSourceScreenshot', {
    sourceName: INPUT, imageFormat: 'png', imageWidth: SHOT_W
  });
  const b64 = shot.imageData.replace(/^data:image\/png;base64,/, '');
  const file = `scene-${n}.png`;
  writeFileSync(file, Buffer.from(b64, 'base64'));
  console.log('wrote', file);
}

await obs.call('SetInputSettings', { inputName: INPUT, inputSettings: prev.inputSettings });
await obs.call('SetCurrentProgramScene', { sceneName: prevScene });
obs.close();
