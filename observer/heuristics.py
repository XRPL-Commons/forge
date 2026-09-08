import re

class Flag:
    def __init__(self, name, evidence):
        self.name = name
        self.evidence = evidence[:120]
    def __eq__(self, o):
        return isinstance(o, Flag) and self.name == o.name and self.evidence == o.evidence
    def __repr__(self):
        return f"Flag({self.name!r}, {self.evidence!r})"

# (name, compiled regex). Each pattern is matched against individual added lines.
_PATTERNS = [
    ("pipe_to_shell", re.compile(r"(curl|wget|fetch)\b[^\n|]*\|\s*(sh|bash|zsh)\b")),
    ("cron_write", re.compile(r"(/etc/cron|crontab|/var/spool/cron|\*\s*\*\s*\*\s*\*)")),
    ("authorized_keys", re.compile(r"authorized_keys")),
    ("ld_preload", re.compile(r"/etc/ld\.so\.preload")),
    ("base64_blob", re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")),
    ("base64_decode_exec", re.compile(r"base64\s+(-d|--decode)\b.*\|\s*(sh|bash)")),
    ("miner_string", re.compile(r"\b(xmrig|stratum\+tcp|kworker|minerd|cpuminer)\b", re.I)),
    ("raw_ip_url", re.compile(r"https?://\d{1,3}(\.\d{1,3}){3}")),
    ("suspicious_tld", re.compile(r"https?://[^\s/]+\.(tk|top|xyz|ru|cc)\b", re.I)),
    ("ci_workflow_change", re.compile(r"\.github/workflows/")),
    ("dependency_change", re.compile(r"(package\.json|pnpm-lock\.yaml|requirements\.txt|package-lock\.json|Cargo\.toml)")),
    ("curl_to_file_exec", re.compile(r"(curl|wget)\b.*-o\s*\S+.*(chmod|\.sh)")),
]

def scan_diff(diff):
    flags = []
    seen = set()
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        for name, rx in _PATTERNS:
            m = rx.search(added)
            if m:
                key = (name, m.group(0))
                if key not in seen:
                    seen.add(key)
                    flags.append(Flag(name, m.group(0)))
    return flags
