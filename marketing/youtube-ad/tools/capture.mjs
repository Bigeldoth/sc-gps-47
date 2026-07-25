import { connect, sleep } from './obs.mjs';

// node tools/capture.mjs [page] [seconds] [WxH]
//   page    html file in this folder's parent      (default short-1-lost.html)
//   seconds total recording length                 (default 39 = 3s countdown + 30s ad + margin)
//   WxH     canvas size                            (default 1080x1920, vertical)
//
// Landscape ad:  node tools/capture.mjs ad-30s.html 39 1920x1080
const SCENE = 'SpaceDrive-Ad';
const INPUT = 'sd-ad-page';
const BASE = 'file:///C:/Users/patri/.cursor/projects/spaceDrive/marketing/youtube-ad/';
const PAGE = process.argv[2] || 'short-1-lost.html';
const RECORD_SECONDS = parseInt(process.argv[3] || '39', 10);
const URL = `${BASE}${PAGE}?auto=1&countdown=3`;
const [CW, CH] = (process.argv[4] || '1080x1920').split('x').map(Number);

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const obs = await connect();

// ── Snapshot everything we are about to change ───────────────────────
const prevVideo = await obs.call('GetVideoSettings');
const prevScene = (await obs.call('GetSceneList')).currentProgramSceneName;
log('saved video settings', `${prevVideo.baseWidth}x${prevVideo.baseHeight}`, 'scene', prevScene);

let outputPath = null;
try {
  // ── Canvas @ 60fps ───────────────────────────────────────────────────
  await obs.call('SetVideoSettings', {
    baseWidth: CW, baseHeight: CH,
    outputWidth: CW, outputHeight: CH,
    fpsNumerator: 60, fpsDenominator: 1
  });
  log(`canvas set to ${CW}x${CH}@60`);

  // ── Dedicated scene + browser source (idempotent) ──────────────────
  const scenes = (await obs.call('GetSceneList')).scenes.map(s => s.sceneName);
  if (!scenes.includes(SCENE)) {
    await obs.call('CreateScene', { sceneName: SCENE });
    log('created scene', SCENE);
  }

  const settings = {
    url: URL, width: CW, height: CH,
    fps_custom: true, fps: 60,
    shutdown: false, restart_when_active: false, reroute_audio: false
  };
  const inputs = (await obs.call('GetInputList')).inputs.map(i => i.inputName);
  if (inputs.includes(INPUT)) {
    await obs.call('SetInputSettings', { inputName: INPUT, inputSettings: settings });
  } else {
    await obs.call('CreateInput', {
      sceneName: SCENE, inputName: INPUT, inputKind: 'browser_source', inputSettings: settings
    });
    log('created browser source', INPUT);
  }

  // A scene item's on-canvas transform is set once when the source is added
  // and does NOT follow later canvas resizes — switching between 1920x1080
  // and 1080x1920 takes would otherwise leave black bars. Pin it every run.
  const itemId = (await obs.call('GetSceneItemId', { sceneName: SCENE, sourceName: INPUT })).sceneItemId;
  await obs.call('SetSceneItemTransform', {
    sceneName: SCENE, sceneItemId: itemId,
    sceneItemTransform: {
      positionX: 0, positionY: 0, rotation: 0,
      boundsType: 'OBS_BOUNDS_STRETCH', boundsAlignment: 0,
      boundsWidth: CW, boundsHeight: CH
    }
  });
  log('scene item transform pinned to fill', `${CW}x${CH}`);

  await obs.call('SetCurrentProgramScene', { sceneName: SCENE });
  await sleep(1500);

  // ── Roll ───────────────────────────────────────────────────────────
  await obs.call('StartRecord');
  log('recording started');
  await sleep(1200);

  // Reload the page so the ad always starts from frame 0 inside the take.
  await obs.call('PressInputPropertiesButton', { inputName: INPUT, propertyName: 'refreshnocache' });
  log('page reloaded — countdown then ad');

  for (let s = RECORD_SECONDS; s > 0; s -= 6) { await sleep(6000); log(`${s - 6}s left`); }

  const stopped = await obs.call('StopRecord');
  outputPath = stopped.outputPath;
  log('recording stopped ->', outputPath);
} finally {
  // ── Restore the user's setup no matter what ────────────────────────
  // Video settings are locked while the recording finalizes, so wait it out.
  for (let i = 0; i < 60; i++) {
    const st = await obs.call('GetRecordStatus');
    if (!st.outputActive) break;
    await sleep(500);
  }
  await obs.call('SetVideoSettings', {
    baseWidth: prevVideo.baseWidth, baseHeight: prevVideo.baseHeight,
    outputWidth: prevVideo.outputWidth, outputHeight: prevVideo.outputHeight,
    fpsNumerator: prevVideo.fpsNumerator, fpsDenominator: prevVideo.fpsDenominator
  });
  await obs.call('SetCurrentProgramScene', { sceneName: prevScene });
  log('restored canvas + scene', prevScene);
  obs.close();
}

console.log('OUTPUT=' + outputPath);
