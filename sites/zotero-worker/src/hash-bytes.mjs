/** Worker-native exact-byte hashes. MD5 is ONLY Zotero's file-integrity protocol,
 * never an authentication, signature, password, or collision-resistant identity. */
export const DEFAULT_MAX_PDF_BYTES = 16 * 1024 * 1024;
export const HARD_MAX_PDF_BYTES = 32 * 1024 * 1024;
const SHIFTS = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
const K = Uint32Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 0x100000000));
const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');

function check(bytes, maxBytes) {
  if (!(bytes instanceof Uint8Array) ||
      (typeof SharedArrayBuffer !== 'undefined' && bytes.buffer instanceof SharedArrayBuffer)) {
    throw new TypeError('HASH_REQUIRES_UNSHARED_UINT8ARRAY');
  }
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 0 || maxBytes > HARD_MAX_PDF_BYTES) {
    throw new RangeError('INVALID_HASH_BYTE_LIMIT');
  }
  if (bytes.byteLength > maxBytes) throw new RangeError('HASH_INPUT_TOO_LARGE');
}

/** RFC 1321, little-endian words. Respects byteOffset/byteLength of subarray views. */
export function md5Bytes(bytes, { maxBytes = HARD_MAX_PDF_BYTES } = {}) {
  check(bytes, maxBytes);
  const length = bytes.byteLength;
  const blockCount = Math.ceil((length + 9) / 64);
  const view = new DataView(bytes.buffer, bytes.byteOffset, length);
  const words = new Uint32Array(16);
  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
  for (let block = 0; block < blockCount; block++) {
    const offset = block * 64;
    words.fill(0);
    if (offset + 64 <= length) {
      for (let j = 0; j < 16; j++) words[j] = view.getUint32(offset + j * 4, true);
    } else {
      for (let j = 0; j < 64 && offset + j < length; j++) words[j >>> 2] |= bytes[offset + j] << ((j % 4) * 8);
    }
    if (length >= offset && length < offset + 64) words[(length - offset) >>> 2] |= 0x80 << (((length - offset) % 4) * 8);
    if (block === blockCount - 1) {
      words[14] = (length * 8) >>> 0;
      words[15] = Math.floor(length / 0x20000000) >>> 0;
    }
    let a = a0, b = b0, c = c0, d = d0;
    for (let i = 0; i < 64; i++) {
      let f, g;
      if (i < 16) { f = (b & c) | (~b & d); g = i; }
      else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
      else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
      else { f = c ^ (b | ~d); g = (7 * i) % 16; }
      const sum = (a + f + K[i] + words[g]) | 0;
      const shift = SHIFTS[(i >>> 4) * 4 + (i % 4)];
      const rotated = (sum << shift) | (sum >>> (32 - shift));
      a = d; d = c; c = b; b = (b + rotated) | 0;
    }
    a0 = (a0 + a) | 0; b0 = (b0 + b) | 0; c0 = (c0 + c) | 0; d0 = (d0 + d) | 0;
  }
  const result = new Uint8Array(16);
  const output = new DataView(result.buffer);
  [a0, b0, c0, d0].forEach((value, i) => output.setUint32(i * 4, value, true));
  return hex(result);
}

export async function sha256Bytes(bytes, { maxBytes = HARD_MAX_PDF_BYTES, subtle = globalThis.crypto?.subtle } = {}) {
  check(bytes, maxBytes);
  if (typeof subtle?.digest !== 'function') throw new Error('WEBCRYPTO_REQUIRED');
  // Copy only the selected view. A caller changing its bytes while awaiting cannot
  // change what this invocation hashes; SharedArrayBuffer input is rejected above.
  const snapshot = new Uint8Array(bytes);
  return hex(new Uint8Array(await subtle.digest('SHA-256', snapshot)));
}

/** Drop-in createZoteroTransport({ hashBytes: createHashBytes({ maxBytes }) }). */
export function createHashBytes({ maxBytes = DEFAULT_MAX_PDF_BYTES, subtle = globalThis.crypto?.subtle } = {}) {
  check(new Uint8Array(), maxBytes);
  if (maxBytes < 8) throw new RangeError('INVALID_PDF_BYTE_LIMIT');
  if (typeof subtle?.digest !== 'function') throw new Error('WEBCRYPTO_REQUIRED');
  return async bytes => {
    check(bytes, maxBytes);
    const snapshot = new Uint8Array(bytes);
    const md5 = md5Bytes(snapshot, { maxBytes });
    const sha256 = hex(new Uint8Array(await subtle.digest('SHA-256', snapshot)));
    return Object.freeze({ md5, sha256 });
  };
}

/** Default bounded adapter. No crypto or network work happens during import. */
export const hashBytes = bytes => createHashBytes()(bytes);
