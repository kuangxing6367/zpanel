# -*- coding: utf-8 -*-
"""
ACME v2 客户端（RFC 8555）—— ssl 扩展私有模块，零第三方依赖。

签名走 **openssl 子进程**（ES256：P-256 私钥 + DER→raw 转换），
不自己实现椭圆曲线 —— 密码学原语交给 openssl，这与项目纪律一致。

流程（webroot http-01）：
    directory → newAccount → newOrder → authz(http-01) → 挑战文件落 webroot
    → respond → poll valid → finalize(CSR) → 证书落盘

**验证的真相**：CA 服务器要主动回源访问 http://域名/.well-known/acme-challenge/…
内网/不可达的机器卡在验证环节，任务里如实报 pending 超时 —— 不是代码问题，
是 http-01 的物理前提（公网可达 80）。
"""
import base64
import binascii
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

LETSENCRYPT_DIR = "https://acme-v02.api.letsencrypt.org/directory"
LETSENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
USER_AGENT = "zpanel-acme/1.0"

_ab = (lambda s: base64.urlsafe_b64encode(s).rstrip(b"=").decode())


def _run(argv, input_bytes: bytes = None) -> bytes:
    r = subprocess.run(argv, input=input_bytes, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"openssl 失败: {(r.stderr or b'').decode('utf-8', 'replace')[:300]}")
    return r.stdout


# ── 密钥 ────────────────────────────────────────────────
def ensure_account_key(path: str) -> dict:
    """EC P-256 账号密钥；返回 JWK。"""
    if not os.path.exists(path):
        _run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt",
              "ec_paramgen_curve:P-256", "-out", path])
        os.chmod(path, 0o600)
    pub = _run(["openssl", "pkey", "-in", path, "-pubout", "-text_pub"]).decode()
    # 从 openssl 文本输出里抠出 64 字节公钥坐标
    hexs = "".join(ln.strip().replace(":", "") for ln in pub.splitlines()
                   if all(c in "0123456789abcdefABCDEF: " for c in ln) and len(ln.strip()) > 8)
    raw = bytes.fromhex(hexs.split("04", 1)[1])       # 去掉 04 前缀
    x, y = raw[:32], raw[32:64]
    return {"kty": "EC", "crv": "P-256", "x": _ab(x), "y": _ab(y)}


def _thumbprint(jwk: dict) -> str:
    return _ab(hashlib.sha256(json.dumps(jwk, sort_keys=True).encode()).digest())


# ── JWS 签名（ES256，openssl 子进程）────────────────────
def _jws_sign(account_key: str, protected: dict, payload: dict) -> dict:
    def _b64(obj):
        return _ab(json.dumps(obj, separators=(",", ":")).encode())
    ph = _b64(protected)
    pl = _b64(payload or {})
    der = _run(["openssl", "dgst", "-sha256", "-sign", account_key],
               input_bytes=(ph + "." + pl).encode())
    # DER SEQUENCE{r INTEGER, s INTEGER} → 定长 32 字节 R||S（JWS ES256 要求）
    data = der
    assert data[0] == 0x30
    idx, body = 2, data[2:]
    if body[0] & 0x80:
        n = body[0] & 0x7F
        body = body[1 + n:]
    def _int():
        nonlocal body
        assert body[0] == 0x02
        ln = body[1]
        v = body[2:2 + ln]
        body = body[2 + ln:]
        return v.lstrip(b"\x00").rjust(32, b"\x00")
    r_int, s_int = _int(), _int()
    sig = _ab(r_int + s_int)
    return {"protected": ph, "payload": pl, "signature": sig}


class AcmeClient:
    """极简 ACME v2 客户端（http-01 webroot）。"""

    def __init__(self, directory_url: str, account_key_path: str, log=None):
        self.dir_url = directory_url
        self.key_path = account_key_path
        self.log = log or (lambda m: None)
        self.jwk = ensure_account_key(account_key_path)
        self.nonce = None
        self.directory = self._get_json(directory_url)
        self.account_url = None

    # ── HTTP 原语 ──
    def _fetch(self, url, data=None, headers=None, allow_error=False):
        req = urllib.request.Request(url, method="POST" if data is not None else "GET")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/jose+json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=30) as resp:
                self.nonce = resp.headers.get("Replay-Nonce", self.nonce)
                body = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body), dict(resp.headers)
        except urllib.error.HTTPError as e:
            self.nonce = e.headers.get("Replay-Nonce", self.nonce)
            body = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"detail": body[:300]}
            if not allow_error:
                raise RuntimeError(f"ACME {url.split('/')[-1]}: "
                                   f"{parsed.get('detail') or parsed}") from None
            return e.code, parsed, dict(e.headers)

    def _get_json(self, url):
        st, body, _ = self._fetch(url)
        return body

    def _post(self, url, payload, kid=True, allow_error=False):
        if not self.nonce:
            self._fetch(self.directory["newNonce"])
        protected = {"alg": "ES256", "nonce": self.nonce, "url": url}
        if kid and self.account_url:
            protected["kid"] = self.account_url
        else:
            protected["jwk"] = self.jwk
        body = json.dumps(_jws_sign(self.key_path, protected, payload)).encode()
        return self._fetch(url, data=body, allow_error=allow_error)

    # ── 流程 ──
    def ensure_account(self, email: str, terms_agreed: bool = True) -> str:
        payload = {"termsOfServiceAgreed": bool(terms_agreed)}
        if email:
            payload["contact"] = ["mailto:" + email]
        st, body, headers = self._post(self.directory["newAccount"], payload,
                                       kid=False, allow_error=True)
        loc = headers.get("Location", "")
        if st in (200, 201) and loc:
            self.account_url = loc
            self.log(f"ACME 账号就绪：{loc[:60]}…")
            return loc
        raise RuntimeError(f"newAccount 失败({st}): {body}")

    def new_order(self, domains: list) -> dict:
        st, body, headers = self._post(self.directory["newOrder"],
                                       {"identifiers": [{"type": "dns", "value": d}
                                                        for d in domains]},
                                       allow_error=True)
        if st not in (200, 201):
            raise RuntimeError(f"newOrder 失败({st}): {body}")
        self.order_url = headers.get("Location", "")    # 轮询订单状态用
        return body

    def get_authz(self, url: str) -> dict:
        st, body, _ = self._post(url, None)
        return body

    def respond_challenge(self, url: str) -> dict:
        st, body, _ = self._post(url, {})
        return body

    def finalize(self, finalize_url: str, csr_der: bytes) -> dict:
        st, body, _ = self._post(finalize_url, {"csr": _ab(csr_der)})
        return body

    def poll(self, url: str, want: str, timeout: float = 90.0, log=None) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            st, body, _ = self._post(url, None)
            last = body
            state = body.get("status")
            if state == want:
                return body
            if state in ("invalid", "deactivated", "expired", "rejected"):
                errs = [ch.get("error", {}).get("detail") for ch in (body.get("challenges") or []) if ch.get("error")]
                raise RuntimeError(f"{url.split('/')[-1]} 状态 {state}: {errs or body}")
            (log or self.log)(f"等待 {state}…")
            time.sleep(4)
        raise TimeoutError(f"等待 {want} 超时（最后状态 {last and last.get('status')}）"
                           f"—— http-01 需要 CA 能访问 http://域名/.well-known/acme-challenge/"
                           f"（内网/防火墙会卡在这一步）")


# ── 证书工具 ────────────────────────────────────────────
def make_domain_key_and_csr(domains: list, key_path: str, csr_path: str):
    """生成站点私钥 + CSR（SAN 覆盖全部域名）。"""
    _run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
          "-out", key_path])
    os.chmod(key_path, 0o600)
    subj = "/CN=" + domains[0]
    san = ",".join(f"DNS:{d}" for d in domains)
    _run(["openssl", "req", "-new", "-key", key_path, "-out", csr_path,
          "-subj", subj, "-addext", f"subjectAltName={san}"])


def split_pem_chain(pem: str) -> list:
    """fullchain 拆单证书列表（落盘 fullchain.pem 用一条、展示/巡检用逐条）。"""
    certs = []
    for block in pem.split("-----END CERTIFICATE-----"):
        if "-----BEGIN CERTIFICATE-----" in block:
            certs.append(block[block.index("-----BEGIN CERTIFICATE-----")
                               + len("-----BEGIN CERTIFICATE-----")
                               + 1:] + "-----END CERTIFICATE-----")
    return certs
