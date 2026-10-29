/* 训练动作演示小人（2026-10-28 她要）：每个动作只写 2 个关键姿势，膝和肘由两段骨的反向运动学自动算，在两个姿势之间来回播放。
   坐标：200×160，地面 y=150，侧面朝右（front: true 的是正面）。姿势字段：
   h 髋，s 肩（或 t 躯干角度：0 竖直向上，正数向前倾），a 脚踝 [近侧, 远侧]，w 手腕 [近侧, 远侧]，
   kb / eb 膝和肘往哪边弯（1 或 -1，可分近远侧给数组），n 头相对躯干再仰多少度。
   hl 发力部位描金：glute 臀、thigh 大腿、back 背、core 核心、shoulder 肩、arm 手臂。
   prop 器械：r 矩形 [x,y,w,h]、l 线 [x1,y1,x2,y2]、c 圆 [x,y,r]；db 手里拿哑铃；cable 绳索锚点（连到近侧手，cableA 连到近侧脚）。 */
"use strict";

const BONE = { torso: 50, head: 15, upper: 26, fore: 24, thigh: 34, shin: 34, foot: 9 };
const P = (x, y) => [x, y];

const MOVES = {
  hip_thrust_machine: { hl: ["glute"], prop: [{ r: [28, 106, 46, 7] }, { l: [34, 113, 34, 150] }, { l: [66, 113, 66, 150] }],
    frames: [{ h: P(92, 130), s: P(50, 104), a: [P(132, 147)], w: [P(94, 124)], kb: -1, eb: 1 },
             { h: P(98, 112), s: P(50, 106), a: [P(132, 147)], w: [P(98, 106)], kb: -1, eb: 1 }] },
  leg_press: { hl: ["thigh", "glute"], prop: [{ l: [44, 138, 74, 92] }, { l: [40, 140, 100, 140] }, { l: [128, 60, 156, 104], w: 5 }],
    frames: [{ h: P(82, 128), t: -40, a: [P(124, 94)], w: [P(92, 132)], kb: -1, eb: 1 },
             { h: P(82, 128), t: -40, a: [P(140, 78)], w: [P(92, 132)], kb: -1, eb: 1 }] },
  hack_squat: { hl: ["thigh", "glute"], prop: [{ l: [52, 150, 96, 20], w: 4 }, { l: [70, 150, 150, 150], w: 5 }],
    frames: [{ h: P(92, 82), t: -14, a: [P(108, 147)], w: [P(86, 42)], kb: -1, eb: -1 },
             { h: P(78, 114), t: -22, a: [P(108, 147)], w: [P(66, 76)], kb: -1, eb: -1 }] },
  bulgarian_split_squat_db: { hl: ["glute", "thigh"], db: true, prop: [{ r: [30, 116, 44, 7] }, { l: [36, 123, 36, 150] }, { l: [68, 123, 68, 150] }],
    frames: [{ h: P(100, 84), t: 8, a: [P(128, 147), P(58, 114)], w: [P(104, 132)], kb: [-1, -1], eb: 1 },
             { h: P(96, 110), t: 16, a: [P(128, 147), P(58, 114)], w: [P(108, 156)], kb: [-1, -1], eb: 1 }] },
  reverse_lunge_bw: { hl: ["glute", "thigh"],
    frames: [{ h: P(104, 80), t: 0, a: [P(108, 147), P(102, 147)], w: [P(100, 84)], kb: -1, eb: 1 },
             { h: P(94, 106), t: 4, a: [P(116, 147), P(56, 147)], w: [P(92, 110)], kb: [-1, -1], eb: 1 }] },
  cable_kickback: { hl: ["glute"], cableA: P(30, 142), prop: [{ l: [22, 20, 22, 150], w: 4 }, { l: [160, 30, 160, 150], w: 4 }],
    frames: [{ h: P(104, 84), t: 38, a: [P(100, 142), P(108, 147)], w: [P(158, 74)], kb: [-1, -1], eb: 1 },
             { h: P(104, 84), t: 38, a: [P(52, 104), P(108, 147)], w: [P(158, 74)], kb: [-1, -1], eb: 1 }] },
  db_rdl: { hl: ["glute", "thigh", "back"], db: true,
    frames: [{ h: P(100, 80), t: 0, a: [P(104, 147)], w: [P(104, 128)], kb: -1, eb: 1 },
             { h: P(86, 86), t: 74, a: [P(104, 147)], w: [P(132, 124)], kb: -1, eb: 1 }] },
  seated_leg_curl: { hl: ["thigh"], prop: [{ r: [62, 102, 60, 8] }, { l: [62, 102, 54, 46], w: 4 }, { l: [90, 110, 90, 150] }],
    frames: [{ h: P(80, 98), t: -10, a: [P(146, 94)], w: [P(96, 96)], kb: 1, eb: 1 },
             { h: P(80, 98), t: -10, a: [P(104, 132)], w: [P(96, 96)], kb: 1, eb: 1 }] },
  hip_abduction: { front: true, hl: ["glute"], prop: [{ r: [74, 104, 52, 8] }, { l: [100, 112, 100, 150] }],
    frames: [{ h: P(100, 100), t: 6, a: [P(90, 147), P(110, 147)], w: [P(88, 104), P(112, 104)], kb: [1, -1], eb: [1, -1] },
             { h: P(100, 100), t: 6, a: [P(64, 147), P(136, 147)], w: [P(88, 104), P(112, 104)], kb: [1, -1], eb: [1, -1] }] },
  back_extension: { hl: ["glute", "back"], prop: [{ l: [40, 146, 104, 96], w: 4 }, { c: [104, 96, 6] }],
    frames: [{ h: P(100, 90), t: 150, a: [P(58, 142)], w: [P(116, 116)], kb: 1, eb: 1 },
             { h: P(100, 90), t: 40, a: [P(58, 142)], w: [P(118, 66)], kb: 1, eb: 1 }] },
  glute_bridge: { hl: ["glute"], prop: [{ l: [20, 150, 180, 150] }],
    frames: [{ h: P(102, 142), s: P(54, 140), a: [P(130, 147)], w: [P(74, 147)], kb: -1, eb: -1, n: 0 },
             { h: P(104, 116), s: P(54, 140), a: [P(130, 147)], w: [P(74, 147)], kb: -1, eb: -1, n: 0 }] },
  lat_pulldown: { hl: ["back", "arm"], cable: P(100, 0), prop: [{ r: [80, 106, 44, 7] }, { l: [70, 4, 132, 4], w: 3 }],
    frames: [{ h: P(98, 104), t: -10, a: [P(132, 147)], w: [P(94, 12), P(102, 12)], kb: -1, eb: -1 },
             { h: P(98, 104), t: -16, a: [P(132, 147)], w: [P(98, 52), P(104, 52)], kb: -1, eb: -1 }] },
  assisted_pullup: { hl: ["back", "arm"], prop: [{ l: [60, 12, 140, 12], w: 4 }, { r: [56, 0, 0, 0] }],
    frames: [{ h: P(100, 114), t: 0, a: [P(66, 146)], w: [P(100, 14)], kb: 1, eb: -1 },
             { h: P(100, 80), t: 0, a: [P(66, 112)], w: [P(100, 14)], kb: 1, eb: -1 }] },
  seated_cable_row: { hl: ["back"], cable: P(192, 112), prop: [{ r: [50, 130, 70, 7] }, { l: [150, 126, 150, 150], w: 4 }],
    frames: [{ h: P(78, 128), t: 18, a: [P(146, 142)], w: [P(150, 112)], kb: -1, eb: 1 },
             { h: P(78, 128), t: -6, a: [P(146, 142)], w: [P(98, 108)], kb: -1, eb: 1 }] },
  chest_supported_db_row: { hl: ["back"], db: true, prop: [{ l: [56, 136, 128, 74], w: 4 }, { l: [92, 106, 92, 150] }],
    frames: [{ h: P(70, 118), t: 52, a: [P(48, 147)], w: [P(112, 140)], kb: -1, eb: 1 },
             { h: P(70, 118), t: 52, a: [P(48, 147)], w: [P(98, 108)], kb: -1, eb: 1 }] },
  db_shoulder_press_seated: { hl: ["shoulder", "arm"], db: true, prop: [{ r: [70, 106, 50, 7] }, { l: [74, 106, 70, 46], w: 4 }],
    frames: [{ h: P(94, 104), t: -4, a: [P(130, 147)], w: [P(100, 50)], kb: -1, eb: 1 },
             { h: P(94, 104), t: -4, a: [P(130, 147)], w: [P(96, 6)], kb: -1, eb: 1 }] },
  db_lateral_raise: { front: true, hl: ["shoulder"], db: true,
    frames: [{ h: P(100, 80), t: 0, a: [P(92, 147), P(108, 147)], w: [P(80, 126), P(120, 126)], kb: [1, -1], eb: [-1, 1] },
             { h: P(100, 80), t: 0, a: [P(92, 147), P(108, 147)], w: [P(40, 38), P(160, 38)], kb: [1, -1], eb: [-1, 1] }] },
  face_pull: { hl: ["shoulder", "back"], cable: P(192, 36), prop: [{ l: [194, 10, 194, 150], w: 4 }],
    frames: [{ h: P(94, 80), t: -4, a: [P(100, 147), P(90, 147)], w: [P(142, 38)], kb: -1, eb: -1 },
             { h: P(94, 80), t: -6, a: [P(100, 147), P(90, 147)], w: [P(104, 30)], kb: -1, eb: -1 }] },
  cable_rear_delt_fly: { front: true, hl: ["shoulder", "back"],
    frames: [{ h: P(100, 80), t: 0, a: [P(92, 147), P(108, 147)], w: [P(108, 40), P(92, 40)], kb: [1, -1], eb: [1, -1] },
             { h: P(100, 80), t: 0, a: [P(92, 147), P(108, 147)], w: [P(42, 36), P(158, 36)], kb: [1, -1], eb: [-1, 1] }] },
  db_bench_press: { hl: ["arm", "shoulder"], db: true, prop: [{ r: [40, 116, 100, 7] }, { l: [50, 123, 50, 150] }, { l: [130, 123, 130, 150] }],
    frames: [{ h: P(116, 112), s: P(66, 112), a: [P(158, 147)], w: [P(70, 98)], kb: -1, eb: 1 },
             { h: P(116, 112), s: P(66, 112), a: [P(158, 147)], w: [P(68, 64)], kb: -1, eb: 1 }] },
  dead_bug: { hl: ["core"], prop: [{ l: [10, 150, 190, 150] }],
    frames: [{ h: P(112, 142), s: P(62, 142), a: [P(146, 108), P(146, 108)], w: [P(64, 94), P(64, 94)], kb: [-1, -1], eb: [1, 1], n: 0 },
             { h: P(112, 142), s: P(62, 142), a: [P(146, 108), P(178, 136)], w: [P(16, 136), P(64, 94)], kb: [-1, -1], eb: [1, 1], n: 0 }] },
  bird_dog: { hl: ["core", "back", "glute"], prop: [{ l: [10, 150, 190, 150] }],
    frames: [{ h: P(80, 104), t: 90, a: [P(48, 147), P(48, 147)], w: [P(130, 147), P(130, 147)], kb: [-1, -1], eb: 1, n: 0 },
             { h: P(80, 104), t: 90, a: [P(48, 147), P(12, 100)], w: [P(178, 98), P(130, 147)], kb: [-1, -1], eb: 1, n: 0 }] },
  pallof_press: { hl: ["core"], cable: P(14, 62), prop: [{ l: [10, 20, 10, 150], w: 4 }],
    frames: [{ h: P(98, 86), t: 0, a: [P(108, 147), P(96, 147)], w: [P(108, 64)], kb: -1, eb: 1 },
             { h: P(98, 86), t: 0, a: [P(108, 147), P(96, 147)], w: [P(146, 62)], kb: -1, eb: 1 }] },
  side_plank: { hl: ["core"], prop: [{ l: [10, 150, 190, 150] }], dur: 4,
    frames: [{ h: P(112, 140), s: P(64, 122), a: [P(176, 149)], w: [P(40, 147), P(116, 132)], kb: -1, eb: [-1, 1], n: 0 },
             { h: P(112, 131), s: P(64, 120), a: [P(176, 148)], w: [P(40, 147), P(114, 124)], kb: -1, eb: [-1, 1], n: 0 }] },
  plank: { hl: ["core"], prop: [{ l: [10, 150, 190, 150] }], dur: 4,
    frames: [{ h: P(108, 134), s: P(62, 122), a: [P(176, 149)], w: [P(86, 147)], kb: -1, eb: -1, n: 0 },
             { h: P(108, 130), s: P(62, 120), a: [P(176, 148)], w: [P(86, 147)], kb: -1, eb: -1, n: 0 }] },
  incline_walk: { hl: ["thigh", "glute"], dur: 1.2, prop: [{ l: [20, 152, 184, 138], w: 5 }],
    frames: [{ h: P(104, 76), t: 6, a: [P(130, 141), P(80, 146)], w: [P(88, 116), P(122, 112)], kb: -1, eb: [1, 1] },
             { h: P(104, 76), t: 6, a: [P(80, 146), P(130, 141)], w: [P(122, 112), P(88, 116)], kb: -1, eb: [1, 1] }] },
};

function ik(r, t, a, b, sign) {
  const dx = t[0] - r[0], dy = t[1] - r[1];
  const d = Math.min(Math.hypot(dx, dy), a + b - 0.01) || 0.01;
  const base = Math.atan2(dy, dx);
  const alpha = Math.acos(Math.max(-1, Math.min(1, (a * a + d * d - b * b) / (2 * a * d))));
  const ang = base + sign * alpha;
  const j = [r[0] + a * Math.cos(ang), r[1] + a * Math.sin(ang)];
  const k = Math.hypot(dx, dy) > a + b ? b / Math.hypot(t[0] - j[0], t[1] - j[1]) : 1;  // 够不着就把手脚伸直指向目标
  return [j, [j[0] + (t[0] - j[0]) * k, j[1] + (t[1] - j[1]) * k]];
}

const lerp = (x, y, k) => x + (y - x) * k;
const lerpP = (p, q, k) => [lerp(p[0], q[0], k), lerp(p[1], q[1], k)];
const pick = (v, i) => (Array.isArray(v) ? v[i] ?? v[0] : v);

function poseAt(move, k) {
  const [f0, f1] = move.frames;
  const shoulder = (f) => f.s || [f.h[0] + BONE.torso * Math.sin(f.t * Math.PI / 180), f.h[1] - BONE.torso * Math.cos(f.t * Math.PI / 180)];
  const two = (arr) => [arr[0], arr[1] || arr[0]];
  return { h: lerpP(f0.h, f1.h, k), s: lerpP(shoulder(f0), shoulder(f1), k), n: lerp(f0.n ?? 8, f1.n ?? 8, k),
           a: two(f0.a).map((p, i) => lerpP(p, two(f1.a)[i], k)), w: two(f0.w).map((p, i) => lerpP(p, two(f1.w)[i], k)),
           kb: [pick(f0.kb, 0), pick(f0.kb, 1)], eb: [pick(f0.eb, 0), pick(f0.eb, 1)] };
}

function figureSVG(id, k) {
  const m = MOVES[id];
  if (!m) return "";
  const p = poseAt(m, k), hl = new Set(m.hl || []);
  const front = !!m.front;
  const seg = (a, b, part, w = 4.2) => `<line x1="${a[0].toFixed(1)}" y1="${a[1].toFixed(1)}" x2="${b[0].toFixed(1)}" y2="${b[1].toFixed(1)}" class="${hl.has(part) ? "hot" : "bone"}" stroke-width="${w}"/>`;
  const tv = [p.s[0] - p.h[0], p.s[1] - p.h[1]], tl = Math.hypot(...tv) || 1, up = [tv[0] / tl, tv[1] / tl];
  const side = [-up[1], up[0]];  // 正面图左右偏移的方向
  const off = (q, d) => (front ? [q[0] + side[0] * d, q[1] + side[1] * d] : q);
  const hips = [off(p.h, -9), off(p.h, 9)], shs = [off(p.s, -13), off(p.s, 13)];
  const rot = (v, deg) => { const r = deg * Math.PI / 180; return [v[0] * Math.cos(r) - v[1] * Math.sin(r), v[0] * Math.sin(r) + v[1] * Math.cos(r)]; };
  const hd = rot(up, p.n), head = [p.s[0] + hd[0] * BONE.head, p.s[1] + hd[1] * BONE.head];
  let out = (m.prop || []).map((o) => o.r ? `<rect x="${o.r[0]}" y="${o.r[1]}" width="${o.r[2]}" height="${o.r[3]}" rx="2" class="prop"/>`
    : o.l ? `<line x1="${o.l[0]}" y1="${o.l[1]}" x2="${o.l[2]}" y2="${o.l[3]}" class="propl" stroke-width="${o.w || 2}"/>`
    : o.c ? `<circle cx="${o.c[0]}" cy="${o.c[1]}" r="${o.c[2]}" class="prop"/>` : "").join("");
  const legs = [0, 1].map((i) => ik(hips[i], p.a[i], BONE.thigh, BONE.shin, p.kb[i]));
  const arms = [0, 1].map((i) => ik(shs[i], p.w[i], BONE.upper, BONE.fore, p.eb[i]));
  if (m.cable) out += `<line x1="${m.cable[0]}" y1="${m.cable[1]}" x2="${p.w[0][0].toFixed(1)}" y2="${p.w[0][1].toFixed(1)}" class="cable"/>`;
  if (m.cableA) out += `<line x1="${m.cableA[0]}" y1="${m.cableA[1]}" x2="${p.a[0][0].toFixed(1)}" y2="${p.a[0][1].toFixed(1)}" class="cable"/>`;
  const limbs = (i, faint) => {
    const [knee, ankle] = legs[i], [elbow, wrist] = arms[i];
    const fdir = front ? [0, 0] : (() => { const v = [ankle[0] - knee[0], ankle[1] - knee[1]], l = Math.hypot(...v) || 1; return [-v[1] / l, v[0] / l]; })();
    const toe = [ankle[0] - fdir[0] * BONE.foot, ankle[1] - fdir[1] * BONE.foot];
    return `<g opacity="${faint ? 0.45 : 1}">${seg(hips[i], knee, "thigh")}${seg(knee, ankle, "thigh", 3.6)}${front ? "" : seg(ankle, toe, "", 3)}
      ${seg(shs[i], elbow, "arm", 3.6)}${seg(elbow, wrist, "arm", 3.2)}${m.db ? `<rect x="${(wrist[0] - 6).toFixed(1)}" y="${(wrist[1] - 2.5).toFixed(1)}" width="12" height="5" rx="1.5" class="dumb"/>` : ""}</g>`;
  };
  out += limbs(1, !front) + seg(p.h, p.s, hl.has("back") ? "back" : hl.has("core") ? "core" : "", 6);
  if (front) out += seg(hips[0], hips[1], "glute", 5) + seg(shs[0], shs[1], "shoulder", 5);
  out += `<circle cx="${head[0].toFixed(1)}" cy="${head[1].toFixed(1)}" r="8" class="headc"/>`;
  if (hl.has("glute") && !front) out += `<circle cx="${p.h[0].toFixed(1)}" cy="${p.h[1].toFixed(1)}" r="5.5" class="hotdot"/>`;
  if (hl.has("shoulder") && !front) out += `<circle cx="${p.s[0].toFixed(1)}" cy="${p.s[1].toFixed(1)}" r="5" class="hotdot"/>`;
  out += limbs(0, false);
  return out;
}

// 弹层里所有小人一起动：两个姿势之间来回，缓入缓出
let moveTimer = null;
function animateMoves() {
  if (moveTimer) cancelAnimationFrame(moveTimer);
  const t0 = performance.now();
  const tick = (now) => {
    const figs = document.querySelectorAll("svg.move[data-move]");
    if (!figs.length) { moveTimer = null; return; }
    figs.forEach((svg) => {
      const m = MOVES[svg.dataset.move];
      if (!m) return;
      const dur = (m.dur || 2.4) * 1000, ph = ((now - t0) % dur) / dur;
      svg.innerHTML = figureSVG(svg.dataset.move, (1 - Math.cos(ph * 2 * Math.PI)) / 2);
    });
    moveTimer = requestAnimationFrame(tick);
  };
  moveTimer = requestAnimationFrame(tick);
}
