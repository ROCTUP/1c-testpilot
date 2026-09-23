"""MCP publication and response encoding for the shared 1C runtime."""
import base64
import functools
import inspect
import json
import logging
import os
import typing
import weakref
import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.server.lowlevel import NotificationOptions
import _response
import _screenshots

CUSTOM_TOOLS = frozenset({'tc_list_custom_bsl_functions', 'tc_execute_custom_bsl_function'})
SERVICE_TOOLS = CUSTOM_TOOLS | {'tc_execute_code', 'tc_execute_query', 'tc_get_metadata'}


class TestpilotMCP(FastMCP):
    """Custom function tools appear only while a connected processing registers functions."""
    def __init__(self, runtime, **kwargs):
        self.runtime = runtime
        self._sessions = weakref.WeakSet()
        self._custom_visible = False
        super().__init__('1c-testpilot', **kwargs)
        create = self._mcp_server.create_initialization_options
        self._mcp_server.create_initialization_options = functools.partial(
            create, notification_options=NotificationOptions(tools_changed=True))

    def custom_visible(self):
        if not self.runtime._code_execution.SETTINGS.functions:
            return False
        pool = self.runtime._pool
        with pool.lock:
            clients = [entry.state.get('client') for entry in pool.entries.values()]
        return any(client is not None and vars(client).get('_testpilot_service', {}).get('functions')
                   for client in clients)

    def remember_session(self):
        try:
            self._sessions.add(self.get_context().session)
        except (ValueError, LookupError):
            pass  # Direct Python calls to FastMCP have no protocol session.

    async def list_tools(self):
        self.remember_session()
        visible = self.custom_visible()
        return [t for t in await super().list_tools() if t.name not in CUSTOM_TOOLS or visible]

    async def call_tool(self, name, arguments):
        self.remember_session()
        if name in CUSTOM_TOOLS and not self.custom_visible():
            raise ValueError('Custom BSL functions are not available in connected clients.')
        try:
            return await super().call_tool(name, arguments)
        finally:
            visible = self.custom_visible()
            if visible != self._custom_visible:
                self._custom_visible = visible
                for session in list(self._sessions):
                    try:
                        await session.send_tool_list_changed()
                    except Exception:
                        # Notification delivery must not replace an already completed action.
                        logging.getLogger(__name__).debug('Could not notify an MCP session of tool changes', exc_info=True)
                        self._sessions.discard(session)


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
    # Status and forced disconnection must remain reachable even when ordinary
    # workers are all waiting for 1C or for a connection lock.
    control_limiter = anyio.CapacityLimiter(4)
    tv = S._ver_tuple(S._TARGET) if S._TARGET else None
    # сначала отбираем ВСЕ публикуемые действия: и enum, и указатели на общие операции должны
    # строиться от одного и того же набора, иначе описание сошлётся на исключённое действие
    published = {}
    for group, actions in S._ACTIONS.items():
        if group == 'tc_execute_code' and not S._code_execution.SETTINGS.code:
            continue
        if group == 'tc_execute_query' and not S._code_execution.SETTINGS.query:
            continue
        if group == 'tc_get_metadata' and not S._code_execution.SETTINGS.metadata_enabled:
            continue
        if group in CUSTOM_TOOLS and not S._code_execution.SETTINGS.functions:
            continue
        acts = {a: fn for a, fn in actions.items()
                if not (tv and S._ver_tuple(S.TOOL_MIN_VERSION.get('tc_' + a, '')) > tv)
                and (a != 'get_screenshot' or S.SCREENSHOTS)
                and (a not in S._call_logging.ACTIONS or S.LOGGING)}
        if acts:
            published[group] = acts        # группа без доступных действий не публикуется вовсе
    pub_group = {a: g for g, acts in published.items() for a in acts}

    for group, acts in published.items():
        if group in SERVICE_TOOLS:
            action, handler = next(iter(acts.items()))
            params = [inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY,
                        default=p.default, annotation=p.annotation) for p in inspect.signature(handler).parameters.values()]
            params.append(inspect.Parameter('connection_id', inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str))
            async def execute_dispatch(_acts=acts, _group=group, _action=action, **kw):
                return await anyio.to_thread.run_sync(functools.partial(S._dispatch_action, _acts, _group,
                                                       dict(action=_action, **kw)))
            execute_dispatch.__signature__ = inspect.Signature(params)
            execute_dispatch.__name__ = group
            desc = inspect.getdoc(handler) + '\nconnection_id selects the client when several clients are connected.'
            S.mcp.tool(name=group, description=desc)(execute_dispatch)
            tool = S.mcp._tool_manager._tools[group]
            tool.fn_metadata.arg_model.model_config['extra'] = 'forbid'
            tool.fn_metadata.arg_model.model_rebuild(force=True)
            tool.parameters = tool.fn_metadata.arg_model.model_json_schema()
            continue
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
            control = _group == 'tc_session' and (kw.get('action') == 'list_connections' or
                      (kw.get('action') == 'disconnect' and kw.get('force') is True))
            return await anyio.to_thread.run_sync(functools.partial(S._dispatch_action, _acts, _group, kw),
                                                  limiter=control_limiter if control else None)

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
    S.mcp = TestpilotMCP(S, host=os.environ.get('TC1C_HTTP_HOST', '127.0.0.1'),
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
