#!/usr/bin/env python3
import asyncio
import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import feedparser
import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

ROOT = Path(__file__).resolve().parents[1]
LIBRARIES_PATH = ROOT / "libraries.json"
OUTPUT_PATH = ROOT / "docs" / "results.json"

MAX_BOOKS = int(os.getenv("MAX_BOOKS", "40"))
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "25000"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "3"))


class AvailabilityStatus(str, Enum):
    AVAILABLE = "available"
    HOLD = "hold"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass
class Book:
    title: str
    author: Optional[str]
    goodreads_id: Optional[str]


@dataclass
class CheckResult:
    library_name: str
    library_base_url: str
    status: str
    search_url: str
    libby_url: Optional[str]
    message: Optional[str] = None


def normalize_title(raw: str) -> str:
    cleaned = re.sub(r"^[★☆]+\s*", "", (raw or "").strip())
    return re.sub(r"\s+", " ", cleaned)


def extract_author(title: str) -> tuple[str, Optional[str]]:
    match = re.match(r"^(.+?)\s+by\s+(.+)$", title, flags=re.IGNORECASE)
    if not match:
        return title, None
    return match.group(1).strip(), match.group(2).strip()


def build_search_url(base_url: str, title: str, author: Optional[str]) -> str:
    parts = [title]
    if author:
        parts.append(author)
    query = " ".join(parts)
    query = re.sub(r"[^\w\s]", " ", query)
    query = re.sub(r"\s+", " ", query).strip().replace(" ", "+")
    return f"{base_url.rstrip('/')}/search?query={query}"


async def fetch_goodreads_books(rss_url: str) -> list[Book]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(rss_url, follow_redirects=True, timeout=30)
        resp.raise_for_status()

    feed = feedparser.parse(resp.text)
    books: list[Book] = []
    for entry in feed.entries:
        raw_title = entry.get("title", "")
        title = normalize_title(raw_title)
        author = entry.get("author_name")
        if not author:
            title, author = extract_author(title)

        goodreads_id = None
        link = entry.get("link", "")
        m = re.search(r"/show/(\d+)", link)
        if m:
            goodreads_id = m.group(1)

        if title:
            books.append(Book(title=title, author=author, goodreads_id=goodreads_id))

    return books[:MAX_BOOKS]


async def check_single(page, library: dict, book: Book) -> CheckResult:
    search_url = build_search_url(library["base_url"], book.title, book.author)
    try:
        await page.goto(search_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        media_id = None
        try:
            media_id = await page.locator("[data-media-id]").first.get_attribute("data-media-id")
        except Exception:
            pass
        if not media_id:
            try:
                href = await page.locator('a[href*="/media/"]').first.get_attribute("href")
                if href:
                    m = re.search(r"/media/(\d+)", href)
                    if m:
                        media_id = m.group(1)
            except Exception:
                pass

        libby_url = f"https://share.libbyapp.com/title/{media_id}" if media_id else None

        available_selectors = [
            ".is-borrow",
            ".js-borrow",
            'a[aria-label*="Borrow"]',
            'button:has-text("Borrow")',
            'a:has-text("Borrow")',
        ]
        for selector in available_selectors:
            if await page.locator(selector).count() > 0:
                return CheckResult(
                    library_name=library["name"],
                    library_base_url=library["base_url"],
                    status=AvailabilityStatus.AVAILABLE.value,
                    search_url=search_url,
                    libby_url=libby_url,
                    message="Available to borrow",
                )

        hold_selectors = [
            ".is-hold",
            ".js-hold",
            'a[aria-label*="Place a hold"]',
            'button:has-text("Place a Hold")',
            'button:has-text("Join Waitlist")',
            'a:has-text("Place a hold")',
        ]
        for selector in hold_selectors:
            if await page.locator(selector).count() > 0:
                return CheckResult(
                    library_name=library["name"],
                    library_base_url=library["base_url"],
                    status=AvailabilityStatus.HOLD.value,
                    search_url=search_url,
                    libby_url=libby_url,
                    message="Available to place hold",
                )

        text = (await page.content()).lower()
        for phrase in ["no results found", "didn't match any titles", "no titles found"]:
            if phrase in text:
                return CheckResult(
                    library_name=library["name"],
                    library_base_url=library["base_url"],
                    status=AvailabilityStatus.NOT_FOUND.value,
                    search_url=search_url,
                    libby_url=libby_url,
                    message="No matching title found",
                )

        return CheckResult(
            library_name=library["name"],
            library_base_url=library["base_url"],
            status=AvailabilityStatus.UNKNOWN.value,
            search_url=search_url,
            libby_url=libby_url,
            message="Result found but status unclear",
        )

    except PlaywrightTimeout:
        return CheckResult(
            library_name=library["name"],
            library_base_url=library["base_url"],
            status=AvailabilityStatus.ERROR.value,
            search_url=search_url,
            libby_url=None,
            message="Timeout loading library search page",
        )
    except Exception as exc:
        return CheckResult(
            library_name=library["name"],
            library_base_url=library["base_url"],
            status=AvailabilityStatus.ERROR.value,
            search_url=search_url,
            libby_url=None,
            message=str(exc),
        )


async def run_checks(books: list[Book], libraries: list[dict]) -> dict:
    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})

        async def check_book(book: Book) -> dict:
            async with sem:
                page = await context.new_page()
                try:
                    checks = []
                    for library in libraries:
                        checks.append(await check_single(page, library, book))

                    return {
                        "title": book.title,
                        "author": book.author,
                        "goodreads_id": book.goodreads_id,
                        "checks": [asdict(c) for c in checks],
                    }
                finally:
                    await page.close()

        try:
            rows = await asyncio.gather(*(check_book(book) for book in books))
        finally:
            await context.close()
            await browser.close()

    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "books_checked": len(rows),
        "libraries_checked": len(libraries),
        "books": rows,
    }


def load_libraries() -> list[dict]:
    if not LIBRARIES_PATH.exists():
        raise FileNotFoundError(f"Missing {LIBRARIES_PATH}")
    data = json.loads(LIBRARIES_PATH.read_text())
    if not isinstance(data, list) or not data:
        raise ValueError("libraries.json must contain a non-empty list")
    for item in data:
        if "name" not in item or "base_url" not in item:
            raise ValueError("Each library must include name and base_url")
    return data


async def main() -> None:
    rss_url = os.getenv("GOODREADS_RSS_URL", "").strip()
    if not rss_url:
        raise RuntimeError("GOODREADS_RSS_URL environment variable is required")

    libraries = load_libraries()
    books = await fetch_goodreads_books(rss_url)
    report = await run_checks(books, libraries)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2))
    print(f"Wrote report: {OUTPUT_PATH} ({report['books_checked']} books)")


if __name__ == "__main__":
    asyncio.run(main())
