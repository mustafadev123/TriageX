// Frame preprocessing on synthetic pixel arrays (no camera frames, nothing written to disk).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { inflateSync } from 'node:zlib';
import { cropAndScale, encodeGrayPng, quality, rotate } from '../frame-utils.mjs';

const plane = (w, h, f) => Uint8Array.from({ length: w * h }, (_, i) => f(i % w, Math.floor(i / w)));

test('cropAndScale: centre 80% of 1280x720 is downscaled to 1024 px wide', () => {
  const img = cropAndScale(plane(1280, 720, () => 100), 1280, 720);
  assert.equal(img.width, 1024);
  assert.equal(img.height, 576);
  assert.ok(img.data.every(v => v === 100));
});

test('cropAndScale: the outer 10% border (FaceTime UI) is cropped away', () => {
  // White border 10% wide, black centre: the result must be all black.
  const w = 200, h = 100;
  const y = plane(w, h, (x, yy) => (x < 20 || x >= 180 || yy < 10 || yy >= 90 ? 255 : 0));
  const img = cropAndScale(y, w, h);
  assert.equal(img.width, 160);
  assert.equal(img.height, 80);
  assert.ok(img.data.every(v => v === 0));
});

test('cropAndScale: box filter averages', () => {
  const y = plane(4, 2, x => (x % 2 ? 200 : 100));
  const img = cropAndScale(y, 4, 2, { cropFraction: 1, maxSide: 2 });
  assert.deepEqual([...img.data], [150, 150]);
});

test('rotate: 90 / 180 / 270 degrees clockwise', () => {
  const d = Uint8Array.from([1, 2, 3, 4, 5, 6]); // 3 wide, 2 high: [1 2 3 / 4 5 6]
  assert.deepEqual(rotate(d, 3, 2, 90), { data: Uint8Array.from([4, 1, 5, 2, 6, 3]), width: 2, height: 3 });
  assert.deepEqual(rotate(d, 3, 2, 180), { data: Uint8Array.from([6, 5, 4, 3, 2, 1]), width: 3, height: 2 });
  assert.deepEqual(rotate(d, 3, 2, 270), { data: Uint8Array.from([3, 6, 2, 5, 1, 4]), width: 2, height: 3 });
});

test('quality: flags glare and a covered lens', () => {
  assert.deepEqual({ ...quality(new Uint8Array(1000).fill(120)) }, { luma: 120, clipped: 0, glare: false, dark: false });
  const glare = new Uint8Array(1000).fill(120);
  glare.fill(255, 0, 100);
  assert.equal(quality(glare).glare, true);
  assert.equal(quality(new Uint8Array(1000).fill(14)).dark, true); // probe: lens covered ~ 12-20
  assert.equal(quality(new Uint8Array(1000).fill(50)).dark, false); // probe: normal room ~ 50-60
});

test('encodeGrayPng: valid PNG whose pixels round-trip', () => {
  const w = 5, h = 3;
  const data = plane(w, h, (x, y) => x * 40 + y);
  const png = encodeGrayPng(data, w, h);
  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  assert.equal(png.toString('ascii', 12, 16), 'IHDR');
  assert.equal(png.readUInt32BE(16), w);
  assert.equal(png.readUInt32BE(20), h);
  const idatLen = png.readUInt32BE(33);
  assert.equal(png.toString('ascii', 37, 41), 'IDAT');
  const raw = inflateSync(png.subarray(41, 41 + idatLen));
  for (let y = 0; y < h; y++) {
    assert.equal(raw[y * (w + 1)], 0);
    assert.deepEqual([...raw.subarray(y * (w + 1) + 1, (y + 1) * (w + 1))], [...data.subarray(y * w, (y + 1) * w)]);
  }
});
