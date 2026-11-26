# Play2Evolve

### Playfulness as Feedback for Agentic Game Development

[Weijia Mao](https://maobenz.github.io/maobenz/)<sup>1</sup>,
[Kevin Qinghong Lin](https://qhlin.me/)<sup>1</sup>,
[Zhen Fang](https://costaliya.github.io/)<sup>2</sup>,
[Danze Chen](https://danzer1xxxxchan.github.io/)<sup>1</sup>,
[Dongxing Mao](https://dongxingmao.github.io/)<sup>1</sup>,
[Mike Zheng Shou](https://cde.nus.edu.sg/ece/staff/shou-zheng-mike/)<sup>1</sup>

<sup>1</sup> Show Lab, National University of Singapore<br>
<sup>2</sup> University of Science and Technology of China

[Paper](https://arxiv.org/abs/2611.33457v1) · [Code](https://github.com/showlab/Play2Evolve) ·
[Project page & video demos](https://play2evolve-project-preview.steadydrake257.chatgpt.site) ·
[Method](https://play2evolve-project-preview.steadydrake257.chatgpt.site/#method) ·
[Results](https://play2evolve-project-preview.steadydrake257.chatgpt.site/#results)

## TL;DR

**Play2Evolve combines video generation with game code to build playable games,
then uses agent playtesting to evolve both together.** Video models generate
appearance and motion; code provides interaction, rules, and game state.
Player, Judge, and Designer agents turn gameplay evidence into coordinated
changes to visual materials and executable logic.

- **Build:** turn a game brief into a runnable game with generated materials.
- **Refine:** playtest, diagnose defects, and repair visuals and code together.
- **Evolve:** propose new gameplay and aesthetic goals, implement upgrades, and test the next version.

<p align="center">
  <a href="docs/media/teaser.png">
    <img src="docs/media/teaser.png" alt="Play2Evolve overview: playtest feedback coordinates video generation and game code" width="100%">
  </a>
</p>

## About this branch

`project-page` contains only the static research website and its media assets.
The implementation and setup documentation remain on the
[`main` branch](https://github.com/showlab/Play2Evolve/tree/main).

The website includes a horizontally scrollable gallery of four gameplay recordings,
stage comparisons, method figures, and manuscript results. Every video has independent
**0.25× / 0.5× / 0.75× / 1×** playback controls. Demo recordings are illustrative
checkpoints, not frame-matched A/B tests or the full benchmark cohort.

## Preview locally

No build step or package installation is needed. From the repository root:

```bash
python3 -m http.server 8000 --directory docs
```

Open **http://localhost:8000**. The page plays recorded demos; it does not run the
games in your browser.

## Website files

```text
README.md          Project overview and preview instructions
 docs/
   index.html      Page content and structure
   styles.css      Dark theme and responsive layout
   page.js         Video controls, navigation, and motion
   media/          Videos, posters, and paper figures
   .nojekyll       Static-file support for GitHub Pages
   README.md       Website editing and hosting notes
```

Edit `docs/index.html`, `docs/styles.css`, or `docs/page.js` and refresh the local
preview. Keep media paths relative so the page works under a GitHub Pages project
path as well as at the root of a host. See [website notes](docs/README.md) for
hosting details.

## Release status

The [paper](https://arxiv.org/abs/2611.33457v1) is available on arXiv.
No open-source license has been selected; this branch does not grant redistribution rights beyond
those already held by the respective owners.
