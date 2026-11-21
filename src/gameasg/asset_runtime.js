/* Generated frames remain code-controlled. No scores, collisions or outcomes in media. */
(() => {
  const records = {}, usage = {};
  for (const [id, record] of Object.entries(window.GAMEASG_ASSET_DATA || {})) {
    records[id] = {...record, images: record.frames.map(src => {
      const img = new Image(); img.src = src; return img;
    })};
  }
  window.GameASGAssets = Object.freeze({
    draw(ctx, id, x, y, width, height, elapsedSeconds = 0) {
      const item = records[id];
      if (!item) throw new Error(`Unknown material: ${id}`);
      const tick = Math.max(0, Math.floor(elapsedSeconds * item.fps));
      const index = item.motion === 'loop' ? tick % item.images.length : Math.min(tick, item.images.length - 1);
      const img = item.images[index];
      if (!img.complete || !img.naturalWidth) return false;
      ctx.drawImage(img, x, y, width, height);
      const u = usage[id] ||= {draws: 0, frames: []};
      u.draws++;
      if (!u.frames.includes(index)) u.frames.push(index);
      return true;
    },
    ready(id) { return !!records[id]?.images.every(x => x.complete && x.naturalWidth); },
    evidence() { return JSON.parse(JSON.stringify(usage)); },
  });
})();
