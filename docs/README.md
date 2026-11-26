# Play2Evolve project page

Static HTML, CSS, JavaScript, and local media. No build step or framework is required.

## Preview and edit

From the repository root, run `python3 -m http.server 8000 --directory docs`, then
open http://localhost:8000. Edit `index.html` for content, `styles.css` for layout,
and `page.js` for interactions. All video players support playback down to 0.25×.

Check desktop and mobile widths, section links, video speed controls, and the
comparison game picker after changes. Preserve reduced-motion support and the
existing media paths.

## Hosting

The [public preview](https://play2evolve-project-preview.steadydrake257.chatgpt.site)
is hosted with Sites and allows access without signing in. Sites deployment is
separate from this GitHub branch; pushing here does not automatically update it.

If GitHub Pages is enabled by a repository administrator, choose **project-page**
as the source branch and **/docs** as the folder. The `.nojekyll` file is included
for static serving. This repository has not yet enabled GitHub Pages.

## Media and interpretation

`media/` contains gameplay videos, posters, and paper figures. The showcase is a
responsive card grid. Each video retains its own playback speed control.
Tower Defense and Boxing Gym compare initialization, refinement, and evolution.
Plant Defense shows the original night/combat route clips from RSI cycles 1, 2, and 3.
Comparison clips illustrate checkpoints rather than frame-matched A/B tests.
Do not change manuscript scores or checkpoint labels as part of visual edits.

Author affiliations follow the paper. [Paper](https://arxiv.org/abs/2611.33457v1) · [Code](https://github.com/showlab/Play2Evolve).
