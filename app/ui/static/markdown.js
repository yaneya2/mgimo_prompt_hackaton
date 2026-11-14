/* Deliberately small, safe Markdown renderer. Raw HTML and remote images stay text. */
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function safeUrl(value) {
  if (/[\u0000-\u0020\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? value : null;
  } catch { return null; }
}

function inline(text) {
  // Parse raw tokens, then escape every text/attribute independently. Never accept source HTML.
  const tokens = /(`+)([^`\n]+)\1|(!?)\[([^\]\n]*)\]\(([^\s)]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|(?<![\p{L}\p{N}_])_([^_\n]+)_(?![\p{L}\p{N}_])|<(https?:\/\/[^<>\s]+)>/gu;
  let html = '', cursor = 0;
  for (const match of text.matchAll(tokens)) {
    html += escape(text.slice(cursor, match.index));
    const [raw, ticks, code, image, label, target, strong, strongAlt, em, emAlt, autolink] = match;
    if (ticks) html += '<code>' + escape(code) + '</code>';
    else if (label !== undefined) {
      const url = safeUrl(target);
      // Images must not cause unsolicited requests to third-party servers.
      html += image ? escape(label) : url ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(label)}</a>` : escape(label);
    } else if (strong !== undefined || strongAlt !== undefined) html += '<strong>' + escape(strong ?? strongAlt) + '</strong>';
    else if (em !== undefined || emAlt !== undefined) html += '<em>' + escape(em ?? emAlt) + '</em>';
    else {
      const url = safeUrl(autolink);
      html += url ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(url)}</a>` : escape(raw);
    }
    cursor = match.index + raw.length;
  }
  return html + escape(text.slice(cursor));
}

function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
}
const listItem = line => /^(\s*)([-+*]|\d+[.)])\s+(.+)$/.exec(line);
const heading = line => /^ {0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*$/.exec(line);
const fence = line => /^ {0,3}(`{3,}|~{3,})/.exec(line);
const rule = line => /^ {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line);
const tableStart = (lines, i) => i + 1 < lines.length && lines[i].includes('|') && lines[i + 1].includes('|') && cells(lines[i + 1]).every(cell => /^:?-{3,}:?$/.test(cell));

export function renderMarkdown(value) {
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n');
  let i = 0, html = '';
  function list(indent) {
    const ordered = /^\d/.test(listItem(lines[i])[2]);
    const tag = ordered ? 'ol' : 'ul';
    const number = ordered ? parseInt(listItem(lines[i])[2], 10) : 1;
    let out = `<${tag}${ordered && number > 1 ? ` start="${Math.min(number, 2147483647)}"` : ''}>`;
    while (i < lines.length) {
      const item = listItem(lines[i]);
      if (!item || item[1].length !== indent || /^\d/.test(item[2]) !== ordered) break;
      out += '<li>' + inline(item[3]); i++;
      while (i < lines.length) {
        const child = listItem(lines[i]);
        if (child && child[1].length > indent) { out += list(child[1].length); continue; }
        if (!child && lines[i].trim() && /^\s+/.test(lines[i]) && lines[i].length - lines[i].trimStart().length > indent) {
          out += '<br>' + inline(lines[i].trim()); i++; continue;
        }
        break;
      }
      out += '</li>';
    }
    return out + `</${tag}>`;
  }
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const code = fence(line);
    if (code) {
      const marker = code[1], content = []; i++;
      while (i < lines.length && !new RegExp('^ {0,3}' + marker[0] + '{' + marker.length + ',}\\s*$').test(lines[i])) content.push(lines[i++]);
      if (i < lines.length) i++;
      html += '<pre><code>' + escape(content.join('\n')) + '</code></pre>'; continue;
    }
    const title = heading(line);
    if (title) { html += `<h${title[1].length}>` + inline(title[2]) + `</h${title[1].length}>`; i++; continue; }
    if (rule(line)) { html += '<hr>'; i++; continue; }
    if (tableStart(lines, i)) {
      const headers = cells(line); i += 2;
      html += '<div class="table-wrap"><table><thead><tr>' + headers.map(c => '<th scope="col">' + inline(c) + '</th>').join('') + '</tr></thead><tbody>';
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        const row = cells(lines[i++]);
        html += '<tr>' + headers.map((_, col) => '<td>' + inline(row[col] ?? '') + '</td>').join('') + '</tr>';
      }
      html += '</tbody></table></div>'; continue;
    }
    if (/^ {0,3}>/.test(line)) {
      const quote = [];
      while (i < lines.length && /^ {0,3}>/.test(lines[i])) quote.push(lines[i++].replace(/^ {0,3}> ?/, ''));
      html += '<blockquote>' + renderMarkdown(quote.join('\n')) + '</blockquote>'; continue;
    }
    const item = listItem(line);
    if (item) { html += list(item[1].length); continue; }
    const paragraph = [line]; i++;
    while (i < lines.length && lines[i].trim() && !heading(lines[i]) && !listItem(lines[i]) && !fence(lines[i]) && !rule(lines[i]) && !/^ {0,3}>/.test(lines[i]) && !tableStart(lines, i)) paragraph.push(lines[i++]);
    html += '<p>' + paragraph.map(inline).join('<br>') + '</p>';
  }
  return html;
}

// Add stable section targets without changing the standalone renderer contract.
export function renderDocument(value, prefix='report') {
  const headings=[];
  const html=renderMarkdown(value).replace(/<h([1-6])>(.*?)<\/h\1>/g, (whole, level, title) => {
    const id=`${prefix}-${headings.length}`;
    const plain=title.replace(/<[^>]*>/g,'').replace(/&(amp|lt|gt|quot|#39);/g, (_, entity)=>({'amp':'&','lt':'<','gt':'>','quot':'"','#39':"'"}[entity]));
    headings.push({id,level:Number(level),title:plain});
    return `<h${level} id="${escape(id)}">${title}</h${level}>`;
  });
  return {html,headings};
}
