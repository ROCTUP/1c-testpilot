"""Bounded search results owned by one test-client connection."""
from collections import OrderedDict
from copy import deepcopy
import json
import time
import uuid

TTL = 300
MAX_SEARCHES = 8
MAX_BYTES = 16 * 1024 * 1024


class Failure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate(limit, cursor, criteria):
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
        raise Failure('invalid_limit', 'limit must be an integer from 1 to 1000; omit it for all matches.')
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 64:
            raise Failure('invalid_cursor', 'Use next_cursor returned by find_objects.')
        if limit is not None or any(value is not None for value in criteria):
            raise Failure('invalid_cursor_arguments', 'Pass cursor without search filters or limit.')


class Store:
    def __init__(self):
        self.results = OrderedDict()

    def prune(self):
        now = time.monotonic()
        for key, value in list(self.results.items()):
            if now >= value['expires']:
                del self.results[key]

    def start(self, result, limit):
        self.prune()
        if len(result['objects']) <= limit:
            return self.page(result, limit, None, 0)
        size = len(json.dumps(result, ensure_ascii=False).encode('utf-8', 'surrogatepass'))
        if size > MAX_BYTES:
            raise Failure('search_result_too_large', 'The search exceeds the paging cache limit. Narrow the search.')
        while self.results and (len(self.results) >= MAX_SEARCHES or
                sum(v['size'] for v in self.results.values()) + size > MAX_BYTES):
            self.results.popitem(last=False)
        identity = uuid.uuid4().hex
        self.results[identity] = dict(result=deepcopy(result), limit=limit, size=size,
                                     expires=time.monotonic() + TTL)
        return self.page(result, limit, identity, 0)

    def resume(self, cursor):
        self.prune()
        identity, separator, offset = cursor.partition(':')
        saved = self.results.get(identity)
        if not separator or not offset.isascii() or not offset.isdecimal():
            raise Failure('invalid_cursor', 'Use next_cursor returned by find_objects.')
        if saved is None:
            raise Failure('cursor_unavailable', 'The cursor expired or belongs to another connection. Repeat the search.')
        offset = int(offset)
        if not 0 < offset < len(saved['result']['objects']) or offset % saved['limit']:
            raise Failure('invalid_cursor', 'Use next_cursor returned by find_objects.')
        return self.page(saved['result'], saved['limit'], identity, offset)

    @staticmethod
    def page(result, limit, identity, offset):
        total = len(result['objects'])
        objects = result['objects'][offset:offset + limit]
        end = offset + len(objects)
        return dict(deepcopy({k:v for k,v in result.items() if k != 'objects'}),
                    objects=deepcopy(objects), total=total, returned=len(objects), offset=offset,
                    has_more=end < total, next_cursor=f'{identity}:{end}' if end < total else None)


def for_client(client):
    store = getattr(client, '_find_pages', None)
    if not isinstance(store, Store):
        store = client._find_pages = Store()
    return store
