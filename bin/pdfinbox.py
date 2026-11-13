#!/usr/bin/env python3
"""
pdfinbox — 伪装成系统打印机的 PDF 收件箱。

设计要点
--------
* 不是一个 CUPS backend，而是一个真实的最小 IPP 打印机（RFC 8010/8011）。
  系统通过 `lpadmin` 把它注册成本地队列，打印对话框里就出现一台"打印机"。
* 一份任务 = 一次 HTTP POST，文档字节直接进内存/落盘，不经过 CUPS 的 filter 链，
  因此在 macOS 15（CUPS 不再把任务转交自建 backend）上仍然工作。
* 文件名由模板渲染，模板变量来自 IPP job 属性 + 本地时间 + 队列名。
* 队列名走 URL path，作为"收件箱路由键"：不同队列 → 不同目录 / 不同模板。

零第三方依赖。仅 Python 标准库。
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import gzip
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "pdfinbox"
VERSION = "1.0.0"
START_TS = time.time()

# --------------------------------------------------------------------------
# IPP 常量
# --------------------------------------------------------------------------

TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04

V_INTEGER = 0x21
V_BOOLEAN = 0x22
V_ENUM = 0x23
V_RANGE = 0x33
V_RESOLUTION = 0x32
V_TEXT = 0x41
V_NAME = 0x42
V_KEYWORD = 0x44
V_URI = 0x45
V_URISCHEME = 0x46
V_CHARSET = 0x47
V_LANGUAGE = 0x48
V_MIME = 0x49

OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CREATE_JOB = 0x0005
OP_SEND_DOCUMENT = 0x0006
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B
OP_CUPS_GET_DEFAULT = 0x4001
OP_CUPS_GET_PRINTERS = 0x4002

OP_NAMES = {
    OP_PRINT_JOB: "Print-Job",
    OP_VALIDATE_JOB: "Validate-Job",
    OP_CREATE_JOB: "Create-Job",
    OP_SEND_DOCUMENT: "Send-Document",
    OP_CANCEL_JOB: "Cancel-Job",
    OP_GET_JOB_ATTRIBUTES: "Get-Job-Attributes",
    OP_GET_JOBS: "Get-Jobs",
    OP_GET_PRINTER_ATTRIBUTES: "Get-Printer-Attributes",
    OP_CUPS_GET_DEFAULT: "CUPS-Get-Default",
    OP_CUPS_GET_PRINTERS: "CUPS-Get-Printers",
}

STATUS_OK = 0x0000
STATUS_BAD_REQUEST = 0x0400
STATUS_NOT_FOUND = 0x0406
STATUS_NOT_POSSIBLE = 0x0404

SUPPORTED_FORMATS = [
    "application/pdf",
    "application/postscript",
    "application/octet-stream",
]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# IPP 编解码
# --------------------------------------------------------------------------


class _Reader:
    __slots__ = ("d", "p")

    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def u8(self) -> int:
        if self.p + 1 > len(self.d):
            raise ValueError("truncated")
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self) -> int:
        if self.p + 2 > len(self.d):
            raise ValueError("truncated")
        v = int.from_bytes(self.d[self.p:self.p + 2], "big")
        self.p += 2
        return v

    def u32(self) -> int:
        if self.p + 4 > len(self.d):
            raise ValueError("truncated")
        v = int.from_bytes(self.d[self.p:self.p + 4], "big")
        self.p += 4
        return v

    def raw(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise ValueError("truncated")
        v = self.d[self.p:self.p + n]
        self.p += n
        return v


def _decode_value(tag: int, val: bytes):
    if tag in (V_INTEGER, V_ENUM):
        return int.from_bytes(val, "big", signed=True) if val else 0
    if tag == V_BOOLEAN:
        return bool(val[0]) if val else False
    if tag == V_RANGE:
        if len(val) >= 8:
            return (
                int.from_bytes(val[0:4], "big", signed=True),
                int.from_bytes(val[4:8], "big", signed=True),
            )
        return (0, 0)
    if tag in (V_TEXT, V_NAME, V_KEYWORD, V_URI, V_URISCHEME, V_CHARSET,
               V_LANGUAGE, V_MIME):
        return val.decode("utf-8", "replace")
    return val


def parse_ipp(data: bytes) -> dict:
    """解析 IPP 请求，返回 {version, op, rid, groups, attrs, doc}。"""
    r = _Reader(data)
    major, minor = r.u8(), r.u8()
    op = r.u16()
    rid = r.u32()

    groups: list[tuple[int, list]] = []
    cur: list = []
    attrs: dict[str, object] = {}
    last_name = ""

    while r.p < len(data):
        tag = r.u8()
        if tag == TAG_END:
            break
        if tag < 0x10:  # 组分隔符
            cur = []
            groups.append((tag, cur))
            last_name = ""
            continue
        nlen = r.u16()
        name = r.raw(nlen).decode("utf-8", "replace")
        vlen = r.u16()
        val = _decode_value(tag, r.raw(vlen))
        if name == "":
            name = last_name  # 多值属性的后续值
            if cur:
                gtag, gname, gval = cur[-1]
                if isinstance(gval, list):
                    cur[-1] = (gtag, gname, gval + [val])
                else:
                    cur[-1] = (gtag, gname, [gval, val])
        else:
            cur.append((tag, name, val))
            last_name = name
        attrs.setdefault(name, val)

    return {
        "version": (major, minor),
        "op": op,
        "rid": rid,
        "groups": groups,
        "attrs": attrs,
        "doc": data[r.p:],
    }


def _enc_one(tag: int, name: str, val) -> bytes:
    nb = name.encode("utf-8")
    if tag in (V_INTEGER, V_ENUM):
        vb = int(val).to_bytes(4, "big", signed=True)
    elif tag == V_BOOLEAN:
        vb = b"\x01" if val else b"\x00"
    elif tag == V_RANGE:
        vb = int(val[0]).to_bytes(4, "big", signed=True) + int(
            val[1]
        ).to_bytes(4, "big", signed=True)
    elif tag == V_RESOLUTION:
        # resolution = 横向(4) + 纵向(4) + 单位(1)，单位 3=dpi / 4=dpcm。共 9 字节。
        units = int(val[2]) if len(val) > 2 else 3
        vb = (
            int(val[0]).to_bytes(4, "big", signed=True)
            + int(val[1]).to_bytes(4, "big", signed=True)
            + bytes([units])
        )
    else:
        vb = str(val).encode("utf-8")
    return bytes([tag]) + len(nb).to_bytes(2, "big") + nb + len(vb).to_bytes(
        2, "big"
    ) + vb


def _enc_attr(tag: int, name: str, val) -> bytes:
    if isinstance(val, (list, tuple)) and tag not in (V_RANGE, V_RESOLUTION):
        out = b""
        for i, v in enumerate(val):
            out += _enc_one(tag, name if i == 0 else "", v)
        return out
    return _enc_one(tag, name, val)


_TLS = threading.local()  # 每请求线程记录客户端 IPP 版本，响应需回显同版本


def build_response(rid: int, status: int, groups: list, message: str | None = None,
                   version: tuple[int, int] | None = None) -> bytes:
    ver = version or getattr(_TLS, "version", (1, 1))
    body = bytes([ver[0], ver[1]]) + status.to_bytes(2, "big") + rid.to_bytes(
        4, "big"
    )
    op_group = [
        (V_CHARSET, "attributes-charset", getattr(_TLS, "charset", "utf-8")),
        (V_LANGUAGE, "attributes-natural-language",
         getattr(_TLS, "lang", "en")),
    ]
    if message:
        op_group.append((V_TEXT, "status-message", message))
    body += bytes([TAG_OPERATION])
    for tag, name, val in op_group:
        body += _enc_attr(tag, name, val)
    for gtag, items in groups:
        body += bytes([gtag])
        for tag, name, val in items:
            body += _enc_attr(tag, name, val)
    body += bytes([TAG_END])
    return body


# --------------------------------------------------------------------------
# 文件名模板
# --------------------------------------------------------------------------

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([^{}]*))?\}")
_RESERVED = {"CON", "PRN", "AUX", "NUL"}


def sanitize(text: str, max_len: int = 120) -> str:
    """把任意标题压成安全的文件名片段。"""
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _ILLEGAL.sub("-", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(". ")          # Windows/macOS 都不喜欢结尾的点
    text = text.replace("..", ".")   # 防目录穿越
    if len(text) > max_len:
        text = text[:max_len].rstrip(". ")
    if text.upper() in _RESERVED:
        text += "_"
    return text


class Template:
    """文件名模板。

    语法: {变量} 或 { now:格式 }，其中 now 走 strftime。
    未知变量渲染为空串（并在 stderr 提示一次）。
    """

    def __init__(self, pattern: str, max_title: int = 120) -> None:
        self.pattern = pattern
        self.max_title = max_title
        self._warned: set[str] = set()

    def render(self, vars: dict) -> str:
        def repl(m: re.Match) -> str:
            key, spec = m.group(1), m.group(2)
            if key == "now":
                return dt.datetime.now().strftime(spec) if spec else (
                    dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                )
            if key in vars:
                v = vars[key]
                return sanitize(str(v), self.max_title)
            if key not in self._warned:
                self._warned.add(key)
                log(f"模板变量 {{{key}}} 不可用（IPP 未提供），按空串处理")
            return ""

        out = _TOKEN.sub(repl, self.pattern)
        out = sanitize(out, 200)
        out = re.sub(r"[-_ ]{2,}", lambda m: m.group(0)[0], out)
        out = out.strip("-_ .")
        return out or "Untitled"


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "port": 8631,
    "bind": "127.0.0.1",
    "output_dir": "~/Documents/PDFInbox",
    "template": "{now:%Y%m%d-%H%M%S}_{title}",
    "max_title_len": 120,
    "unique_suffix": True,
    "post_command": None,
    "index": True,
    "routes": {},
}


def expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


class Config:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or expand("~/.config/pdfinbox/config.json")
        self.data = dict(DEFAULT_CONFIG)
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            self.data.update(user)

    def route(self, queue: str) -> dict:
        """队列名 → 该队列的目录/模板覆盖。"""
        routes = self.data.get("routes") or {}
        r = dict(self.data)
        r.update(routes.get(queue) or {})
        r["output_dir"] = expand(r["output_dir"])
        return r

    def queue_template(self, queue: str) -> str:
        return self.route(queue).get("template") or self.data["template"]


# --------------------------------------------------------------------------
# 收件箱
# --------------------------------------------------------------------------


class Inbox:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.state_path = expand("~/.local/state/pdfinbox/state.json")
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)

    # -- 序号 ------------------------------------------------------------
    def _next_seq(self) -> int:
        today = dt.date.today().isoformat()
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                st = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            st = {}
        if st.get("date") != today:
            st = {"date": today, "seq": 0}
        st["seq"] = int(st.get("seq", 0)) + 1
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, self.state_path)
        return st["seq"]

    # -- 内容嗅探 --------------------------------------------------------
    @staticmethod
    def sniff(doc: bytes) -> str:
        if doc[:5] == b"%PDF-":
            return "pdf"
        if doc[:4] == b"%!PS":
            return "ps"
        if doc[:2] == b"\x1f\x8b":
            try:
                return Inbox.sniff(gzip.decompress(doc))
            except OSError:
                return "gzip"
        return "bin"

    @staticmethod
    def page_count(pdf: bytes) -> int | None:
        """粗略页数（不解析 xref，够用即可）。"""
        try:
            n = len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf))
            return n or None
        except Exception:
            return None

    # -- 落盘 ------------------------------------------------------------
    def save(self, queue: str, attrs: dict, doc: bytes) -> dict:
        route = self.cfg.route(queue)
        outdir = route["output_dir"]
        os.makedirs(outdir, exist_ok=True)

        kind = self.sniff(doc)
        ext = {"pdf": ".pdf", "ps": ".ps", "gzip": ".gz"}.get(kind, ".bin")

        title = str(attrs.get("job-name") or "").strip()
        with self.lock:
            seq = self._next_seq()
            now = dt.datetime.now()
            vars = {
                "title": title or "Untitled",
                "job": attrs.get("job-id", ""),
                "seq": f"{seq:03d}",
                "user": attrs.get("job-originating-user-name", ""),
                "host": attrs.get("job-originating-host-name", ""),
                "queue": queue,
                "printer": attrs.get("printer-name", queue),
                "fmt": kind,
                "pages": self.page_count(doc) or "",
                "uuid": uuid.uuid4().hex[:8],
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H%M%S"),
                "datetime": now.strftime("%Y%m%d-%H%M%S"),
            }
            tpl = Template(route.get("template") or self.cfg.data["template"],
                           int(route.get("max_title_len", 120)))
            stem = tpl.render(vars)

            path = os.path.join(outdir, stem + ext)
            if self.cfg.data.get("unique_suffix", True):
                n = 2
                while os.path.exists(path):
                    path = os.path.join(outdir, f"{stem}-{n}{ext}")
                    n += 1
                    if n > 999:
                        path = os.path.join(outdir, f"{stem}-{uuid.uuid4().hex[:6]}{ext}")
                        break

            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(doc)
            os.replace(tmp, path)

        record = {
            "ts": now.isoformat(timespec="seconds"),
            "path": path,
            "bytes": len(doc),
            "format": kind,
            "queue": queue,
            "job_name": title,
            "job_id": attrs.get("job-id"),
            "user": attrs.get("job-originating-user-name"),
        }
        if self.cfg.data.get("index", True):
            idx = os.path.join(outdir, ".pdfinbox-index.jsonl")
            with open(idx, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        cmd = route.get("post_command")
        if cmd:
            self._run_post(cmd, path, record)

        log(f"保存 {path}  ({len(doc)} B, {kind}, queue={queue})")
        return record

    def _run_post(self, cmd: str, path: str, record: dict) -> None:
        import subprocess
        env = dict(os.environ, PDFINBOX_PATH=path, PDFINBOX_JSON=json.dumps(record))
        try:
            subprocess.Popen(cmd, shell=True, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.STDOUT)
        except OSError as exc:
            log(f"post_command 失败: {exc}")


# --------------------------------------------------------------------------
# IPP 应用逻辑
# --------------------------------------------------------------------------


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.inbox = Inbox(cfg)
        self.jobs: dict[int, dict] = {}
        self._job_lock = threading.Lock()
        self._job_id = 0
        self.stats = {"jobs": 0, "errors": 0}

    # -- 工具 ------------------------------------------------------------
    def _next_job(self) -> int:
        with self._job_lock:
            self._job_id += 1
            return self._job_id

    def _queue_from_path(self, path: str, attrs: dict) -> str:
        """队列名 = 路由键。

        IPP 的权威来源是 printer-uri 属性（注册队列时我们给它不同的 path），
        URL path 只是兜底，printer-name 最后兜底。
        """
        for key in ("printer-uri", "job-printer-uri"):
            uri = attrs.get(key)
            if isinstance(uri, str) and uri.strip():
                seg = uri.rstrip("/").rsplit("/", 1)[-1]
                if seg:
                    return seg
        p = path.split("?")[0].strip("/")
        parts = [x for x in p.split("/") if x]
        if parts and parts[0] in ("printers", "classes"):
            parts = parts[1:]
        if parts:
            return parts[-1]
        return str(attrs.get("printer-name") or "PDFInbox")

    def printer_uri(self, queue: str) -> str:
        host, port = self.cfg.data["bind"], self.cfg.data["port"]
        return f"ipp://{host}:{port}/printers/{queue}"

    def printer_uuid(self) -> str:
        # PWG 要求稳定 UUID；这里由主机名派生，重启不变。
        seed = f"pdfinbox:{socket.gethostname()}"
        h = uuid.uuid5(uuid.NAMESPACE_DNS, seed)
        return f"urn:uuid:{h}"

    def _printer_group(self, queue: str) -> list:
        model = "PDF Inbox (virtual PDF printer)"
        media = [
            "iso_a4_210x297mm",
            "iso_a3_297x420mm",
            "iso_a5_148x210mm",
            "na_letter_8.5x11in",
            "na_legal_8.5x14in",
            "na_ledger_11x17in",
            "jis_b5_182x257mm",
            "na_number-10_4.125x9.5in",
        ]
        items = [
            (V_URI, "printer-uri-supported", self.printer_uri(queue)),
            (V_URI, "printer-uuid", self.printer_uuid()),
            (V_KEYWORD, "uri-security-supported", "none"),
            (V_KEYWORD, "uri-authentication-supported", "none"),
            (V_NAME, "printer-name", queue),
            (V_TEXT, "printer-info", model),
            (V_TEXT, "printer-make-and-model", model),
            (V_TEXT, "printer-location", "local"),
            (V_ENUM, "printer-state", 3),
            (V_KEYWORD, "printer-state-reasons", "none"),
            (V_BOOLEAN, "printer-is-accepting-jobs", True),
            (V_INTEGER, "queued-job-count", 0),
            (V_MIME, "document-format-supported", SUPPORTED_FORMATS),
            (V_MIME, "document-format-default", "application/pdf"),
            (V_MIME, "document-format-preferred", "application/pdf"),
            (V_KEYWORD, "media-supported", media),
            (V_KEYWORD, "media-default", "iso_a4_210x297mm"),
            (V_KEYWORD, "media-ready", "iso_a4_210x297mm"),
            (V_KEYWORD, "media-type-supported", ["stationery", "auto"]),
            (V_KEYWORD, "media-type-default", "stationery"),
            (V_KEYWORD, "sides-supported",
             ["one-sided", "two-sided-long-edge", "two-sided-short-edge"]),
            (V_KEYWORD, "sides-default", "one-sided"),
            (V_KEYWORD, "print-color-mode-supported", ["color", "monochrome"]),
            (V_KEYWORD, "print-color-mode-default", "color"),
            (V_ENUM, "print-quality-supported", [3, 4, 5]),
            (V_ENUM, "print-quality-default", 4),
            (V_RESOLUTION, "printer-resolution-supported", (300, 300)),
            (V_RESOLUTION, "printer-resolution-default", (300, 300)),
            (V_KEYWORD, "ipp-features-supported", ["ipp-everywhere"]),
            (V_ENUM, "operations-supported",
             [OP_PRINT_JOB, OP_VALIDATE_JOB, OP_CREATE_JOB, OP_SEND_DOCUMENT,
              OP_CANCEL_JOB, OP_GET_JOB_ATTRIBUTES, OP_GET_JOBS,
              OP_GET_PRINTER_ATTRIBUTES, OP_CUPS_GET_DEFAULT, OP_CUPS_GET_PRINTERS]),
            (V_CHARSET, "charset-supported", "utf-8"),
            (V_LANGUAGE, "natural-language-supported", "en"),
            (V_KEYWORD, "ipp-versions-supported", ["1.1", "2.0"]),
            (V_KEYWORD, "compression-supported", ["none", "gzip"]),
            (V_KEYWORD, "pdl-override-supported", "attempted"),
            (V_INTEGER, "printer-up-time", int(time.time() - START_TS) + 1),
            (V_RANGE, "copies-supported", (1, 99)),
            (V_INTEGER, "copies-default", 1),
            (V_TEXT, "printer-device-id",
             "MFG:pdfinbox;MDL:PDF Inbox;CMD:PDF;"),
            (V_KEYWORD, "job-creation-attributes-supported",
             ["copies", "job-name", "media", "sides", "print-color-mode",
              "document-format"]),
        ]
        only = os.environ.get("PDFINBOX_ONLY")
        if only:
            keep = {x.strip() for x in only.split(",") if x.strip()}
            items = [x for x in items if x[1] in keep]
        return items

    def _job_group(self, attrs: dict, job_id: int, queue: str, state: int = 9) -> list:
        items = [
            (V_URI, "job-uri", f"{self.printer_uri(queue)}/{job_id}"),
            (V_INTEGER, "job-id", job_id),
            (V_URI, "job-printer-uri", self.printer_uri(queue)),
            (V_NAME, "job-name", str(attrs.get("job-name") or "")),
            (V_NAME, "job-originating-user-name",
             str(attrs.get("job-originating-user-name") or "")),
            (V_NAME, "job-originating-host-name",
             str(attrs.get("job-originating-host-name") or "")),
            (V_ENUM, "job-state", state),
            (V_KEYWORD, "job-state-reasons", "none"),
            (V_INTEGER, "job-printer-up-time", int(time.time() - START_TS) + 1),
        ]
        return items

    # -- 主分发 ----------------------------------------------------------
    def handle(self, req: dict, path: str, headers: dict) -> bytes:
        op, rid, attrs = req["op"], req["rid"], req["attrs"]
        _TLS.version = req["version"]
        _TLS.charset = str(attrs.get("attributes-charset") or "utf-8")
        _TLS.lang = str(attrs.get("attributes-natural-language") or "en")
        queue = self._queue_from_path(path, attrs)
        name = OP_NAMES.get(op, hex(op))
        if os.environ.get("PDFINBOX_DEBUG"):
            log(f"  path={path!r} printer-uri={attrs.get('printer-uri')!r} "
                f"job-name={attrs.get('job-name')!r} → queue={queue!r}")

        if op in (OP_GET_PRINTER_ATTRIBUTES, OP_CUPS_GET_DEFAULT):
            log(f"{name} queue={queue}")
            return build_response(
                rid, STATUS_OK,
                [(TAG_PRINTER, self._printer_group(queue))],
                "successful-ok",
            )

        if op == OP_CUPS_GET_PRINTERS:
            log(f"{name} queue={queue}")
            return build_response(
                rid, STATUS_OK,
                [(TAG_PRINTER, self._printer_group(queue))],
                "successful-ok",
            )

        if op == OP_VALIDATE_JOB:
            return build_response(rid, STATUS_OK, [], "successful-ok")

        if op == OP_PRINT_JOB:
            doc = self._inflate(req["doc"], attrs)
            if not doc:
                self.stats["errors"] += 1
                return build_response(rid, STATUS_BAD_REQUEST, [], "no document data")
            attrs = dict(attrs)
            job_id = self._next_job()
            attrs["job-id"] = job_id
            rec = self.inbox.save(queue, attrs, doc)
            self.stats["jobs"] += 1
            self.jobs[job_id] = {"attrs": attrs, "record": rec, "queue": queue}
            return build_response(
                rid, STATUS_OK,
                [(TAG_JOB, self._job_group(attrs, job_id, queue))],
                "successful-ok",
            )

        if op == OP_CREATE_JOB:
            job_id = self._next_job()
            self.jobs[job_id] = {"attrs": dict(attrs), "doc": b"", "queue": queue}
            return build_response(
                rid, STATUS_OK,
                [(TAG_JOB, self._job_group(attrs, job_id, queue))],
                "successful-ok",
            )

        if op == OP_SEND_DOCUMENT:
            job_id = int(attrs.get("job-id") or 0)
            job = self.jobs.get(job_id)
            if job is None:
                return build_response(rid, STATUS_NOT_FOUND, [], "job not found")
            doc = self._inflate(req["doc"], attrs)
            job["doc"] = doc
            if not attrs.get("last-document", True) is False:
                merged = dict(job["attrs"])
                merged.update({k: v for k, v in attrs.items() if k != "job-id"})
                merged["job-id"] = job_id
                rec = self.inbox.save(job["queue"], merged, doc)
                job["record"] = rec
                self.stats["jobs"] += 1
            return build_response(
                rid, STATUS_OK,
                [(TAG_JOB, self._job_group(job["attrs"], job_id, job["queue"]))],
                "successful-ok",
            )

        if op == OP_GET_JOB_ATTRIBUTES:
            job_id = int(attrs.get("job-id") or 0)
            job = self.jobs.get(job_id)
            if job is None:
                return build_response(rid, STATUS_NOT_FOUND, [], "job not found")
            return build_response(
                rid, STATUS_OK,
                [(TAG_JOB, self._job_group(job["attrs"], job_id, job["queue"], 9))],
                "successful-ok",
            )

        if op == OP_GET_JOBS:
            groups = []
            for jid, job in list(self.jobs.items()):
                groups.append((TAG_JOB, self._job_group(job["attrs"], jid, job["queue"], 9)))
            return build_response(rid, STATUS_OK, groups, "successful-ok")

        if op == OP_CANCEL_JOB:
            return build_response(rid, STATUS_OK, [], "successful-ok")

        log(f"未实现的操作 {name}，返回 not-possible")
        return build_response(rid, STATUS_NOT_POSSIBLE, [], "operation-not-supported")

    @staticmethod
    def _inflate(doc: bytes, attrs: dict) -> bytes:
        if doc[:2] == b"\x1f\x8b" or attrs.get("compression") == "gzip":
            try:
                return gzip.decompress(doc)
            except OSError:
                return doc
        return doc


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    # 必须 HTTP/1.1：libcups 请求走 1.1，响应降级到 1.0 会被判为不可用。
    protocol_version = "HTTP/1.1"
    app: App = None  # type: ignore

    def version_string(self):
        # CUPS 客户端会看 Server 头，装成 cupsd 更省事
        return "CUPS/2.3.4 IPP/2.1"

    def log_message(self, fmt, *args):  # 关掉默认访问日志
        pass

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                out += self.rfile.read(size)
                self.rfile.readline()
            return out
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        body = self._read_body()
        try:
            req = parse_ipp(body)
            resp = self.app.handle(req, self.path, dict(self.headers))
        except Exception as exc:  # 任何解析错误都不能让打印机挂掉
            log(f"请求处理异常: {exc!r}")
            self.app.stats["errors"] += 1
            try:
                req
            except NameError:
                req = {"rid": 1}
            resp = build_response(req.get("rid", 1), STATUS_BAD_REQUEST, [], "bad-request")
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)
        if os.environ.get("PDFINBOX_DUMP"):
            log(f"  REQ {len(body)}B {body[:80].hex()}")
            log(f"  RESP {len(resp)}B {resp[:80].hex()}")

    def do_GET(self):
        body = (
            f"{APP_NAME} {VERSION}\n"
            f"queue path: {self.path}\n"
            f"jobs: {self.app.stats['jobs']}  errors: {self.app.stats['errors']}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_serve(args) -> int:
    cfg = Config(args.config)
    if args.port:
        cfg.data["port"] = args.port
    if args.dir:
        cfg.data["output_dir"] = args.dir
    if args.template:
        cfg.data["template"] = args.template

    Handler.app = App(cfg)
    port = int(cfg.data["port"])
    bind = cfg.data["bind"]
    httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.daemon_threads = True
    log(f"{APP_NAME} {VERSION} 监听 ipp://{bind}:{port}/  "
        f"→ {expand(cfg.data['output_dir'])}")
    log(f"模板: {cfg.data['template']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，退出")
    return 0


def cmd_doctor(args) -> int:
    cfg = Config(args.config)
    checks = []

    port = int(cfg.data["port"])
    s = socket.socket()
    s.settimeout(0.3)
    in_use = s.connect_ex((cfg.data["bind"], port)) == 0
    s.close()
    checks.append(("端口 " + str(port), "占用（服务已在跑）" if in_use else "空闲", True))

    outdir = expand(cfg.data["output_dir"])
    ok = os.path.isdir(outdir) and os.access(outdir, os.W_OK)
    checks.append(("输出目录 " + outdir, "可写" if ok else "不存在或不可写", ok))

    checks.append(("CUPS", "存在" if os.path.exists("/usr/sbin/lpadmin") else "缺失",
                   os.path.exists("/usr/sbin/lpadmin")))
    gs = any(os.path.exists(p) for p in ("/opt/homebrew/bin/gs", "/usr/local/bin/gs"))
    checks.append(("Ghostscript", "已装" if gs else "未装（可选，仅转 PS 时需要）", True))

    print(f"{APP_NAME} {VERSION} 自检")
    for name, status, ok in checks:
        print(f"  [{'OK ' if ok else 'ERR'}] {name}: {status}")
    tpl = Template(cfg.data["template"])
    sample = tpl.render({"title": "示例标题/带非法:字符", "seq": "001", "job": 7})
    print(f"  模板预览: {sample}")
    return 0


def cmd_render(args) -> int:
    cfg = Config(args.config)
    tpl = Template(args.template or cfg.data["template"])
    vars = {
        "title": args.title or "示例标题",
        "seq": "001",
        "job": 42,
        "queue": "PDFInbox",
        "user": os.environ.get("USER", "me"),
        "host": socket.gethostname(),
        "fmt": "pdf",
        "pages": 3,
        "datetime": dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
        "date": dt.date.today().isoformat(),
        "time": dt.datetime.now().strftime("%H%M%S"),
        "uuid": uuid.uuid4().hex[:8],
    }
    for t in args.samples or [args.title]:
        print(tpl.render(dict(vars, title=t or "")))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog=APP_NAME, description="伪装系统打印机的 PDF 收件箱")
    p.add_argument("--config", help="配置文件路径 (默认 ~/.config/pdfinbox/config.json)")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("serve", help="启动 IPP 打印机服务")
    ps.add_argument("--port", type=int)
    ps.add_argument("--dir")
    ps.add_argument("--template")
    ps.set_defaults(func=cmd_serve)

    pd = sub.add_parser("doctor", help="环境自检")
    pd.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("render", help="预览文件名模板渲染结果")
    pr.add_argument("--template")
    pr.add_argument("--title")
    pr.add_argument("--samples", nargs="*")
    pr.set_defaults(func=cmd_render)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
