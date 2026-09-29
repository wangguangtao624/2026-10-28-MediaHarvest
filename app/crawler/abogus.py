"""抖音 Web 接口签名（a_bogus）。

背景
----
抖音网页版从 2023 年起对 `/aweme/v1/web/*` 系列接口加了 `a_bogus` 签名校验。
不带签名的请求不会报错，而是**返回 HTTP 200 + 空 body**（最容易误判成「接口挂了」）。
本文件按公开逆向资料重写了这套签名算法，纯 Python，不依赖 Node.js / 浏览器。

算法链路（GET 请求）
--------------------
    string_1 = 3 组 4 字节随机因子拼成的 12 字符串（注入随机，防重放）
    string_2 = RC4( 固定 44 字节模板 + UA 指纹校验位 + 浏览器指纹 + 校验字节 )
    result   = 自定义 Base64(s4 字母表) 编码 string_1 + string_2

其中固定模板里嵌入了：
    - params 的 SM3 二次哈希（第二次喂的是 32 个**裸字节**，不是 hex 文本）
    - method 的 SM3 二次哈希
    - 起止时间戳（毫秒）各 4 字节 + 4 字节扩展位

⚠️ 稳定性说明：这是**逆向**出来的算法，抖音随时可能改版（换 salt、换字母表、
把算法搬进 JSVMP 虚拟机）。一旦接口又开始返回空 body，优先怀疑签名失效：
先用 `python -m app.crawler.abogus` 自测，再对照最新公开资料更新本文件。

⚠️ 授权说明：a_bogus 算法本身是抖音的私有实现，本文件是对其的独立重写（clean-room
式复述公开资料中的算法描述与协议常量），用于个人学习/自用采集。若你要把本项目对外
分发或商用，请先自行评估合规性。
"""

from __future__ import annotations

import hashlib
import random
from time import time
from urllib.parse import quote, urlencode

__all__ = ["ABogus", "sign_params"]

# 协议常量 —— 改这些值前请先跑自测，它们直接决定签名是否被服务端接受
_END_STRING = "cus"                 # SM3 盐值
_ARGUMENTS = [0, 1, 14]             # 旧版遗留常量，保留以对齐公开实现
_UA_KEY = "\x00\x01\x0e"

# 自定义 Base64 字母表（抖音私有，非标准表）
_CHARSETS = {
    "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
    "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
    "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
}

# Chrome/Windows 默认指纹（由浏览器 JS 上报，服务端会做一致性校验）
_DEFAULT_BROWSER = "1536|742|1536|864|0|0|0|0|1536|864|1536|864|1536|742|24|24|MacIntel"

# UA 特征码：对应上面 UA 的一组 32 字节指纹位
_UA_CODE = [
    76, 98, 15, 131, 97, 245, 224, 133, 122, 199,
    241, 166, 79, 34, 90, 191, 128, 126, 122, 98,
    66, 11, 14, 40, 49, 110, 110, 173, 67, 96, 138, 252,
]


def _sm3_pure(data: bytes) -> list[int]:
    """纯 Python SM3（国密 GB/T 32905-2016），仅在 hashlib 不提供 sm3 时兜底。"""
    iv = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
          0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]
    t = [0x79CC4519] * 16 + [0x7A879D8A] * 48

    def rotl(x: int, n: int) -> int:
        n %= 32
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    def p0(x: int) -> int:
        return x ^ rotl(x, 9) ^ rotl(x, 17)

    def p1(x: int) -> int:
        return x ^ rotl(x, 15) ^ rotl(x, 23)

    def ff(x: int, y: int, z: int, j: int) -> int:
        return (x ^ y ^ z) if j < 16 else ((x & y) | (x & z) | (y & z))

    def gg(x: int, y: int, z: int, j: int) -> int:
        return (x ^ y ^ z) if j < 16 else ((x & y) | (~x & 0xFFFFFFFF & z))

    msg = bytearray(data)
    bit_len = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += bit_len.to_bytes(8, "big")

    v = iv[:]
    for off in range(0, len(msg), 64):
        blk = msg[off:off + 64]
        w = [int.from_bytes(blk[i * 4:i * 4 + 4], "big") for i in range(16)]
        for j in range(16, 68):
            w.append(p1(w[j - 16] ^ w[j - 9] ^ rotl(w[j - 3], 15))
                     ^ rotl(w[j - 13], 7) ^ w[j - 6])
        w1 = [w[j] ^ w[j + 4] for j in range(64)]
        a, b, c, d, e, f, g, h = v
        for j in range(64):
            ss1 = rotl((rotl(a, 12) + e + rotl(t[j], j)) & 0xFFFFFFFF, 7)
            ss2 = ss1 ^ rotl(a, 12)
            tt1 = (ff(a, b, c, j) + d + ss2 + w1[j]) & 0xFFFFFFFF
            tt2 = (gg(e, f, g, j) + h + ss1 + w[j]) & 0xFFFFFFFF
            d, c, b, a = c, rotl(b, 9), a, tt1
            h, g, f, e = g, rotl(f, 19), e, p0(tt2)
        v = [x ^ y for x, y in zip(v, [a, b, c, d, e, f, g, h])]
    return [b for x in v for b in x.to_bytes(4, "big")]


def _sm3_bytes(data: bytes | str) -> list[int]:
    """SM3 哈希，返回 32 个字节值。

    优先用 OpenSSL 提供的实现（标准库 hashlib，Python 3.11+ 带 SM3）；
    没有就退回纯 Python 实现，保证任何环境都能跑，不引入 gmssl 依赖。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        return list(hashlib.new("sm3", data).digest())
    except (ValueError, AttributeError):
        return _sm3_pure(data)


def _sm3_double(text: str) -> list[int]:
    """二次 SM3：先对 text+salt 求一次，再把**裸字节**喂进第二次。

    踩坑记录：第二次的输入必须是 32 个裸字节，不能是第一次的 hex 字符串
    （64 个 ASCII 字符）。喂错了每个字节都对不上。
    """
    first = _sm3_bytes(text + _END_STRING)
    return _sm3_bytes(bytes(first))


def _rc4(plaintext: str, key: str) -> str:
    """RC4 变体。密钥固定为单字符，但保留通用实现方便日后替换。"""
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + ord(key[i % len(key)])) % 256
        box[i], box[j] = box[j], box[i]
    i = j = 0
    out: list[str] = []
    for ch in plaintext:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out.append(chr(box[(box[i] + box[j]) % 256] ^ ord(ch)))
    return "".join(out)


def _b64_custom(s: str, charset: str = "s4") -> str:
    """按指定字母表做 Base64 编码。"""
    table = _CHARSETS[charset]
    out: list[str] = []
    for i in range(0, len(s), 3):
        rest = len(s) - i
        if rest >= 3:
            n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8) | ord(s[i + 2])
            out += [table[(n >> 18) & 63], table[(n >> 12) & 63],
                    table[(n >> 6) & 63], table[n & 63]]
        elif rest == 2:
            n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8)
            out += [table[(n >> 18) & 63], table[(n >> 12) & 63],
                    table[(n >> 6) & 63]]
        else:
            n = ord(s[i]) << 16
            out += [table[(n >> 18) & 63], table[(n >> 12) & 63]]
    out.append("=" * ((4 - len(out) % 4) % 4))
    return "".join(out)


class ABogus:
    """a_bogus 签名器。

    同一个实例可以反复使用；随机因子每次都重新生成。
    """

    def __init__(self, browser: str = "") -> None:
        self.browser = browser or _DEFAULT_BROWSER
        self.browser_code = [ord(c) for c in self.browser]
        self.browser_len = len(self.browser)

    # ---------- 随机因子 ----------
    @staticmethod
    def _rand_list(seed=None, a: int = 170, b: int = 85,
                   d: int = 0, e: int = 0, f: int = 0, g: int = 0) -> list[int]:
        r = seed if seed is not None else random.random() * 10000
        lo, hi = int(r) & 255, int(r) >> 8
        return [lo & a | d, lo & b | e, hi & a | f, hi & b | g]

    def _string_1(self, s1=None, s2=None, s3=None) -> str:
        a = self._rand_list(s1, 170, 85, 1, 2, 5, 45 & 170)
        b = self._rand_list(s2, 170, 85, 1, 0, 0, 0)
        c = self._rand_list(s3, 170, 85, 1, 0, 5, 0)
        return "".join(chr(v) for v in a + b + c)

    # ---------- 主体 ----------
    def _string_2(self, params: str, method: str,
                  start_ms: int, end_ms: int) -> str:
        # 顺序不能反：先算校验字节（只覆盖 44 字节模板），
        # 再把浏览器指纹拼上去，最后把校验字节补在末尾
        buf = self._template(params, method, start_ms, end_ms)
        check = self._checksum(buf)
        buf.extend(self.browser_code)
        buf.append(check)
        return _rc4("".join(chr(v) for v in buf), "y")

    def _template(self, params: str, method: str,
                  start_ms: int, end_ms: int) -> list[int]:
        """44 字节固定模板。下标顺序是协议定的，不要"顺手整理"。"""
        p = _sm3_double(params)          # params 二次 SM3
        m = _sm3_double(method)          # method 二次 SM3
        e32 = (end_ms // 0x100000000) & 0xFF
        s32 = (start_ms // 0x100000000) & 0xFF
        return [
            44,                              # 0  长度标记
            (end_ms >> 24) & 0xFF,           # 1
            0, 0, 0, 0,                      # 2-5
            24,                              # 6
            p[21],                           # 7  params 哈希位
            m[21],                           # 8  method 哈希位
            0,                               # 9
            _UA_CODE[23],                    # 10 UA 指纹位
            (end_ms >> 16) & 0xFF,           # 11
            0, 0, 0,                         # 12-14
            1, 0, 239,                       # 15-17
            p[22],                           # 18 params 哈希位
            m[22],                           # 19 method 哈希位
            _UA_CODE[24],                    # 20 UA 指纹位
            (end_ms >> 8) & 0xFF,            # 21
            0, 0, 0, 0,                      # 22-25
            end_ms & 0xFF,                   # 26
            0, 0,                            # 27-28
            14,                              # 29
            (start_ms >> 24) & 0xFF,         # 30
            (start_ms >> 16) & 0xFF,         # 31
            0,                               # 32
            (start_ms >> 8) & 0xFF,          # 33
            start_ms & 0xFF,                 # 34
            3,                               # 35
            e32,                             # 36 结束时间戳高位
            1,                               # 37
            s32,                             # 38 起始时间戳高位
            1,                               # 39
            self.browser_len,                # 40 浏览器指纹长度
            0, 0, 0,                         # 41-43
        ]

    @staticmethod
    def _checksum(buf: list[int]) -> int:
        r = 0
        for v in buf:
            r ^= v
        return r

    # ---------- 对外接口 ----------
    def get_value(self, params: dict | str, method: str = "GET",
                  start_ms: int = 0, end_ms: int = 0) -> str:
        """生成 a_bogus 原值（未 URL 编码）。"""
        if isinstance(params, dict):
            params = urlencode(params)
        start_ms = start_ms or int(time() * 1000)
        end_ms = end_ms or (start_ms + random.randint(4, 8))
        return _b64_custom(self._string_1() + self._string_2(
            params, method, start_ms, end_ms), "s4")

    def sign(self, params: dict | str, method: str = "GET",
             start_ms: int = 0, end_ms: int = 0) -> str:
        """返回可直接拼进 query 的 URL 编码后的 a_bogus。"""
        return quote(self.get_value(params, method, start_ms, end_ms), safe="")


def sign_params(params: dict[str, str], method: str = "GET") -> dict[str, str]:
    """给参数字典补上 a_bogus 字段，返回新字典。"""
    signed = dict(params)
    signed["a_bogus"] = ABogus().sign(params, method)
    return signed


if __name__ == "__main__":
    demo = {
        "device_platform": "webapp", "aid": "6383",
        "channel": "channel_pc_web", "aweme_id": "7345492945006595379",
    }
    for _ in range(3):
        v = ABogus().sign(demo)
        print(f"len={len(v):3d}  {v}")
