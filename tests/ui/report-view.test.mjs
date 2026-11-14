import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const source=readFileSync(new URL('../../app/ui/static/report-view.js',import.meta.url),'utf8');
const {readableReport}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const snapshot=()=>({brief:{input:{budget:{amount:'30000',currency:'RUB'},horizon:'Шесть недель',constraints:[],assumptions:['Условная цена'],unknowns:['Спрос']}},artifacts:[{stage:'review',freshness:'current',summary:'Проверка выполнена'}],data:{decisions:[{author:'user',action:'select',option_id:'agent_option',rationale:'Выбор'}],options:[{id:'agent_option',name:'Офисы',resource_assumptions:[],priorities:[],channels:[],objections:[]}],messages:[],experiments:[],capabilities:[{id:'agent_cap',name:'Уборка',status:'available'}],claims:[],sources:[],issues:[]}});
test('report translates statuses, formats money and preserves review and uncertainty',()=>{
 const {html}=readableReport(snapshot());
 assert.match(html,/30000 ₽/);assert.match(html,/Доступно/);assert.match(html,/Проверка выполнена/);assert.match(html,/Условная цена/);assert.match(html,/Спрос/);
 assert.doesNotMatch(html,/agent_|available|"amount"/);
});
test('blocking issues remain visible and source text cannot execute HTML',()=>{
 const s=snapshot();s.data.issues=[{severity:'blocking',status:'open',explanation:'Не хватает ресурсов',suggested_fix:'Сократить нагрузку'}];s.data.options[0].name='<img src=x onerror=alert(1)>';
 const {html}=readableReport(s);assert.match(html,/Не хватает ресурсов/);assert.match(html,/Сократить нагрузку/);assert.doesNotMatch(html,/<img/);
});
test('no selection yields an explicit pending report',()=>{
 const s=snapshot();s.data.decisions=[];assert.match(readableReport(s).html,/ещё не выбрано/);
});
