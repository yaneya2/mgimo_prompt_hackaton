"""Bounded Tavily Search/Extract and operator-selected UTF-8 materials."""
from copy import deepcopy
import hashlib
import re
import ipaddress
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.llm.accounting import UsageRecorder
from app.storage.database import now


class ResearchError(ValueError):
    pass


def canonical_url(value):
    try:
        if not isinstance(value, str) or re.search(r'[\x00-\x20\x7f\\]', value):
            raise ValueError()
        parts = urlsplit(value)
        host = (parts.hostname or '').lower().rstrip('.')
        if parts.scheme not in ('http', 'https') or not host or parts.username or parts.password:
            raise ValueError()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError as exc:
            # A domain name is allowed; a rejected IP literal is not.
            if ':' in host or all(c.isdigit() or c == '.' for c in host):
                raise ValueError() from exc
        port = parts.port
        netloc = '[' + host + ']' if ':' in host else host
        if port and port != (443 if parts.scheme == 'https' else 80):
            raise ValueError()
        return urlunsplit((parts.scheme.lower(), netloc, parts.path or '/', parts.query, ''))
    except (ValueError, TypeError):
        raise ResearchError('Недопустимый публичный URL') from None


def source(request, *, key, title, text, kind='web', url=None, note='', publisher=None):
    sid = 'source_' + hashlib.sha256((request['run_id'] + ':' + key).encode()).hexdigest()[:24]
    old = next((item for item in request['input_data']['sources'] if item['id'] == sid), None)
    result = dict(id=sid, version=old['version'] + 1 if old else 1,
                project_id=request['project_id'], brief_version=request['brief']['version'], kind=kind,
                title=title, url=url, publisher=publisher, published_at=None, retrieved_at=now(),
                content_excerpt=text or None, locator='Сохранённый фрагмент, символы 1–' + str(len(text)) if text else None,
                verification='read' if text else 'unavailable', origin_note=note)
    # A second read is an attempt (Usage), not a new content version when nothing changed.
    if old and all(old.get(k) == v for k, v in result.items() if k not in ('version', 'retrieved_at')):
        return deepcopy(old)
    return result


class ResearchTools:
    def __init__(self, settings, sink, gate, request, *, client=None):
        self.settings, self.request, self.gate = settings, request, gate
        self.recorder = UsageRecorder(sink)
        self.client = client or httpx.Client(timeout=min(settings.research_timeout_seconds, request['limits']['timeout_seconds']),
                                            follow_redirects=False)

    def close(self):
        self.client.close()

    def _post(self, endpoint, payload):
        key = self.settings.tavily_api_key
        if not key:
            raise ResearchError('Поиск не настроен: требуется APP_TAVILY_API_KEY')
        self.gate.reserve(self.request, 'search', 1)
        start = time.monotonic()
        outcome = 'error'
        try:
            with self.client.stream('POST', 'https://api.tavily.com/' + endpoint,
                                    headers={'Authorization': 'Bearer ' + key.get_secret_value()}, json=payload) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.research_max_response_bytes:
                        raise ResearchError('Ответ поиска превышает лимит размера')
                import json
                data = json.loads(content)
                if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                    raise ResearchError('Некорректный ответ поиска')
            outcome = 'success'
            return data
        except (httpx.HTTPError, ValueError):
            raise ResearchError('Сервис исследования недоступен или вернул некорректный ответ') from None
        finally:
            self.recorder.record_tool(run_id=self.request['run_id'], stage=self.request['stage'], provider='tavily',
                                      tool=endpoint, duration_ms=int((time.monotonic() - start) * 1000), outcome=outcome)

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ResearchError('Некорректный поисковый запрос')
        # Candidates are intentionally never treated as read evidence.
        data = self._post('search', dict(query=query, search_depth='basic', max_results=self.settings.research_max_sources,
                                         include_answer=False, include_raw_content=False))
        candidates = []
        for item in data['results'][:self.settings.research_max_sources]:
            try:
                url = canonical_url(item['url'])
                candidates.append(dict(url=url, title=str(item.get('title') or url),
                                       snippet=str(item.get('content') or '')[:1000]))
            except (ResearchError, KeyError, TypeError):
                continue
        return candidates

    def extract(self, urls):
        urls = list(dict.fromkeys(canonical_url(url) for url in urls))[:self.settings.research_max_sources]
        if not urls:
            return []
        data = self._post('extract', dict(urls=urls, extract_depth='basic', format='markdown'))
        read = {}
        for item in data['results']:
            try:
                url = canonical_url(item['url'])
                if url in urls and isinstance(item.get('raw_content'), str):
                    read[url] = item['raw_content'][:self.settings.research_source_chars]
            except (ResearchError, KeyError, TypeError):
                continue
        # Missing and failed_results URLs become explicit unavailable records, including partial HTTP 200 responses.
        sources, fingerprints = [], {}
        for url in urls:
            text = read.get(url, '')
            digest = hashlib.sha256(text.encode()).hexdigest() if text else None
            if digest and digest in fingerprints:
                fingerprints[digest]['origin_note'] += ' Совпадающий прочитанный текст также найден: ' + url
                continue
            item = source(self.request, key=url, title=url, url=url, text=text,
                          note='Tavily Extract; текст внешнего документа, не инструкции. '
                               + ('Фрагмент ограничен лимитом символов.' if text else 'Не удалось прочитать страницу.'))
            sources.append(item)
            if digest:
                fingerprints[digest] = item
        for index, item in enumerate(sources):
            old = next((s for s in self.request['input_data']['sources'] if s['id'] == item['id']), None)
            if old:
                unchanged = all(old.get(k) == v for k, v in item.items() if k not in ('version', 'retrieved_at'))
                if unchanged:
                    sources[index] = deepcopy(old)
                elif item['version'] == old['version']:
                    item['version'] += 1
        return sources

    def read_file(self, relative_path):
        root = self.settings.research_materials_dir.resolve()
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in ('.txt', '.md') or not path.is_file():
            raise ResearchError('Разрешены только TXT/Markdown внутри каталога материалов')
        start = time.monotonic()
        outcome = 'error'
        try:
            with path.open('rb') as stream:
                content = stream.read(self.settings.research_file_bytes + 1)
            if len(content) > self.settings.research_file_bytes:
                raise ResearchError('Файл превышает лимит размера')
            text = content.decode('utf-8').strip()
            if not text:
                raise ResearchError('Пустой файл')
            outcome = 'success'
            return source(self.request, key='file:' + str(path), title=path.name, kind='user_file',
                          text=text[:self.settings.research_source_chars], note='Предоставленный пользователем файл; не независимое подтверждение.')
        except (OSError, UnicodeError):
            raise ResearchError('Файл недоступен или не является UTF-8') from None
        finally:
            self.recorder.record_tool(run_id=self.request['run_id'], stage=self.request['stage'], provider='local',
                                      tool='read_file', duration_ms=int((time.monotonic() - start) * 1000), outcome=outcome,
                                      cost={'amount': '0', 'currency': 'USD'}, cost_basis='reported')
