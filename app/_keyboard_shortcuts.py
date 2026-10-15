"""Offline, paged reference of standard managed-application shortcuts."""
from _keyboard import SUPPORTED_KEYS
from _keyboard_shortcuts_data import SHORTCUTS


def catalogue(search=None, category=None, limit=20, offset=0):
    categories = sorted({row[0] for row in SHORTCUTS})
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        return dict(ok=False, code='invalid_arguments', error='Use limit=1..100 and offset>=0.')
    if category is not None and category not in categories:
        return dict(ok=False, code='invalid_arguments', error='Unknown shortcut category.', categories=categories)
    if search is not None and not isinstance(search, str):
        return dict(ok=False, code='invalid_arguments', error='search must be text.')
    words = (search or '').casefold().split()
    matches = []
    for group, context, description, alternatives, russian, source, since in SHORTCUTS:
        if category is not None and group != category:
            continue
        text = ' '.join([group, context, description, russian,
                         ' '.join('+'.join(keys) for keys in alternatives)]).casefold()
        if all(word in text for word in words):
            matches.append(dict(category=group, context=context or group,
                                description=description, alternatives=[list(keys) for keys in alternatives],
                                **({'since': since} if since else {})))
    end = min(offset + limit, len(matches))
    return dict(ok=True, shortcuts=matches[offset:end], total=len(matches), offset=offset,
                next_offset=end if end < len(matches) else None, categories=categories,
                key_names=list(SUPPORTED_KEYS), modifiers=['CTRL', 'SHIFT', 'ALT'],
                message='Each alternatives entry is one keys chord for send_keys. Context and focus matter; '
                        'availability depends on the platform version, configuration and OS.')
