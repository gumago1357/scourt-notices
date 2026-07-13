#!/usr/bin/env python3
"""
대법원 파산·회생 자산매각 공고 스크래퍼 v2 (Playwright)
- 실제 URL 구조 반영: RealNoticeList.work / RealNoticeView.work
- 파일 다운로드: RealNoticeFileDown.work?seq_id=...&file_id=...
"""

import asyncio
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

BASE_DIR     = Path(__file__).parent.parent
DATA_DIR     = BASE_DIR / "data"
FILES_DIR    = BASE_DIR / "docs" / "files"
NOTICES_JSON = DATA_DIR / "notices.json"

DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL     = "https://www.scourt.go.kr"
LIST_URL     = BASE_URL + "/portal/notice/realestate/RealNoticeList.work"
VIEW_URL     = BASE_URL + "/portal/notice/realestate/RealNoticeView.work"
DOWNLOAD_URL = BASE_URL + "/portal/notice/realestate/RealNoticeFileDown.work"

MIN_VALID_BYTES = 5_000
HTML_SIGNATURES = (b"<!DOCTYPE", b"<html", b"<HTML", b"<!doctype")

def is_broken_file(path: Path) -> bool:
    if not path.exists():
        return False
    if path.stat().st_size < MIN_VALID_BYTES:
        return True
    try:
        with open(path, "rb") as f:
            header = f.read(512)
        return any(sig in header for sig in HTML_SIGNATURES)
    except OSError:
        return False

def purge_broken_files() -> int:
    removed = 0
    if not FILES_DIR.exists():
        return 0
    for f in FILES_DIR.iterdir():
        if f.is_file() and is_broken_file(f):
            size = f.stat().st_size
            f.unlink()
            print(f"  [삭제] {f.name} ({size} bytes)")
            removed += 1
    return removed

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

async def get_total_pages(page: Page) -> int:
    try:
        content = await page.content()
        nums = re.findall(r"pageIndex=(\d+)", content)
        if nums:
            return max(int(n) for n in nums)
    except Exception:
        pass
    return 1

async def scrape_list_page(page: Page, page_idx: int) -> list[dict]:
    if page_idx > 1:
        url = f"{LIST_URL}?pageIndex={page_idx}"
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)

    content = await page.content()
    notices = []

    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    link_pattern = re.compile(r'seq_id=(\d+)', re.IGNORECASE)
    td_pattern   = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
    tag_pattern  = re.compile(r'<[^>]+>')

    for row_m in row_pattern.finditer(content):
        row_html = row_m.group(1)
        link_m   = link_pattern.search(row_html)
        if not link_m:
            continue

        seq_id = link_m.group(1)
        tds    = td_pattern.findall(row_html)
        if len(tds) < 4:
            continue

        def clean(html):
            return tag_pattern.sub('', html).strip()

        texts = [clean(td) for td in tds]

        title = ""
        for td in tds:
            if 'seq_id=' in td:
                title = clean(td)
                break
        if not title and texts:
            title = max(texts, key=len)

        date = ""
        for t in texts:
            if re.match(r'\d{4}\.\d{2}\.\d{2}', t):
                date = t
                break

        notices.append({
            "id":    seq_id,
            "seq_id": seq_id,
            "court": texts[1] if len(texts) > 1 else "",
            "org":   texts[2] if len(texts) > 2 else "",
            "title": title,
            "date":  date,
            "files": [],
        })

    return notices

async def scrape_detail(page: Page, seq_id: str) -> dict:
    detail_url = f"{VIEW_URL}?pageIndex=1&seq_id={seq_id}&bub_cd=&searchWord=&searchOption="

    for attempt in range(2):
        try:
            await page.goto(
                detail_url,
                wait_until="domcontentloaded" if attempt else "networkidle",
                timeout=60000,
            )
            break
        except PWTimeout:
            if attempt == 1:
                return {"files": [], "end_date": "", "phone": "", "detail_url": detail_url}

    content = await page.content()
    result  = {"files": [], "end_date": "", "phone": "", "detail_url": detail_url}

    for label, key in [("공고만료일", "end_date"), ("전화번호", "phone")]:
        m = re.search(label + r'</th>\s*<td[^>]*>(.*?)</td>', content, re.DOTALL | re.IGNORECASE)
        if m:
            result[key] = re.sub(r'<[^>]+>', '', m.group(1)).strip()

    dl_pattern = re.compile(r"download\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", re.IGNORECASE)
    seen_ids   = set()
    for m in dl_pattern.finditer(content):
        file_id   = m.group(1).strip()
        file_name = m.group(2).strip()
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)

        ext = ""
        nl  = file_name.lower()
        if nl.endswith(".pdf"):
            ext = "pdf"
        elif nl.endswith((".hwp", ".hwpx")):
            ext = "hwp"
        elif nl.endswith((".docx", ".doc")):
            ext = "docx"

        result["files"].append({
            "id":    file_id,
            "name":  file_name,
            "url":   f"{DOWNLOAD_URL}?seq_id={seq_id}&file_id={file_id}",
            "ext":   ext,
            "local": "",
        })

    if result["files"]:
        print(f"    → 첨부파일 {len(result['files'])}개: {[f['name'] for f in result['files']]}")

    return result

async def download_file(page: Page, seq_id: str, file_info: dict) -> str:
    file_id   = file_info.get("id", "")
    file_name = file_info.get("name", "unknown")
    file_url  = file_info.get("url", "")

    if not file_id or not file_url:
        return ""

    safe_name  = re.sub(r'[\\/*?:"<>|\s]', "_", file_name)
    local_name = f"{seq_id}_{file_id}_{safe_name}"
    local_path = FILES_DIR / local_name

    if local_path.exists() and not is_broken_file(local_path):
        size_kb = local_path.stat().st_size // 1024
        print(f"    [스킵] {local_name} ({size_kb} KB)")
        return local_name

    detail_url = f"{VIEW_URL}?pageIndex=1&seq_id={seq_id}&bub_cd=&searchWord=&searchOption="

    # 방법 1: javascript:download() 트리거
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        fn_exists = await page.evaluate("typeof download === 'function'")
        if fn_exists:
            async with page.expect_download(timeout=90_000) as dl_info:
                await page.evaluate(f"download('{file_id}', '{file_name}')")
            dl         = await dl_info.value
            suggested  = dl.suggested_filename or file_name
            safe_sug   = re.sub(r'[\\/*?:"<>|\s]', "_", suggested)
            local_name = f"{seq_id}_{file_id}_{safe_sug}"
            local_path = FILES_DIR / local_name
            tmp        = Path(await dl.path())
            if tmp and tmp.exists():
                shutil.move(str(tmp), str(local_path))
                if not is_broken_file(local_path):
                    print(f"    [완료] {local_name} ({local_path.stat().st_size//1024} KB)")
                    return local_name
                local_path.unlink(missing_ok=True)
    except PWTimeout:
        print(f"    [타임아웃] download() 방식: {file_name}")
    except Exception as e:
        print(f"    [오류] download() 방식: {e}")

    # 방법 2: 직접 URL 접근
    try:
        async with page.expect_download(timeout=60_000) as dl_info:
            await page.goto(file_url, wait_until="domcontentloaded", timeout=60000)
        dl         = await dl_info.value
        suggested  = dl.suggested_filename or file_name
        safe_sug   = re.sub(r'[\\/*?:"<>|\s]', "_", suggested)
        local_name = f"{seq_id}_{file_id}_{safe_sug}"
        local_path = FILES_DIR / local_name
        tmp        = Path(await dl.path())
        if tmp and tmp.exists():
            shutil.move(str(tmp), str(local_path))
            if not is_broken_file(local_path):
                print(f"    [완료/직접] {local_name} ({local_path.stat().st_size//1024} KB)")
                return local_name
            local_path.unlink(missing_ok=True)
    except PWTimeout:
        print(f"    [타임아웃] 직접 URL: {file_name}")
    except Exception as e:
        print(f"    [오류] 직접 URL: {e}")

    print(f"    [실패] {file_name}")
    return ""

def load_existing() -> dict[str, dict]:
    if not NOTICES_JSON.exists():
        return {}
    try:
        with open(NOTICES_JSON, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("notices", [])
        return {n["id"]: n for n in items if "id" in n}
    except Exception as e:
        print(f"[경고] 기존 데이터 로드 실패: {e}")
        return {}

def save_notices(notices: list[dict]):
    output = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count":   len(notices),
        "notices": notices,
    }
    with open(NOTICES_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[저장] notices.json → {len(notices)}건")

async def main():
    print("=" * 60)
    print("대법원 파산·회생 자산매각 공고 스크래퍼 v2")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    print("\n[1단계] 깨진 파일 정리")
    removed = purge_broken_files()
    print(f"  → {removed}개 삭제")

    existing = load_existing()
    print(f"\n[2단계] 기존 공고: {len(existing)}건")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=USER_AGENT,
            accept_downloads=True,
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Referer": BASE_URL,
            },
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        print(f"\n[3단계] 목록 수집")
        await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)

        total_pages = await get_total_pages(page)
        print(f"  → 총 {total_pages}페이지")

        all_raw: list[dict] = []
        for pg in range(1, total_pages + 1):
            print(f"  목록 {pg}/{total_pages}...", end="\r", flush=True)
            rows = await scrape_list_page(page, pg)
            all_raw.extend(rows)
            if pg < total_pages:
                await asyncio.sleep(1.0)

        print(f"\n  → 총 {len(all_raw)}건")

        print(f"\n[4단계] 상세 + 파일 다운로드")
        final: list[dict] = []
        new_count  = 0
        file_count = 0

        for idx, raw in enumerate(all_raw, 1):
            nid = raw["id"]
            print(f"\n  [{idx:3d}/{len(all_raw)}] {raw['title'][:50]}")

            if nid in existing:
                notice  = existing[nid]
                missing = [fi for fi in notice.get("files", [])
                           if fi.get("id") and not fi.get("local")]
                if not missing:
                    print("    → 스킵")
                    final.append(notice)
                    continue
            else:
                detail = await scrape_detail(page, nid)
                raw.update(detail)
                notice = raw
                new_count += 1
                await asyncio.sleep(1.0)

            for fi in notice.get("files", []):
                if not fi.get("id") or fi.get("local"):
                    continue
                local = await download_file(page, nid, fi)
                if local:
                    fi["local"] = local
                    file_count += 1
                await asyncio.sleep(1.5)

            final.append(notice)

        await browser.close()

    print(f"\n[5단계] 저장")
    save_notices(final)

    print("\n" + "=" * 60)
    print(f"완료! 신규: {new_count}건 | 파일: {file_count}개 | 전체: {len(final)}건")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
