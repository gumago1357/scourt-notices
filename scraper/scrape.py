#!/usr/bin/env python3
"""
대법원 파산·회생 자산매각 공고 스크래퍼 v8
- v7 대비: 작성일(date) 수집 추가
- v8 대비: monthly_stats.json 누적 저장 추가
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

# ── 경로 설정
BASE_DIR      = Path(__file__).parent.parent
DATA_DIR      = BASE_DIR / "data"
FILES_DIR     = BASE_DIR / "docs" / "files"
NOTICES_JSON  = DATA_DIR / "notices.json"
MONTHLY_JSON  = DATA_DIR / "monthly_stats.json"   # ★ 추가

DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

# ── URL 상수
BASE_URL     = "https://www.scourt.go.kr"
LIST_URL     = BASE_URL + "/portal/notice/realestate/RealNoticeList.work"
VIEW_URL     = BASE_URL + "/portal/notice/realestate/RealNoticeView.work"
DOWNLOAD_URL = BASE_URL + "/portal/notice/realestate/RealNoticeFileDown.work"

# ── Cloudflare Worker 프록시
PROXY_BASE = "https://scourt-proxy.gumago1357.workers.dev"

def proxy(url: str) -> str:
    return f"{PROXY_BASE}/?url={quote(url, safe='')}"

# ── 딜레이 설정
DELAY_LIST        = 2.0
DELAY_DETAIL      = 2.0
DELAY_FILE        = 5.0
DELAY_RETRY_PAUSE = 30.0

# ── HTTP 세션
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

# ── 파일 유효성 검사
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

def build_existing_files_index() -> dict[str, str]:
    index = {}
    if not FILES_DIR.exists():
        return index
    for f in FILES_DIR.iterdir():
        if f.is_file() and not is_broken_file(f):
            stem  = f.stem
            parts = stem.split("_")
            for part in parts:
                if len(part) >= 10 and part.isdigit():
                    index[part] = f.name
    return index

# ── 목록 수집
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
        notices.append({
            "id":     seq_id,
            "seq_id": seq_id,
            "court":  texts[1] if len(texts) > 1 else "",
            "org":    texts[2] if len(texts) > 2 else "",
            "title":  title,
            "date":   "",
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

# ── 상세 수집 (작성일 포함)
DATE_LABELS = ("작성일", "등록일", "게시일", "작 성 일")

def scrape_detail(seq_id: str, files_index: dict) -> dict:
    params = {
        "pageIndex": "1", "seq_id": seq_id,
        "bub_cd": "", "searchWord": "", "searchOption": "",
    }
    detail_url = VIEW_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    result = {
        "files":      [],
        "date":       "",
        "end_date":   "",
        "phone":      "",
        "detail_url": detail_url,
    }

    soup = get_page(VIEW_URL, params=params)
    if not soup:
        return result

    for th in soup.find_all("th"):
        label = th.get_text(strip=True)
        td    = th.find_next_sibling("td")
        if not td:
            continue
        val = td.get_text(strip=True)

        if label in DATE_LABELS:
            m = re.search(r"\d{4}\.\d{2}\.\d{2}", val)
            result["date"] = m.group(0) if m else val
        elif label == "공고만료일":
            m = re.search(r"\d{4}\.\d{2}\.\d{2}", val)
            result["end_date"] = m.group(0) if m else val
        elif label == "전화번호":
            result["phone"] = val

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
        local_val  = ""
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
        print(f"    → 파일 {total}개 (복원 {ok}개) | 작성일: {result['date'] or '—'}")

    return result

# ── ★ monthly_stats.json 저장 (누적, 덮어쓰기 방식)
def save_monthly_stats(notices: list[dict]):
    """
    작성일 기준 월별 공고 수를 계산해서 monthly_stats.json에 저장.
    - 기존 파일의 과거 달 데이터는 유지
    - 현재 notices에 있는 달은 새로 계산한 값으로 덮어쓰기 (중복 방지)
    """
    # 기존 데이터 로드
    existing_monthly = {}
    if MONTHLY_JSON.exists():
        try:
            with open(MONTHLY_JSON, encoding="utf-8") as f:
                data = json.load(f)
            existing_monthly = data.get("monthly", {})
        except Exception as e:
            print(f"  [경고] monthly_stats.json 로드 실패: {e}")

    # 현재 notices에서 월별 집계
    current_monthly: dict[str, int] = {}
    no_date_count = 0
    for n in notices:
        date_str = n.get("date", "")
        if not date_str:
            no_date_count += 1
            continue
        m = re.match(r"(\d{4})\.(\d{2})", date_str)
        if not m:
            no_date_count += 1
            continue
        key = f"{m.group(1)}-{m.group(2)}"
        current_monthly[key] = current_monthly.get(key, 0) + 1

    # 병합: 기존 과거 달 유지 + 현재 달 덮어쓰기
    merged = dict(existing_monthly)
    merged.update(current_monthly)  # 같은 달은 새 값으로 덮어쓰기

    output = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "monthly": dict(sorted(merged.items())),  # 날짜순 정렬
    }
    with open(MONTHLY_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_months = len(merged)
    total_counted = sum(merged.values())
    print(f"  [저장] monthly_stats.json → {total_months}개월, 총 {total_counted}건")
    if no_date_count:
        print(f"  [참고] 작성일 없어서 집계 제외: {no_date_count}건")

# ── 파일 다운로드 (Playwright)
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
    for i in range(1, 4):
        print(f"    시도 {i}/3 (1차)")
        result = await try_download_once(page, seq_id, file_info)
        if result:
            return result
        if i < 3:
            await asyncio.sleep(DELAY_FILE)

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

# ── 데이터 저장/로드
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

# ── 메인
async def main():
    print("=" * 60)
    print("대법원 파산·회생 자산매각 공고 스크래퍼 v8")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    print("\n[1단계] 깨진 파일 정리")
    removed = purge_broken_files()
    print(f"  → {removed}개 삭제")

    print("\n[2단계] 기존 파일 인덱스 구축")
    files_index = build_existing_files_index()
    print(f"  → {len(files_index)}개 파일 발견")

    existing = load_existing()
    print(f"\n[3단계] 기존 공고 데이터: {len(existing)}건")

    print(f"\n[4단계] 목록 수집 (Cloudflare 프록시)")
    all_raw = scrape_all_list()

    print(f"\n[5단계] 상세 정보 수집 (작성일 포함)")
    final: list[dict] = []
    rescrape_count = 0

    for idx, raw in enumerate(all_raw, 1):
        nid = raw["id"]
        print(f"  [{idx:3d}/{len(all_raw)}] {raw['title'][:50]}")
        detail = scrape_detail(nid, files_index)
        raw.update(detail)
        final.append(raw)
        rescrape_count += 1
        time.sleep(DELAY_DETAIL)

        if idx % 50 == 0:
            save_notices(final)
            print(f"  [중간저장] {idx}건")

    save_notices(final)

    # ★ monthly_stats.json 저장
    print(f"\n[5.5단계] 월별 통계 저장")
    save_monthly_stats(final)

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

    date_ok   = sum(1 for n in final if n.get("date"))
    date_fail = sum(1 for n in final if not n.get("date"))
    failed_total = sum(
        1 for n in final
        for fi in n.get("files", [])
        if fi.get("local") == "FAILED"
    )

    print("\n" + "=" * 60)
    print(f"완료!")
    print(f"  재수집 공고:        {rescrape_count}건")
    print(f"  작성일 수집 성공:   {date_ok}건")
    print(f"  작성일 수집 실패:   {date_fail}건")
    print(f"  파일 복원:          {len(files_index)}개")
    print(f"  파일 신규 성공:     {file_ok}개")
    print(f"  파일 실패(FAILED):  {file_fail}개")
    print(f"  전체 FAILED 누적:   {failed_total}개")
    print(f"  전체 공고:          {len(final)}건")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
