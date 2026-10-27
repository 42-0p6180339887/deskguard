"""Local application password verifier; does not encrypt or lock photo files."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import tempfile
import time

ITERATIONS = 600_000


class AccessError(ValueError):
    pass


class AccessControl:
    def __init__(self, path):
        self.path = Path(path)
        self.record = None
        self.error = None
        self.failures = 0
        self.blocked_until = 0.0
        try:
            with self.path.open('r', encoding='utf-8') as handle:
                raw = handle.read(8193)
            if len(raw) > 8192:
                raise ValueError('oversized record')
            record = json.loads(raw)
            if (not isinstance(record, dict) or record.get('version') != 1
                    or record.get('iterations') != ITERATIONS
                    or record.get('algorithm') != 'pbkdf2-sha256'
                    or len(bytes.fromhex(record['salt'])) != 16
                    or len(bytes.fromhex(record['digest'])) != 32):
                raise ValueError('invalid record')
            self.record = record
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            self.error = '密码文件无法读取或已损坏，受保护操作已锁定。请恢复原来的 access.json 后重启。'

    @property
    def configured(self):
        return self.record is not None

    def wait_seconds(self):
        return max(0, math.ceil(self.blocked_until - time.monotonic()))

    def verify(self, password):
        if self.error:
            raise AccessError(self.error)
        if not self.configured:
            raise AccessError('尚未设置访问密码。')
        remaining = self.wait_seconds()
        if remaining:
            raise AccessError(f'尝试过多，请 {remaining} 秒后再试。守护仍在继续。')
        valid_input = isinstance(password, str) and 8 <= len(password) <= 128
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                    bytes.fromhex(self.record['salt']), ITERATIONS).hex() if valid_input else ''
        if hmac.compare_digest(digest, self.record['digest']):
            self.failures = 0
            return True
        self.failures += 1
        if self.failures >= 5:
            self.failures = 0
            self.blocked_until = time.monotonic() + 30
        return False

    def set_password(self, password):
        if self.error:
            raise AccessError(self.error)
        if not isinstance(password, str) or not 8 <= len(password) <= 128 or not password.strip():
            raise AccessError('密码需要 8–128 个字符，不能全为空白。')
        salt = os.urandom(16)
        record = dict(version=1, algorithm='pbkdf2-sha256', iterations=ITERATIONS,
                      salt=salt.hex(), digest=hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS).hex())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.deskguard-access-',
                                             suffix='.tmp', dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        # A failed write must never replace the currently accepted password.
        self.record = record
        self.failures = 0
        self.blocked_until = 0.0
