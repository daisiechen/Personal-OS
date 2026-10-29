"""Markdown Wiki：小伴 维护、Mia 可直接修改的主题页。

每页一个 .md 文件，开头是简单的 frontmatter（title、updated、sources），
页面之间用 [[页面名]] 互相链接。index.md 由程序自动生成。
"""

import re
from datetime import datetime, timezone
from pathlib import Path

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*$")
LINK = re.compile(r"\[\[([^\]]+)\]\]")


class Wiki:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, slug: str) -> Path:
        if not SLUG.match(slug):
            raise ValueError(f"非法页面名：{slug}")
        return self.root / f"{slug}.md"

    def write(self, slug: str, title: str, body: str, sources=()):
        path = self._path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = ["---", f"title: {title}", f"updated: {stamp}"]
        if sources:
            lines.append("sources: " + "; ".join(sources))
        lines += ["---", "", body.strip(), ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        self._rebuild_index()

    def read(self, slug: str) -> dict:
        text = self._path(slug).read_text(encoding="utf-8")
        meta, body = {}, text
        if text.startswith("---\n"):
            head, _, body = text[4:].partition("\n---\n")
            for line in head.splitlines():
                key, _, value = line.partition(": ")
                meta[key] = value
        return {"slug": slug, **meta, "body": body.strip(), "links": LINK.findall(body)}

    def exists(self, slug: str) -> bool:
        return self._path(slug).is_file()

    def delete(self, slug: str):
        self._path(slug).unlink()
        self._rebuild_index()

    def pages(self) -> list:
        return sorted(
            p.relative_to(self.root).with_suffix("").as_posix()
            for p in self.root.rglob("*.md")
            if p.name != "index.md"
        )

    def search(self, query: str) -> list:
        terms = [t.lower() for t in query.split() if t]
        hits = []
        for slug in self.pages():
            text = self._path(slug).read_text(encoding="utf-8").lower()
            score = sum(text.count(t) for t in terms)
            if score:
                hits.append((score, slug))
        return [slug for _, slug in sorted(hits, key=lambda x: (-x[0], x[1]))]

    def _rebuild_index(self):
        lines = ["# Wiki 索引", ""]
        for slug in self.pages():
            lines.append(f"- [[{slug}]] {self.read(slug).get('title', '')}")
        (self.root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
