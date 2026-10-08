#!/usr/bin/env node
/* Procedural soundtrack for the Ribbon Field demo: a calm, driving electronic
 * bed plus UI sound effects aligned to cue times exported by the composition.
 * Pure Node, no dependencies. Writes 48 kHz stereo 16-bit WAV.
 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");

const SR = 48000;
const args = process.argv.slice(2);
const duration = Number(args[0] || 90);
const cuesPath = args[1] || path.join(__dirname, "out", "cues.json");
const outPath = args[2] || path.join(__dirname, "out", "soundtrack.wav");
const BPM = 104;
const BEAT = 60 / BPM;
const BAR = BEAT * 4;
const N = Math.ceil(duration * SR);
const L = new Float64Array(N), R = new Float64Array(N);

let seed = 20261008;
const rand = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
const midi = n => 440 * Math.pow(2, (n - 69) / 12);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

function cues() {
  try { return JSON.parse(fs.readFileSync(cuesPath, "utf8")); } catch (e) { return []; }
}

// Deterministic envelope: attack, decay to sustain, release after gate.
function env(t, gate, a, d, s, r) {
  if (t < 0) return 0;
  if (t < a) return t / a;
  if (t < a + d) return 1 - (1 - s) * ((t - a) / d);
  if (t < gate) return s;
  const rel = t - Math.max(gate, a + d);
  return s * Math.exp(-rel / r);
}

function addVoice({start, length, freq, wave = "saw", gain = 0.1, pan = 0, a = 0.01, d = 0.2, s = 0.7, r = 0.3,
                   detune = 0, cutoff = null, vibrato = 0}) {
  const total = length + r * 6;
  const i0 = Math.max(0, Math.floor(start * SR)), i1 = Math.min(N, Math.ceil((start + total) * SR));
  const f = freq * Math.pow(2, detune / 1200);
  let phase = rand(), lp = 0;
  const gl = Math.cos((pan + 1) * Math.PI / 4), gr = Math.sin((pan + 1) * Math.PI / 4);
  for (let i = i0; i < i1; i++) {
    const t = i / SR - start;
    const e = env(t, length, a, d, s, r);
    if (e <= 0.0001 && t > length) break;
    const vib = vibrato ? 1 + vibrato * Math.sin(2 * Math.PI * 5.2 * t) : 1;
    phase += f * vib / SR; if (phase >= 1) phase -= 1;
    let v;
    if (wave === "saw") v = 2 * phase - 1;
    else if (wave === "square") v = phase < 0.5 ? 1 : -1;
    else if (wave === "tri") v = 4 * Math.abs(phase - 0.5) - 1;
    else v = Math.sin(2 * Math.PI * phase);
    if (cutoff) { const k = clamp(cutoff(t) / SR * 2 * Math.PI, 0.001, 1); lp += k * (v - lp); v = lp; }
    const o = v * e * gain;
    L[i] += o * gl; R[i] += o * gr;
  }
}

function addNoise({start, length, gain = 0.1, pan = 0, a = 0.002, d = 0.05, s = 0, r = 0.03, hp = 0.9, lpk = 1}) {
  const i0 = Math.max(0, Math.floor(start * SR)), i1 = Math.min(N, Math.ceil((start + length + r * 6) * SR));
  let prev = 0, lp = 0;
  const gl = Math.cos((pan + 1) * Math.PI / 4), gr = Math.sin((pan + 1) * Math.PI / 4);
  for (let i = i0; i < i1; i++) {
    const t = i / SR - start;
    const e = env(t, length, a, d, s, r);
    if (e <= 0.0001 && t > length) break;
    const white = rand() * 2 - 1;
    const hpv = white - prev * hp; prev = white;  // crude high-pass
    lp += lpk * (hpv - lp);
    const o = lp * e * gain;
    L[i] += o * gl; R[i] += o * gr;
  }
}

function kick(start, gain = 0.9) {
  const i0 = Math.floor(start * SR), i1 = Math.min(N, i0 + Math.floor(0.45 * SR));
  let phase = 0;
  for (let i = i0; i < i1; i++) {
    const t = (i - i0) / SR;
    const f = 42 + 160 * Math.exp(-t * 26);
    phase += f / SR;
    const e = Math.exp(-t * 9) * (1 - Math.exp(-t * 400));
    const o = Math.tanh(Math.sin(2 * Math.PI * phase) * 2.2) * e * gain;
    L[i] += o; R[i] += o;
  }
}

function hat(start, gain = 0.08, open = false) {
  addNoise({start, length: open ? 0.12 : 0.02, gain, a: 0.001, d: open ? 0.14 : 0.035, s: 0, r: open ? 0.08 : 0.02, hp: 0.97, lpk: 0.9, pan: 0.25});
}

function clap(start, gain = 0.22) {
  for (let k = 0; k < 3; k++) addNoise({start: start + k * 0.011, length: 0.02, gain: gain * 0.5, a: 0.001, d: 0.03, s: 0, r: 0.02, hp: 0.85, lpk: 0.5});
  addNoise({start: start + 0.03, length: 0.05, gain, a: 0.002, d: 0.12, s: 0, r: 0.09, hp: 0.8, lpk: 0.45});
}

function riser(start, length, gain = 0.25) {
  const i0 = Math.floor(start * SR), i1 = Math.min(N, Math.ceil((start + length) * SR));
  let lp = 0;
  for (let i = i0; i < i1; i++) {
    const p = (i - i0) / (i1 - i0);
    const white = rand() * 2 - 1;
    const k = 0.02 + 0.6 * p * p;
    lp += k * (white - lp);
    const e = Math.pow(p, 2.2) * gain;
    L[i] += lp * e * (0.9 + 0.1 * Math.sin(p * 40)); R[i] += lp * e * (0.9 - 0.1 * Math.sin(p * 40));
  }
}

function impact(start, gain = 0.8) {
  kick(start, gain);
  addVoice({start, length: 1.2, freq: midi(26), wave: "sine", gain: gain * 0.45, a: 0.005, d: 0.9, s: 0.0, r: 0.6});
  addNoise({start, length: 0.3, gain: gain * 0.18, a: 0.001, d: 0.5, s: 0, r: 0.5, hp: 0.5, lpk: 0.25});
}

function click(start, gain = 0.3) {
  addNoise({start, length: 0.004, gain, a: 0.0005, d: 0.012, s: 0, r: 0.01, hp: 0.6, lpk: 0.8});
  addVoice({start, length: 0.03, freq: 1900, wave: "sine", gain: gain * 0.35, a: 0.001, d: 0.03, s: 0, r: 0.02});
}

function pop(start, gain = 0.18, pitch = 880) {
  addVoice({start, length: 0.05, freq: pitch, wave: "sine", gain, a: 0.002, d: 0.08, s: 0, r: 0.07, vibrato: 0});
  addVoice({start, length: 0.04, freq: pitch * 1.5, wave: "sine", gain: gain * 0.4, a: 0.002, d: 0.05, s: 0, r: 0.05});
}

function whoosh(start, length = 0.5, gain = 0.2) {
  const i0 = Math.floor(start * SR), i1 = Math.min(N, Math.ceil((start + length) * SR));
  let lp = 0;
  for (let i = i0; i < i1; i++) {
    const p = (i - i0) / (i1 - i0);
    const white = rand() * 2 - 1;
    const k = 0.03 + 0.5 * Math.sin(Math.PI * p);
    lp += k * (white - lp);
    const e = Math.sin(Math.PI * p) ** 1.5 * gain;
    L[i] += lp * e * (1 - p * 0.6); R[i] += lp * e * (0.4 + p * 0.6);
  }
}

// ---- Arrangement ---------------------------------------------------------
// D minor, four-bar loop. Chord tones as MIDI numbers.
const CHORDS = [
  {root: 38, tones: [62, 65, 69, 72, 76]},     // Dm9
  {root: 46, tones: [58, 62, 65, 69, 72]},     // Bbmaj9
  {root: 41, tones: [65, 69, 72, 76, 79]},     // Fmaj9
  {root: 36, tones: [60, 62, 67, 72, 74]},     // Csus2 add9
];

const intro = BEAT * 13, mainStart = BEAT * 38, outro = Math.min(BEAT * 165, Math.max(mainStart + 8, duration - 8));

function section(t) {
  if (t < intro) return "intro";
  if (t < mainStart) return "build";
  if (t < outro) return "main";
  return "outro";
}

const bars = Math.ceil(duration / BAR) + 1;
for (let bar = 0; bar < bars; bar++) {
  const t0 = bar * BAR;
  if (t0 > duration) break;
  const chord = CHORDS[bar % CHORDS.length];
  const sec = section(t0);
  const padGain = sec === "intro" ? 0.040 : sec === "build" ? 0.048 : sec === "main" ? 0.052 : 0.040;
  // Pad: detuned saws through a slowly opening filter.
  for (const n of chord.tones) {
    for (const det of [-7, 7]) {
      addVoice({start: t0, length: BAR, freq: midi(n), wave: "saw", gain: padGain / chord.tones.length, detune: det,
                pan: det / 14 * 0.6, a: 0.6, d: 0.5, s: 0.85, r: 0.9,
                cutoff: t => 380 + 900 * (0.5 + 0.5 * Math.sin(2 * Math.PI * (t0 + t) / 16)) + (sec === "main" ? 500 : 0)});
    }
  }
  // Sub bass, ducked by leaving space right at the kick.
  if (sec !== "intro") {
    for (let b = 0; b < 4; b++) {
      const len = b % 2 === 0 ? BEAT * 0.9 : BEAT * 0.45;
      addVoice({start: t0 + b * BEAT + 0.015, length: len, freq: midi(chord.root - 12), wave: "sine",
                gain: sec === "main" ? 0.30 : 0.22, a: 0.012, d: 0.1, s: 0.8, r: 0.12});
      addVoice({start: t0 + b * BEAT + 0.015, length: len, freq: midi(chord.root), wave: "tri",
                gain: sec === "main" ? 0.07 : 0.05, a: 0.012, d: 0.1, s: 0.7, r: 0.1, cutoff: () => 600});
    }
  }
  // Plucks: 16th-note arpeggio with rests, ping-ponged.
  const pattern = [1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1];
  const density = sec === "intro" ? 0.35 : sec === "build" ? 0.7 : sec === "main" ? 1 : 0.45;
  for (let s16 = 0; s16 < 16; s16++) {
    if (!pattern[s16] || rand() > density) continue;
    const tone = chord.tones[(s16 * 3 + bar) % chord.tones.length] + (s16 % 5 === 0 ? 12 : 0);
    const start = t0 + s16 * BEAT / 4;
    const pan = (s16 % 2 ? 0.45 : -0.45);
    addVoice({start, length: 0.09, freq: midi(tone), wave: "tri", gain: 0.085, pan, a: 0.002, d: 0.16, s: 0.0, r: 0.12,
              cutoff: t => 2600 * Math.exp(-t * 12) + 500});
    // echo
    addVoice({start: start + BEAT * 0.75, length: 0.07, freq: midi(tone), wave: "tri", gain: 0.035, pan: -pan, a: 0.002, d: 0.14, s: 0, r: 0.1,
              cutoff: t => 1800 * Math.exp(-t * 12) + 400});
  }
  // Drums.
  if (sec === "build") {
    for (let b = 0; b < 4; b++) kick(t0 + b * BEAT, 0.55);
    for (let e8 = 0; e8 < 8; e8++) hat(t0 + e8 * BEAT / 2, 0.045 + (e8 % 2 ? 0 : 0.02));
  } else if (sec === "main") {
    for (let b = 0; b < 4; b++) kick(t0 + b * BEAT, 0.8);
    clap(t0 + BEAT, 0.16); clap(t0 + 3 * BEAT, 0.16);
    for (let s16 = 0; s16 < 16; s16++) {
      const accent = s16 % 4 === 0 ? 0.075 : s16 % 2 === 0 ? 0.05 : 0.03;
      hat(t0 + s16 * BEAT / 4, accent, s16 === 14);
    }
  } else if (sec === "outro") {
    for (let b = 0; b < 4; b += 2) kick(t0 + b * BEAT, 0.35);
    for (let e8 = 0; e8 < 8; e8 += 2) hat(t0 + e8 * BEAT / 2, 0.03);
  }
}

// Section transitions.
riser(mainStart - 3.2, 3.2, 0.3);
impact(mainStart, 0.9);

// UI cues from the composition.
for (const cue of cues()) {
  if (cue.t >= duration) continue;
  if (cue.kind === "click") click(cue.t, 0.28);
  else if (cue.kind === "pop") pop(cue.t, 0.14, cue.pitch || 880);
  else if (cue.kind === "whoosh") whoosh(cue.t, cue.length || 0.5, 0.16);
  else if (cue.kind === "impact") impact(cue.t, cue.gain || 0.7);
  else if (cue.kind === "riser") riser(cue.t, cue.length || 2, 0.2);
}

// Master: gentle fade in/out, soft clip, normalize.
let peak = 0;
for (let i = 0; i < N; i++) {
  const t = i / SR;
  const fade = Math.min(1, t / 1.5) * Math.min(1, Math.max(0, (duration - t) / 3));
  L[i] = Math.tanh(L[i] * 1.4) * fade; R[i] = Math.tanh(R[i] * 1.4) * fade;
  peak = Math.max(peak, Math.abs(L[i]), Math.abs(R[i]));
}
const norm = peak > 0 ? 0.89 / peak : 1;
const buffer = Buffer.alloc(44 + N * 4);
buffer.write("RIFF", 0); buffer.writeUInt32LE(36 + N * 4, 4); buffer.write("WAVE", 8);
buffer.write("fmt ", 12); buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(2, 22);
buffer.writeUInt32LE(SR, 24); buffer.writeUInt32LE(SR * 4, 28); buffer.writeUInt16LE(4, 32); buffer.writeUInt16LE(16, 34);
buffer.write("data", 36); buffer.writeUInt32LE(N * 4, 40);
for (let i = 0; i < N; i++) {
  buffer.writeInt16LE(Math.round(clamp(L[i] * norm, -1, 1) * 32767), 44 + i * 4);
  buffer.writeInt16LE(Math.round(clamp(R[i] * norm, -1, 1) * 32767), 46 + i * 4);
}
fs.mkdirSync(path.dirname(outPath), {recursive: true});
fs.writeFileSync(outPath, buffer);
console.log("wrote", outPath, duration + "s");
