#!/usr/bin/env python3
"""
대법원 파산·회생 자산매각 공고 스크래퍼 v5 (하이브리드)
- 목록/상세: requests + BeautifulSoup
- 파일 다운로드: Playwright
- 파일명: 파일ID.확장자 (짧고 안전)
- 실패 처리: 3번 시도 → 30초 대기 → 3번 더 → FAILED 기록
- 재실행 시: 성공 파일 스킵, FAILED 파일 재시도
"""

import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

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

# ── 딜레이 설정 ───────────────────────────────────────────────────────────────
DELAY_LIST        = 2.0    # 목록 페이지 간
DELAY_DETAIL      = 3.0    # 상세 페이지 간
DELAY_FILE        = 5.0    # 파일 다운로드 간
DELAY_RETRY_PAUSE = 30.0   # 1차 실패 후 2차 시도 전 대기

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
    "Referer":         BASE_URL,
})

def get_page(url: str, params: dict = None, retries: int = 3) -> BeautifulSoup | None:
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=30)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            wait = 5 * (attempt + 1)
            print(f"  [재시도 {attempt+1}/{retries}] {e} → {wait}초 후 재시도")
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
    """깨진 파일(HTML 에러페이지) 삭제"""
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
    """
    local 필드 상태 확인
      - ""        → 아직 시도 안 함
      - "FAILED"  → 실패 기록 → 재시도 대상
      - "파일명"  → 실제 파일 존재 확인
    """
    if not local_val or local_val == "FAILED":
        return False
    path = FILES_DIR / local_val
    return path.exists() and not is_broken_file(path)

# ── 목록 수집 (requests) ──────────────────────────────────────────────────────
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

        date = ""
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

# ── 상세 수집 (requests) ──────────────────────────────────────────────────────
def scrape_detail(seq_id: str) -> dict:
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

        # 확장자 추출
        ext = "pdf"   # 기본값
        nl  = file_name.lower()
        if nl.endswith((".hwp", ".hwpx")):
            ext = "hwp"
        elif nl.endswith((".docx", ".doc")):
            ext = "docx"
        elif nl.endswith(".pdf"):
            ext = "pdf"

        # 파일명: 파일ID.확장자 (짧고 안전)
        safe_local = f"{file_id}.{ext}"

        result["files"].append({
            "id":       file_id,
            "name":     file_name,          # 원본 파일명 (표시용)
            "url":      f"{DOWNLOAD_URL}?seq_id={seq_id}&file_id={file_id}",
            "ext":      ext,
            "local":    "",                 # 다운로드 후 채워짐
            "local_name": safe_local,       # 저장할 파일명 (짧은 버전)
        })

    if result["files"]:
        print(f"    → 첨부파일 {len(result['files'])}개")

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
    """
    파일 다운로드 1회 시도.
    성공 시 로컬 파일명 반환, 실패 시 "" 반환.
    """
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
        print(f"    [오류] download() 방식: {e}")

    # 방법 2: 직접 URL 접근
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
    """
    다운로드 재시도 로직:
      1차: 3번 시도
      실패 → 30초 대기
      2차: 3번 더 시도
      그래도 실패 → "FAILED" 반환
    """
    file_name = file_info["name"]

    # 1차 시도 (3번)
    for i in range(1, 4):
        print(f"    시도 {i}/3 (1차)")
        result = await try_download_once(page, seq_id, file_info)
        if result:
            return result
        if i < 3:
            await asyncio.sleep(DELAY_FILE)

    # 30초 대기
    print(f"    → 1차 실패. {int(DELAY_RETRY_PAUSE)}초 대기 후 2차 시도...")
    await asyncio.sleep(DELAY_RETRY_PAUSE)

    # 2차 시도 (3번)
    for i in range(1, 4):
        print(f"    시도 {i}/3 (2차)")
        result = await try_download_once(page, seq_id, file_info)
        if result:
            return result
        if i < 3:
            await asyncio.sleep(DELAY_FILE)

    print(f"    → 최종 실패: {file_name} → FAILED 기록")
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
    print("대법원 파산·회생 자산매각 공고 스크래퍼 v5")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1단계: 깨진 파일 정리
    print("\n[1단계] 깨진 파일 정리")
    removed = purge_broken_files()
    print(f"  → {removed}개 삭제")

    # 2단계: 기존 데이터 로드
    existing = load_existing()
    print(f"\n[2단계] 기존 공고: {len(existing)}건")

    # 3단계: 목록 수집
    print(f"\n[3단계] 목록 수집 (requests)")
    all_raw = scrape_all_list()

    # 4단계: 상세 수집
    print(f"\n[4단계] 상세 정보 수집 (requests)")
    final: list[dict] = []
    new_count = 0

    for idx, raw in enumerate(all_raw, 1):
        nid = raw["id"]
        print(f"  [{idx:3d}/{len(all_raw)}] {raw['title'][:50]}")

        if nid in existing:
            print("    → 스킵 (기존)")
            final.append(existing[nid])
            continue

        detail = scrape_detail(nid)
        raw.update(detail)
        final.append(raw)
        new_count += 1
        time.sleep(DELAY_DETAIL)

        if idx % 50 == 0:
            save_notices(final)
            print(f"  [중간저장] {idx}건")

    save_notices(final)
    print(f"  → 신규 상세 수집: {new_count}건")

    # 5단계: 파일 다운로드
    # 대상: local이 비어있거나 "FAILED" 인 파일
    need_download = [
        n for n in final
        if any(
            fi.get("id") and not local_file_ok(fi.get("local", ""))
            for fi in n.get("files", [])
        )
    ]

    print(f"\n[5단계] 파일 다운로드 (Playwright)")
    print(f"  → 다운로드 대상: {len(need_download)}건")

    file_ok    = 0
    file_fail  = 0

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
                    # 이미 성공한 파일은 스킵
                    if local_file_ok(fi.get("local", "")):
                        print(f"    [스킵] {fi['local']} (이미 존재)")
                        continue

                    result = await download_file_with_retry(page, nid, fi)
                    fi["local"] = result  # 성공: 파일명 / 실패: "FAILED"

                    if result and result != "FAILED":
                        file_ok += 1
                    else:
                        file_fail += 1

                    await asyncio.sleep(DELAY_FILE)

                # 10건마다 중간 저장
                if ni % 10 == 0:
                    save_notices(final)
                    print(f"  [중간저장] 파일 {ni}건 처리")

            await browser.close()

        save_notices(final)

    # 완료
    failed_total = sum(
        1 for n in final
        for fi in n.get("files", [])
        if fi.get("local") == "FAILED"
    )

    print("\n" + "=" * 60)
    print(f"완료!")
    print(f"  신규 공고:       {new_count}건")
    print(f"  파일 성공:       {file_ok}개")
    print(f"  파일 실패(FAILED): {file_fail}개")
    print(f"  전체 FAILED 누적: {failed_total}개 (다음 실행 때 재시도)")
    print(f"  전체 공고:       {len(final)}건")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
