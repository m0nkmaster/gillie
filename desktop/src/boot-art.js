// Generative boot screen for the Roosta desktop app.
// Seeded by the calendar date, so the look is stable through a given morning
// but different the next day — a fresh scene, palette and greeting each day.
// Pure canvas, no network, CSP-safe (served from 'self').

(() => {
  "use strict";

  // --- Seeded PRNG (mulberry32) keyed to today's date -----------------------
  const now = new Date();
  const dayKey = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}`;
  function hashStr(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rng = mulberry32(hashStr(dayKey));
  const pick = (arr) => arr[Math.floor(rng() * arr.length)];
  const range = (lo, hi) => lo + rng() * (hi - lo);

  // --- Palettes -------------------------------------------------------------
  const PALETTES = [
    { name: "aurora",   bg: ["#05121a", "#0a2432"], a: "#5de4c7", b: "#6aa8ff" },
    { name: "ember",    bg: ["#180a0f", "#2a0f16"], a: "#ff8a5c", b: "#ffd166" },
    { name: "violet",   bg: ["#0e0a1e", "#1a1038"], a: "#b78bff", b: "#6aa8ff" },
    { name: "deepsea",  bg: ["#04121c", "#06202e"], a: "#3fd0ff", b: "#4f8bff" },
    { name: "forest",   bg: ["#071610", "#0c2419"], a: "#62d7a5", b: "#c7f05d" },
    { name: "rose",     bg: ["#190a12", "#2a0f1e"], a: "#ff6ea9", b: "#ffb36a" },
    { name: "arctic",   bg: ["#0a1119", "#122233"], a: "#9ad6ff", b: "#e4ecff" },
    { name: "mono",     bg: ["#0b0d12", "#16191f"], a: "#c3ccd8", b: "#8a94a3" },
  ];
  const palette = pick(PALETTES);

  // Expose accent colours to CSS (mark, status dot, buttons).
  const rootStyle = document.documentElement.style;
  rootStyle.setProperty("--spark-a", palette.a);
  rootStyle.setProperty("--spark-b", palette.b);
  const mark = document.querySelectorAll(".mark span");
  if (mark[0]) mark[0].classList.add("lit-a");
  if (mark[3]) mark[3].classList.add("lit-b");

  // --- Greeting + flavour ---------------------------------------------------
  const hour = now.getHours();
  const greeting =
    hour < 5 ? "Burning the midnight oil" :
    hour < 12 ? "Good morning" :
    hour < 18 ? "Good afternoon" :
    "Good evening";
  const FLAVOURS = [
    "Warming up your second brain…",
    "Gathering the threads that need you…",
    "Reading the room across Teams, Jira and the vault…",
    "Tuning the signal, trimming the noise…",
    "Lining up today's measurements…",
    "Dusting off the board, one tile at a time…",
    "Fetching what moved while you were away…",
    "Rounding up the overnight runs…",
    "Pouring the coffee, metaphorically…",
    "Checking what the squad shipped…",
    "Sweeping the inbox of the mind…",
    "Setting the stage for a sharp day…",
  ];
  const gEl = document.getElementById("greeting");
  const fEl = document.getElementById("flavour");
  if (gEl) gEl.textContent = greeting;
  if (fEl) fEl.textContent = pick(FLAVOURS);

  // --- Canvas setup ---------------------------------------------------------
  const canvas = document.getElementById("art");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let W = 0, H = 0, dpr = 1;

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.floor(W * dpr);
    canvas.height = Math.floor(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (scene && scene.layout) scene.layout();
  }

  function paintBackground() {
    const g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, palette.bg[0]);
    g.addColorStop(1, palette.bg[1]);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);
  }

  // --- Scenes ---------------------------------------------------------------
  // Each scene: { layout(), frame(t) }

  function constellationScene() {
    let nodes = [];
    function layout() {
      const count = Math.round((W * H) / 26000);
      nodes = Array.from({ length: Math.max(28, Math.min(120, count)) }, () => ({
        x: rng() * W, y: rng() * H,
        vx: range(-0.18, 0.18), vy: range(-0.18, 0.18),
        r: range(1, 2.6),
      }));
    }
    function frame() {
      paintBackground();
      const link = 150;
      for (const n of nodes) {
        n.x += n.vx; n.y += n.vy;
        if (n.x < 0 || n.x > W) n.vx *= -1;
        if (n.y < 0 || n.y > H) n.vy *= -1;
      }
      ctx.lineWidth = 1;
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
          const d = Math.hypot(dx, dy);
          if (d < link) {
            ctx.strokeStyle = palette.a + hex2((1 - d / link) * 0.5);
            ctx.beginPath();
            ctx.moveTo(nodes[i].x, nodes[i].y);
            ctx.lineTo(nodes[j].x, nodes[j].y);
            ctx.stroke();
          }
        }
      }
      for (const n of nodes) {
        ctx.fillStyle = palette.b;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    return { layout, frame };
  }

  function starfieldScene() {
    let stars = [];
    const cx = () => W / 2, cy = () => H / 2;
    function layout() {
      const count = Math.round((W * H) / 7000);
      stars = Array.from({ length: Math.max(120, Math.min(520, count)) }, () => ({
        a: rng() * Math.PI * 2,
        d: rng() * Math.max(W, H) * 0.6,
        s: range(0.4, 2.2),
        r: range(0.4, 1.8),
      }));
    }
    function frame() {
      paintBackground();
      for (const st of stars) {
        st.d += st.s;
        const max = Math.max(W, H) * 0.72;
        if (st.d > max) { st.d = 0; st.a = rng() * Math.PI * 2; }
        const x = cx() + Math.cos(st.a) * st.d;
        const y = cy() + Math.sin(st.a) * st.d;
        const alpha = Math.min(1, st.d / max + 0.1);
        ctx.fillStyle = (rng() < 0.08 ? palette.a : "#ffffff") + hex2(alpha * 0.9);
        ctx.beginPath();
        ctx.arc(x, y, st.r * (0.6 + st.d / max), 0, Math.PI * 2);
        ctx.fill();
      }
    }
    return { layout, frame };
  }

  function orbsScene() {
    let orbs = [];
    function layout() {
      const count = Math.max(5, Math.round(W / 220));
      orbs = Array.from({ length: count }, () => ({
        x: rng() * W, y: rng() * H,
        vx: range(-0.25, 0.25), vy: range(-0.25, 0.25),
        r: range(90, 260),
        c: rng() < 0.5 ? palette.a : palette.b,
      }));
    }
    function frame() {
      paintBackground();
      ctx.globalCompositeOperation = "lighter";
      for (const o of orbs) {
        o.x += o.vx; o.y += o.vy;
        if (o.x < -o.r) o.x = W + o.r; if (o.x > W + o.r) o.x = -o.r;
        if (o.y < -o.r) o.y = H + o.r; if (o.y > H + o.r) o.y = -o.r;
        const g = ctx.createRadialGradient(o.x, o.y, 0, o.x, o.y, o.r);
        g.addColorStop(0, o.c + "4d");
        g.addColorStop(1, o.c + "00");
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(o.x, o.y, o.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalCompositeOperation = "source-over";
    }
    return { layout, frame };
  }

  function wavesScene() {
    const layers = 5;
    const phase = Array.from({ length: layers }, () => rng() * Math.PI * 2);
    function layout() {}
    function frame(t) {
      paintBackground();
      for (let l = 0; l < layers; l++) {
        const amp = 26 + l * 14;
        const yBase = H * (0.45 + l * 0.11);
        const speed = 0.0006 + l * 0.0002;
        const col = l % 2 ? palette.b : palette.a;
        ctx.beginPath();
        ctx.moveTo(0, H);
        for (let x = 0; x <= W; x += 12) {
          const y = yBase + Math.sin(x * 0.006 + t * speed + phase[l]) * amp
                          + Math.sin(x * 0.013 + t * speed * 1.7) * amp * 0.4;
          ctx.lineTo(x, y);
        }
        ctx.lineTo(W, H);
        ctx.closePath();
        ctx.fillStyle = col + hex2(0.08 + l * 0.015);
        ctx.fill();
      }
    }
    return { layout, frame };
  }

  function spiroScene() {
    // Slowly drawn generative rose/spirograph, then gently fades and redraws.
    const R = () => Math.min(W, H) * 0.34;
    const k = Math.floor(range(2, 7));
    const petals = Math.floor(range(3, 9));
    let theta = 0;
    function layout() { theta = 0; paintBackground(); }
    function frame() {
      // Fade previous strokes a touch for a glowing trail.
      ctx.fillStyle = palette.bg[0] + "0c";
      ctx.fillRect(0, 0, W, H);
      const cx = W / 2, cy = H / 2, r = R();
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = (rng() < 0.5 ? palette.a : palette.b) + "cc";
      ctx.beginPath();
      let first = true;
      for (let i = 0; i < 60; i++) {
        theta += 0.018;
        const rr = r * Math.cos(petals * theta);
        const x = cx + Math.cos(theta) * rr + Math.cos(k * theta) * r * 0.18;
        const y = cy + Math.sin(theta) * rr + Math.sin(k * theta) * r * 0.18;
        if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
      if (theta > Math.PI * 24) layout();
    }
    return { layout, frame };
  }

  const SCENES = {
    constellation: constellationScene,
    starfield: starfieldScene,
    orbs: orbsScene,
    waves: wavesScene,
    spiro: spiroScene,
  };
  const sceneName = pick(Object.keys(SCENES));

  // 2-digit hex alpha from a 0..1 float.
  function hex2(a) {
    const v = Math.max(0, Math.min(255, Math.round(a * 255)));
    return v.toString(16).padStart(2, "0");
  }

  const scene = SCENES[sceneName]();
  resize();
  window.addEventListener("resize", resize);

  if (reduced) {
    // One static frame, no animation.
    scene.frame(0);
    return;
  }

  let start = performance.now();
  function loop(now2) {
    scene.frame(now2 - start);
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();
