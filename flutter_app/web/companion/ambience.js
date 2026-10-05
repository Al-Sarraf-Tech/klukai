// Ambience layer behind her: a light 2D canvas of weather and place —
// rain streaks, snow, hangar dust, night stars, lounge bokeh. Particle counts
// scale with the screen and are capped; DPR ≤ 1.5; stops when hidden or when
// the user prefers reduced motion.

/** What drifts behind her for a scene (from logic.sceneFor). */
export function ambienceKind({ scene, rain, snow }) {
  if (rain) return "rain";
  if (snow) return "snow";
  if (scene === "hangar") return "dust";
  if (scene === "night") return "stars";
  if (scene === "lounge") return "bokeh";
  return "dust";
}

export function particleCount(kind, width, height) {
  const area = (width * height) / (390 * 844);
  const base = { rain: 90, snow: 60, dust: 28, stars: 70, bokeh: 14 }[kind] || 0;
  return Math.max(0, Math.min(Math.round(base * area), base * 3));
}

function spawn(kind, w, h, rand) {
  const p = { x: rand() * w, y: rand() * h, r: 1, vx: 0, vy: 0, a: 0.5, tw: rand() * 6.28 };
  if (kind === "rain") Object.assign(p, { vy: 9 + rand() * 6, vx: -1.2, r: 10 + rand() * 10, a: 0.18 });
  else if (kind === "snow") Object.assign(p, { vy: 0.6 + rand() * 0.9, vx: rand() - 0.5, r: 1 + rand() * 2.2, a: 0.7 });
  else if (kind === "dust") Object.assign(p, { vy: -0.12 - rand() * 0.2, vx: (rand() - 0.5) * 0.3, r: 0.8 + rand() * 1.4, a: 0.35 });
  else if (kind === "stars") Object.assign(p, { y: rand() * h * 0.6, r: 0.5 + rand() * 1.1, a: 0.6 });
  else if (kind === "bokeh") Object.assign(p, { vy: -0.08, vx: 0.05, r: 14 + rand() * 30, a: 0.06 + rand() * 0.06 });
  return p;
}

export function createAmbience(canvas, {
  raf = (fn) => requestAnimationFrame(fn),
  caf = (id) => cancelAnimationFrame(id),
  rand = Math.random,
  reducedMotion = () => globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false,
  dpr = () => Math.min(globalThis.devicePixelRatio || 1, 1.5),
} = {}) {
  const ctx = canvas.getContext("2d");
  let kind = "dust";
  let parts = [];
  let loop = null;
  let running = false;
  let w = 0; let h = 0;

  function resize() {
    const scale = dpr();
    w = canvas.clientWidth; h = canvas.clientHeight;
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    reseed();
  }

  function reseed() {
    parts = Array.from({ length: particleCount(kind, w, h) }, () => spawn(kind, w, h, rand));
  }

  function draw() {
    ctx.clearRect(0, 0, w, h);
    for (const p of parts) {
      p.x += p.vx; p.y += p.vy; p.tw += 0.03;
      if (p.y > h + 20) { p.y = -20; p.x = rand() * w; }
      if (p.y < -40) { p.y = h + 20; p.x = rand() * w; }
      if (p.x < -40) p.x = w + 20; else if (p.x > w + 40) p.x = -20;
      if (kind === "rain") {
        ctx.strokeStyle = `rgba(160,200,230,${p.a})`;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p.x + p.vx * 2, p.y + p.r); ctx.stroke();
      } else {
        const alpha = kind === "stars" ? p.a * (0.55 + 0.45 * Math.sin(p.tw)) : p.a;
        ctx.fillStyle = kind === "bokeh" ? `rgba(232,146,62,${alpha})` : `rgba(212,221,230,${alpha})`;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.2832); ctx.fill();
      }
    }
  }

  function frame() {
    loop = null;
    if (!running) return;
    draw();
    loop = raf(frame);
  }

  return {
    resize,
    setKind(next) {
      if (next === kind && parts.length) return;
      kind = next;
      reseed();
    },
    start() {
      if (running) return;
      running = true;
      if (!w) resize();
      if (reducedMotion()) { draw(); return; } // one still frame, no animation
      loop = raf(frame);
    },
    stop() {
      running = false;
      if (loop) { caf(loop); loop = null; }
    },
    get kind() { return kind; },
    get count() { return parts.length; },
  };
}
