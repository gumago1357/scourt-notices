#!/usr/bin/env python3
"""
대법원 파산·회생 자산매각 공고 스크래퍼 (Playwright 버전)
"""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Download, Page, TimeoutError as PWTimeout

BASE_DIR     = Path(__file__).parent.parent
DATA_DIR     = BASE_DIR / "data"
FILES_DIR    = BASE_DIR / "docs" / "files"
NOTICES_JSON = DATA_DIR / "notices.json"

DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL      = "https://www.scourt.go.kr"
LIST_URL      = f"{BASE_URL}/portal/notice/realestate/RealNoticeList.work"
DETAIL_PREFIX = f"{BASE_URL}/portal/notice/realestate/RealNoticeView.work"

MIN_VALID_BYTES = 5_000
HTML_SIGNATURES = (b"<!DOCTYPE", b"<html", b"<HTML", b"<!doctype")


def is_broken_file(path: Path) -> bool:
    if not path.exists():
        return False
    size = path.stat().st_size
    if size < MIN_VALID_BYTES:
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
            print(f"  [삭제] 깨진 파일: {f.name} ({size} bytes)")
            removed += 1
    return removed


BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1280,900",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


async def get_total_pages(page: Page) -> int:
    try:
        await page.wait_for_selector("table", timeout=15000)
    except PWTimeout:
        return 1

    try:
        hrefs = await page.eval_on_selector_all(
            "a[href*='pageIndex'], a[onclick*='goPage'], a[onclick*='page']",
            "els => els.map(e => e.getAttribute('href') || e.getAttribute('onclick') || '')"
        )
        nums = []
        for h in hrefs:
            for m in re.finditer(r"(?:pageIndex|goPage)\s*[=(,]\s*(\d+)", h):
                nums.append(int(m.group(1)))
        if nums:
            return max(nums)
    except Exception:
        pass

    try:
        paging_text = await page.evaluate("""
            () => {
                const el = document.querySelector('.paging, .pagination, #paging, .page-wrap');
                return el ? el.innerText : document.body.innerText;
            }
        """)
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*페이지", paging_text)
        if m:
            return int(m.group(2))
        nums = re.findall(r"\b(\d+)\b", paging_text)
        if nums:
            return max(int(n) for n in nums if int(n) < 10000)
    except Exception:
        pass

    return 1


async def scrape_list_page(page: Page, page_idx: int) -> list[dict]:
    if page_idx > 1:
        navigated = False
        for expr in [
            f"goPage({page_idx})",
            f"fn_goPage({page_idx})",
            f"goList({page_idx})",
        ]:
            try:
                await page.evaluate(expr)
                await page.wait_for_load_state("networkidle", timeout=20000)
                navigated = True
                break
            except Exception:
                continue

        if not navigated:
            url = f"{LIST_URL}&pageIndex={page_idx}"
            await page.goto(url, wait_until="networkidle", timeout=25000)

    notices = []
    selectors = [
        "table.bbsList tbody tr",
        "table.bbs_list tbody tr",
        "#bbsList tbody tr",
        "table tbody tr",
    ]

    rows = None
    for sel in selectors:
        candidate = page.locator(sel)
        if await candidate.count() > 0:
            rows = candidate
            break

    if rows is None:
        print(f"  [경고] 페이지 {page_idx}: 테이블 행을 찾을 수 없음")
        return []

    count = await rows.count()
    for i in range(count):
        row = rows.nth(i)
        cells = row.locator("td")
        cell_count = await cells.count()
        if cell_count < 4:
            continue

        try:
            title_el  = None
            title     = ""
            notice_id = ""

            for cell_idx in range(cell_count):
                link = cells.nth(cell_idx).locator("a").first
                if await link.count() > 0:
                    t = (await link.inner_text()).strip()
                    if t and len(t) > 3:
                        title_el  = link
                        title     = t
                        href      = await link.get_attribute("href") or ""
                        onclick   = await link.get_attribute("onclick") or ""
                        combined  = href + onclick

                        m = re.search(r"['\"](\d{6,})['\"]", combined)
                        if not m:
                            m = re.search(r"(\d{6,})", combined)
                        if m:
                            notice_id = m.group(1)
                        break

            if not title or not notice_id:
                continue

            texts = []
            for ci in range(cell_count):
                t = (await cells.nth(ci).inner_text()).strip()
                texts.append(t)

            court = texts[1] if len(texts) > 1 else ""
            org   = texts[2] if len(texts) > 2 else ""
            date  = texts[-1] if texts else ""

            notices.append({
                "id":    notice_id,
                "num":   texts[0] if texts else "",
                "court": court,
                "org":   org,
                "title": title,
                "date":  date,
                "files": [],
            })

        except Exception as e:
            print(f"  [경고] 행 {i} 파싱 오류: {e}")

    return notices


async def scrape_detail(page: Page, notice: dict) -> dict:
    notice_id  = notice["id"]
    detail_url = f"{DETAIL_PREFIX}?gubun=DG18&searchSeq={notice_id}"

    for attempt in range(2):
        try:
            await page.goto(
                detail_url,
                wait_until="networkidle" if attempt == 0 else "domcontentloaded",
                timeout=25000,
            )
            break
        except PWTimeout:
            if attempt == 1:
                print(f"  [타임아웃] 상세 페이지 로드 실패: {notice_id}")
                return notice

    content = ""
    for sel in [".bbsView", ".viewContent", "#viewContent", ".view_content", ".bbs_view"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                content = (await el.inner_text()).strip()
                break
        except Exception:
            continue

    files = []
    try:
        html_content = await page.content()
        pattern = re.compile(
            r"download\s*\(\s*['\"]?([A-Za-z0-9_\-]+)['\"]?\s*,\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        seen_ids = set()
        for m in pattern.finditer(html_content):
            fid   = m.group(1).strip()
            fname = m.group(2).strip()
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                files.append({"id": fid, "name": fname, "local": ""})
    except Exception as e:
        print(f"  [경고] 첨부파일 파싱 오류: {e}")

    notice["content"] = content
    notice["files"]   = files

    if files:
        print(f"    → 첨부파일 {len(files)}개: {[f['name'] for f in files]}")

    return notice


async def download_file(page: Page, notice_id: str, file_info: dict) -> str:
    file_id   = file_info.get("id", "")
    file_name = file_info.get("name", "unknown")

    if not file_id:
        return ""

    safe_name  = re.sub(r'[\\/*?:"<>|\s]', "_", file_name)
    local_name = f"{file_id}_{safe_name}"
    local_path = FILES_DIR / local_name

    if local_path.exists() and not is_broken_file(local_path):
        size_kb = local_path.stat().st_size // 1024
        print(f"    [스킵] 이미 존재: {local_name} ({size_kb} KB)")
        return local_name

    detail_url = f"{DETAIL_PREFIX}?gubun=DG18&searchSeq={notice_id}"
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        print(f"    [오류] 상세페이지 이동 실패: {e}")
        return ""

    fn_exists = await page.evaluate("typeof download === 'function'")
    if not fn_exists:
        print(f"    [경고] download() 함수 없음: {file_name}")
        return await download_file_via_form(page, notice_id, file_id, file_name, local_path)

    try:
        async with page.expect_download(timeout=90_000) as dl_info:
            for call in [
                f"download('{file_id}', '{file_name}')",
                f"download('{file_id}')",
                f"fnDownload('{file_id}', '{file_name}')",
            ]:
                try:
                    await page.evaluate(call)
                    break
                except Exception:
                    continue

        dl: Download = await dl_info.value

        suggested = dl.suggested_filename
        if suggested:
            safe_suggested = re.sub(r'[\\/*?:"<>|\s]', "_", suggested)
            local_name = f"{file_id}_{safe_suggested}"
            local_path = FILES_DIR / local_name

        tmp = Path(await dl.path())
        if not tmp or not tmp.exists():
            print(f"    [실패] 임시 파일 없음: {file_name}")
            return ""

        shutil.move(str(tmp), str(local_path))

        if is_broken_file(local_path):
            size = local_path.stat().st_size
            local_path.unlink(missing_ok=True)
            print(f"    [실패] 에러페이지 수신 ({size} bytes): {file_name}")
            return ""

        size_kb = local_path.stat().st_size // 1024
        print(f"    [완료] {local_name} ({size_kb} KB)")
        return local_name

    except PWTimeout:
        print(f"    [타임아웃] 90초 초과: {file_name}")
        return ""
    except Exception as e:
        print(f"    [오류] {file_name}: {type(e).__name__}: {e}")
        return ""


async def download_file_via_form(
    page: Page, notice_id: str, file_id: str, file_name: str, local_path: Path
) -> str:
    DOWNLOAD_ACTION = f"{BASE_URL}/portal/download/FileDownAction.work"

    try:
        async with page.expect_download(timeout=60_000) as dl_info:
            await page.evaluate(f"""
                () => {{
                    const form = document.createElement('form');
                    form.method = 'POST';
                    form.action = '{DOWNLOAD_ACTION}';
                    const inp = document.createElement('input');
                    inp.type  = 'hidden';
                    inp.name  = 'fileId';
                    inp.value = '{file_id}';
                    form.appendChild(inp);
                    document.body.appendChild(form);
                    form.submit();
                }}
            """)

        dl: Download = await dl_info.value
        tmp = Path(await dl.path())
        if tmp and tmp.exists():
            shutil.move(str(tmp), str(local_path))
            if not is_broken_file(local_path):
                size_kb = local_path.stat().st_size // 1024
                print(f"    [완료/폼] {local_path.name} ({size_kb} KB)")
                return local_path.name
            else:
                local_path.unlink(missing_ok=True)

    except Exception as e:
        print(f"    [폼 폴백 실패] {file_name}: {e}")

    return ""


def load_existing_notices() -> dict[str, dict]:
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
    print(f"[저장] {NOTICES_JSON} → {len(notices)}건")


async def main():
    print("=" * 60)
    print("대법원 파산·회생 자산매각 공고 스크래퍼 (Playwright)")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    print("\n[1단계] 깨진 첨부파일 정리")
    removed = purge_broken_files()
    print(f"  → {removed}개 삭제 완료")

    existing = load_existing_notices()
    print(f"\n[2단계] 기존 공고: {len(existing)}건")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=USER_AGENT,
            accept_downloads=True,
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        page = await context.new_page()

        print(f"\n[3단계] 공고 목록 수집")
        await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        total_pages = await get_total_pages(page)
        print(f"  → 총 {total_pages}페이지")

        all_raw: list[dict] = []
        for pg in range(1, total_pages + 1):
            print(f"  수집 중: {pg}/{total_pages} 페이지", end="\r", flush=True)
            rows = await scrape_list_page(page, pg)
            all_raw.extend(rows)
            if pg < total_pages:
                await asyncio.sleep(1.0)

        print(f"\n  → 목록 총 {len(all_raw)}건")

        print(f"\n[4단계] 상세 정보 + 파일 다운로드")
        final_notices: list[dict] = []
        new_count  = 0
        file_count = 0

        for idx, raw in enumerate(all_raw, 1):
            nid = raw["id"]
            print(f"\n  [{idx:3d}/{len(all_raw)}] {raw['title'][:45]}")

            if nid in existing:
                notice = existing[nid]
                missing = [fi for fi in notice.get("files", []) if fi.get("id") and not fi.get("local")]
                if not missing:
                    print("    → 기존 데이터 (스킵)")
                    final_notices.append(notice)
                    continue

            else:
                notice = await scrape_detail(page, raw)
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

            final_notices.append(notice)

        await browser.close()

    print(f"\n[5단계] 결과 저장")
    save_notices(final_notices)

    print("\n" + "=" * 60)
    print(f"완료! 신규: {new_count}건 | 파일: {file_count}개 | 전체: {len(final_notices)}건")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
