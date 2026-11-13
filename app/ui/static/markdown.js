// Deliberately small, safe Markdown renderer. Raw HTML is always text.
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function inline(value) {
  const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let html = '', end = 0;
  for (const match of value.matchAll(tokens)) {
    html += escapeHtml(value.slice(end, match.index));
    const text = match[0];
    if (text.startsWith('`')) html += '<code>' + escapeHtml(text.slice(1,-1)) + '</code>';
    else if (text.startsWith('**')) html += '<strong>' + escapeHtml(text.slice(2,-2)) + '</strong>';
    else {
      const [,label,url] = text.match(/^\[([^\]]+)\]\((.*)\)$/);
      let safe = false;
      try {const parsed = new URL(url); safe = ['https:','http:'].includes(parsed.protocol) && !parsed.username && !parsed.password;} catch {}
      html += safe ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : escapeHtml(text);
    }
    end = match.index + text.length;
  }
  return html + escapeHtml(value.slice(end));
}
export function renderMarkdown(source, prefix='report') {
  const lines = String(source).replace(/\r\n?/g,'\n').split('\n');
  let html = '', paragraph = [], list = null, headingIndex = 0;
  const headings = [];
  const flush = () => {if(paragraph.length) html += '<p>'+paragraph.map(inline).join('<br>')+'</p>'; paragraph=[];};
  const closeList = () => {if(list) html += `</li></${list}>`; list=null;};
  const cells = line => line.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
  for(let i=0;i<lines.length;i++) {
    const line=lines[i];
    if (/^\s*```/.test(line)) {
      flush();closeList();const code=[];
      while(++i<lines.length&&!/^\s*```/.test(lines[i])) code.push(lines[i]);
      html+='<pre><code>'+escapeHtml(code.join('\n'))+'</code></pre>';continue;
    }
    if(line.includes('|') && i+1<lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i+1]) && cells(lines[i+1]).every(c=>/^:?-{3,}:?$/.test(c))) {
      flush();closeList();html+='<div class="table-wrap"><table><thead><tr>'+cells(line).map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>';i++;
      while(i+1<lines.length&&lines[i+1].includes('|')) html+='<tr>'+cells(lines[++i]).map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>';
      html+='</tbody></table></div>';continue;
    }
    const heading=line.match(/^(#{1,6})\s+(.+)$/);
    if(heading) {
      flush();closeList();const level=heading[1].length,id=`${prefix}-${headingIndex++}`;
      headings.push({id,title:heading[2],level});html+=`<h${level} id="${id}">${inline(heading[2])}</h${level}>`;continue;
    }
    const item=line.match(/^\s*(?:([-*])|\d+\.)\s+(.+)$/);
    if(item) {
      flush();const type=item[1]?'ul':'ol';
      if(list!==type){closeList();html+=`<${type}><li>`;list=type;}else html+='</li><li>';
      html+=inline(item[2]);continue;
    }
    if(!line.trim()){flush();closeList();continue;}
    if(list && /^\s{2,}\S/.test(line)){html+='<br>'+inline(line.trim());continue;}
    closeList();
    if(/^>\s?/.test(line)){flush();html+='<blockquote>'+inline(line.replace(/^>\s?/,''))+'</blockquote>';continue;}
    paragraph.push(line);
  }
  flush();closeList();return {html,headings};
}
