// Presentation of saved structured data. Original evidence is never rewritten.
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const words={RUB:'₽',available:'Доступно',planned:'Запланировано',unavailable:'Недоступно',allowed:'Допустимое обещание',blocked:'Заблокировано',hypothesis_only:'Гипотеза для проверки',user_provided:'Со слов пользователя',external_fact:'Внешнее утверждение',hypothesis:'Гипотеза',forecast:'Прогноз',supported:'Поддержано источником',unverified:'Не проверено',contradicted:'Есть противоречия',supports:'Подтверждает',contradicts:'Противоречит',context:'Контекст',read:'Прочитан',resolved:'Исправлено',open:'Открыто',accepted_limitation:'Принятое ограничение',capabilities:'возможности продукта',claims:'утверждения',claim:'утверждение',issues:'замечания',constraints:'ограничения',baseline:'исходный показатель'};
export function reportText(value,data){
 const names=new Map();
 for(const [key,label] of [['options','Направление'],['capabilities','Возможность'],['claims','Утверждение'],['sources','Источник'],['messages','Сообщение'],['experiments','Эксперимент'],['issues','Замечание']])
  (data[key]||[]).forEach((x,i)=>names.set(x.id,x.name||x.title||`${label} ${i+1}`));
 return String(value??'').replace(/brief\.input\.product/g,'описании продукта').replace(/brief\.input\./g,'брифе: ').replace(/\b[A-Za-z][A-Za-z0-9_]*\b/g,t=>names.get(t)||words[t]||(/^agent_|^source_/.test(t)?'связанный материал':t));
}
export function readableReport(snapshot){
 const d=snapshot.data,b=snapshot.brief.input;
 const text=v=>esc(reportText(v,d));
 const para=(label,v)=>v?`<p><strong>${label}:</strong> ${text(v)}</p>`:'';
 const list=xs=>`<ul>${(xs||[]).map(v=>`<li>${text(v)}</li>`).join('')}</ul>`;
 const decision=[...(d.decisions||[])].reverse().find(x=>x.author==='user'&&x.action==='select');
 const o=d.options.find(x=>x.id===decision?.option_id);
 if(!o)return {html:'<h2>Направление ещё не выбрано</h2><p>Сравните варианты и сохраните выбор, чтобы получить итоговый план.</p>',headings:[]};
 const sections=[];
 const add=(title,html)=>sections.push({id:`readable-report-${sections.length}`,title,html});
 add('Решение',`<h3>${text(o.name)}</h3>${para('Почему выбрано',decision.rationale)}${para('Для кого',o.segment)}${para('Проблема',o.problem)}${para('Предлагаемая ценность',o.value)}`);
 add('Бюджет и ресурсы',para('Исходный бюджет',b.budget?`${b.budget.amount} ${b.budget.currency}`:'Не указан')+para('Горизонт',b.horizon)+list(o.resource_assumptions));
 add('План действий',list(o.priorities)+para('Каналы',(o.channels||[]).join('; ')));
 add('Сообщения клиентам',(d.messages||[]).filter(m=>m.option_id===o.id).map(m=>`<h3>${text(m.channel)}</h3>${para('Статус',words[m.status]||m.status)}<p>${text(m.text)}</p>`).join(''));
 add('Как проверить результат',(d.experiments||[]).filter(x=>x.option_id===o.id).map(x=>`<h3>${text(x.hypothesis)}</h3>${para('Порядок проверки',x.method)}${para('Метрики',x.metric)}${para('Исходный показатель',x.baseline||'Требуется измерить')}${para('Критерий продолжения',x.decision_threshold)}${para('Почему выбран этот порог',x.threshold_basis)}${para('Срок',x.duration)}${para('Ответственный',x.owner||'Не назначен')}${para('Когда остановиться',x.stop_condition)}`).join(''));
 add('Риски и ограничения',list(o.objections)+para('Когда пересмотреть решение',o.failure_condition)+list(b.constraints)+para('Допущения',(b.assumptions||[]).join('; '))+para('Неизвестно',(b.unknowns||[]).join('; ')));
 add('Другие варианты',d.options.filter(x=>x.id!==o.id).map(x=>`<h3>${text(x.name)}</h3>${para('Сегмент',x.segment)}${para('Основания',x.rationale)}`).join(''));
 add('Возможности продукта',list((d.capabilities||[]).map(c=>`${c.name} — ${words[c.status]||c.status}`)));
 add('Основания и источники',(d.claims||[]).map((c,i)=>`<h3>Утверждение ${i+1}</h3><p>${text(c.text)}</p><p class="small muted">${text(words[c.kind]||c.kind)} · ${text(words[c.verification]||c.verification)}</p>${list(c.limitations)}<p class="small">Источники: ${text([...new Set((c.evidence||[]).map(v=>d.sources.find(s=>s.id===v.source_id)?.title||'Источник не найден'))].join('; ')||'Не приведены')}</p>`).join('')+`<h3>Использованные материалы</h3>`+list(d.sources.map(s=>`${s.title} — ${s.kind==='web'?'веб-страница':'предоставленный материал'}; ${words[s.verification]||s.verification}`)));
 const reviews=snapshot.artifacts.filter(a=>a.stage==='review'&&a.freshness==='current');
 add('Результат проверки',(d.issues||[]).map(i=>`<article><h3>${text(i.status==='resolved'?'Исправленное замечание':i.severity==='blocking'?'Требуется исправить':'Ограничение результата')}</h3><p>${text(i.explanation)}</p>${para('Что исправить',i.suggested_fix)}</article>`).join('')+reviews.map(a=>`<p>${text(a.summary)}</p>`).join('')||'<p>Актуальное заключение пока не сохранено.</p>');
 return {html:sections.map(s=>`<section><h2 id="${s.id}">${s.title}</h2>${s.html}</section>`).join(''),headings:sections.map(s=>({id:s.id,title:s.title,level:2}))};
}
