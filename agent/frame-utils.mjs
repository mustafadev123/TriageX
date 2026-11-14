// Frame preprocessing for card reading. Everything happens in memory: no frame or image is ever
// written to disk. The output is a small grayscale PNG (bytes in a Buffer) for one vision request.
import { crc32, deflateSync } from 'node:zlib';

/** Fraction of each dimension kept, centred. Drops FaceTime controls and self-preview at the edges. */
export const CROP_FRACTION = 0.8;
/** Longest side of the image sent to the vision model. */
export const MAX_SIDE = 1024;

// VideoBufferType values from @livekit/rtc-node whose first plane is full-resolution 8-bit luma
// stored with stride = width (see getPlaneInfos in rtc-node's video_frame).
const LUMA_FIRST = new Set(['I420', 'I420A', 'I422', 'I444', 'NV12']);

/**
 * Returns the luma (Y) plane of a frame: width*height bytes, stride = width.
 * @param {{ data: Uint8Array, width: number, height: number, type: number, convert?: Function }} frame
 * @param {Record<number, string>} bufferTypeNames VideoBufferType from @livekit/rtc-node
 * @param {number} i420 VideoBufferType.I420
 */
export function lumaPlane(frame, bufferTypeNames, i420) {
  const f = LUMA_FIRST.has(bufferTypeNames[frame.type]) ? frame : frame.convert(i420);
  return { y: f.data.subarray(0, f.width * f.height), width: f.width, height: f.height };
}

/**
 * Centre-crops, rotates upright and box-downscales a luma plane.
 * @param {Uint8Array} y luma plane, stride = width
 * @param {number} width
 * @param {number} height
 * @param {{ rotation?: 0 | 90 | 180 | 270, cropFraction?: number, maxSide?: number }} [opts]
 *   rotation: clockwise degrees needed to display the frame upright (WebRTC rotation metadata).
 * @returns {{ data: Uint8Array, width: number, height: number }}
 */
export function cropAndScale(y, width, height, opts = {}) {
  const rotation = opts.rotation ?? 0;
  const crop = opts.cropFraction ?? CROP_FRACTION;
  const maxSide = opts.maxSide ?? MAX_SIDE;
  const cw = Math.max(1, Math.round(width * crop));
  const ch = Math.max(1, Math.round(height * crop));
  const x0 = Math.floor((width - cw) / 2);
  const y0 = Math.floor((height - ch) / 2);
  const scale = Math.min(1, maxSide / Math.max(cw, ch));
  const sw = Math.max(1, Math.round(cw * scale));
  const sh = Math.max(1, Math.round(ch * scale));

  // Box filter: each output pixel averages the source pixels that fall inside it.
  const scaled = new Uint8Array(sw * sh);
  for (let oy = 0; oy < sh; oy++) {
    const sy0 = y0 + Math.floor((oy * ch) / sh);
    const sy1 = Math.max(sy0 + 1, y0 + Math.floor(((oy + 1) * ch) / sh));
    for (let ox = 0; ox < sw; ox++) {
      const sx0 = x0 + Math.floor((ox * cw) / sw);
      const sx1 = Math.max(sx0 + 1, x0 + Math.floor(((ox + 1) * cw) / sw));
      let sum = 0;
      for (let sy = sy0; sy < sy1; sy++) {
        const row = sy * width;
        for (let sx = sx0; sx < sx1; sx++) sum += y[row + sx];
      }
      scaled[oy * sw + ox] = Math.round(sum / ((sy1 - sy0) * (sx1 - sx0)));
    }
  }
  return rotate(scaled, sw, sh, rotation);
}

/** Rotates a grayscale image clockwise by 0, 90, 180 or 270 degrees. */
export function rotate(data, width, height, rotation) {
  if (rotation === 0) return { data, width, height };
  const out = new Uint8Array(data.length);
  const w2 = rotation === 180 ? width : height;
  const h2 = rotation === 180 ? height : width;
  for (let yy = 0; yy < height; yy++) {
    for (let xx = 0; xx < width; xx++) {
      let nx, ny;
      if (rotation === 90) { nx = height - 1 - yy; ny = xx; }
      else if (rotation === 180) { nx = width - 1 - xx; ny = height - 1 - yy; }
      else { nx = yy; ny = width - 1 - xx; }
      out[ny * w2 + nx] = data[yy * width + xx];
    }
  }
  return { data: out, width: w2, height: h2 };
}

/**
 * Exposure statistics of a grayscale image: mean luma, and the share of clipped highlights (glare)
 * and near-black pixels. Thresholds were chosen from the Stage C probe (lens covered: luma 12-20).
 */
export function quality(data) {
  let sum = 0, bright = 0, black = 0;
  for (let i = 0; i < data.length; i++) {
    const v = data[i];
    sum += v;
    if (v >= 250) bright++;
    if (v <= 24) black++;
  }
  const n = Math.max(1, data.length);
  const mean = sum / n;
  return {
    luma: Math.round(mean * 10) / 10,
    clipped: Math.round((bright / n) * 1000) / 1000,
    glare: bright / n >= 0.06,
    dark: mean < 30 && black / n > 0.5,
  };
}

/** Encodes an 8-bit grayscale image as PNG (in memory). */
export function encodeGrayPng(data, width, height) {
  const raw = Buffer.alloc((width + 1) * height);
  for (let yy = 0; yy < height; yy++) {
    raw[yy * (width + 1)] = 0; // filter: none
    raw.set(data.subarray(yy * width, (yy + 1) * width), yy * (width + 1) + 1);
  }
  const chunk = (type, body) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(body.length);
    const tb = Buffer.concat([Buffer.from(type, 'ascii'), body]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(tb) >>> 0);
    return Buffer.concat([len, tb, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 0; // colour type: grayscale
  ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 6 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}
