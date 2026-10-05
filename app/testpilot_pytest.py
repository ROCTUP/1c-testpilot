"""Pytest fixtures for direct 1C testing. Loaded by the testpilot pytest entry point."""
import hashlib
import json
import os
import logging
from pathlib import Path
import re
import time
import uuid

import pytest
import _log_reports


def pytest_addoption(parser):
    group = parser.getgroup('testpilot', '1C Testpilot')
    group.addoption('--tc-profile', help='YAML profile from TC1C_PROFILES_FILE for the testpilot fixture.')
    group.addoption('--tc-artifacts', default='test-results', help='Directory for per-test Testpilot artifacts.')
    group.addoption('--tc-reports', help='html, allure, html,allure, or none (JSONL only); default TC1C_LOG_REPORTS or html.')
    group.addoption('--tc-screenshots', choices=('off', 'actions', 'all'), default='off',
                    help='Call-journal screenshots; default off. Requires server screenshot settings to allow capture.')
    group.addoption('--tc-client-scope', choices=('test', 'session'), default='test',
                    help='Client lifetime: test (default) or session. Session reuses application state; '
                         'journals and artifacts remain separate for each test.')


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    try:
        config._testpilot_reports = _log_reports.formats(config.getoption('--tc-reports')
                                                        or os.environ.get('TC1C_LOG_REPORTS', 'html'))
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None
    if 'allure' in config._testpilot_reports:
        if not hasattr(config.option, 'allure_report_dir'):
            raise pytest.UsageError('Allure reports require allure-pytest. Install 1c-testpilot[allure] '
                                    'and enable its pytest plugin (allure_pytest.plugin).')
        if not config.option.allure_report_dir:
            config.option.allure_report_dir = str(Path(config.getoption('--tc-artifacts')) / 'allure-results')


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, '_testpilot_report_' + report.when, report)


@pytest.fixture
def testpilot_artifacts(request):
    """Unique artifact directory for this test, including parameterized/parallel runs."""
    name = re.sub(r'[^\w.-]+', '_', request.node.name)[:70]
    digest = hashlib.sha256(request.node.nodeid.encode('utf8')).hexdigest()[:10]
    directory = Path(request.config.getoption('--tc-artifacts')).resolve() / f'{name}-{digest}-{uuid.uuid4().hex[:8]}'
    directory.mkdir(parents=True)
    request.node.user_properties.append(('testpilot_artifacts', str(directory)))
    return directory


class _ClientSession:
    """Own the client until close succeeds, including partially failed starts."""

    def __init__(self, profile):
        self.profile = profile
        self.client = None
        self.started = False
        self.recovery = None

    def prepare(self, logs):
        from testpilot import Client
        self.recovery = None
        if self.client is not None:
            reason = self._reuse_failure() if self.started else 'previous_setup_or_cleanup_failed'
            if reason:
                self.recovery = reason
                logging.getLogger(__name__).warning('Preparing a new Testpilot client: %s', reason)
                self.close()  # Do not replace an owner whose cleanup failed.
        if self.client is None:
            self.client = Client(profile=self.profile)
            self.client._runtime.logs = logs
            self.client.start()
            self.started = True
        else:
            self.client._runtime.logs = logs
        return self.client

    def _reuse_failure(self):
        from testpilot import ActionError
        import _connections
        client = self.client
        with client._access('pytest_prepare'):
            if client._closed:
                return 'client_closed'
            pool = client._runtime.pool
            with pool.lock:
                entry = next(iter(pool.entries.values()), None)
            if entry is None:
                return 'disconnected'
            with pool.use(entry, action='pytest_prepare'):
                proc = entry.state.get('launched_process')
                if proc is not None and proc.poll() is not None:
                    return 'process_exited'
                wire = entry.state.get('client')
                if not _connections.client_connected(wire):
                    return 'connection_closed'
                if getattr(wire, '_pending', 0):
                    # Read only outstanding replies; never send a probe or repeat an action.
                    previous = wire._io_deadline
                    deadline = time.monotonic() + wire.RESYNC_TIMEOUT
                    wire._io_deadline = min(previous, deadline) if previous is not None else deadline
                    try:
                        wire._resync()
                    except (OSError, RuntimeError) as exc:
                        raise ActionError('pytest_prepare', dict(ok=False, code='connection_not_ready',
                            error='The previous reply could not be recovered. The client was not restarted; '
                                  'the previous action may still be running.', details=str(exc))) from exc
                    finally:
                        wire._io_deadline = previous
        return None

    def close(self):
        if self.client is not None:
            self.started = False
            try:
                self.client.close()
            finally:
                _stop_test_logging(self.client)
            self.client = None


@pytest.fixture(scope='session')
def _testpilot_session(request):
    session = _ClientSession(request.config.getoption('--tc-profile'))
    try:
        yield session
    finally:
        session.close()


def _stop_test_logging(client):
    # Finalize even if the test disconnected or closed Client itself.
    with client._access('pytest_stop_logging'):
        with client._runtime.pool.lock:
            states = [entry.state for entry in client._runtime.pool.entries.values()]
        for state in [client._runtime.legacy, *states]:
            journal = state.get('_call_journal')
            if journal is not None:
                journal.stop()


@pytest.fixture
def testpilot(request, testpilot_artifacts):
    """Per-test artifacts and a test- or session-owned client selected by the profile."""
    profile = request.config.getoption('--tc-profile')
    if not profile:
        raise pytest.UsageError('The testpilot fixture requires --tc-profile and TC1C_PROFILES_FILE.')
    import _runtime as R

    mode = request.config.getoption('--tc-screenshots')
    if mode != 'off' and (not R.SCREENSHOTS or not R.LOGGING or not R._call_logging.enabled('TC1C_LOG_SCREENSHOTS')):
        raise pytest.UsageError('Screenshot logging must be enabled to use --tc-screenshots.')
    shared = request.config.getoption('--tc-client-scope') == 'session'
    session = request.getfixturevalue('_testpilot_session') if shared else _ClientSession(profile)
    reports = getattr(request.config, '_testpilot_reports', None)
    allure_enabled = reports is not None and 'allure' in reports
    logs = None
    if R.LOGGING:
        logs = R._call_logging.Store(root=testpilot_artifacts,
            screenshots=mode != 'off', default_mode=mode, reports=reports,
            allure_factory=_log_reports.PytestAllure if allure_enabled else None,
            allure_directory=request.config.option.allure_report_dir if allure_enabled else None)
    cleanup_error = None
    diagnostics = None
    client = None
    prepared = False
    setup_error = None
    try:
        client = session.prepare(logs)
        if R.LOGGING:
            client.start_logging(screenshot_mode=mode)
        prepared = True
        yield client
    except Exception as exc:
        if not prepared:
            setup_error = str(exc)
        raise
    finally:
        report = getattr(request.node, '_testpilot_report_call', None)
        setup = getattr(request.node, '_testpilot_report_setup', None)
        if prepared and ((report and report.failed) or (setup and setup.failed)):
            try:
                # No table selection or editing during failure diagnostics.
                diagnostics = client.call('get_context', result_mode='full', check=False)
            except Exception as exc:
                diagnostics = {'ok': False, 'error': str(exc)}
        try:
            if not shared or (not prepared and not session.started):
                session.close()
            elif session.client is not None and R.LOGGING:
                _stop_test_logging(session.client)
        except Exception as exc:
            cleanup_error = str(exc)
            session.started = False
        summary = {'test': request.node.nodeid,
                   'outcome': report.outcome if report else (setup.outcome if setup else 'setup_failed'),
                   'cleanup_error': cleanup_error}
        if setup_error is not None:
            summary['setup_error'] = setup_error
        if session.recovery is not None:
            summary['client_recovery'] = session.recovery
        (testpilot_artifacts/'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf8')
        if diagnostics is not None:
            (testpilot_artifacts/'context.json').write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding='utf8')
        if allure_enabled:
            try:
                import allure
                for name, data in [('Testpilot result', summary), ('Form context', diagnostics)]:
                    if data is not None:
                        allure.attach(json.dumps(R._call_logging.clean(R._profiles.redact(data)), ensure_ascii=False),
                                      name=name, attachment_type=allure.attachment_type.JSON)
            except Exception as exc:
                logging.getLogger(__name__).warning('Could not attach Testpilot diagnostics to Allure: %s', exc)
        if cleanup_error is not None and setup_error is None:
            pytest.fail('Testpilot cleanup failed: ' + cleanup_error, pytrace=False)
