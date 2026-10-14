"""Publish into throwaway Git repositories and check the static page's local links."""

import os
import re
import shutil
import stat
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SITE = PROJECT / "site"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def site_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy2(PROJECT / "deploy/site.sh", repo / "deploy/site.sh")
    shutil.copytree(SITE, repo / "site")
    (repo / ".gitignore").write_text("site/ignored.txt\n")
    git(repo, "init", "--quiet")
    git(repo, "config", "user.email", "site-test@example.invalid")
    git(repo, "config", "user.name", "Site test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "test: static site fixture")
    return repo


def publish(repo: Path, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "deploy/site.sh"), *args],
        cwd=root.parent,
        env=os.environ | {"ORULON_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_publish_flips_current_and_keeps_previous_release(site_repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "served site"
    # Copied files can have restrictive modes despite the publisher's umask.
    (site_repo / "site/assets").chmod(0o700)
    (site_repo / "site/assets/style.css").chmod(0o600)
    first = publish(site_repo, root)
    assert first.returncode == 0, first.stderr
    release = Path(first.stdout.strip())
    assert release.parent == root / "releases"
    assert re.fullmatch(
        r"\d{8}T\d{6}Z-" + git(site_repo, "rev-parse", "--short", "HEAD"), release.name
    )
    assert (root / "current").is_symlink()
    assert (root / "current").resolve() == release
    assert (release / "index.html").read_bytes() == (SITE / "index.html").read_bytes()
    assert (release / "404.html").is_file()
    assert (release / "favicon.svg").is_file()
    assert (release / "assets/motion.js").read_bytes() == (SITE / "assets/motion.js").read_bytes()
    assert not (release / "tools").exists()
    for entry in [root, root / "releases", release, *release.rglob("*")]:
        expected = 0o755 if entry.is_dir() else 0o644
        assert stat.S_IMODE(entry.stat().st_mode) == expected, entry

    # A new SHA allows a second publish in the same UTC second, without sleeps.
    page = site_repo / "site/index.html"
    original = page.read_text()
    page.write_text(original + "\n<!-- second release -->\n")
    git(site_repo, "add", "site/index.html")
    git(site_repo, "commit", "--quiet", "-m", "test: second release")
    second = publish(site_repo, root)
    assert second.returncode == 0, second.stderr
    second_release = Path(second.stdout.strip())
    assert second_release != release
    assert (root / "current").resolve() == second_release
    assert (root / "current/index.html").read_text() == page.read_text()
    assert (release / "index.html").read_text() == original
    assert set((root / "releases").iterdir()) == {release, second_release}
    assert not list(root.glob(".publish.*"))


@pytest.mark.parametrize("kind", ["modified", "staged", "untracked", "ignored"])
def test_publish_refuses_dirty_site(site_repo: Path, tmp_path: Path, kind: str) -> None:
    root = tmp_path / "served"
    first = publish(site_repo, root)
    assert first.returncode == 0, first.stderr
    original = (root / "current").resolve()
    if kind in {"modified", "staged"}:
        (site_repo / "site/index.html").write_text("changed")
        if kind == "staged":
            git(site_repo, "add", "site/index.html")
    else:
        (site_repo / "site" / f"{kind}.txt").write_text("uncommitted")
    for args in [(), ("--dry-run",)]:
        result = publish(site_repo, root, *args)
        assert result.returncode != 0
        assert "dirty under site/" in result.stderr
    assert (root / "current").resolve() == original
    assert list((root / "releases").iterdir()) == [original]


@pytest.mark.parametrize("page", ["index.html", "404.html"])
def test_publish_requires_both_pages(site_repo: Path, tmp_path: Path, page: str) -> None:
    (site_repo / "site" / page).unlink()
    root = tmp_path / "served"
    result = publish(site_repo, root)
    assert result.returncode != 0
    assert f"site/{page} is missing" in result.stderr
    assert not root.exists()


def test_dry_run_writes_nothing_and_allows_unrelated_changes(
    site_repo: Path, tmp_path: Path
) -> None:
    (site_repo / "unrelated.txt").write_text("This is outside site/.")
    root = tmp_path / "served"
    result = publish(site_repo, root, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert f"Would atomically replace {root}/current" in result.stdout
    assert "excluding tools/" in result.stdout
    assert not root.exists()
    assert publish(site_repo, root).returncode == 0


class PageLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value is not None:
                if name in {"href", "src"}:
                    self.links.append(value)
                elif name == "id":
                    self.ids.add(value)


@pytest.mark.parametrize("page", sorted(SITE.glob("*.html")), ids=lambda p: p.name)
def test_local_html_links_resolve(page: Path) -> None:
    parsed = PageLinks()
    parsed.feed(page.read_text())
    assert parsed.links
    for link in parsed.links:
        url = urlsplit(link)
        if url.scheme or url.netloc:
            continue
        if not url.path:
            target = page
        elif url.path.startswith("/"):
            target = SITE / unquote(url.path).lstrip("/")
        else:
            target = page.parent / unquote(url.path)
        target = target.resolve()
        assert target.is_relative_to(SITE), (page, link)
        if target.is_dir():
            target /= "index.html"
        assert target.is_file(), (page, link)
        if url.fragment:
            destination = PageLinks()
            destination.feed(target.read_text())
            assert unquote(url.fragment) in destination.ids, (page, link)


def test_static_payload_is_under_400_kb_and_icon_matches() -> None:
    payload = [
        entry
        for entry in SITE.rglob("*")
        if entry.is_file() and "tools" not in entry.relative_to(SITE).parts
    ]
    assert sum(entry.stat().st_size for entry in payload) < 400_000
    assert (SITE / "favicon.svg").read_bytes() == (PROJECT / "assets/icon.svg").read_bytes()


def test_motion_asset_size_and_cache_versions() -> None:
    assert (SITE / "assets/motion.js").stat().st_size <= 2048
    for page in SITE.glob("*.html"):
        parsed = PageLinks()
        parsed.feed(page.read_text())
        assets = [urlsplit(link) for link in parsed.links if link.startswith("/assets/")]
        assert {asset.path for asset in assets} == {"/assets/style.css", "/assets/motion.js"}
        assert {asset.query for asset in assets} == {"v=2"}
