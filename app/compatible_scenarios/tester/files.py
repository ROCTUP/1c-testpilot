"""Tester report exports and screenshot values using shared Testpilot operations."""
from compatible_scenarios.shared.bsl.binary import BinaryData
from decimal import Decimal
from pathlib import Path
import io
import re
import tempfile
import time
import zipfile

from compatible_scenarios.shared.bsl.language import Failure, boolean


def artifact(host, suffix):
    directory = getattr(host, '_artifact_directory', None)
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix='testpilot-scenario-'))
        host._artifact_directory = directory
    count = getattr(host, '_artifact_count', 0) + 1
    host._artifact_count = count
    return directory / f'{count}{suffix}'


def write_binary(host, value, filename):
    if not isinstance(filename, str) or not filename:
        raise Failure('scenario_failed', 'Write requires a nonempty filename.')
    return host.operation('write_screenshot', {'filename': filename},
                          lambda: Path(filename).write_bytes(value.data) and None)


def binary_method(host, obj, name, args):
    if name in ('write', 'записать') and len(args) == 1: return write_binary(host, obj, args[0])
    if name in ('size', 'размер') and not args: return Decimal(len(obj.data))
    raise Failure('unsupported_scenario', f'Unsupported binary-data method {name}.')


def screenshot(adapter, args, filename=False):
    pattern = args[0] if args else ''
    compressed = args[1] if len(args) > 1 else None
    if not isinstance(pattern, str): raise Failure('scenario_failed', 'Screenshot pattern must be a string.')
    if compressed is not None: boolean(compressed)
    if pattern:
        active = adapter.action('get_active_window')
        match = re.fullmatch(re.escape(pattern).replace(r'\*','.*').replace(r'\?','.'),
                             active.get('title') or '', re.IGNORECASE)
        if not match:
            raise Failure('screenshot_target_unavailable', 'The screenshot pattern must match the active 1C window.')
    shot = adapter.action('get_screenshot')
    if shot.metadata.get('capture_complete') is False:
        raise Failure('screenshot_incomplete', 'The screenshot does not include all windows.', **shot.metadata)
    data = shot.png
    if compressed:
        from PIL import Image
        output = io.BytesIO()
        with Image.open(io.BytesIO(data)) as image: image.save(output, format='PNG', optimize=True)
        data = output.getvalue()
    host = adapter.host
    total = getattr(host, '_screenshot_bytes', 0) + len(data)
    if total > 64 * 1024 * 1024: raise Failure('scenario_limit', 'Scenario screenshots exceeded 64 MiB.')
    host._screenshot_bytes = total
    value = BinaryData(data)
    if not filename: return value
    path = artifact(host, '.png')
    write_binary(host, value, str(path))
    return str(path)


def spreadsheet(adapter, args):
    obj = adapter.field(args[0], args[1] if len(args) > 1 else None)
    if obj.data.get('type') != 'SpreadsheetDocumentField':
        raise Failure('unsupported_element_type', 'GetSpreadsheetContent requires a spreadsheet document field.')
    path = artifact(adapter.host, '.xlsx')
    saved = adapter.action('write_content_to_file', obj, filename=str(path), file_format='xlsx')
    # An accepted save is not evidence that the file has finished writing.
    deadline = min(adapter.host.deadline, time.monotonic() + 15)
    def wait_file():
        while True:
            try:
                with zipfile.ZipFile(path) as archive:
                    if {'[Content_Types].xml', 'xl/workbook.xml'} <= set(archive.namelist()):
                        if archive.testzip() is None: return str(path)
            except (OSError, zipfile.BadZipFile, EOFError): pass
            if time.monotonic() >= deadline:
                raise Failure('export_file_unavailable', 'The XLSX file did not become readable within 15 seconds.', filename=str(path))
            adapter.host.pause(.1)
    result = adapter.host.operation('wait_export_file', {'filename': str(path)}, wait_file)
    if saved.get('dialog_answer_cleared') is False:
        # Do not erase the successful export or its cleanup diagnostics.
        adapter.host.messages.append('XLSX saved; file-dialog cleanup failed: ' + str(saved.get('cleanup_error')))
    return result
