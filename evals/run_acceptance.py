"""Measure the same frozen evidence in three modes; no fabricated human choice or scores."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
from app.config import Settings
from scripts.compare_agents import compare


def run(model,output):
    root=Path('evals/cases');manifest=json.loads((root/'manifest.json').read_text())
    for name,digest in manifest['sha256'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise ValueError('Holdout input changed')
    output.mkdir(parents=True,exist_ok=True);rows=[];blind=[];rng=random.Random(1771)
    for case in ['returns','sharpening','rental']:
        settings=Settings(model_name=model,data_dir=output/case/'limits',model_timeout_seconds=180,model_max_tokens=8192)
        snapshot=json.loads((root/(case+'.json')).read_text())
        results=compare(snapshot,settings,output/case)
        for item in results:
            result=json.loads(Path(item['file']).read_text())
            rows.append(dict(case=case,mode=item['mode'],model=model,status=item['status'],calls=item['calls'],
                             seconds=round(result['duration_seconds'],2),stage=result['stage'],comparison_complete=result['comparison_complete'],
                             code_defects=item['defects'] if result['comparison_complete'] else None,
                             found=item['found'],fixed=item['fixed'],residual=item['residual'],
                             unknown_cost_events=sum(v['cost'] is None for v in result['cost_events']),
                             result_file=str(Path(item['file']).relative_to(output))))
            label=f'pack_{rng.randrange(100000,999999)}';folder=output/'blind';folder.mkdir(exist_ok=True)
            (folder/(label+'.json')).write_text(json.dumps(dict(case=case,brief=snapshot['brief']['input'],
                sources=snapshot['data']['sources'],claims=snapshot['data']['claims'],
                options=result['data']['options'],messages=result['data']['messages'],experiments=result['data']['experiments']),ensure_ascii=False,indent=2)+'\n')
            blind.append(dict(label=label,case=case,mode=item['mode']))
        (output/'results.json').write_text(json.dumps(dict(rows=rows,independent_human_scoring=False,sample_repeats=1),ensure_ascii=False,indent=2)+'\n')
    with (output/'results.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (output/'blind-key.json').write_text(json.dumps(blind,indent=2)+'\n')
    with (output/'manual-rubric.csv').open('w') as f:
        fields=['label','unsupported_claims','impossible_promises','alternatives_0_2','experiments_0_2','decision_clarity_0_2','reviewer','notes']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(dict(label=b['label']) for b in blind)
    print(json.dumps(rows,ensure_ascii=False,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model',required=True);p.add_argument('--output',type=Path,default=Path('data/acceptance'))
    p.add_argument('--live',action='store_true',help='Explicitly consume model account limits')
    args=p.parse_args()
    if not args.live:raise SystemExit('Для реальных вызовов требуется --live; файлы условий не меняются.')
    run(args.model,args.output)


if __name__=='__main__':main()
