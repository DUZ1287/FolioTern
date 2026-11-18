"""Adapt editable source definitions to the existing, versioned item format."""
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser

from .collectors import CollectionError, _item, _now, _date, collect as legacy_collect, _request
from .safe_feed import fetch_feed
from .preferences import Preferences


def collect_source(client, config, source):
    kind, target = source['kind'], source['target']
    limit = source['limit']
    checked = _now()
    if kind in ('github_releases', 'github_search', 'arxiv_category'):
        key = {'github_releases': 'github_repos', 'github_search': 'discovery_queries', 'arxiv_category': 'arxiv_categories'}[kind]
        items, statuses = legacy_collect(client, {**config, 'sources': {key: [target]}, 'source_limit': limit})
        status = statuses[0]
    else:
        try:
            if kind == 'rss':
                raw = fetch_feed(target, config.get('_deadline'))
            else:
                raw = _request(client, 'https://export.arxiv.org/api/query', params={
                    'search_query': target, 'max_results': limit + 1,
                    'sortBy': 'submittedDate', 'sortOrder': 'descending'}, deadline=config.get('_deadline'), timeout=20).content
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise CollectionError('来源 XML 包含不支持的实体声明')
            feed = feedparser.parse(raw)
            if feed.bozo or not feed.get('version'):
                raise CollectionError('来源不是有效 RSS / Atom')
            items = []
            cutoff = datetime.now(timezone.utc) - timedelta(days=config.get('lookback_days', 7))
            for entry in feed.entries[:limit]:
                published = _date(entry.get('published') or entry.get('updated'))
                if published and datetime.fromisoformat(published) < cutoff:
                    continue
                link = entry.get('link', '')
                identity = entry.get('id') or link
                if not identity:
                    identity = hashlib.sha256((entry.get('title', '') + entry.get('summary', '')).encode()).hexdigest()
                name = 'rss:' + hashlib.sha256(target.encode()).hexdigest()[:20]
                version = entry.get('updated')
                if kind == 'arxiv_search':
                    match = re.fullmatch(r'https?://arxiv\.org/abs/([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?', str(identity))
                    if not match:
                        raise CollectionError('arXiv 返回错误条目或无效论文标识')
                    identity, version, name = match.group(1), match.group(2), 'arxiv'
                    link = 'https://arxiv.org/abs/' + identity
                items.append(_item(name, identity, entry.get('title', '未命名'), link, published,
                                   entry.get('summary', ''), 'research' if kind == 'arxiv_search' else 'engineering', version))
            capped = len(feed.entries) > limit
            status = {'source': source['id'], 'status': 'partial' if capped else 'ok', 'checked_at': checked,
                      'count': len(items), 'error': None, 'checkpoint': None if capped else checked,
                      'coverage_note': '近期订阅快照；不保证完整历史覆盖。' + (' 已达数量上限。' if capped else '')}
        except CollectionError as exc:
            items = []
            status = {'source': source['id'], 'status': 'error', 'checked_at': checked, 'count': 0,
                      'error': str(exc), 'checkpoint': None, 'coverage_note': ''}
    if source.get('keywords'):
        items = [x for x in items if any(k.casefold() in (x['title'] + ' ' + x['raw_text']).casefold() for k in source['keywords'])]
    return items, {**status, 'source_id': source['id'], 'name': source['name'], 'count': len(items)}


def collect(client, config, checkpoints=None):
    if '_managed_sources' not in config:
        return legacy_collect(client, config, checkpoints)
    items, statuses = [], []
    prefs = Preferences(config['_root'])
    for source in config['_managed_sources']:
        if not source['enabled']:
            continue
        try:
            if time.monotonic() >= config.get('_deadline', float('inf')):
                raise CollectionError('运行时间预算耗尽')
            found, status = collect_source(client, config, source)
        except Exception as exc:
            found, status = [], {'source': source['id'], 'status': 'error', 'count': 0, 'checked_at': _now(),
                                 'error': str(exc) if isinstance(exc, CollectionError) else '来源解析失败', 'coverage_note': '', 'checkpoint': None}
        prefs.set_option('source_result_' + source['id'], status)
        items.extend(found)
        statuses.append(status)
    unique = {(x['source'], x['external_id'], x['version_key']): x for x in items}
    return list(unique.values()), statuses
