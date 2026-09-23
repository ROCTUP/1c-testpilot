"""Pytest fixtures for direct 1C testing. Loaded by the testpilot pytest entry point."""
import hashlib
import json
import os
import logging
from pathlib import Path
import re
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


@pytest.fixture
def testpilot(request, testpilot_artifacts):
    """A separate client per test; profile decides launch versus external connection."""
    profile = request.config.getoption('--tc-profile')
    if not profile:
        raise pytest.UsageError('The testpilot fixture requires --tc-profile and TC1C_PROFILES_FILE.')
    from testpilot import Client
    import _runtime as R

    mode = request.config.getoption('--tc-screenshots')
    if mode != 'off' and (not R.SCREENSHOTS or not R.LOGGING or not R._call_logging.enabled('TC1C_LOG_SCREENSHOTS')):
        raise pytest.UsageError('Screenshot logging must be enabled to use --tc-screenshots.')
    client = Client(profile=profile)
    reports = getattr(request.config, '_testpilot_reports', None)
    allure_enabled = reports is not None and 'allure' in reports
    if R.LOGGING:
        client._runtime.logs = R._call_logging.Store(root=testpilot_artifacts,
            screenshots=mode != 'off', default_mode=mode, reports=reports,
            allure_factory=_log_reports.PytestAllure if allure_enabled else None,
            allure_directory=request.config.option.allure_report_dir if allure_enabled else None)
    cleanup_error = None
    diagnostics = None
    try:
        client.start()
        if R.LOGGING:
            client.start_logging(screenshot_mode=mode)
        yield client
    finally:
        report = getattr(request.node, '_testpilot_report_call', None)
        setup = getattr(request.node, '_testpilot_report_setup', None)
        if (report and report.failed) or (setup and setup.failed):
            try:
                # No table selection or editing during failure diagnostics.
                diagnostics = client.call('get_context', check=False)
            except Exception as exc:
                diagnostics = {'ok': False, 'error': str(exc)}
        try:
            client.close()
        except Exception as exc:
            cleanup_error = str(exc)
        summary = {'test': request.node.nodeid,
                   'outcome': report.outcome if report else (setup.outcome if setup else 'setup_failed'),
                   'cleanup_error': cleanup_error}
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
        if cleanup_error is not None:
            pytest.fail('Testpilot cleanup failed: ' + cleanup_error, pytrace=False)
