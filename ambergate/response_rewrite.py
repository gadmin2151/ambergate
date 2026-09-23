"""Generate bounded, opt-in response rewrites; no application-specific rules."""
import re


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def directives(route, domain, targets):
    prefix = route['path']
    escaped = re.escape(prefix[1:])
    result = [
        'absolute_redirect off;',
        'proxy_set_header Accept-Encoding "";',
        f'proxy_redirect ~^(/(?!/|{escaped}(?:/|$|\\?)).*)$ {prefix}$1;',
        f'proxy_cookie_path ~^(/(?!{escaped}(?:/|$)).*)$ {prefix}$1;',
        'sub_filter_once off;',
    ]
    authorities = {re.escape(domain.lower()) + r'(?::[0-9]+)?'}
    authorities.update(re.escape(('['+t['address']+']') if ':' in t['address'] else t['address']) + ':' + str(t['port']) for t in targets)
    for authority in sorted(authorities):
        result.append(f'proxy_redirect ~^https?://{authority}(/(?!{escaped}(?:/|$|\\?)).*)$ {prefix}$1;')
    # Custom literal substitutions apply only to HTML, including embedded boot
    # data. Strings cannot inject directives or arbitrary nginx variables.
    pairs = [(r['search'], r['replace'].replace('{prefix}', prefix)) for r in route['response_rewrite']]
    for attr in ('href', 'src', 'action', 'formaction', 'poster'):
        for quote in ('"', "'"):
            start = attr + '=' + quote + '/'
            # Longer matches protect protocol-relative and already-prefixed URLs.
            for suffix in ('/', prefix[1:]+'/', prefix[1:]+quote, prefix[1:]+'?', prefix[1:]+'#'):
                pairs.append((start+suffix, start+suffix))
            pairs.append((start, attr+'='+quote+prefix+'/'))
    for search, replacement in pairs:
        result.append('sub_filter ' + quoted(search) + ' ' + quoted(replacement) + ';')
    return result
