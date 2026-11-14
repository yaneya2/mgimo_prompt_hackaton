import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const source=readFileSync(new URL('../../app/ui/static/markdown.js',import.meta.url),'utf8');
const {renderDocument:renderMarkdown}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('renders report headings, lists, emphasis, tables and literal code',()=>{
 const result=renderMarkdown('# Отчёт\n\n## Решение\n**Важно**\n\n- Первый\n  пояснение\n- Второй\n\n1. Проверить\n2. Измерить\n\n| Метрика | Порог |\n| --- | --- |\n| Оплаты | 2 |\n\n```\n<b>literal</b>\n```');
 for(const fragment of ['<h1','<h2','<strong>Важно</strong>','<ul>','<ol>','<table>','<td>Оплаты</td>','&lt;b&gt;literal&lt;/b&gt;']) assert.ok(result.html.includes(fragment),fragment);
 assert.equal(result.headings[1].title,'Решение');
});
test('untrusted content cannot create executable markup or unsafe links',()=>{
 const {html}=renderMarkdown('<img src=x onerror=alert(1)>\n\n[x](javascript:alert) [safe](https://example.org) [credentials](https://user:pass@example.org)\n\n## <script>bad</script>');
 assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.ok(!html.includes('href="javascript:'));assert.ok(!html.includes('href="https://user:'));
 assert.ok(html.includes('href="https://example.org"'));assert.ok(html.includes('rel="noopener noreferrer"'));
});
test('section identifiers remain distinct across reports',()=>{
 assert.notEqual(renderMarkdown('## Same','first').headings[0].id,renderMarkdown('## Same','second').headings[0].id);
});
