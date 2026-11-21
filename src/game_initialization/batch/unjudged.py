"""Reuse decodable generated files without judging their visual quality."""
import hashlib
import json
from pathlib import Path
from PIL import Image
from .__main__ import read, write, inside


def reuse(root, plan):
    from gamecraft_assets.generate import _backend, _process, _process_still
    manifest = read(root/'asset_manifest.json')
    reused=[]
    for asset in plan['assets']:
        if asset['id'] in manifest['assets']:
            continue  # Preserve selections already made, do not regenerate them.
        signature=hashlib.sha256(json.dumps(asset,sort_keys=True).encode()).hexdigest()
        for dest in sorted((root/'generated'/asset['id']).glob('v[0-9]*')):
            request=read(dest/'request.json')
            if request.get('plan_sha256') != signature:
                continue
            record=read(dest/'result.json') if (dest/'result.json').exists() else {}
            files=record.get('files',[])
            try:
                if len(files)!=asset['frames']:
                    if (dest/'raw.png').is_file() and asset['frames']==1:
                        paths,_,metrics=_process_still(dest/'raw.png',asset,dest)
                    elif (dest/'raw.mp4').is_file():
                        paths,_,metrics=_process(_backend(),dest/'raw.mp4',asset,dest)
                    else:
                        continue
                    files=[str(p.relative_to(root)) for p in paths]
                else:metrics=record.get('metrics',{})
                for name in files:
                    with Image.open(inside(root,name)) as im:im.verify()
            except Exception:
                continue
            manifest['assets'][asset['id']]={**asset,'version':request['version'],'files':files,
                 'plan_sha256':signature,'verdict':{},'metrics':metrics,'quality_status':'not_evaluated',
                 'selection_reason':'First available decodable candidate; visual Judge bypassed by user request'}
            reused.append({'asset':asset['id'],'version':request['version'],'quality_status':'not_evaluated'})
            break
    write(root/'asset_manifest.json',manifest)
    write(root/'unjudged_reuse.json',reused)
