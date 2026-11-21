"""Build isolated material/code variants and select only complete replayed games."""
import copy
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from gamecraft_behavior.core import digest, write_json
from gamecraft_composition.core import snapshot
from gamecraft_assets.contract import validate_bound
from .config import relative
from .selection import promotion


def bundle_plan(repair_plan):
    """Keep root-cause units for the editor, but test whole-game versions once."""
    groups=[g for g in repair_plan['groups'] if g['candidates']]
    if not groups:
        return {**repair_plan,'groups':[]}
    escalated=[g for g in groups if len(g['candidates'])==2]

    def option(kind):
        units=[];materials=[];controls=[];video=[];code=[];instructions=[];scopes=[]
        for group in groups:
            proposal=group['candidates'][1] if kind=='higher_level' and group in escalated else group['candidates'][0]
            unit={'id':group['id'],'issue_ids':group['issue_ids'],
                  'root_cause':group['root_cause'],'scope':proposal['scope'],
                  'upgrade_axis':proposal.get('upgrade_axis'),
                  'code_instructions':proposal['code_instructions'],
                  'video_controls':proposal['video_controls'],
                  'code_controls':proposal['code_controls'],
                  'required_controls':group['required_controls'],
                  'materials':[entry['asset']['id'] for entry in proposal['materials']]}
            units.append(unit);materials.extend(copy.deepcopy(proposal['materials']))
            instructions.append(f"{group['id']} ({', '.join(group['issue_ids'])}): "
                                f"{proposal['code_instructions']}")
            scopes.append(proposal['scope'])
            for source,target in ((group['required_controls'],controls),
                                  (proposal['video_controls'],video),(proposal['code_controls'],code)):
                for value in source:
                    if value not in target:target.append(value)
        scope=max(scopes,key=('single_subject','coupled_action','scene').index)
        upgrade_axes=sorted({unit['upgrade_axis'] for unit in units if unit['upgrade_axis']})
        return {'kind':kind,'scope':scope,'video_controls':video,'code_controls':code,
                'upgrade_axes':upgrade_axes,
                'code_instructions':'Implement all repair units in ONE game version:\n'+'\n'.join(instructions),
                'materials':materials,'repair_units':units}

    issue_ids=[issue_id for group in groups for issue_id in group['issue_ids']]
    skipped=[{'id':g['id'],'issue_ids':g['issue_ids'],'reason':g['skip_reason']}
             for g in repair_plan['groups'] if not g['candidates']]
    candidates=[option('same_level')]
    if escalated:candidates.append(option('higher_level'))
    bundled={'id':'round_bundle','issue_ids':issue_ids,'root_cause':'Multiple planned repair units',
             'grouping_reason':'One integrated game candidate and one matched replay per branch',
             'required_controls':list(dict.fromkeys(
                 control for group in groups for control in group['required_controls'])),
             'candidates':candidates,'source_groups':[g['id'] for g in groups],
             'escalated_groups':[g['id'] for g in escalated],
             'skipped_groups':skipped}
    return {**repair_plan,'groups':[bundled],'source_groups':repair_plan['groups']}


def run_round_bundle(**kwargs):
    """Use the existing isolated-candidate runner for a whole-round comparison."""
    kwargs['repair_plan']=bundle_plan(kwargs['repair_plan'])
    return run_groups(**kwargs)


def material_state(plan, config, option, prefix):
    updated=copy.deepcopy(plan); cfg=copy.deepcopy(config)
    assets={a['id']:a for a in updated['assets']}
    for entry in option['materials']:
        item=copy.deepcopy(entry['asset']); aid=item['id']
        if entry['operation']=='replace':
            assets[aid]={**assets[aid],**item}
            # An empty reference choice must not silently inherit a stale one.
            if not item.get('reference_asset_id'):assets[aid].pop('reference_asset_id',None)
        else:
            assets[aid]=item
            cfg['assets']['bindings'][aid]=[
                f'assets/{prefix}/{aid}/frame{i:04d}.png' for i in range(item['frames'])]
    updated['assets']=list(assets.values())
    return updated,cfg


def install(project, output, generator, assets, plan, config, protected):
    """Install validated generated files, including new bindings, in a private copy."""
    snapshot(project,output)
    if not assets:return
    manifest=json.loads((generator.root/'asset_manifest.json').read_text())['assets']
    planned={a['id']:a for a in plan['assets']}
    owned=[]
    for aid in assets:
        files=manifest[aid]['files']; targets=config['assets']['bindings'][aid]
        if len(files)!=len(targets):raise ValueError('Generated material frame count mismatch')
        validate_bound(generator.root,planned[aid],files)
        for src,dst in zip(files,targets):
            if dst in owned or any(dst==p or dst.startswith(p+'/') for p in protected):
                raise ValueError('Material destination is shared or protected')
            owned.append(dst)
            target=relative(output,dst);target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(relative(generator.root,src),target)
        validate_bound(output,planned[aid],targets)


def run_groups(*, repair_plan, project, plan, config, generator, directory, adapter, evaluation,
               episodes, history, services, code_editor, edit_policy, generator_factory,
               asset_python, asset_candidates, video_skill, replay_same,
               global_video_skill_store=None, replay_candidate=None):
    """At most two variants per group, never a Cartesian product across groups."""
    root=Path(directory);root.mkdir()
    current=Path(project); current_runs=episodes
    current_plan=plan; current_config=config; current_generator=generator
    reports=[]; accepted=False;accepted_assets=False;attempts=[]
    issue_map={x['id']:x for x in repair_plan['issues']}
    for ordinal,group in enumerate(repair_plan['groups'],1):
        gd=root/f'group-{ordinal:03}';gd.mkdir()
        baseline=current; before_hash=digest(baseline)
        baseline_runs=current_runs
        rows=[issue_map[i] for i in group['issue_ids']]
        # Judge receives observations, not branch labels/implementation hints.
        diagnosis={'id':group['id'],'problem':'\n'.join(x['problem'] for x in rows),
                   'issues':rows,'required_controls':group['required_controls'],
                   'rendered_acceptance':[v for x in rows for v in x['rendered_acceptance']]}
        report={'id':group['id'],'plan':group,'baseline':str(baseline),
                'baseline_sha256':before_hash,'candidates':[],'winner':'incumbent'}
        eligible=[]
        def execute_candidate(number,option):
            selected=None
            branch_diagnosis=copy.deepcopy(diagnosis)
            cd=gd/f'candidate-{number:03}';cd.mkdir()
            record={'kind':option['kind'],'scope':option['scope'],
                    'upgrade_axes':option.get('upgrade_axes',[]),'action':'pending'}
            write_json(cd/'plan.json',option)
            try:
                branch_plan,branch_config=material_state(current_plan,current_config,option,
                    f'repair_{root.parent.name}_{ordinal:03}_{number:03}')
                write_json(cd/'asset_plan.json',branch_plan)
                write_json(cd/'bindings.json',branch_config['assets']['bindings'])
                aids=[e['asset']['id'] for e in option['materials']]
                branch_generator=current_generator; generated=[]
                record['generations']=generated
                joint=bool(getattr(code_editor,'supports_material_tools',False))
                if aids and not joint:
                    branch_config['_reference_project']=str(baseline)
                    branch_generator=generator_factory(cd/'generation',branch_plan,branch_config,
                                                        asset_python,video_skill,1)
                    if video_skill and (current_generator.root/'video_skill').is_dir():
                        from gamecraft_video_skill import copy_skill
                        target=branch_generator.root/'video_skill'
                        if target.exists():shutil.rmtree(target)
                        copy_skill(current_generator.root/'video_skill',branch_generator.root)
                    for asset in option['materials']:
                        item=asset['asset']
                        result=branch_generator(item['id'],{'problem':diagnosis['problem'],
                                                'prompt':item['prompt']},cd/item['id'])
                        generated.append({'asset':item['id'],**result})
                        if result['status']!='generated':
                            raise RuntimeError('Material candidate unavailable: '+item['id']+' '+result.get('reason',result['status']))
                material_project=cd/'material_project'
                install(baseline,material_project,branch_generator,[] if joint else aids,branch_plan,branch_config,[])
                integration={**branch_diagnosis,'route':'both' if aids else 'composition','asset_ids':aids,
                    'code_instructions':option['code_instructions'],
                    'boundary_plan':{'scope':option['scope'],'video_controls':option['video_controls'],
                                     'code_controls':option['code_controls'],'required_controls':group['required_controls'],
                                     'repair_units':option.get('repair_units',[])},
                    'generated_materials':[{'asset':e['asset'],
                        'files':branch_config['assets']['bindings'][e['asset']['id']]} for e in option['materials']]}
                editor_out=cd/'code';editor_out.mkdir()
                candidate=cd/'project'
                extra={}
                if joint:
                    integration.pop('generated_materials',None)
                    integration['planned_materials']=option['materials']
                    extra['material_execution']={'plan':branch_plan,'config':branch_config,'option':option,
                        'python':asset_python,'video_skill':video_skill,
                        'skill_source':str(current_generator.root/'video_skill'),
                        'cache_source':str(root/'shared_materials')}
                edit=code_editor(material_project,candidate,editor_out,diagnosis=integration,
                                 evaluation=copy.deepcopy(evaluation),episodes=copy.deepcopy(baseline_runs),history=copy.deepcopy(history),
                                 adapter=adapter,policy=edit_policy,**extra)
                if joint:
                    from types import SimpleNamespace
                    generated=edit['generations'];branch_plan=edit['material_plan']
                    branch_generator=SimpleNamespace(root=Path(edit['generation_workspace']))
                record['edit']=edit;record['generations']=generated
                if digest(baseline)!=before_hash:raise RuntimeError('Candidate mutated the shared incumbent')
                if replay_candidate is not None:
                    runs=replay_candidate(candidate,baseline_runs,cd/'replay',
                        diagnosis=integration,changes=edit,config=branch_config)
                    record['trajectory']=str(cd/'replay/result.json')
                else:
                    runs=replay_same(candidate,baseline_runs,cd/'replay',preserve_state=False)
                record.update(action='replayed',project=str(candidate),replay=str(cd/'replay'))
                selected={'record':record,'project':candidate,'episodes':runs,'plan':branch_plan,
                    'config':branch_config,'generator':branch_generator,'generations':generated,
                    'cache_workspace':edit.get('material_cache_workspace')}
            except Exception as exc:
                record.update(action='error',reason=f'{type(exc).__name__}: {exc}')
            write_json(cd/'result.json',record)
            return record,selected
        # Each editor owns a private project, job and material cache. Both variants
        # read the same frozen incumbent; only the selected cache is published.
        options=group['candidates']
        report['candidate_parallelism']=min(2,len(options))
        with ThreadPoolExecutor(max_workers=report['candidate_parallelism'] or 1) as pool:
            futures=[pool.submit(execute_candidate,number,option)
                     for number,option in enumerate(options,1)]
            results=[future.result() for future in futures]
        for record,selected in results:
            report['candidates'].append(record);attempts.append(record)
            if selected is not None:eligible.append(selected)
        if digest(baseline)!=before_hash:
            raise RuntimeError('Incumbent changed during candidate execution')
        winner=None
        report['selection_reviews']=[]
        # A two-match tournament needs at most two Judge calls. An uncertain
        # result retains the current winner, including the original incumbent.
        for item in eligible:
            prior=winner
            incumbent_runs=prior['episodes'] if prior else baseline_runs
            comparison={'incumbent':prior['record']['kind'] if prior else 'incumbent',
                        'candidate':item['record']['kind']}
            try:
                reviews=services.selection(item['config'],adapter,diagnosis,
                    copy.deepcopy(incumbent_runs),item['episodes'],max_reviews=1)
                ok,reason=promotion(reviews)
                comparison['reviews']=reviews
                item['record'].update(action='eligible' if ok else 'reject',reason=reason,
                                      baseline_reviews=reviews)
                if ok:winner=item
            except Exception as exc:
                comparison['error']=f'{type(exc).__name__}: {exc}'
                item['record'].update(action='reject',reason='Candidate selection unavailable')
            report['selection_reviews'].append(comparison)
        for item in eligible:
            if item['record']['action']=='replayed':
                item['record'].update(action='reject',reason='Candidate selection unavailable')
        if winner:
            current=winner['project'];current_runs=winner['episodes']
            current_plan=winner['plan'];current_config=winner['config'];current_generator=winner['generator']
            if cache_workspace:=winner.get('cache_workspace'):
                shared=root/'shared_materials'
                if shared.exists():shutil.rmtree(shared)
                shutil.copytree(cache_workspace,shared)
            accepted=True;accepted_assets |= bool(winner['generations'])
            report['winner']=winner['record']['kind']
            winner['record']['action']='accept'
            if video_skill:
                from gamecraft_video_skill import commit_patch,submit_global_candidate
                for item in winner['generations']:
                    if item.get('video_skill_patch'):
                        evidence={'phase':config['_profile']['name'],'group':group['id'],
                                  'asset':item['asset'],'comparison':winner['record']['baseline_reviews']}
                        commit_patch(current_generator.root,item['video_skill_patch'],evidence=evidence)
                        if global_video_skill_store and config['_profile']['submit_global_video_skill']:
                            submit_global_candidate(global_video_skill_store,item['video_skill_patch'],
                                                    task_id=adapter.task_id,evidence=evidence)
            # Adopt the complete schema/bindings with the code, never just the project path.
            write_json(root.parent/'accepted_asset_plan.json',current_plan)
            write_json(root.parent/'accepted_bindings.json',current_config['assets']['bindings'])
        for number,record in enumerate(report['candidates'],1):
            if record['action']=='eligible':record['action']='not_selected'
            write_json(gd/f'candidate-{number:03}'/'result.json',record)
        write_json(gd/'result.json',report);reports.append(report)
    action='accept' if accepted else ('error' if attempts and all(r['action']=='error' for r in attempts)
                                     else 'reject' if attempts else 'skip')
    summary={'action':action,'reason':'Grouped candidate replay selection','groups':str(root)}
    write_json(root/'result.json',{'groups':reports,'project':str(current),'action':action})
    return {'project':current,'episodes':current_runs,'plan':current_plan,'config':current_config,
            'generator':current_generator,'groups':reports,
            'sides':{'composition':summary,
                     'asset':{**summary,'action':'accept' if accepted_assets else
                              ('reject' if any(r.get('generations') for r in attempts) else 'skip')}}}
