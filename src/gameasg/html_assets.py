"""Bundle validated material frames into a self-contained HTML submission."""
import base64
import json
import re
from pathlib import Path

from .__main__ import digest, read_json, write_json, validate_html

MARKER = '<script src="gameasg-assets.js"></script>'
BEGIN = '<!-- GAMEASG MATERIALS BEGIN -->'
END = '<!-- GAMEASG MATERIALS END -->'


def source_text(game):
    text = (Path(game) / 'index.html').read_text()
    text = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END),
                  '<!-- material runtime omitted; see assets-catalog.json -->', text, flags=re.S)
    return re.sub(r'data:[^\s\"\']+;base64,[A-Za-z0-9+/=]+', '[embedded media]', text)


def stage_materials(version, plan):
    """Keep metadata accessible to Builder without feeding huge base64 strings."""
    version = Path(version)
    game, material = version / 'game', version / 'material'
    game.mkdir(exist_ok=True)
    manifest = read_json(material / 'asset_manifest.json') if plan['assets'] else {'assets': {}}
    bundle, catalog = {}, []
    for asset in plan['assets']:
        record = manifest['assets'].get(asset['id'])
        if not record or len(record['files']) != asset['frames']:
            raise ValueError(f'Missing accepted frames: {asset["id"]}')
        files, hashes = [], []
        for name in record['files']:
            path = (material / name).resolve()
            if not path.is_relative_to(material.resolve()) or not path.is_file():
                raise ValueError('Material frame escapes workspace or is missing')
            files.append('data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode())
            hashes.append(digest(path))
        bundle[asset['id']] = dict(frames=files, fps=asset['fps'], motion=asset['motion'])
        catalog.append({**asset, 'frame_sha256': hashes})
    runtime = Path(__file__).with_name('asset_runtime.js').read_text()
    encoded = json.dumps(bundle, separators=(',', ':')).replace('<', '\\u003c')
    (game / 'gameasg-assets.js').write_text('window.GAMEASG_ASSET_DATA=' + encoded + ';\n' + runtime)
    write_json(game / 'assets-catalog.json', catalog)
    return catalog


def bundle_html(game):
    game = Path(game)
    html = (game / 'index.html').read_text()
    html = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END), MARKER, html, flags=re.S)
    if html.count(MARKER) != 1:
        raise ValueError('Builder must include exactly one gameasg-assets.js script marker')
    runtime = (game / 'gameasg-assets.js').read_text().replace('</script', '<\\/script')
    html = html.replace(MARKER, BEGIN + '<script>\n' + runtime + '\n</script>' + END)
    (game / 'index.html').write_text(html)
    return validate_html(game / 'index.html')


def verify_bundle(game):
    game = Path(game)
    html = (game / 'index.html').read_text()
    expected = (game / 'gameasg-assets.js').read_text().replace('</script', '<\\/script')
    if BEGIN + '<script>\n' + expected + '\n</script>' + END not in html:
        raise ValueError('HTML material bundle no longer matches accepted material files')
    return validate_html(game / 'index.html')
