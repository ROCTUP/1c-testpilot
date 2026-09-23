"""Report adapters for the shared call journal; no calls to the test client."""
from datetime import datetime
import json
import uuid


def formats(value):
    """Normalize environment/CLI lists and the start_logging reports argument."""
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise ValueError('reports must contain html, allure, or none')
    values = list(dict.fromkeys(v.strip().lower() for v in value))
    if values == ['none'] or not values:
        return ()
    if any(v not in ('html', 'allure') for v in values):
        raise ValueError('reports must contain html, allure, or none alone')
    return tuple(values)


def identity(event):
    return event['wait_id'] if event['event'].startswith('wait_') else event['call_id']


def title(event):
    if event.get('tool') == 'scenario':
        return f"{event['action']} — {event.get('arguments', {}).get('target') or '<unknown>'}"
    return event.get('description') or f"{event['tool']} / {event['action']}"


def status(event):
    result = event.get('result')
    if not isinstance(result, dict):
        return 'passed'
    if result.get('skipped'):
        return 'skipped'
    # A probe returning false is not a failed wait. Its answer remains attached.
    if 'wait_id' in event and event['event'] == 'finish':
        return 'passed'
    if result.get('exception') or result.get('exception_type'):
        return 'broken'
    return 'failed' if result.get('ok') is False else 'passed'


def detail(event):
    result = event.get('result') or {}
    return str(result.get('error') or result.get('message') or result.get('code') or 'Action failed')


def timestamp(value):
    return round(datetime.fromisoformat(value).timestamp() * 1000)


class SessionAllure:
    """Write standard Allure results locally, without an Allure Python dependency."""
    def __init__(self, journal):
        self.journal = journal
        self.directory = journal.directory / 'allure-results'
        self.directory.mkdir()
        self.steps, self.nodes = [], {}
        self.finished = False

    def attachment(self, node, name, data, extension, mime):
        source = f'{uuid.uuid4()}-attachment.{extension}'
        if self.journal.store.append(self.journal, 'allure-results/' + source, data):
            node.setdefault('attachments', []).append(dict(name=name, source=source, type=mime))
        else:
            raise OSError('Allure attachment was not saved: ' + str(self.journal.reason))

    def begin(self, event):
        node = dict(name=title(event), start=timestamp(event['time']), steps=[])
        parent_id = event.get('wait_id') if event['event'] == 'start' else None
        parent = self.nodes.get(event.get('parent_call_id')) or self.nodes.get(parent_id)
        (parent['steps'] if parent else self.steps).append(node)
        self.nodes[identity(event)] = node

    def finish(self, event):
        node = self.nodes[identity(event)]
        node.update(stop=timestamp(event['time']), status=status(event), stage='finished')
        if node['status'] in ('failed', 'broken'):
            node['statusDetails'] = dict(message=detail(event))
        self.attachment(node, 'Call details', json.dumps(event, ensure_ascii=False).encode('utf8'),
                        'json', 'application/json')
        shot = event.get('screenshot', {}).get('path')
        if shot:
            self.attachment(node, 'Screenshot', (self.journal.directory / shot).read_bytes(), 'png', 'image/png')

    def stop(self, reason):
        if self.finished:
            return
        self.finished = True
        journal = self.journal
        stop = timestamp(datetime.now().astimezone().isoformat())
        for node in self.nodes.values():
            if 'stop' not in node:
                node.update(stop=stop, status='broken', stage='interrupted',
                            statusDetails=dict(message='Call recording was interrupted.'))
        failed = any(node.get('status') in ('failed', 'broken') for node in self.steps)
        incomplete = bool(journal.reason or journal.report_errors)
        uid = str(uuid.uuid4())
        # A session is an operational log, not a verdict about the user's task.
        result = dict(uuid=uid, name='Testpilot session: ' + journal.directory.name,
                      fullName='testpilot.session.' + journal.directory.name,
                      description='Recorded client actions. Status describes call errors and recording completeness.',
                      labels=[dict(name='suite', value='Testpilot sessions'),
                              dict(name='framework', value='1C Testpilot')],
                      start=timestamp(journal.started_at), stop=stop,
                      status='broken' if failed or incomplete else 'passed', stage='finished',
                      statusDetails=dict(message=('Recording incomplete: ' + str(journal.reason or 'report_error') if incomplete else
                                                  'Session finished with action errors.' if failed else
                                                  'Session recording completed.')),
                      steps=self.steps)
        try:
            if not self.journal.store.append(journal, f'allure-results/{uid}-result.json',
                                            json.dumps(result, ensure_ascii=False).encode('utf8')):
                raise OSError('Allure result was not saved: ' + str(journal.reason))
        finally:
            self.nodes.clear()
            self.steps.clear()


class PytestAllure:
    """Use the standard plugin's active test/fixture and public step/attachment API."""
    def __init__(self, journal):
        import allure
        self.allure, self.journal = allure, journal
        self.contexts = {}

    def begin(self, event):
        context = self.allure.step(title(event))
        context.__enter__()
        self.contexts[identity(event)] = context

    def finish(self, event):
        context = self.contexts.pop(identity(event), None)
        if context is None:
            return
        error = None
        state = status(event)
        if state == 'failed':
            error = AssertionError(detail(event))
        elif state == 'broken':
            error = RuntimeError(detail(event))
        elif state == 'skipped':
            import pytest
            error = pytest.skip.Exception('Unsupported scenario step')
        try:
            self.allure.attach(json.dumps(event, ensure_ascii=False), name='Call details',
                               attachment_type=self.allure.attachment_type.JSON)
            shot = event.get('screenshot', {}).get('path')
            if shot:
                self.allure.attach.file(str(self.journal.directory / shot), name='Screenshot',
                                        attachment_type=self.allure.attachment_type.PNG)
        finally:
            context.__exit__(type(error) if error else None, error, None)

    def stop(self, reason):
        for context in reversed(list(self.contexts.values())):
            error = RuntimeError('Call recording was interrupted: ' + reason)
            context.__exit__(type(error), error, None)
        self.contexts.clear()
