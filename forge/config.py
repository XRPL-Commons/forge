"""Parse legacy repository declarations as data, never execute shell config."""
from dataclasses import dataclass, replace
from pathlib import Path
import re
import shlex
from .app_access import REPO_PATTERN


@dataclass(frozen=True)
class Repository:
    repo: str
    owner_label: str
    enabled: bool = True
    author: str = "forge-bot"
    clone_build: str | None = None
    clone_review: str | None = None
    clone_audit: str | None = None

    @property
    def short(self):
        return self.repo.split("/")[-1]


def load_repositories(directory):
    result = []
    for path in sorted(Path(directory).glob("*.repo")):
        values = {}
        for line in path.read_text().splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens:
                continue
            if len(tokens) != 1 or "=" not in tokens[0]:
                raise ValueError(f"Unsupported declaration in {path.name}")
            key, value = tokens[0].split("=", 1)
            if key not in {"REPO", "OWNER_LABEL", "ENABLED", "CLONE_BUILD", "CLONE_REVIEW", "CLONE_AUDIT", "RECOVERY_AUTHOR"}:
                raise ValueError(f"Unknown configuration key {key}")
            if key in values:
                raise ValueError(f"Duplicate configuration key {key}")
            values[key] = value
        repo = values.get("REPO", "")
        owner = values.get("OWNER_LABEL", "")
        if not REPO_PATTERN.fullmatch(repo) or not re.fullmatch(r"auto:owned-by-[\w.-]+", owner):
            raise ValueError(f"Invalid repository/owner in {path.name}")
        enabled = values.get("ENABLED", "true")
        if enabled not in {"true", "false"}:
            raise ValueError(f"Invalid enabled value in {path.name}")
        author = values.get("RECOVERY_AUTHOR", "forge-bot")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\[\]-]*", author):
            raise ValueError(f"Invalid recovery author in {path.name}")
        clones = [values.get(key) or None for key in ("CLONE_BUILD", "CLONE_REVIEW", "CLONE_AUDIT")]
        if any(value is not None and (not Path(value).is_absolute() or any(ord(c) < 32 for c in value)) for value in clones):
            raise ValueError(f"Clone paths must be absolute without control characters in {path.name}")
        result.append(Repository(repo, owner, enabled == "true", author, *clones))
    factory = Path(directory).parent / 'factory.json'
    if factory.exists():
        from .settings import Settings
        settings = Settings(factory.parent)
        configured = settings.load()['repositories']
        legacy = {r.repo.lower(): r for r in result}
        selected = {r['name'].lower() for r in configured}
        # Legacy files retain clone/ownership metadata during migration. The
        # canonical enabled flag comes ONLY from factory.json when present.
        result = [replace(legacy[row['name'].lower()], enabled=row['enabled'])
                  if row['name'].lower() in legacy else
                  Repository(row['name'], 'auto:owned-by-forge', row['enabled'])
                  for row in configured]
        # Preserve visibility of old workers for cleanup, even after deselection.
        result.extend(replace(repo, enabled=False) for name, repo in legacy.items() if name not in selected)
    if len({r.repo.lower() for r in result}) != len(result):
        raise ValueError("Duplicate repository identity")
    # Legacy tmux still uses short names. Reject rather than cross-wire two repos.
    if len({r.short for r in result}) != len(result):
        raise ValueError("Duplicate repository basename requires migration before admission")
    return result


def supervisor_records(directory, home, offset=0):
    """Tab records are data for a quoted shell read, never executable shell text."""
    home = Path(home)
    if not home.is_absolute() or any(ord(c) < 32 for c in str(home)):
        raise ValueError("Absolute home without control characters required")
    repositories = load_repositories(directory)
    if repositories:
        offset %= len(repositories)
        repositories = repositories[offset:] + repositories[:offset]
    return "\n".join("\t".join((r.repo, r.owner_label,
        r.clone_build or str(home / r.short),
        r.clone_review or str(home / (r.short + "-review")),
        r.clone_audit or str(home / (r.short + "-audit")), r.author,
        "true" if r.enabled else "false")) for r in repositories)


def main():
    import argparse
    import json
    from .control import atomic_json
    parser = argparse.ArgumentParser(description="Read repository configuration as data")
    parser.add_argument("--config", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    cursor = Path(args.state) / "supervisor-cursor.json"
    value = json.loads(cursor.read_text()) if cursor.exists() else {"offset": 0}
    if type(value.get("offset")) is not int or value["offset"] < 0:
        raise ValueError("Invalid supervisor cursor")
    records = supervisor_records(args.config, args.home, value["offset"])
    # Supervisor singleton owns this cursor. Advance only after complete validation.
    atomic_json(cursor, {"offset": (value["offset"] + 1) % max(1, len(records.splitlines()))})
    print(records)


if __name__ == "__main__":
    main()
