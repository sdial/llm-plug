import csv
import ipaddress
import os
from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass
class WhitelistRule:
    path_pattern: str
    methods: set[str]   # 空集合 = 允许所有方法
    network: ipaddress.IPv4Network | ipaddress.IPv6Network
    description: str


class WhitelistCache:
    def __init__(self, path: str) -> None:
        self._path = path
        self._mtime: float = -1.0
        self._rules: list[WhitelistRule] = []

    def get_rules(self) -> list[WhitelistRule]:
        try:
            mtime = os.stat(self._path).st_mtime
        except FileNotFoundError:
            self._rules = []
            self._mtime = -1.0
            return self._rules
        if mtime != self._mtime:
            self._rules = load_rules(self._path)
            self._mtime = mtime
        return self._rules


def load_rules(path: str) -> list[WhitelistRule]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    filtered = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    rules: list[WhitelistRule] = []
    reader = csv.reader(filtered)
    for row in reader:
        if len(row) < 4:
            continue
        path_pat, methods_str, ip_cidr, description = (col.strip() for col in row[:4])
        if path_pat == "path_pattern":
            continue
        if not path_pat or not ip_cidr:
            continue
        methods: set[str] = set()
        if methods_str and methods_str != "*":
            methods = {m.strip().upper() for m in methods_str.split("|")}
        try:
            network = ipaddress.ip_network(ip_cidr, strict=False)
        except ValueError:
            continue
        rules.append(WhitelistRule(
            path_pattern=path_pat,
            methods=methods,
            network=network,
            description=description,
        ))
    return rules


def validate_rules_text(text: str) -> tuple[bool, str, list[WhitelistRule]]:
    """校验并解析 CSV 文本。返回 (valid, error_message, parsed_rules)。"""
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    rules: list[WhitelistRule] = []
    reader = csv.reader(lines)
    for i, row in enumerate(reader):
        if not row or row[0].strip() == "path_pattern":
            continue
        if len(row) < 4:
            return False, f"第 {i + 1} 行格式错误：需要 4 列，实际 {len(row)} 列", []
        path_pat, methods_str, ip_cidr, description = (col.strip() for col in row[:4])
        if not path_pat:
            return False, f"第 {i + 1} 行：path_pattern 不能为空", []
        if not ip_cidr:
            return False, f"第 {i + 1} 行：ip_cidr 不能为空", []
        try:
            network = ipaddress.ip_network(ip_cidr, strict=False)
        except ValueError:
            return False, f"第 {i + 1} 行：无效的 IP 或 CIDR：{ip_cidr!r}", []
        methods: set[str] = set()
        if methods_str and methods_str != "*":
            methods = {m.strip().upper() for m in methods_str.split("|")}
        rules.append(WhitelistRule(
            path_pattern=path_pat,
            methods=methods,
            network=network,
            description=description,
        ))
    return True, "", rules


def check_request(
    rules: list[WhitelistRule],
    path: str,
    method: str,
    client_ip: str,
) -> tuple[bool, str]:
    """检查请求是否通过白名单。返回 (allow, reason)；允许时 reason 为空字符串。"""
    path_rules = [r for r in rules if fnmatch(path, r.path_pattern)]
    if not path_rules:
        return True, ""
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False, "无法确定客户端 IP 地址"
    ip_rules = [r for r in path_rules if addr in r.network]
    if not ip_rules:
        return False, "不在 IP 白名单范围内"
    method_upper = method.upper()
    for r in ip_rules:
        if not r.methods or method_upper in r.methods:
            return True, ""
    return False, f"该 IP 不允许使用 {method.upper()} 方法"
