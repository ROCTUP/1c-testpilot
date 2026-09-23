"""Local screenshot orchestration. Platform capture runs in a bounded child process."""
import base64
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys

MAX_PNG_BYTES = 8 * 1024 * 1024
CAPTURE_TIMEOUT = 10


class CaptureError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass
class Screenshot:
    png: bytes
    metadata: dict


def validate_options(scale, grid, region):
    if type(scale) is not int or not 25 <= scale <= 100:
        raise CaptureError('invalid_screenshot_options', 'scale must be an integer from 25 to 100.')
    if type(grid) is not bool:
        raise CaptureError('invalid_screenshot_options', 'grid must be true or false.')
    if region is not None and (not isinstance(region, list) or len(region) != 4
            or any(type(v) is not int for v in region) or min(region[:2]) < 0 or min(region[2:]) <= 0):
        raise CaptureError('invalid_screenshot_options', 'region must be [x, y, width, height] in unscaled screenshot pixels.')


def _address(value):
    ip = ipaddress.ip_address(value.split('%')[0])
    return str(ip.ipv4_mapped or ip) if isinstance(ip, ipaddress.IPv6Address) else str(ip)


def resolve_process(client):
    """Match the live socket's reverse connection, never a title or just a listening port."""
    import psutil
    try:
        peer, local = client.s.getpeername(), client.s.getsockname()
        peer_ip, local_ip = _address(peer[0]), _address(local[0])
    except (AttributeError, OSError, ValueError) as exc:
        raise CaptureError('screenshot_client_unavailable', 'Reconnect to the test client before taking a screenshot.') from exc
    local_ips = {_address(a.address) for addresses in psutil.net_if_addrs().values()
                 for a in addresses if a.family in (2, 23, 10)}
    if not ipaddress.ip_address(peer_ip).is_loopback and peer_ip not in local_ips:
        raise CaptureError('screenshot_remote_client',
                           'Screenshots require the MCP server and the 1C client on the same computer.')
    try:
        def matches_socket(n):
            return (n.status == psutil.CONN_ESTABLISHED and n.laddr and n.raddr
                    and (_address(n.laddr.ip), n.laddr.port) == (peer_ip, peer[1])
                    and (_address(n.raddr.ip), n.raddr.port) == (local_ip, local[1]))
        if sys.platform == 'linux':
            # WebKit children inherit the client's socket. The system-wide listing
            # can attribute that shared inode to a child instead of the 1C process.
            matches = set()
            for p in psutil.process_iter(['name']):
                if p.info['name'] not in ('1cv8', '1cv8c') or not p.is_running():
                    continue
                try:
                    connections = p.net_connections(kind='tcp')
                    if any(matches_socket(n) for n in connections): matches.add(p.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            matches = {n.pid for n in psutil.net_connections(kind='tcp') if n.pid and matches_socket(n)}
        if len(matches) != 1:
            raise CaptureError('screenshot_process_unavailable', 'The local process for this connection could not be identified.')
        process = psutil.Process(matches.pop())
        names = ('1cv8', '1cv8c') if sys.platform == 'linux' else ('1cv8.exe', '1cv8c.exe')
        if process.name().lower() not in names:
            raise CaptureError('screenshot_process_unavailable', 'This connection could not be matched to a 1C client process.')
        return process.pid, process.create_time()
    except psutil.Error as exc:
        raise CaptureError('screenshot_process_unavailable', 'The operating system did not allow identifying the connected 1C process.') from exc


def capture(client, scale=100, grid=False, region=None):
    validate_options(scale, grid, region)
    import _code_execution
    try:
        _code_execution.check_screenshot(client)
    except _code_execution.Failure as exc:
        raise CaptureError(exc.result['code'], str(exc)) from exc
    if sys.platform not in ('win32', 'linux'):
        raise CaptureError('screenshot_platform_unsupported', 'Screenshot capture is available on Windows and Linux with X11/XWayland.')
    pid, created = resolve_process(client)
    request = dict(pid=pid, created=created, scale=scale, grid=grid, region=region)
    isolated = vars(client).get('_isolated_process')
    try:
        if isolated is not None:
            out = isolated.call('screenshot', request, timeout=CAPTURE_TIMEOUT)
        else:
            helper = '_screenshot_linux.py' if sys.platform == 'linux' else '_screenshot_windows.py'
            result = subprocess.run([sys.executable, str(Path(__file__).with_name(helper)),
                                     json.dumps(request)], stdin=subprocess.DEVNULL, capture_output=True, timeout=CAPTURE_TIMEOUT,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0)
            if result.returncode or len(result.stdout) > MAX_PNG_BYTES * 2:
                raise CaptureError('screenshot_failed', 'The screenshot could not be captured.')
            out = json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise CaptureError('screenshot_timeout', '1C did not provide a screenshot in time. The client was left unchanged.') from exc
    except OSError as exc:
        raise CaptureError('screenshot_failed', 'The screenshot helper could not be started.') from exc
    except ValueError as exc:
        if isinstance(exc, CaptureError): raise
        raise CaptureError('screenshot_failed', 'The screenshot result could not be read.') from exc
    try:
        if not out.get('ok'):
            raise CaptureError(out['code'], out['error'])
        png = base64.b64decode(out.pop('png'), validate=True)
        if not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) > MAX_PNG_BYTES:
            raise ValueError('invalid PNG')
    except (ValueError, KeyError) as exc:
        if isinstance(exc, CaptureError):
            raise
        raise CaptureError('screenshot_failed', 'The screenshot result could not be read.') from exc
    # Reject a reconnect/process replacement during capture.
    if resolve_process(client) != (pid, created):
        raise CaptureError('screenshot_client_changed', 'The connected client changed during capture. Take another screenshot.')
    return Screenshot(png, out)
