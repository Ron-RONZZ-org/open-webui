import asyncio
import logging
import uuid
from typing import Optional

import aiohttp
import websockets
from open_webui.env import AIOHTTP_CLIENT_ALLOW_REDIRECTS
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.kernel_pool import KernelPool
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Kernels are reused per conversation so that state survives between calls. The
# map is bounded (LRU) and the Jupyter server culls idle kernels independently;
# every reuse is verified, so a culled kernel is recreated rather than failing.
_KERNEL_POOL = KernelPool()
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _session_lock(key: str) -> asyncio.Lock:
    lock = _SESSION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[key] = lock
    return lock


class ResultModel(BaseModel):
    """
    Execute Code Result Model
    """

    stdout: Optional[str] = ''
    stderr: Optional[str] = ''
    result: Optional[str] = ''


class JupyterCodeExecuter:
    """
    Execute code in jupyter notebook
    """

    def __init__(
        self,
        base_url: str,
        code: str,
        token: str = '',
        password: str = '',
        timeout: int = 60,
        session_key: str | None = None,
        pool: KernelPool | None = None,
    ):
        """
        :param base_url: Jupyter server URL (e.g., "http://localhost:8888")
        :param code: Code to execute
        :param token: Jupyter authentication token (optional)
        :param password: Jupyter password (optional)
        :param timeout: WebSocket timeout in seconds (default: 60s)
        :param session_key: conversation key whose kernel is reused between calls
        :param pool: kernel cache shared by all calls of this process
        """
        self.base_url = base_url
        self.code = code
        self.token = token
        self.password = password
        self.timeout = timeout
        self.session_key = session_key
        self.pool = pool
        self.kernel_id = ''
        # A kernel that could not be interrupted is left running; deleting it on
        # exit is the only way to guarantee the next call starts clean.
        self.kernel_broken = False
        if self.base_url[-1] != '/':
            self.base_url += '/'
        self.session = aiohttp.ClientSession(trust_env=True, base_url=self.base_url)
        self.params = {}
        self.result = ResultModel()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.kernel_id and (self.session_key is None or self.kernel_broken):
            if self.kernel_broken and self.pool is not None and self.session_key is not None:
                self.pool.pop(self.session_key)
            await self.delete_kernel(self.kernel_id)
        await self.session.close()

    async def run(self) -> ResultModel:
        try:
            await self.sign_in()
            await self.ensure_kernel()
            await self.execute_code()
        except Exception as err:
            logger.exception('execute code failed, %s', err)
            self.result.stderr = f'Error: {err}'
        return self.result

    async def sign_in(self) -> None:
        # password authentication
        if self.password and not self.token:
            async with self.session.get('login') as response:
                response.raise_for_status()
                xsrf_token = response.cookies['_xsrf'].value
                if not xsrf_token:
                    raise ValueError('_xsrf token not found')
                self.session.cookie_jar.update_cookies(response.cookies)
                self.session.headers.update({'X-XSRFToken': xsrf_token})
            async with self.session.post(
                'login',
                data={'_xsrf': xsrf_token, 'password': self.password},
                allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS,
            ) as response:
                response.raise_for_status()
                self.session.cookie_jar.update_cookies(response.cookies)

        # token authentication
        if self.token:
            self.params.update({'token': self.token})

    async def init_kernel(self) -> None:
        async with self.session.post(url='api/kernels', params=self.params) as response:
            response.raise_for_status()
            kernel_data = await response.json()
            self.kernel_id = kernel_data['id']

    async def ensure_kernel(self) -> None:
        """Reuse this conversation's kernel when it is still alive, else create one."""
        if self.session_key is not None and self.pool is not None:
            kernel_id = self.pool.get(self.session_key)
            if kernel_id is not None and await self.kernel_alive(kernel_id):
                self.kernel_id = kernel_id
                return
            if kernel_id is not None:
                self.pool.pop(self.session_key)

        await self.init_kernel()
        if self.session_key is None or self.pool is None:
            return

        evicted = self.pool.put(self.session_key, self.kernel_id)
        if evicted is not None:
            await self.delete_kernel(evicted)

    async def kernel_alive(self, kernel_id: str) -> bool:
        async with self.session.get(f'api/kernels/{kernel_id}', params=self.params) as response:
            return response.status == 200

    async def delete_kernel(self, kernel_id: str) -> None:
        try:
            async with self.session.delete(f'api/kernels/{kernel_id}', params=self.params) as response:
                response.raise_for_status()
        except Exception as err:
            # Non-fatal: the Jupyter server culls idle kernels on its own. Log it
            # with the id so a leak is observable instead of silent.
            logger.warning('could not delete kernel %s: %s', kernel_id, err)

    async def interrupt(self) -> bool:
        """Stop the running execution without destroying the kernel's state."""
        try:
            async with self.session.post(f'api/kernels/{self.kernel_id}/interrupt', params=self.params) as response:
                response.raise_for_status()
            return True
        except Exception as err:
            logger.warning('could not interrupt kernel %s: %s', self.kernel_id, err)
            return False

    def init_ws(self) -> (str, dict):
        ws_base = self.base_url.replace('http', 'ws', 1)
        ws_params = '?' + '&'.join([f'{key}={val}' for key, val in self.params.items()])
        websocket_url = f'{ws_base}api/kernels/{self.kernel_id}/channels{ws_params if len(ws_params) > 1 else ""}'
        ws_headers = {}
        if self.password and not self.token:
            ws_headers = {
                'Cookie': '; '.join([f'{cookie.key}={cookie.value}' for cookie in self.session.cookie_jar]),
                **self.session.headers,
            }
        return websocket_url, ws_headers

    async def execute_code(self) -> None:
        # initialize ws
        websocket_url, ws_headers = self.init_ws()
        # execute
        async with websockets.connect(websocket_url, additional_headers=ws_headers) as ws:
            await self.execute_in_jupyter(ws)

    async def execute_in_jupyter(self, ws) -> None:
        # send message
        msg_id = uuid.uuid4().hex
        await ws.send(
            JSONCodec.dumps(
                {
                    'header': {
                        'msg_id': msg_id,
                        'msg_type': 'execute_request',
                        'username': 'user',
                        'session': uuid.uuid4().hex,
                        'date': '',
                        'version': '5.3',
                    },
                    'parent_header': {},
                    'metadata': {},
                    'content': {
                        'code': self.code,
                        'silent': False,
                        'store_history': True,
                        'user_expressions': {},
                        'allow_stdin': False,
                        'stop_on_error': True,
                    },
                    'channel': 'shell',
                }
            )
        )
        # parse message
        stdout, stderr, result = '', '', []
        while True:
            try:
                # wait for message
                message = await asyncio.wait_for(ws.recv(), self.timeout)
                message_data = JSONCodec.loads(message)
                # msg id not match, skip
                if message_data.get('parent_header', {}).get('msg_id') != msg_id:
                    continue
                # check message type
                msg_type = message_data.get('msg_type')
                match msg_type:
                    case 'stream':
                        if message_data['content']['name'] == 'stdout':
                            stdout += message_data['content']['text']
                        elif message_data['content']['name'] == 'stderr':
                            stderr += message_data['content']['text']
                    case 'execute_result' | 'display_data':
                        data = message_data['content']['data']
                        if 'image/png' in data:
                            result.append(f'data:image/png;base64,{data["image/png"]}')
                        elif 'text/plain' in data:
                            result.append(data['text/plain'])
                    case 'error':
                        stderr += '\n'.join(message_data['content']['traceback'])
                    case 'status':
                        if message_data['content']['execution_state'] == 'idle':
                            break

            except asyncio.TimeoutError:
                stderr += '\nExecution timed out.'
                # A persistent kernel keeps running the abandoned code, so the
                # next call in this conversation would queue behind it. Interrupt
                # it; if that fails, mark it broken and delete it on exit.
                if not await self.interrupt():
                    self.kernel_broken = True
                    stderr += ' The kernel could not be interrupted and will be restarted.'
                break
        self.result.stdout = stdout.strip()
        self.result.stderr = stderr.strip()
        self.result.result = '\n'.join(result).strip() if result else ''


async def execute_code_jupyter(
    base_url: str,
    code: str,
    token: str = '',
    password: str = '',
    timeout: int = 60,
    session_key: str | None = None,
) -> dict:
    """Execute code, reusing one kernel per session_key.

    Calls for the same conversation are serialised so two tool calls cannot
    interleave on one kernel. Without a session_key each call gets its own
    kernel, which is deleted afterwards.
    """
    if session_key is None:
        async with JupyterCodeExecuter(base_url, code, token, password, timeout) as executor:
            result = await executor.run()
            return result.model_dump()

    pool_key = f'{base_url}|{session_key}'
    async with _session_lock(pool_key):
        async with JupyterCodeExecuter(
            base_url,
            code,
            token,
            password,
            timeout,
            session_key=pool_key,
            pool=_KERNEL_POOL,
        ) as executor:
            result = await executor.run()
            return result.model_dump()
