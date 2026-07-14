#!/usr/bin/env python3
"""
대법원 파산·회생 자산매각 공고 스크래퍼 v7
- docs/files/ 에 이미 있는 파일 자동으로 local 값 복원
- 텍스트(제목/법원/기관) 항상 새로 수집 → 한글 정상화
- 파일은 local 없는 것만 다운로드 → 타임아웃 방지
"""

import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent
DATA_DIR     = BASE_DIR / "data"
FILES_DIR    = BASE_DIR / "docs" / "files"
NOTICES_JSON = DATA_DIR / "notices.json"

DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

# ── URL 상수 ──────────────────────────────────────────────────────────────────
BASE_URL     = "https://www.scourt.go.kr"
LIST_URL     = BASE_URL + "/portal/notice/realestate/RealNoticeList.work"
VIEW_URL     = BASE_URL + "/portal/notice/realestate/RealNoticeView.work"
DOWNLOAD_URL = BASE_URL + "/portal/notice/realestate/RealNoticeFileDown.work"

# ── Cloudflare Worker 프록시 ──────────────────────────────────────────────────
PROXY_BASE = "https://scourt-proxy.gumago1357.workers.dev"

def proxy(url: str) -> str:
    return f"{PROXY_BASE}/?url={quote(url, safe='')}"

# ── 딜레이 설정 ───────────────────────────────────────────────────────────────
DELAY_LIST        = 2.0
DELAY_DETAIL      = 2.0
DELAY_FILE        = 5.0
DELAY_RETRY_PAUSE = 30.0

# ── HTTP 세션 ─────────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
})

def get_page(url: str, params: dict = None, retries: int = 3) -> BeautifulSoup | None:
    if params:
        qs  = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    proxied_url = proxy(url)
    for attempt in range(retries):
        try:
            resp = SESSION.get(proxied_url, timeout=30)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            wait = 5 * (attempt + 1)
            print(f"  [재시도 {attempt+1}/{retries}] {e} → {wait}초 후")
            time.sleep(wait)
    return None

# ── 파일 유효성 검사 ──────────────────────────────────────────────────────────
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

def local_file_ok(local_val: str) -> bool:
    if not local_val or local_val == "FAILED":
        return False
    path = FILES_DIR / local_val
    return path.exists() and not is_broken_file(path)

# ── docs/files/ 에서 파일ID로 기존 파일 찾기 ─────────────────────────────────
def build_existing_files_index() -> dict[str, str]:
    """
    docs/files/ 폴더에 있는 파일들을 스캔해서
    { 파일ID: 파일명 } 딕셔너리 반환
    파일명 형식: {seq_id}_{file_id}.{ext} 또는 {file_id}.{ext}
    """
    index = {}
    if not FILES_DIR.exists():
        return index
    for f in FILES_DIR.iterdir():
        if f.is_file() and not is_broken_file(f):
            # 파일ID 추출: 파일명에서 숫자로 된 긴 ID 찾기
            stem = f.stem  # 확장자 제외
            # 형식: seq_id_file_id 또는 file_id
            parts = stem.split("_")
            for part in parts:
                if len(part) >= 10 and part.isdigit():
                    index[part] = f.name
    return index

# ── 목록 수집 ─────────────────────────────────────────────────────────────────
def get_total_pages(soup: BeautifulSoup) -> int:
    nums = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"pageIndex=(\d+)", a["href"])
        if m:
            nums.append(int(m.group(1)))
    for tag in soup.find_all(onclick=True):
        m = re.search(r"pageIndex=(\d+)|goPage\((\d+)\)", tag.get("onclick", ""))
        if m:
            nums.append(int(m.group(1) or m.group(2)))
    return max(nums) if nums else 1

def parse_list_page(soup: BeautifulSoup) -> list[dict]:
    notices = []
    if not soup:
        return notices
    for a in soup.find_all("a", href=True):
        href    = a["href"]
        onclick = a.get("onclick", "")
        m = re.search(r"seq_id=(\d+)", href + onclick)
        if not m:
            continue
        seq_id = m.group(1)
        title  = a.get_text(strip=True)
        if not title or len(title) < 3:
            continue
        tr = a.find_parent("tr")
        if not tr:
            continue
        tds   = tr.find_all("td")
        texts = [td.get_text(strip=True) for td in tds]
        date  = ""
        for t in texts:
            if re.match(r"\d{4}\.\d{2}\.\d{2}", t):
                date = t
                break
        notices.append({
            "id":     seq_id,
            "seq_id": seq_id,
            "court":  texts[1] if len(texts) > 1 else "",
            "org":    texts[2] if len(texts) > 2 else "",
            "title":  title,
            "date":   date,
            "files":  [],
        })
    return notices

def scrape_all_list() -> list[dict]:
    print(f"  URL: {LIST_URL}")
    soup = get_page(LIST_URL)
    if not soup:
        print("  [오류] 목록 페이지 로드 실패")
        return []
    total_pages = get_total_pages(soup)
    print(f"  → 총 {total_pages}페이지")
    all_notices = parse_list_page(soup)
    for pg in range(2, total_pages + 1):
        print(f"  목록 {pg}/{total_pages}...", end="\r", flush=True)
        s = get_page(LIST_URL, params={"pageIndex": pg})
        if s:
            all_notices.extend(parse_list_page(s))
        time.sleep(DELAY_LIST)
    print(f"\n  → 총 {len(all_notices)}건")
    return all_notices

# ── 상세 수집 ─────────────────────────────────────────────────────────────────
def scrape_detail(seq_id: str, files_index: dict) -> dict:
    """
    상세 페이지 수집.
    files_index: { 파일ID: 파일명 } — 이미 받은 파일 복원용
    """
    params = {
        "pageIndex": "1", "seq_id": seq_id,
        "bub_cd": "", "searchWord": "", "searchOption": "",
    }
    detail_url = VIEW_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    result = {"files": [], "end_date": "", "phone": "", "detail_url": detail_url}

    soup = get_page(VIEW_URL, params=params)
    if not soup:
        return result

    for th in soup.find_all("th"):
        label = th.get_text(strip=True)
        td    = th.find_next_sibling("td")
        if not td:
            continue
        if label == "공고만료일":
            result["end_date"] = td.get_text(strip=True)
        elif label == "전화번호":
            result["phone"] = td.get_text(strip=True)

    dl_pattern = re.compile(
        r"download\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", re.IGNORECASE
    )
    seen_ids = set()
    for m in dl_pattern.finditer(str(soup)):
        file_id   = m.group(1).strip()
        file_name = m.group(2).strip()
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)

        ext = "pdf"
        nl  = file_name.lower()
        if nl.endswith((".hwp", ".hwpx")):
            ext = "hwp"
        elif nl.endswith((".docx", ".doc")):
            ext = "docx"

        local_name = f"{file_id}.{ext}"

        # 이미 받은 파일인지 확인 (files_index에서 찾기)
        local_val = ""
        if file_id in files_index:
            existing = files_index[file_id]
            if local_file_ok(FILES_DIR / existing):
                local_val = existing
                print(f"    [복원] {existing}")

        result["files"].append({
            "id":         file_id,
            "name":       file_name,
            "url":        f"{DOWNLOAD_URL}?seq_id={seq_id}&file_id={file_id}",
            "ext":        ext,
            "local":      local_val,
            "local_name": local_name,
        })

    if result["files"]:
        ok    = sum(1 for f in result["files"] if f["local"])
        total = len(result["files"])
        print(f"    → 첨부파일 {total}개 (복원 {ok}개)")

    return result

# ── 파일 다운로드 (Playwright) ────────────────────────────────────────────────
BROWSER_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

async def try_download_once(page: Page, seq_id: str, file_info: dict) -> str:
    file_id    = file_info["id"]
    file_name  = file_info["name"]
    file_url   = file_info["url"]
    local_name = file_info.get("local_name") or f"{file_id}.pdf"
    local_path = FILES_DIR / local_name

    detail_url = (
        f"{VIEW_URL}?pageIndex=1&seq_id={seq_id}"
        f"&bub_cd=&searchWord=&searchOption="
    )

    # 방법 1: javascript:download() 트리거
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        fn_exists = await page.evaluate("typeof download === 'function'")
        if fn_exists:
            async with page.expect_download(timeout=90_000) as dl_info:
                await page.evaluate(f"download('{file_id}', '{file_name}')")
            dl  = await dl_info.value
            tmp = Path(await dl.path())
            if tmp and tmp.exists():
                shutil.move(str(tmp), str(local_path))
                if not is_broken_file(local_path):
                    size_kb = local_path.stat().st_size // 1024
                    print(f"    [완료] {local_name} ({size_kb} KB)")
                    return local_name
                local_path.unlink(missing_ok=True)
    except PWTimeout:
        print(f"    [타임아웃] download() 방식")
    except Exception as e:
        print(f"    [오류] download(): {e}")

    # 방법 2: 직접 URL
    try:
        async with page.expect_download(timeout=60_000) as dl_info:
            await page.goto(file_url, wait_until="domcontentloaded", timeout=60000)
        dl  = await dl_info.value
        tmp = Path(await dl.path())
        if tmp and tmp.exists():
            shutil.move(str(tmp), str(local_path))
            if not is_broken_file(local_path):
                size_kb = local_path.stat().st_size // 1024
                print(f"    [완료/직접] {local_name} ({size_kb} KB)")
                return local_name
            local_path.unlink(missing_ok=True)
    except PWTimeout:
        print(f"    [타임아웃] 직접 URL")
    except Exception as e:
        print(f"    [오류] 직접 URL: {e}")

    return ""

async def download_file_with_retry(page: Page, seq_id: str, file_info: dict) -> str:
    # 1차 3번
    for i in range(1, 4):
        print(f"    시도 {i}/3 (1차)")
        result = await try_download_once(page, seq_id, file_info)
        if result:
            return result
        if i < 3:
            await asyncio.sleep(DELAY_FILE)

    # 30초 대기 후 2차 3번
    print(f"    → 1차 실패. {int(DELAY_RETRY_PAUSE)}초 대기 후 2차...")
    await asyncio.sleep(DELAY_RETRY_PAUSE)

    for i in range(1, 4):
        print(f"    시도 {i}/3 (2차)")
        result = await try_download_once(page, seq_id, file_info)
        if result:
            return result
        if i < 3:
            await asyncio.sleep(DELAY_FILE)

    print(f"    → 최종 실패 → FAILED")
    return "FAILED"

# ── 데이터 저장/로드 ──────────────────────────────────────────────────────────
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
    print(f"  [저장] notices.json → {len(notices)}건")

# ── 메인 ─────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("대법원 파산·회생 자산매각 공고 스크래퍼 v7")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1단계: 깨진 파일 정리
    print("\n[1단계] 깨진 파일 정리")
    removed = purge_broken_files()
    print(f"  → {removed}개 삭제")

    # 2단계: docs/files/ 스캔해서 기존 파일 인덱스 구축
    print("\n[2단계] 기존 파일 인덱스 구축")
    files_index = build_existing_files_index()
    print(f"  → {len(files_index)}개 파일 발견")

    # 3단계: 기존 notices.json 로드
    existing = load_existing()
    print(f"\n[3단계] 기존 공고 데이터: {len(existing)}건")

    # 4단계: 목록 수집
    print(f"\n[4단계] 목록 수집 (Cloudflare 프록시)")
    all_raw = scrape_all_list()

    # 5단계: 상세 수집 (전부 새로 수집 → 한글 정상화)
    # 단, 파일 local 값은 files_index로 복원
    print(f"\n[5단계] 상세 정보 수집 (한글 재수집)")
    final: list[dict] = []
    rescrape_count = 0

    for idx, raw in enumerate(all_raw, 1):
        nid = raw["id"]
        print(f"  [{idx:3d}/{len(all_raw)}] {raw['title'][:50]}")

        # 상세 페이지 항상 새로 수집 (한글 정상화)
        detail = scrape_detail(nid, files_index)
        raw.update(detail)
        final.append(raw)
        rescrape_count += 1
        time.sleep(DELAY_DETAIL)

        if idx % 50 == 0:
            save_notices(final)
            print(f"  [중간저장] {idx}건")

    save_notices(final)
    print(f"  → 재수집 완료: {rescrape_count}건")

    # 6단계: 파일 다운로드 (local 없는 것만)
    need_download = [
        n for n in final
        if any(
            fi.get("id") and not local_file_ok(fi.get("local", ""))
            for fi in n.get("files", [])
        )
    ]

    print(f"\n[6단계] 파일 다운로드 (Playwright)")
    print(f"  → 다운로드 필요: {len(need_download)}건")

    file_ok   = 0
    file_fail = 0

    if need_download:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
            context = await browser.new_context(
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                user_agent=USER_AGENT,
                accept_downloads=True,
                extra_http_headers={
                    "Accept-Language": "ko-KR,ko;q=0.9",
                    "Referer": BASE_URL,
                },
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            for ni, n in enumerate(need_download, 1):
                nid = n["id"]
                print(f"\n  [{ni}/{len(need_download)}] {n['title'][:45]}")
                for fi in n.get("files", []):
                    if not fi.get("id"):
                        continue
                    if local_file_ok(fi.get("local", "")):
                        print(f"    [스킵] {fi['local']}")
                        continue
                    result = await download_file_with_retry(page, nid, fi)
                    fi["local"] = result
                    if result and result != "FAILED":
                        file_ok += 1
                    else:
                        file_fail += 1
                    await asyncio.sleep(DELAY_FILE)

                if ni % 10 == 0:
                    save_notices(final)
                    print(f"  [중간저장] {ni}건 처리")

            await browser.close()

        save_notices(final)

    failed_total = sum(
        1 for n in final
        for fi in n.get("files", [])
        if fi.get("local") == "FAILED"
    )

    print("\n" + "=" * 60)
    print(f"완료!")
    print(f"  재수집 공고:      {rescrape_count}건")
    print(f"  파일 복원:        {len(files_index)}개")
    print(f"  파일 신규 성공:   {file_ok}개")
    print(f"  파일 실패(FAILED): {file_fail}개")
    print(f"  전체 FAILED 누적: {failed_total}개")
    print(f"  전체 공고:        {len(final)}건")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
