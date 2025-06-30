from __future__ import annotations

import http.cookiejar
import json
import platform
import re
import subprocess
import typing
import urllib.parse


from ..utils import (
    ExtractorError,
    Popen,
    int_or_none,
    shell_quote,
    unified_timestamp,
    version_tuple,
)
from ._helper import TempFileWrapper, random_string, override_navigator_js, extract_script_tags
from .common import ExternalJSI


class DenoJSI(ExternalJSI):
    """JS interpreter class using Deno binary"""
    _BASE_PREFERENCE = 5
    _EXE_NAME = 'deno'
    _DENO_FLAGS = ['--cached-only', '--no-prompt', '--no-check']
    _INIT_SCRIPT = 'localStorage.clear(); delete globalThis.Deno; global = window = globalThis;\n'

    def __init__(self, *args, flags=[], replace_flags=False, init_script=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._flags = flags if replace_flags else [*self._DENO_FLAGS, *flags]
        self._init_script = self._INIT_SCRIPT if init_script is None else init_script

    @property
    def _override_navigator_js(self):
        return override_navigator_js(self.user_agent)

    def _run_deno(self, cmd):
        self.write_debug(f'Deno command line: {shell_quote(cmd)}')
        try:
            stdout, stderr, returncode = Popen.run(
                cmd, timeout=self.timeout, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            raise ExtractorError('Unable to run Deno binary', cause=e)
        if returncode:
            raise ExtractorError(f'Failed with returncode {returncode}:\n{stderr}')
        elif stderr:
            self.report_warning(f'JS console error msg:\n{stderr.strip()}')
        return stdout.strip()

    def execute(self, jscode, video_id=None, note='Executing JS in Deno'):
        self.report_note(video_id, note)
        location_args = ['--location', self._url] if self._url else []
        with TempFileWrapper(f'{self._init_script};\n{self._override_navigator_js}\n{jscode}', suffix='.js') as js_file:
            cmd = [self.exe, 'run', *self._flags, *location_args, js_file.name]
            return self._run_deno(cmd)


class DenoJSDomJSI(DenoJSI):
    _BASE_PREFERENCE = 4
    _DENO_FLAGS = ['--cached-only', '--no-prompt', '--no-check']
    _JSDOM_VERSION = None
    _JSDOM_URL = 'https://esm.sh/v135/jsdom'  # force use esm v135, see esm-dev/esm.sh #1034

    @staticmethod
    def serialize_cookie(cookiejar: YoutubeDLCookieJar | None, url: str):
        """serialize netscape-compatible fields from cookiejar for tough-cookie loading"""
        # JSDOM use tough-cookie as its CookieJar https://github.com/jsdom/jsdom/blob/main/lib/api.js
        # tough-cookie use Cookie.fromJSON and Cookie.toJSON for cookie serialization
        # https://github.com/salesforce/tough-cookie/blob/master/lib/cookie/cookie.ts
        if not cookiejar:
            return json.dumps({'cookies': []})
        cookies: list[http.cookiejar.Cookie] = list(cookiejar.get_cookies_for_url(url))
        return json.dumps({'cookies': [{
            'key': cookie.name,
            'value': cookie.value,
            # leading dot of domain must be removed, otherwise will fail to match
            'domain': cookie.domain.lstrip('.') or urllib.parse.urlparse(url).hostname,
            'expires': int_or_none(cookie.expires, invscale=1000),
            'hostOnly': not cookie.domain_initial_dot,
            'secure': bool(cookie.secure),
            'path': cookie.path,
        } for cookie in cookies if cookie.value]})

    @staticmethod
    def apply_cookies(cookiejar: YoutubeDLCookieJar | None, cookies: list[dict]):
        """apply cookies from serialized tough-cookie"""
        # see serialize_cookie
        if not cookiejar:
            return
        for cookie_dict in cookies:
            if not all(cookie_dict.get(k) for k in ('key', 'value', 'domain')):
                continue
            if cookie_dict.get('hostOnly'):
                cookie_dict['domain'] = cookie_dict['domain'].lstrip('.')
            else:
                cookie_dict['domain'] = '.' + cookie_dict['domain'].lstrip('.')

            cookiejar.set_cookie(http.cookiejar.Cookie(
                0, cookie_dict['key'], cookie_dict['value'],
                None, False,
                cookie_dict['domain'], True, not cookie_dict.get('hostOnly'),
                cookie_dict.get('path', '/'), True,
                bool(cookie_dict.get('secure')),
                unified_timestamp(cookie_dict.get('expires')),
                False, None, None, {}))

    def _ensure_jsdom(self):
        if self._JSDOM_VERSION:
            return
        # `--allow-import` is unsupported in v1, and esm.sh:443 is default allowed remote host for v2
        result = self._run_deno([self.exe, 'info', self._JSDOM_URL])
        version_line = next((line for line in result.splitlines() if self._JSDOM_URL in line), '')
        if m := re.search(r'@([\d\.]+)', version_line):
            self._JSDOM_VERSION = m[1]

    def report_version(self):
        super().report_version()
        self._ensure_jsdom()
        self.write_debug(f'JSDOM lib version {self._JSDOM_VERSION}')

    def execute(self, jscode, video_id=None, note='Executing JS in Deno with jsdom', html='', cookiejar=None):
        self.report_note(video_id, note)
        self._ensure_jsdom()

        if cookiejar and not self._url:
            self.report_warning('No valid url scope provided, cookiejar is not applied')
            cookiejar = None

        html, inline_scripts = extract_script_tags(html)
        wrapper_scripts = '\n'.join(['try { %s } catch (e) {}' % script for script in inline_scripts])

        callback_varname = f'__callback_{random_string()}'
        script = f'''{self._init_script};
        import jsdom from "{self._JSDOM_URL}";
        let {callback_varname} = (() => {{
            const jar = jsdom.CookieJar.deserializeSync({json.dumps(self.serialize_cookie(cookiejar, self._url))});
            const dom = new jsdom.JSDOM({json.dumps(str(html))}, {{
                {'url: %s,' % json.dumps(str(self._url)) if self._url else ''}
                cookieJar: jar,
                pretendToBeVisual: true,
            }});
            Object.keys(dom.window).filter(key => !['atob', 'btoa', 'crypto', 'location'].includes(key))
            .filter(key => !(window.location? [] : ['sessionStorage', 'localStorage']).includes(key))
            .forEach((key) => {{
                try {{globalThis[key] = dom.window[key]}} catch (e) {{ console.error(e) }}
            }});
            {self._override_navigator_js};

            window.screen = {{
                availWidth: 1920,
                availHeight: 1040,
                width: 1920,
                height: 1080,
                colorDepth: 24,
                isExtended: true,
                onchange: null,
                orientation: {{angle: 0, type: 'landscape-primary', onchange: null}},
                pixelDepth: 24,
            }}
            Object.defineProperty(document.body, 'clientWidth', {{value: 1903}});
            Object.defineProperty(document.body, 'clientHeight', {{value: 2000}});
            document.domain = location?.hostname;

            delete window.jsdom;
            const origLog = console.log;
            console.log = () => {{}};
            console.info = () => {{}};
            return () => {{
                const stdout = [];
                console.log = (...msg) => stdout.push(msg.map(m => '' + m).join(' '));
                return () => {{ origLog(JSON.stringify({{
                    stdout: stdout.join('\\n'), cookies: jar.serializeSync().cookies}})); }}
            }}
        }})();
        {wrapper_scripts}
        {callback_varname} = {callback_varname}(); // begin to capture console.log
        try {{
            {jscode}
        }} finally {{
            {callback_varname}();
        }}
        '''

        # https://github.com/prebuild/node-gyp-build/blob/6822ec5/node-gyp-build.js#L196-L198
        # This jsdom dependency raises fatal error on linux unless read for this file is allowed
        additional_flags = ['--allow-read=/etc/alpine-release'] if platform.system() == 'Linux' else []

        location_args = ['--location', self._url] if self._url else []

        if version_tuple(self.exe_version) >= (2, 3, 0):
            self.report_warning('`--allow-env` flag is enabled for deno >= 2.3.0 to avoid import panic, '
                                'use `deno upgrade` to downgrade to a lower version to avoid this', only_once=True)
            additional_flags.append('--allow-env')

        with TempFileWrapper(script, suffix='.js') as js_file:
            cmd = [self.exe, 'run', *self._flags, *additional_flags, *location_args, js_file.name]
            result = self._run_deno(cmd)
            try:
                data = json.loads(result)
            except json.JSONDecodeError as e:
                raise ExtractorError(f'Failed to parse JSON output from Deno: {result}', cause=e)
        self.apply_cookies(cookiejar, data['cookies'])
        return data['stdout']


if typing.TYPE_CHECKING:
    from ..cookies import YoutubeDLCookieJar
