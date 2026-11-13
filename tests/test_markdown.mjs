import test from 'node:test';
import assert from 'node:assert/strict';
import {renderMarkdown} from '../app/ui/static/markdown.js';

test('report headings, emphasis, ordered and nested lists render as HTML', () => {
  const html=renderMarkdown('# Стратегия\n\n## Решение\n**Важно**: *пилот* и `метрика`.\n\n- Первый\n  - Вложенный\n  Основание\n- Второй\n\n3. Третий\n4. Четвёртый');
  assert.match(html, /<h1>Стратегия<\/h1>/);
  assert.match(html, /<strong>Важно<\/strong>: <em>пилот<\/em> и <code>метрика<\/code>/);
  assert.match(html, /<ul><li>Первый<ul><li>Вложенный<\/li><\/ul><br>Основание<\/li><li>Второй<\/li><\/ul>/);
  assert.match(html, /<ol start="3"><li>Третий<\/li><li>Четвёртый<\/li><\/ol>/);
});

test('comparison tables and code retain their meaning', () => {
  const html=renderMarkdown('| Вариант | Порог |\n| --- | --- |\n| Пилот | **10** |\n\n```html\n<script>alert(1)</script>\n```\n\n> Проверить основания');
  assert.match(html, /<th scope="col">Вариант<\/th>/);
  assert.match(html, /<td><strong>10<\/strong><\/td>/);
  assert.match(html, /<pre><code>&lt;script&gt;alert\(1\)&lt;\/script&gt;<\/code><\/pre>/);
  assert.match(html, /<blockquote><p>Проверить основания<\/p><\/blockquote>/);
});

test('untrusted HTML, unsafe URLs, credentials and image trackers cannot execute', () => {
  const html=renderMarkdown('<img src=x onerror=alert(1)>\n\n[x](javascript:alert) [x](data:text/html,test) [x](https://user:secret@example.org) ![tracker](https://evil.example/pixel)\n\n[Источник](https://example.org/?a=1&b=2)');
  assert.doesNotMatch(html, /<(script|img|iframe)\b|href="(?:javascript|data):|href="https:\/\/user/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /href="https:\/\/example.org\/\?a=1&amp;b=2" target="_blank" rel="noopener noreferrer"/);
});

test('plain text, empty input, escaped pipes and unfinished fences remain readable', () => {
  assert.equal(renderMarkdown(null), '');
  assert.equal(renderMarkdown('Обычный текст\nЕщё строка'), '<p>Обычный текст<br>Ещё строка</p>');
  assert.match(renderMarkdown('| A | B |\n| --- | --- |\n| x\\|y | z |'), /<td>x\|y<\/td><td>z<\/td>/);
  assert.equal(renderMarkdown('```\n**Не жирный**'), '<pre><code>**Не жирный**</code></pre>');
});

test('entity references and underscores inside words are preserved', () => {
  assert.equal(renderMarkdown('claim_demand_123 source_brief_456 hypothesis_only _курсив_'), '<p>claim_demand_123 source_brief_456 hypothesis_only <em>курсив</em></p>');
});
