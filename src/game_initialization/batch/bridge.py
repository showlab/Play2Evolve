"""Shared-filesystem Codex bridge; requests execute on the authenticated developer host."""
import fcntl
import os
from pathlib import Path
import socket
import time
import uuid
from .__main__ import read, write, codex


def submit(task, config, stage, prompt, schema):
    queue = Path(config['codex_queue'])
    job = queue / uuid.uuid4().hex
    job.mkdir(parents=True)
    write(job/'request.json', {'task':str(task.resolve()),'stage':stage,'prompt':prompt,'schema':schema,
                              'model':config['model'],'timeout':config['timeout'], 'created':time.time()})
    (job/'ready').touch()
    end = time.monotonic() + config['timeout'] + 60
    while time.monotonic() < end:
        if (job/'response.json').exists():
            result = read(job/'response.json')
            if not result['ok']:
                raise RuntimeError('Developer Codex failed: '+result['error'])
            return result['answer']
        time.sleep(2)
    (job/'expired').touch()
    raise TimeoutError(f'Developer Codex timeout: {job}')


def worker(root, binary):
    root = Path(root).resolve()
    config = read(root/'batch.json')
    queue = Path(config['codex_queue']).resolve()
    queue.mkdir(parents=True,exist_ok=True)
    allowed = {root} | {root/t for t in config['tasks']}
    with (queue/'worker.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def status(state, job=None):
            write(queue/'worker_state.json', {'state':state,'job':job,'time':time.time(),'host':socket.gethostname(),'pid':os.getpid()})
        while not (root/'run.exit').exists():
            status('ready')
            for job in sorted(queue.iterdir()):
                if not job.is_dir() or not (job/'ready').exists() or any((job/n).exists() for n in ('response.json','started.json','expired')):
                    continue
                request = read(job/'request.json')
                if time.time()-request['created']>request['timeout']:
                    (job/'expired').touch()
                    continue
                write(job/'started.json', {'pid':os.getpid(),'time':time.time()})
                status('editing',job.name)
                try:
                    task = Path(request['task']).resolve()
                    if task not in allowed or request['model']!=config['model'] or '/' in request['stage']:
                        raise ValueError('Invalid queued task/model/stage')
                    local = dict(config,codex=binary)
                    local.pop('codex_queue',None)
                    answer = codex(task,local,request['stage'],request['prompt'],schema=request['schema'])
                    write(job/'response.json', {'ok':True,'answer':answer})
                except Exception as exc:
                    write(job/'response.json', {'ok':False,'error':str(exc)})
                status('ready')
            time.sleep(2)
        status('stopped')


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch',type=Path,required=True)
    p.add_argument('--codex',required=True)
    a=p.parse_args()
    worker(a.batch,a.codex)
