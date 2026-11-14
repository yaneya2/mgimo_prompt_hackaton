/* Presentation decisions derived only from saved run data; no new model calls. */
export function shouldOpenReport(previous, next) {
  if (!next || next.run.status !== 'completed') return false;
  if (next.brief.version !== next.project.current_brief_version) return false;
  if (!next.artifacts.some(a => a.stage === 'report' && a.freshness === 'current')) return false;
  return previous?.run?.id !== next.run.id || previous?.run?.status !== 'completed';
}

export function reportSummary(snapshot) {
  const data = snapshot.data;
  const decision = [...data.decisions].reverse().find(d => d.author === 'user' && d.action === 'select');
  const option = data.options.find(o => o.id === decision?.option_id);
  if (!option) return null;
  const experiments = data.experiments.filter(x => x.option_id === option.id);
  const actions = [];
  const seen = new Set();
  for (const item of [
    ...experiments.map(x => ({text:x.method, origin:'План эксперимента', duration:x.duration})),
    ...option.priorities.map(text => ({text, origin:'Продуктовый приоритет'})),
    ...experiments.filter(x=>x.metric&&x.decision_threshold).map(x=>({text:'Оценить результат: '+x.metric+'. Порог решения: '+x.decision_threshold,origin:'Критерий эксперимента',basis:x.threshold_basis}))
  ]) {
    const text = item.text?.trim();
    if (!text || seen.has(text.toLocaleLowerCase('ru'))) continue;
    seen.add(text.toLocaleLowerCase('ru'));
    actions.push({...item, text});
    if (actions.length === 3) break;
  }
  return {name:option.name, value:option.value, why:option.rationale,
    choiceReason:decision.rationale, risk:option.objections[0] || null,
    reconsider:option.failure_condition, actions};
}

export function comparisonRows(snapshot) {
  const options = snapshot.data.options;
  const items = key => options.map(o => Array.isArray(o[key]) ? o[key] : [o[key]]);
  return [
    {label:'Аудитория', values:items('segment')},
    {label:'Ценность', values:items('value')},
    {label:'Ресурсы', values:items('resource_assumptions')},
    {label:'Риски', values:items('objections')},
    {label:'Первый эксперимент', values:options.map(o => {
      const experiment = snapshot.data.experiments.find(x => x.option_id === o.id);
      return experiment ? [experiment.method, 'Срок: ' + experiment.duration] : ['Эксперимент пока не задан'];
    })}
  ];
}
