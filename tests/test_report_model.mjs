import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../app/ui/static/report-model.js',import.meta.url),'utf8');
const {shouldOpenReport, reportSummary, comparisonRows}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

function snapshot(){return {run:{id:'run',status:'completed'},brief:{version:1},project:{current_brief_version:1},
 artifacts:[{stage:'report',freshness:'current'}],data:{decisions:[{author:'user',action:'select',option_id:'b',rationale:'Решение пользователя'}],
 options:[{id:'a',name:'A',segment:'Сегмент A',value:'Ценность A',resource_assumptions:['Ресурс A'],objections:['Риск A'],priorities:['Приоритет A'],rationale:'Основание A',failure_condition:'Условие A'},
 {id:'b',name:'Б',segment:'Сегмент Б',value:'Ценность Б',resource_assumptions:['Ресурс Б'],objections:['Риск Б'],priorities:['Интервью','Приоритет Б','Приоритет В'],rationale:'Основание Б',failure_condition:'Условие Б'}],
 experiments:[{option_id:'a',method:'Чужой эксперимент',duration:'Три дня'},{option_id:'b',method:'Интервью',duration:'Неделя'}]}}}

test('opening a completed current project or completing a run opens its report',()=>{
 const next=snapshot();assert.equal(shouldOpenReport(null,next),true);
 assert.equal(shouldOpenReport({run:{id:'run',status:'running'}},next),true);
 assert.equal(shouldOpenReport({run:{id:'other',status:'completed'}},next),true);
});
test('polling does not pull the user back from another tab; stale/incomplete reports do not open',()=>{
 const next=snapshot();assert.equal(shouldOpenReport(next,next),false);
 next.run.status='waiting_for_input';assert.equal(shouldOpenReport(null,next),false);
 next.run.status='completed';next.artifacts=[];assert.equal(shouldOpenReport(null,next),false);
 next.artifacts=[{stage:'report',freshness:'current'}];next.project.current_brief_version=2;assert.equal(shouldOpenReport(null,next),false);
});
test('summary follows the human choice and uses only its saved experiments and priorities',()=>{
 const result=reportSummary(snapshot());assert.equal(result.name,'Б');assert.equal(result.why,'Основание Б');
 assert.equal(result.choiceReason,'Решение пользователя');assert.equal(result.risk,'Риск Б');
 assert.deepEqual(result.actions.map(a=>a.text),['Интервью','Приоритет Б','Приоритет В']);
 assert.equal(result.actions[0].duration,'Неделя');
});
test('missing decisions and short plans are not filled with invented recommendations',()=>{
 const s=snapshot();s.data.decisions=[];assert.equal(reportSummary(s),null);
 s.data.decisions=[{author:'user',action:'select',option_id:'b'}];s.data.experiments=[];s.data.options[1].priorities=[];
 s.data.options[1].objections=[];assert.deepEqual(reportSummary(s).actions,[]);assert.equal(reportSummary(s).risk,null);
});
test('table aligns every criterion by option and shows absent experiments explicitly',()=>{
 const s=snapshot();s.data.experiments=s.data.experiments.slice(0,1);const rows=comparisonRows(s);
 assert.deepEqual(rows.map(r=>r.label),['Аудитория','Ценность','Ресурсы','Риски','Первый эксперимент']);
 assert.deepEqual(rows[0].values,[['Сегмент A'],['Сегмент Б']]);
 assert.deepEqual(rows[4].values,[['Чужой эксперимент','Срок: Три дня'],['Эксперимент пока не задан']]);
});

test('a short experiment plan can include its saved decision criterion as the third step',()=>{
 const s=snapshot();s.data.options[1].priorities=['Подготовить пилот'];
 s.data.experiments[1].metric='Заявки';s.data.experiments[1].decision_threshold='3 из 5';s.data.experiments[1].threshold_basis='Учебное допущение';
 const actions=reportSummary(s).actions;assert.equal(actions.length,3);
 assert.equal(actions[2].text,'Оценить результат: Заявки. Порог решения: 3 из 5');assert.equal(actions[2].basis,'Учебное допущение');
});
