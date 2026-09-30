"""Numeric separators from the tested session, with a host OS fallback."""
import ctypes
import json
import os
import subprocess
import sys
import time

from compatible_scenarios.shared.bsl.language import Failure, Structure
from compatible_scenarios.shared.bsl.dates import pattern, input_text, presentation, sample_format


def validate(value):
    if not isinstance(value, dict):
        raise Failure('invalid_number_separators', 'LatestSeparatorsInfo must contain Fractions and Groups.')
    if isinstance(value, Structure):
        fractions, groups = value.get(value.actual('Fractions')), value.get(value.actual('Groups'))
    else:
        fractions, groups = value.get('Fractions'), value.get('Groups')
    if (any(not isinstance(s, str) or len(s) > 1 or s.isdigit() or s in ('+', '-') for s in (fractions, groups))
            or fractions and (fractions == groups or fractions.isspace())):
        raise Failure('invalid_number_separators', 'Fractions and Groups must be distinct separator characters or empty strings.')
    return Structure(Fractions=fractions, Groups=groups)


def system_separators(timeout):
    if os.name == 'nt':
        from ctypes import wintypes
        get = ctypes.WinDLL('kernel32', use_last_error=True).GetLocaleInfoEx
        get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int]
        get.restype = ctypes.c_int
        def read(kind):
            size = get(None, kind, None, 0)  # User default, including custom overrides.
            if not size: raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_unicode_buffer(size)
            if not get(None, kind, buffer, size): raise ctypes.WinError(ctypes.get_last_error())
            return buffer.value
        return dict(Fractions=read(0x0000000e), Groups=read(0x0000000f))
    # setlocale is process-wide. Read LC_NUMERIC in a child without changing the server.
    code = ('import json,locale; locale.setlocale(locale.LC_NUMERIC, ""); '
            'v=locale.localeconv(); print(json.dumps(dict(Fractions=v["decimal_point"], Groups=v["thousands_sep"])))')
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True,
                            text=True, encoding='utf-8', timeout=timeout, check=True)
    return json.loads(result.stdout)


def get(host):
    explicit = host.globals.get('latestseparatorsinfo')
    if explicit is not None and (host.separators_explicit or explicit is not host.auto_separators
                                 or explicit != host.auto_separator_values):
        return validate(explicit)
    key = id(host.client)
    if key not in host.separators_cache:
        service = vars(host.client).get('_testpilot_service')
        if service is None and hasattr(host.R, '_search_objects'):
            import _code_execution as E
            with host.connections.bound():
                service, _ = host.operation('read_regional_settings', {}, lambda: E.discover(host.R, host.client))
        value = service.get('separators') if service else None
        if value is not None:
            value, source = validate(value), 'client'
        else:
            remaining = host.deadline - time.monotonic()
            if remaining <= 0: raise Failure('scenario_timeout', 'Scenario deadline expired.')
            try: value = validate(system_separators(min(remaining, 5)))
            except subprocess.TimeoutExpired as exc:
                raise Failure('scenario_timeout' if time.monotonic() >= host.deadline else 'regional_settings_unavailable',
                              'Reading host regional settings timed out.') from exc
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                raise Failure('regional_settings_unavailable', 'Cannot read host regional settings. Set LatestSeparatorsInfo explicitly.') from exc
            source = 'host'
        host.separators_cache[key] = (host.client, value, source)
    _, value, _ = host.separators_cache[key]
    value = validate(value)
    host.auto_separator_values = dict(value)
    host.globals['latestseparatorsinfo'] = host.auto_separators = value
    host.separators_explicit = False
    return value


def system_locale(timeout):
    if os.name == 'nt':
        from ctypes import wintypes
        get = ctypes.WinDLL('kernel32', use_last_error=True).GetLocaleInfoEx
        get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int]
        get.restype = ctypes.c_int
        buffer = ctypes.create_unicode_buffer(85)
        if not get(None, 0x5c, buffer, len(buffer)): raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value.replace('-', '_')
    code = 'import locale; print(locale.setlocale(locale.LC_TIME, ""))'
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True,
                            text=True, encoding='utf-8', timeout=timeout, check=True)
    return result.stdout.strip().split('.')[0].split('@')[0].replace('-', '_')


def scenario_language(host):
    language = host.globals.get('testerlanguage') or language_context(host)[0]
    if not isinstance(language, str):
        raise Failure('scenario_failed', 'TesterLanguage must be a language code.')
    return language


def system_date_sample(timeout):
    if os.name == 'nt':
        from ctypes import wintypes
        class SYSTEMTIME(ctypes.Structure):
            _fields_ = [(name, wintypes.WORD) for name in ('year','month','weekday','day','hour','minute','second','ms')]
        sample = SYSTEMTIME(2004,11,1,22,17,48,59,0)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        date = kernel.GetDateFormatEx
        date.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(SYSTEMTIME),wintypes.LPCWSTR,wintypes.LPWSTR,ctypes.c_int,wintypes.LPCWSTR]
        date.restype = ctypes.c_int
        clock = kernel.GetTimeFormatEx
        clock.argtypes = date.argtypes[:-1]
        clock.restype = ctypes.c_int
        d, t = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        if not date(None,1,ctypes.byref(sample),None,d,len(d),None) or not clock(None,0,ctypes.byref(sample),None,t,len(t)):
            raise OSError('Cannot read host regional date settings; set TesterDateFormat.')
        return d.value+' '+t.value
    code = ('import locale,time; locale.setlocale(locale.LC_TIME, ""); '
            'print(time.strftime("%x %X", (2004,11,22,17,48,59,0,327,-1)))')
    result = subprocess.run([sys.executable,'-I','-c',code],capture_output=True,
                            text=True,encoding='utf-8',timeout=timeout,check=True)
    return result.stdout.strip()


def language_context(host):
    key = id(host.client)
    if key not in host.locale_cache:
        service = vars(host.client).get('_testpilot_service')
        if service is None and hasattr(host.R, '_search_objects') and not host.connections.detached:
            import _code_execution as E
            with host.connections.bound():
                service, _ = host.operation('read_language_settings', {}, lambda: E.discover(host.R, host.client))
        language, locale = (service.get('language'), service.get('locale')) if service else (None, None)
        if not language or not locale:
            remaining = host.deadline-time.monotonic()
            if remaining <= 0: raise Failure('scenario_timeout', 'Scenario deadline expired.')
            try: fallback = system_locale(min(remaining, 5))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                code = 'scenario_timeout' if time.monotonic() >= host.deadline else 'regional_settings_unavailable'
                raise Failure(code, 'Cannot read host language settings; specify the language explicitly.') from exc
            if fallback in ('C', 'POSIX'): fallback = 'en_US'
            locale = locale or fallback
            language = language or fallback.split('_')[0]
        if not isinstance(language, str) or not isinstance(locale, str):
            raise Failure('helper_protocol_error', 'Invalid language settings in ServiceInfo.')
        host.locale_cache[key] = (host.client, language, locale)
    return host.locale_cache[key][1:]


def for_host(host, value, *, for_input=False):
    fmt = format_for(host)
    locale = 'ru'
    if any(p in ('MMM','MMMM') for p in pattern(fmt)):
        locale = language_context(host)[1]
    return (input_text if for_input else presentation)(value, fmt, locale)


def format_for(host):
    explicit = host.globals.get('testerdateformat')
    if explicit is not None and explicit != '':
        pattern(explicit)
        return explicit
    key = id(host.client)
    if key not in host.date_formats_cache:
        service = vars(host.client).get('_testpilot_service')
        if service is None and hasattr(host.R, '_search_objects') and not host.connections.detached:
            import _code_execution as E
            with host.connections.bound():
                service, _ = host.operation('read_date_settings', {}, lambda: E.discover(host.R,host.client))
        sample = service.get('date_sample') if service else None
        if sample is None:
            remaining = host.deadline-time.monotonic()
            if remaining <= 0:
                raise Failure('scenario_timeout','Scenario deadline expired.')
            try: sample = system_date_sample(min(remaining,5))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                code = 'scenario_timeout' if time.monotonic() >= host.deadline else 'regional_settings_unavailable'
                raise Failure(code, 'Cannot read host date settings; set TesterDateFormat explicitly.') from exc
        host.date_formats_cache[key] = (host.client, sample_format(sample))
    return host.date_formats_cache[key][1]
