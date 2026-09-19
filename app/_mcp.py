"""MCP publication and response encoding for the shared 1C runtime."""
import base64
import functools
import inspect
import json
import os
import typing
import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent
import _response
import _screenshots


def format_result(result, *, addrs=False):
    if isinstance(result, _screenshots.Screenshot):
        message = _response.respond(result.metadata)
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        return CallToolResult(content=[TextContent(type='text', text=message),
            ImageContent(type='image', mimeType='image/png', data=base64.b64encode(result.png).decode('ascii'))],
            structuredContent=result.metadata)
    return _response.respond(result, addrs=addrs)


def register(S):
    tv = S._ver_tuple(S._TARGET) if S._TARGET else None
    # сначала отбираем ВСЕ публикуемые действия: и enum, и указатели на общие операции должны
    # строиться от одного и того же набора, иначе описание сошлётся на исключённое действие
    published = {}
    for group, actions in S._ACTIONS.items():
        acts = {a: fn for a, fn in actions.items()
                if not (tv and S._ver_tuple(S.TOOL_MIN_VERSION.get('tc_' + a, '')) > tv)
                and (a != 'get_screenshot' or S.SCREENSHOTS)
                and (a not in S._call_logging.ACTIONS or S.LOGGING)}
        if acts:
            published[group] = acts        # группа без доступных действий не публикуется вовсе
    pub_group = {a: g for g, acts in published.items() for a in acts}

    for group, acts in published.items():
        # перечень допустимых действий уходит в СХЕМУ: клиент видит их до вызова, а недоступные
        # в целевой версии платформы в него не попадают
        params = [inspect.Parameter('action', inspect.Parameter.KEYWORD_ONLY,
                                    annotation=typing.Literal[tuple(sorted(acts))]),
                  inspect.Parameter('connection_id', inspect.Parameter.KEYWORD_ONLY,
                                    default=None, annotation=str)]
        seen = set()
        for fn in acts.values():
            for p in S._public_parameters(fn):
                if p.name not in seen:
                    seen.add(p.name)
                    params.append(inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY,
                                                    default=None, annotation=p.annotation))
        notes = S._PARAM_NOTE + (S._EFFECT_NOTE if set(acts) & S._VERIFY_ACTIONS else '')
        desc = '%s%s%s\nActions:\n%s' % (S._GROUP_DOC.get(group, group),
                                         S._common_hint(group, acts, pub_group), notes,
                                         '\n'.join(S._fmt_action(a, acts[a]) for a in sorted(acts)))
        desc += '\nconnection_id selects the client. '
        ref_params = [name for name in ('ref', 'root_ref') if name in seen]
        if S._response.REF_MODE == 'id' and ref_params:
            desc += ('Passing %s selects the client automatically; otherwise, with several clients, '
                     'connection_id is required. ' % '/'.join(ref_params))
            if set(acts) & S._batches.FIELD_ACTIONS.keys():
                desc += 'References in targets/entries also select the client; all must belong to one connection. '
        else:
            desc += 'With several clients, connection_id is required. '
        desc += 'Use tc_session(action="list_connections").'
        if S._response.COMPACT and any(('tc_' + a) in S._response.ADDR_TOOLS for a in acts):
            desc += S._response.HINT
        if S._response.REF_MODE == 'id' and (ref_params or
                any(a in S._refs.OBJECT_SLOTS or a == 'get_active_window' for a in acts)):
            desc += ('\nPass reference values returned by the tools unchanged in %s. ' % '/'.join(ref_params)
                     if ref_params else '\nReturned references can be passed unchanged to actions that accept them. ')
            desc += 'If a reference expires, find the element again.'

        async def dispatch(_acts=acts, _group=group, **kw):
            return await anyio.to_thread.run_sync(functools.partial(S._dispatch_action, _acts, _group, kw))

        dispatch.__signature__ = inspect.Signature(params)
        dispatch.__name__ = group
        dispatch.__doc__ = desc
        S.mcp.tool(name=group, description=desc)(dispatch)
        if S._response.REF_MODE == 'id':
            # Pydantic otherwise silently drops key/handle arguments in ID mode before dispatch,
            # potentially executing an action despite a contradictory key/handle supplied.
            tool = S.mcp._tool_manager._tools[group]
            model = tool.fn_metadata.arg_model
            model.model_config['extra'] = 'forbid'
            model.model_rebuild(force=True)
            tool.parameters = model.model_json_schema()


def install(S):
    S.mcp = FastMCP('1c-testpilot', host=os.environ.get('TC1C_HTTP_HOST', '127.0.0.1'),
                   port=int(os.environ.get('TC1C_HTTP_PORT', '6004')),
                   streamable_http_path=os.environ.get('TC1C_HTTP_PATH', '/mcp'))
    S._format_result = format_result
    S._register_groups = functools.partial(register, S)
    S._register_groups()
    def main():
        transport = os.environ.get('TC1C_TRANSPORT', 'stdio').strip().lower().replace('_', '-')
        try:
            S.mcp.run(transport='streamable-http' if transport in ('http', 'streamable-http') else
                      'sse' if transport == 'sse' else 'stdio')
        finally:
            try:
                for connection in list(S._pool.entries.values()):
                    with S._pool.use(connection):
                        journal = S._state.get('_call_journal')
                        if journal is not None:
                            journal.stop(reason='server_stopped')
            finally:
                S._pool.close()
    S.main = main
