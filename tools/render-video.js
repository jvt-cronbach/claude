#!/usr/bin/env node
/*
 * Rendert index.html Bild für Bild zu einem MP4 (H.264, yuv420p), das PowerPoint,
 * Keynote und Google Slides direkt abspielen.
 *
 * Aufruf:
 *   node tools/render-video.js [--page index.html] [--api embeddingFlight] [--mode full|canvas]
 *                              [--out export/datei.mp4] [--seconds 26] [--fps 30]
 *                              [--width 1920] [--height 1080] [--no-zoom]
 *
 *   --page         zu rendernde Seite (Standard index.html; themenwelten.html mit --api siebenWelten)
 *   --api          Name der Render-Schnittstelle der Seite (window.<name>.seek)
 *
 *   --mode full    ganze Seite mit Textspalte (Standard)
 *   --mode canvas  nur die Animation, ohne Textspalte und Logo (index.html#canvas)
 *   --no-zoom      ohne die Zoomfahrt am Ende
 *
 * Voraussetzungen:
 *   npm install playwright && npx playwright install chromium
 *   ffmpeg mit libx264 im PATH (oder Umgebungsvariable FFMPEG=/pfad/zu/ffmpeg)
 * Optional: CHROMIUM=/pfad/zu/chromium, PLAYWRIGHT_MODULE=/pfad/zu/playwright
 */
'use strict';
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const opts = { page: 'index.html', api: 'embeddingFlight', mode: 'full', out: '', seconds: 26, fps: 30, width: 1920, height: 1080, zoom: true };
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i], v = argv[i + 1];
  if (a === '--page') { opts.page = v; i++; }
  else if (a === '--api') { opts.api = v; i++; }
  else if (a === '--mode') { opts.mode = v; i++; }
  else if (a === '--out') { opts.out = v; i++; }
  else if (a === '--seconds') { opts.seconds = Number(v); i++; }
  else if (a === '--fps') { opts.fps = Number(v); i++; }
  else if (a === '--width') { opts.width = Number(v); i++; }
  else if (a === '--height') { opts.height = Number(v); i++; }
  else if (a === '--no-zoom') { opts.zoom = false; }
  else { console.error('Unbekannte Option: ' + a); process.exit(1); }
}
const pageBase = path.basename(opts.page, path.extname(opts.page)).replace(/^index$/, 'embedding-flight');
if (!opts.out) opts.out = path.join('export', `${pageBase}-${opts.mode}-${opts.width}x${opts.height}.mp4`);

const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const ffmpegBin = process.env.FFMPEG || 'ffmpeg';
const pageFile = path.isAbsolute(opts.page) ? opts.page : path.resolve(__dirname, '..', opts.page);
const url = 'file://' + pageFile + (opts.mode === 'canvas' ? '#canvas' : '');

/* Zoomfahrt am Zielpunkt: hinein, halten, weit heraus, zurück auf 1,0 */
const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
function zoomAt(t) {
  if (!opts.zoom) return 1;
  const seg = (a, b, from, to) => from + (to - from) * ease((t - a) / (b - a));
  if (t < 14.5) return 1;
  if (t < 17.5) return seg(14.5, 17.5, 1, 2.2);
  if (t < 19) return 2.2;
  if (t < 22.5) return seg(19, 22.5, 2.2, 0.7);
  if (t < 24.5) return seg(22.5, 24.5, 0.7, 1);
  return 1;
}

function writeFrame(stream, buf) {
  return new Promise((resolve, reject) => {
    const ok = stream.write(buf, (err) => (err ? reject(err) : null));
    if (ok) resolve(); else stream.once('drain', resolve);
  });
}

(async () => {
  fs.mkdirSync(path.dirname(path.resolve(opts.out)), { recursive: true });
  const browser = await playwright.chromium.launch({
    executablePath: process.env.CHROMIUM || undefined,
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage({
    viewport: { width: opts.width, height: opts.height },
    deviceScaleFactor: 1,
    ignoreHTTPSErrors: true,
  });
  page.on('pageerror', (e) => console.error('Seitenfehler:', e));
  await page.goto(url);
  await page.evaluate(() => document.fonts.ready);
  const gabarito = await page.evaluate(() => document.fonts.check('700 16px Gabarito'));
  console.log('Schrift Gabarito verfügbar:', gabarito ? 'ja' : 'nein (Fallback-Schrift wird gerendert)');

  const ff = spawn(ffmpegBin, [
    '-y', '-loglevel', 'error',
    '-f', 'image2pipe', '-framerate', String(opts.fps), '-i', '-',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-profile:v', 'high',
    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-r', String(opts.fps),
    opts.out,
  ], { stdio: ['pipe', 'inherit', 'inherit'] });
  const ffExit = new Promise((resolve, reject) => {
    ff.on('exit', (code) => (code === 0 ? resolve() : reject(new Error('ffmpeg beendet mit Code ' + code))));
    ff.on('error', reject);
  });

  const total = Math.round(opts.seconds * opts.fps);
  const started = Date.now();
  for (let i = 0; i < total; i++) {
    const t = i / opts.fps;
    await page.evaluate(([sec, z, api]) => window[api].seek(sec, { zoom: z }), [t, zoomAt(t), opts.api]);
    const png = await page.screenshot({ type: 'png' });
    await writeFrame(ff.stdin, png);
    if (i % 60 === 0) console.log(`Bild ${i}/${total} (t = ${t.toFixed(1)} s, ${((Date.now() - started) / 1000).toFixed(0)} s vergangen)`);
  }
  ff.stdin.end();
  await ffExit;
  await browser.close();
  const size = fs.statSync(opts.out).size;
  console.log(`Fertig: ${opts.out} (${(size / 1048576).toFixed(1)} MB, ${opts.seconds} s, ${opts.fps} fps, ${opts.width}x${opts.height})`);
})().catch((e) => { console.error(e); process.exit(1); });
