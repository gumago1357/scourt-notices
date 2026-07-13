import requests
from bs4 import BeautifulSoup
import json
import time
import re
import os
import subprocess
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, quote

BASE_URL = "https://www.scourt.go.kr"
LIST_URL = BASE_URL + "/portal/notice/realestate/RealNoticeList.work"
VIEW_URL = BASE_URL + "/portal/notice/realestate/RealNoticeView.work"
FILE_BASE_URL = BASE_URL + "/upload/notice/realestate/"

PROXY_URL = "https://scourt-proxy.gumago1357.workers.dev"

KST = timezone(timedelta(hours=9))

# 파일 저장 경로 (GitHub 저장소 내)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(SCRIPT_DIR, "..")
FILES_DIR = os.path.join(REPO_DIR, "docs", "files")


def fetch_html(url, params=None, encoding="euc-kr"):
    if params:
        full_url = url + "?" + urlencode(params)
    else:
        full_url = url
    proxy_url = PROXY_URL + "?url=" + quote(full_url, safe="")
    for attempt in range(3):
        try:
            resp = requests.get(proxy_url, timeout=30)
            resp.encoding = encoding
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"  HTML 재시도 {attempt+1}/3: {e}")
            time.sleep(5)
    return None


def fetch_file(file_id):
    """파일을 프록시를 통해 다운로드"""
    file_url = FILE_BASE_URL + file_id
    proxy_url = PROXY_URL + "?url=" + quote(file_url, safe="")
    for attempt in range(3):
        try:
            resp = requests.get(proxy_url, timeout=60)
            if resp.status_code == 200 and len(resp.content) > 100:
                return resp.content
        except Exception as e:
            print(f"  파일 다운로드 재시도 {attempt+1}/3: {e}")
            time.sleep(3)
    return None


def get_total_pages(soup):
    if not soup:
        return 1
    last = soup.select_one('a[title="마지막 페이지"]')
    if last:
        m = re.search(r"pageIndex=(\d+)", last.get("href", ""))
        if m:
            return int(m.group(1))
    nums = []
    for p in soup.select(".paginate a"):
        m = re.search(r"pageIndex=(\d+)", p.get("href", ""))
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 1


def parse_list_page(soup):
    if not soup:
        return []
    items = []
    for row in soup.select("table tbody tr"):
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        no_text = cols[0].get_text(strip=True)
        if not no_text.isdigit():
            continue
        no = int(no_text)
        court = cols[1].get_text(strip=True)
        agency = cols[2].get_text(strip=True)
        link_tag = cols[3].find("a")
        if not link_tag:
            continue
        title = link_tag.get_text(strip=True)
        m = re.search(r"seq_id=(\d+)", link_tag.get("href", ""))
        if not m:
            continue
        seq_id = m.group(1)
        date = ""
        attach_count = 0
        if len(cols) >= 6:
            dt = cols[4].get_text(strip=True)
            if re.match(r"\d{4}\.\d{2}\.\d{2}", dt):
                date = dt
            m2 = re.search(r"\d+", cols[5].get_text(strip=True))
            if m2:
                attach_count = int(m2.group())
        elif len(cols) == 5:
            dt = cols[4].get_text(strip=True)
            if re.match(r"\d{4}\.\d{2}\.\d{2}", dt):
                date = dt
        items.append({
            "no": no, "seq_id": seq_id, "court": court,
            "agency": agency, "title": title, "date": date,
            "attach_count": attach_count,
        })
    return items


def parse_detail_page(seq_id):
    soup = fetch_html(VIEW_URL, {
        "pageIndex": 1, "seq_id": seq_id,
        "bub_cd": "", "searchWord": "", "searchOption": "",
    })
    if not soup:
        return {}

    result = {"phone": "", "end_date": "", "date": "", "files": []}

    for tbl in soup.find_all("table"):
        tds = tbl.find_all(["th", "td"])
        text = " ".join(td.get_text() for td in tds)
        if "작성일" in text or "공고만료일" in text or "전화번호" in text:
            for i, td in enumerate(tds):
                label = td.get_text(strip=True)
                if label == "작성일" and i + 1 < len(tds):
                    result["date"] = tds[i + 1].get_text(strip=True)
                if label == "공고만료일" and i + 1 < len(tds):
                    result["end_date"] = tds[i + 1].get_text(strip=True)
                if label == "전화번호" and i + 1 < len(tds):
                    result["phone"] = tds[i + 1].get_text(strip=True)
            break

    # javascript:download('파일ID', '파일명') 패턴 파싱
    seen = set()
    for text in [a.get("href", "") + " " + a.get("onclick", "") for a in soup.find_all("a")]:
        _parse_download(text, result["files"], seen)
    for tag in soup.find_all(onclick=True):
        _parse_download(tag.get("onclick", ""), result["files"], seen)

    return result


def _parse_download(text, files_list, seen):
    m = re.search(r"download\('([^']+)',\s*'([^']+)'\)", text)
    if not m:
        return
    file_id = m.group(1)
    file_name = m.group(2)
    if file_id in seen:
        return
    seen.add(file_id)
    name_lower = file_name.lower()
    if name_lower.endswith(".pdf"):
        ext = "pdf"
    elif name_lower.endswith(".hwp") or name_lower.endswith(".hwpx"):
        ext = "hwp"
    elif name_lower.endswith(".docx") or name_lower.endswith(".doc"):
        ext = "docx"
    else:
        ext = ""
    files_list.append({
        "name": file_name,
        "file_id": file_id,
        "ext": ext,
        "local_path": "",  # 다운로드 후 채워짐
    })


def download_files(items):
    """파일 다운로드 및 저장"""
    os.makedirs(FILES_DIR, exist_ok=True)

    for item in items:
        for f in item.get("files", []):
            file_id = f.get("file_id", "")
            if not file_id:
                continue
            local_path = os.path.join(FILES_DIR, file_id)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 10000:
                # 이미 있고 정상 크기면 스킵
                f["local_path"] = f"files/{file_id}"
                continue
            print(f"    파일 다운로드: {f['name']}")
            content = fetch_file(file_id)
            if content:
                with open(local_path, "wb") as fp:
                    fp.write(content)
                f["local_path"] = f"files/{file_id}"
                print(f"    저장 완료: {file_id} ({len(content)//1024}KB)")
            else:
                print(f"    다운로드 실패: {file_id}")
            time.sleep(0.5)


def cleanup_old_files(current_items):
    """현재 공고에 없는 파일 삭제"""
    if not os.path.exists(FILES_DIR):
        return

    # 현재 공고에서 사용 중인 file_id 목록
    active_ids = set()
    for item in current_items:
        for f in item.get("files", []):
            if f.get("file_id"):
                active_ids.add(f["file_id"])

    # 저장된 파일 중 active_ids에 없는 것 삭제
    deleted = 0
    for fname in os.listdir(FILES_DIR):
        if fname not in active_ids:
            os.remove(os.path.join(FILES_DIR, fname))
            deleted += 1
            print(f"    삭제: {fname}")

    if deleted:
        print(f"  총 {deleted}개 파일 삭제됨")


def categorize(title, agency):
    text = title + " " + agency
    if any(k in text for k in ["부동산", "토지", "건물", "아파트", "상가", "임야", "전답", "주택"]):
        return "부동산"
    if any(k in text for k in ["차량", "자동차", "트럭", "버스", "화물차", "승용차", "지게차", "굴착기", "포클레인"]):
        return "자동차·차량"
    if any(k in text for k in ["특허", "상표", "저작권", "지식재산", "SW", "소프트웨어", "프로그램"]):
        return "특허·지식재산"
    if any(k in text for k in ["채권", "매출채권", "대여금", "판매대금"]):
        return "채권"
    if any(k in text for k in ["비품", "집기", "가전", "냉장", "에어컨", "컴퓨터", "사무용"]):
        return "비품·집기"
    if any(k in text for k in ["기계", "설비", "장비", "공작기계", "건설기계", "크레인"]):
        return "기계·장비"
    if any(k in text for k in ["재고", "상품", "제품", "원재료", "물품"]):
        return "재고·상품"
    if any(k in text for k in ["유체동산", "동산"]):
        return "유체동산"
    if any(k in text for k in ["주식", "지분", "출자"]):
        return "주식·지분"
    if any(k in text for k in ["회원권", "골프", "콘도"]):
        return "회원권"
    if any(k in text for k in ["분양권"]):
        return "분양권"
    return "자산(일반)"


def scrape_all():
    print("=" * 50)
    print("대법원 자산매각 공고 스크래핑 시작")
    print("=" * 50)

    print("\n[1단계] 전체 페이지 수 확인...")
    soup1 = fetch_html(LIST_URL, {"pageIndex": 1})
    if not soup1:
        print("첫 페이지 로드 실패!")
        return
    total_pages = get_total_pages(soup1)
    print(f"  총 {total_pages}페이지")

    print("\n[2단계] 목록 수집 중...")
    all_items = []
    seen_seq = set()
    for page in range(1, total_pages + 1):
        print(f"  목록 {page}/{total_pages} 페이지...")
        soup = soup1 if page == 1 else fetch_html(LIST_URL, {"pageIndex": page})
        if page > 1:
            time.sleep(1)
        for item in parse_list_page(soup):
            if item["seq_id"] not in seen_seq:
                seen_seq.add(item["seq_id"])
                all_items.append(item)
    print(f"  총 {len(all_items)}건 수집 완료")

    print("\n[3단계] 상세 정보 수집 중...")
    for i, item in enumerate(all_items):
        print(f"  상세 {i+1}/{len(all_items)} (no={item['no']})...")
        detail = parse_detail_page(item["seq_id"])
        if detail.get("date"):
            item["date"] = detail["date"]
        item["end_date"] = detail.get("end_date", "")
        item["phone"] = detail.get("phone", "")
        item["files"] = detail.get("files", [])
        item["cat"] = categorize(item["title"], item["agency"])
        item["detail_url"] = f"{VIEW_URL}?pageIndex=1&seq_id={item['seq_id']}&bub_cd=&searchWord=&searchOption="
        time.sleep(0.8)

    print("\n[4단계] 첨부파일 다운로드 중...")
    download_files(all_items)

    print("\n[5단계] 오래된 파일 정리 중...")
    cleanup_old_files(all_items)

    now_kst = datetime.now(KST)
    output = {
        "updated_at": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_display": now_kst.strftime("%Y-%m-%d %H:%M"),
        "total": len(all_items),
        "notices": all_items,
    }

    out_path = os.path.join(REPO_DIR, "data", "notices.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n완료! {len(all_items)}건 저장됨")


if __name__ == "__main__":
    scrape_all()
