"use strict";

const comparisons = {
  "tower-defense": { title: "Tower Defense", scene: "combat scene" },
  boxing: { title: "Boxing Gym", scene: "ring combat" },
  "pirate-port": { title: "Pirate Port", scene: "harbor construction" },
};
const stages = ["bootstrap", "init3", "rsi"];
const stageLabels = ["initialization", "refinement", "evolution"];
const videos = stages.map(stage => document.getElementById(`compare-${stage}`));
const playButton = document.getElementById("play-comparison");
const status = document.getElementById("comparison-status");
let selection = 0;

document.querySelectorAll("[data-game]").forEach(button => {
  button.addEventListener("click", () => {
    selection++;
    const id = button.dataset.game;
    const game = comparisons[id];
    document.querySelectorAll("[data-game]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
    videos.forEach((video, index) => {
      video.pause();
      video.poster = `media/compare-${id}-${stages[index]}.jpg`;
      video.src = `media/compare-${id}-${stages[index]}.mp4`;
      video.setAttribute("aria-label", `${game.title} ${stageLabels[index]}`);
      video.parentElement.parentElement.querySelector(".video-error")?.remove();
      video.load();
    });
    document.getElementById("comparison-scene").textContent = `${game.title} · ${game.scene}`;
    playButton.textContent = "Play all three";
    playButton.disabled = false;
    status.textContent = "";
  });
});

playButton.addEventListener("click", async () => {
  if (videos.some(video => !video.paused)) {
    videos.forEach(video => video.pause());
    return;
  }
  const requestSelection = selection;
  playButton.disabled = true;
  status.textContent = "Loading comparison clips…";
  try {
    await Promise.all(videos.map(video => new Promise((resolve, reject) => {
      if (video.readyState >= 3) return resolve();
      const timer = setTimeout(() => finish(false), 20000);
      const ready = () => finish(true);
      const failed = () => finish(false);
      const finish = ok => {
        clearTimeout(timer);
        video.removeEventListener("canplay", ready);
        video.removeEventListener("error", failed);
        ok ? resolve() : reject(new Error("Video unavailable"));
      };
      video.addEventListener("canplay", ready);
      video.addEventListener("error", failed);
      video.load();
    })));
    if (selection !== requestSelection) return;
    videos.forEach(video => { video.currentTime = 0; });
    const results = await Promise.allSettled(videos.map(video => video.play()));
    status.textContent = results.some(result => result.status === "rejected")
      ? "Your browser paused playback. Use the video controls to play the clips." : "";
  } catch {
    if (selection === requestSelection) status.textContent = "A clip could not load. Check the connection or use each video's controls.";
  } finally {
    if (selection === requestSelection) playButton.disabled = false;
  }
});

videos.forEach(video => ["play", "pause", "ended"].forEach(event => video.addEventListener(event, () => {
  playButton.textContent = videos.some(item => !item.paused && !item.ended) ? "Pause all three" : "Play all three";
})));

document.querySelectorAll("video").forEach(video => {
  video.addEventListener("error", () => {
    const parent = video.parentElement.parentElement;
    if (parent.querySelector(".video-error")) return;
    const message = document.createElement("p");
    message.className = "video-error";
    message.setAttribute("role", "status");
    message.textContent = "This video could not be loaded. Please reload the page.";
    video.parentElement.insertAdjacentElement("afterend", message);
  });
});

// Stop off-screen videos so the gallery does not decode every clip at once.
if ("IntersectionObserver" in window) {
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => { if (!entry.isIntersecting) entry.target.pause(); });
  }, { threshold: 0.01 });
  document.querySelectorAll("video").forEach(video => observer.observe(video));
}

// Decorative canvas only. Suspend rendering when hidden, off-screen, or paused.
(() => {
  const canvas = document.getElementById('hero-code-flow');
  const hero = canvas?.closest('.hero');
  const button = document.querySelector('.hero-motion');
  const ctx = canvas?.getContext('2d');
  if (!ctx || !hero || !button) return;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const alphabet = '<>{}[]01=+*;():/';
  let paused = reduced.matches;
  let visible = true;
  let frame = 0;
  let last = 0;
  let width = 0;
  let height = 0;
  let streams = [];
  function paint(dt = 0) {
    ctx.clearRect(0, 0, width, height);
    ctx.font = '14px monospace';
    streams.forEach(stream => {
      stream.y += dt * stream.speed;
      if (stream.y - stream.length * 21 > height) stream.y = -21;
      for (let j = 0; j < stream.length; j++) {
        const y = stream.y - j * 21;
        if (y < 0 || y > height + 20) continue;
        const alpha = (1 - j / stream.length) * .38;
        ctx.fillStyle = j === 0 ? 'rgba(147,222,192,.65)' : `rgba(52,174,140,${alpha})`;
        ctx.fillText(alphabet[(stream.seed + j * 7 + Math.floor(stream.y / 42)) % alphabet.length], stream.x, y);
      }
    });
  }
  function tick(now) {
    frame = 0;
    if (paused || !visible || document.hidden) { last = 0; return; }
    if (!last || now - last >= 42) {
      paint(last ? Math.min((now - last) / 1000, .1) : 0);
      last = now;
    }
    frame = requestAnimationFrame(tick);
  }
  function sync() {
    button.textContent = paused ? 'Play background' : 'Pause background';
    button.setAttribute('aria-pressed', String(paused));
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    last = 0;
    if (!paused && visible && !document.hidden) frame = requestAnimationFrame(tick);
  }
  function resize() {
    width = hero.clientWidth;
    height = hero.clientHeight;
    const scale = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    streams = Array.from({length: Math.ceil(width / 32)}, (_, i) => ({
      x: i * 32 + 7, y: Math.random() * height,
      speed: 24 + Math.random() * 28, length: 9 + Math.floor(Math.random() * 16),
      seed: Math.floor(Math.random() * alphabet.length),
    }));
    paint();
  }
  button.addEventListener('click', () => { paused = !paused; sync(); });
  reduced.addEventListener('change', () => { paused = reduced.matches; sync(); });
  document.addEventListener('visibilitychange', sync);
  new ResizeObserver(resize).observe(hero);
  new IntersectionObserver(entries => { visible = entries[0].isIntersecting; sync(); }).observe(hero);
  resize();
  sync();
})();
