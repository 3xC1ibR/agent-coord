#!/usr/bin/env node
/* Original procedural soundtrack for the 100 games film. Pure Node, no dependencies, deterministic.
 * 120 BPM so every bar is two seconds; the composition's cues.json supplies section marks and sound
 * effects (landing pops, ticks, whooshes, risers, impacts). Writes 48 kHz stereo 16-bit WAV.
 *   node music.js <seconds> <cues.json> <out.wav> */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const SR = 48000;
const args = process.argv.slice(2);
const duration = Number(args[0] || 59);
const cuesPath = args[1] || path.join(__dirname, "out", "cues.json");
const outPath = args[2] || path.join(__dirname, "out", "soundtrack.wav");
const BPM = 120, BEAT = 60 / BPM, BAR = BEAT * 4;
const N = Math.ceil(duration * SR);
const music = [new Float64Array(N), new Float64Array(N)], drums = [new Float64Array(N), new Float64Array(N)], fx = [new Float64Array(N), new Float64Array(N)];
let seed = 20261012;
const rand = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
const midi = n => 440 * Math.pow(2, (n - 69) / 12);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const cues = (() => { try { return JSON.parse(fs.readFileSync(cuesPath, "utf8")); } catch (e) { return []; } })();
const marks = cues.filter(c => c.kind === "mark");
const sectionAt = t => { let name = "intro"; for (const m of marks) if (t >= m.t) name = m.name; return name; };

function env(t, gate, a, d, s, r) {
  if (t < 0) return 0;
  if (t < a) return t / a;
  if (t < a + d) return 1 - (1 - s) * ((t - a) / d);
  if (t < gate) return s;
  return s * Math.exp(-(t - Math.max(gate, a + d)) / r);
}
function voice(bus, {start, length, freq, wave = "saw", gain = .1, pan = 0, a = .01, d = .2, s = .7, r = .3, detune = 0, cutoff = null, vibrato = 0}) {
  const [L, R] = bus, total = length + r * 6;
  const i0 = Math.max(0, Math.floor(start * SR)), i1 = Math.min(N, Math.ceil((start + total) * SR));
  const f = freq * Math.pow(2, detune / 1200);
  let phase = rand(), lp = 0;
  const gl = Math.cos((pan + 1) * Math.PI / 4), gr = Math.sin((pan + 1) * Math.PI / 4);
  for (let i = i0; i < i1; i++) {
    const t = i / SR - start, e = env(t, length, a, d, s, r);
    if (e <= .0001 && t > length) break;
    const vib = vibrato ? 1 + vibrato * Math.sin(2 * Math.PI * 5.2 * t) : 1;
    phase += f * vib / SR; if (phase >= 1) phase -= 1;
    let v = wave === "saw" ? 2 * phase - 1 : wave === "square" ? (phase < .5 ? 1 : -1) : wave === "tri" ? 4 * Math.abs(phase - .5) - 1 : Math.sin(2 * Math.PI * phase);
    if (cutoff) { const k = clamp(cutoff(t) / SR * 2 * Math.PI, .001, 1); lp += k * (v - lp); v = lp; }
    const o = v * e * gain; L[i] += o * gl; R[i] += o * gr;
  }
}
function noise(bus, {start, length, gain = .1, pan = 0, a = .002, d = .05, s = 0, r = .03, hp = .9, lpk = 1}) {
  const [L, R] = bus, i0 = Math.max(0, Math.floor(start * SR)), i1 = Math.min(N, Math.ceil((start + length + r * 6) * SR));
  let prev = 0, lp = 0;
  const gl = Math.cos((pan + 1) * Math.PI / 4), gr = Math.sin((pan + 1) * Math.PI / 4);
  for (let i = i0; i < i1; i++) {
    const t = i / SR - start, e = env(t, length, a, d, s, r);
    if (e <= .0001 && t > length) break;
    const white = rand() * 2 - 1, hpv = white - prev * hp; prev = white; lp += lpk * (hpv - lp);
    const o = lp * e * gain; L[i] += o * gl; R[i] += o * gr;
  }
}
const kicks = [];
function kick(start, gain = .9) {
  kicks.push(start);
  const [L, R] = drums, i0 = Math.floor(start * SR), i1 = Math.min(N, i0 + Math.floor(.42 * SR));
  let phase = 0;
  for (let i = i0; i < i1; i++) {
    const t = (i - i0) / SR, f = 44 + 170 * Math.exp(-t * 28); phase += f / SR;
    const e = Math.exp(-t * 9.5) * (1 - Math.exp(-t * 500));
    const o = Math.tanh(Math.sin(2 * Math.PI * phase) * 2.4) * e * gain; L[i] += o; R[i] += o;
  }
}
const hat = (start, gain = .08, open = false) => noise(drums, {start, length: open ? .12 : .02, gain, a: .001, d: open ? .14 : .035, s: 0, r: open ? .08 : .02, hp: .97, lpk: .9, pan: .25});
function clap(start, gain = .22) {
  for (let k = 0; k < 3; k++) noise(drums, {start: start + k * .011, length: .02, gain: gain * .5, a: .001, d: .03, s: 0, r: .02, hp: .85, lpk: .5});
  noise(drums, {start: start + .03, length: .05, gain, a: .002, d: .12, s: 0, r: .09, hp: .8, lpk: .45});
}
function riser(start, length, gain = .25) {
  const [L, R] = fx, i0 = Math.floor(start * SR), i1 = Math.min(N, Math.ceil((start + length) * SR));
  let lp = 0;
  for (let i = i0; i < i1; i++) {
    const p = (i - i0) / (i1 - i0), white = rand() * 2 - 1, k = .02 + .6 * p * p; lp += k * (white - lp);
    const e = Math.pow(p, 2.2) * gain; L[i] += lp * e * (.9 + .1 * Math.sin(p * 40)); R[i] += lp * e * (.9 - .1 * Math.sin(p * 40));
  }
}
function impact(start, gain = .8) {
  kick(start, gain);
  voice(fx, {start, length: 1.2, freq: midi(33), wave: "sine", gain: gain * .5, a: .005, d: .9, s: 0, r: .6});
  noise(fx, {start, length: .3, gain: gain * .2, a: .001, d: .5, s: 0, r: .5, hp: .5, lpk: .25});
}
function click(start, gain = .3) {
  noise(fx, {start, length: .004, gain, a: .0005, d: .012, s: 0, r: .01, hp: .6, lpk: .8});
  voice(fx, {start, length: .03, freq: 1900, wave: "sine", gain: gain * .35, a: .001, d: .03, s: 0, r: .02});
}
function pop(start, gain = .18, pitch = 880, pan = 0) {
  voice(fx, {start, length: .05, freq: pitch, wave: "sine", gain, pan, a: .002, d: .09, s: 0, r: .08});
  voice(fx, {start, length: .04, freq: pitch * 2, wave: "sine", gain: gain * .3, pan, a: .002, d: .05, s: 0, r: .05});
}
function whoosh(start, length = .5, gain = .2) {
  const [L, R] = fx, i0 = Math.floor(start * SR), i1 = Math.min(N, Math.ceil((start + length) * SR));
  let lp = 0;
  for (let i = i0; i < i1; i++) {
    const p = (i - i0) / (i1 - i0), white = rand() * 2 - 1, k = .03 + .5 * Math.sin(Math.PI * p); lp += k * (white - lp);
    const e = Math.sin(Math.PI * p) ** 1.5 * gain; L[i] += lp * e * (1 - p * .6); R[i] += lp * e * (.4 + p * .6);
  }
}
function accent(start) {
  noise(fx, {start, length: .4, gain: .3, a: .002, d: .6, s: 0, r: .5, hp: .75, lpk: .35});
  [76, 79, 81, 84].forEach((n, k) => voice(fx, {start: start + k * .04, length: 1.4, freq: midi(n), wave: "tri", gain: .07, pan: (k - 1.5) * .3, a: .01, d: .8, s: .2, r: .7}));
}

// ---- Arrangement: A minor, bright and bouncy -----------------------------------------------------
const CHORDS = [
  {root: 45, tones: [69, 72, 76, 79, 83]},   // Am9
  {root: 41, tones: [65, 69, 72, 76, 79]},   // Fmaj9
  {root: 48, tones: [67, 72, 76, 79, 83]},   // Cmaj9 (G in the bass voicing)
  {root: 43, tones: [67, 71, 74, 76, 79]},   // G6/9
];
const MOTIF = [76, 79, 81, 84, 81, 79];
const bars = Math.ceil(duration / BAR) + 1;
for (let bar = 0; bar < bars; bar++) {
  const t0 = bar * BAR;
  if (t0 >= duration) break;
  const chord = CHORDS[bar % CHORDS.length], sec = sectionAt(t0 + .01), nextSec = sectionAt(t0 + BAR + .01);
  const padGain = {intro: .045, groove: .05, break: .06, outro: .05}[sec];
  for (const n of chord.tones) for (const det of [-6, 6]) voice(music, {start: t0, length: BAR, freq: midi(n), wave: "saw", gain: padGain / chord.tones.length, detune: det, pan: det / 12 * .6,
    a: .5, d: .4, s: .85, r: sec === "outro" ? 2.4 : .8, cutoff: t => 420 + 1100 * (.5 + .5 * Math.sin(2 * Math.PI * (t0 + t) / 12)) + (sec === "groove" ? 600 : 0)});
  if (sec === "groove") {
    for (let b = 0; b < 4; b++) {
      voice(music, {start: t0 + b * BEAT + .012, length: BEAT * .7, freq: midi(chord.root - 12), wave: "sine", gain: .3, a: .01, d: .1, s: .85, r: .1});
      voice(music, {start: t0 + b * BEAT + .012, length: BEAT * .7, freq: midi(chord.root), wave: "saw", gain: .07, a: .01, d: .1, s: .7, r: .1, cutoff: () => 700});
      if (b % 2 === 1) voice(music, {start: t0 + b * BEAT + BEAT * .5 + .012, length: BEAT * .35, freq: midi(chord.root), wave: "sine", gain: .22, a: .008, d: .08, s: .8, r: .08});
    }
  } else if (sec === "break") {
    for (let b = 0; b < 4; b += 2) voice(music, {start: t0 + b * BEAT + .012, length: BEAT * 1.6, freq: midi(chord.root - 12), wave: "sine", gain: .26, a: .02, d: .3, s: .7, r: .3});
  } else if (sec === "outro") {
    voice(music, {start: t0 + .012, length: BAR, freq: midi(chord.root - 12), wave: "sine", gain: .2, a: .05, d: .5, s: .7, r: 1.5});
  }
  const pattern = [1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1];
  const density = {intro: .4, groove: 1, break: .3, outro: .35}[sec];
  for (let s16 = 0; s16 < 16; s16++) {
    if (!pattern[s16] || rand() > density) continue;
    const tone = chord.tones[(s16 * 3 + bar) % chord.tones.length] + (s16 % 5 === 0 ? 12 : 0), start = t0 + s16 * BEAT / 4, pan = s16 % 2 ? .45 : -.45;
    voice(music, {start, length: .08, freq: midi(tone), wave: "tri", gain: .08, pan, a: .002, d: .15, s: 0, r: .11, cutoff: t => 2800 * Math.exp(-t * 12) + 500});
    voice(music, {start: start + BEAT * .75, length: .06, freq: midi(tone), wave: "tri", gain: .032, pan: -pan, a: .002, d: .13, s: 0, r: .1, cutoff: t => 1900 * Math.exp(-t * 12) + 400});
  }
  if (sec === "groove") {
    for (let b = 0; b < 4; b++) kick(t0 + b * BEAT, .82);
    clap(t0 + BEAT, .17); clap(t0 + 3 * BEAT, .17);
    for (let s16 = 0; s16 < 16; s16++) hat(t0 + s16 * BEAT / 4, s16 % 4 === 0 ? .075 : s16 % 2 === 0 ? .05 : .03, s16 === 14);
    if (nextSec === "break") for (let k = 0; k < 4; k++) clap(t0 + 3 * BEAT + k * BEAT / 4, .1 + k * .03);
  } else if (sec === "break") {
    for (let e8 = 0; e8 < 8; e8 += 2) hat(t0 + e8 * BEAT / 2, .035);
    kick(t0, .4);
  } else if (sec === "intro") {
    if (bar >= 1) for (let e8 = 0; e8 < 8; e8++) hat(t0 + e8 * BEAT / 2, .03 + (e8 % 2 ? 0 : .015));
    if (bar >= 1) for (let b = 0; b < 4; b++) kick(t0 + b * BEAT, .5);
  } else if (sec === "outro") {
    if (bar % 2 === 0) kick(t0, .3);
  }
}
// A short lead motif each time the groove (re)starts.
for (const m of marks.filter(m => m.name === "groove")) MOTIF.forEach((n, k) => {
  voice(music, {start: m.t + k * BEAT / 2, length: BEAT * .42, freq: midi(n), wave: "square", gain: .045, pan: .2, a: .004, d: .2, s: .5, r: .18, cutoff: t => 2600 * Math.exp(-t * 7) + 900});
  voice(music, {start: m.t + k * BEAT / 2 + BEAT * .75, length: BEAT * .3, freq: midi(n), wave: "square", gain: .018, pan: -.3, a: .004, d: .2, s: .4, r: .15, cutoff: t => 1800 * Math.exp(-t * 7) + 700});
});
// Final chord at the outro impact.
for (const m of marks.filter(m => m.name === "outro")) [57, 64, 69, 72, 76, 79].forEach((n, k) => voice(music, {start: m.t, length: 4.5, freq: midi(n), wave: k ? "tri" : "sine", gain: k ? .06 : .16, pan: (k % 2 ? .35 : -.35), a: .02, d: 1.5, s: .6, r: 2.0}));

// Sound effects from the composition.
for (const cue of cues) {
  if (cue.t >= duration) continue;
  if (cue.kind === "click") click(cue.t, .26);
  else if (cue.kind === "pop") pop(cue.t, cue.gain ?? .14, cue.pitch || 880, (rand() - .5) * .8);
  else if (cue.kind === "tick") pop(cue.t, cue.gain ?? .05, cue.pitch || 1046, (rand() - .5) * 1.2);
  else if (cue.kind === "whoosh") whoosh(cue.t, cue.length || .5, .14);
  else if (cue.kind === "impact") impact(cue.t, cue.gain || .7);
  else if (cue.kind === "riser") riser(cue.t, cue.length || 2, .28);
  else if (cue.kind === "accent") accent(cue.t);
}

// Sidechain-style ducking of the music bus after every kick, then master.
const duck = new Float64Array(N).fill(1);
for (const k of kicks) { const i0 = Math.floor(k * SR), i1 = Math.min(N, i0 + Math.floor(.3 * SR)); for (let i = i0; i < i1; i++) { const p = (i - i0) / (i1 - i0); duck[i] = Math.min(duck[i], .45 + .55 * p * p); } }
const out = [new Float64Array(N), new Float64Array(N)];
let peak = 0;
for (let ch = 0; ch < 2; ch++) for (let i = 0; i < N; i++) {
  const t = i / SR, fadeIn = Math.min(1, t / 1.2), fadeOut = Math.min(1, Math.max(0, (duration - t) / 2.5));
  const v = Math.tanh((music[ch][i] * duck[i] + drums[ch][i] + fx[ch][i]) * 1.45) * fadeIn * fadeOut;
  out[ch][i] = v; peak = Math.max(peak, Math.abs(v));
}
const norm = peak > 0 ? .89 / peak : 1;
const buffer = Buffer.alloc(44 + N * 4);
buffer.write("RIFF", 0); buffer.writeUInt32LE(36 + N * 4, 4); buffer.write("WAVE", 8);
buffer.write("fmt ", 12); buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(2, 22);
buffer.writeUInt32LE(SR, 24); buffer.writeUInt32LE(SR * 4, 28); buffer.writeUInt16LE(4, 32); buffer.writeUInt16LE(16, 34);
buffer.write("data", 36); buffer.writeUInt32LE(N * 4, 40);
for (let i = 0; i < N; i++) { buffer.writeInt16LE(Math.round(clamp(out[0][i] * norm, -1, 1) * 32767), 44 + i * 4); buffer.writeInt16LE(Math.round(clamp(out[1][i] * norm, -1, 1) * 32767), 46 + i * 4); }
fs.mkdirSync(path.dirname(outPath), {recursive: true});
fs.writeFileSync(outPath, buffer);
console.log("wrote", outPath, duration + "s", "sections:", marks.map(m => `${m.name}@${m.t}`).join(" "));
