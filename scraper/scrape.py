import requests
from bs4 import BeautifulSoup
import json
import time
import re
import os
from datetime import datetime, timezone, timedelta

BASE_URL = "https://www.scourt.go.kr"
LIST_URL = BASE_URL + "/portal/notice/realestate/RealNoticeList.work"
VIEW_URL = BASE_URL + "/portal/notice/realestate/RealNoticeView.work"
FILE_BASE_URL = BASE_URL + "/upload/notice/realestate/"

PROXY_URL = "https://scourt-proxy.gumago1357.workers.dev"

KST = timezone(timedelta(hours=9))


def fetch(url, params=None, encoding="euc-kr"):
    if params:
        from urllib.parse import urlencode
        full_url = url + "?" + urlencode(params)
    else:
        full_url = url

    proxy_request_url = PROXY_URL + "?url=" + requests.utils.quote(full_url, safe="")

    for attempt in range(3):
        try:
            resp = requests.get(proxy_request_url, timeout=30)
            resp.encoding = encoding
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"  재시도 {attempt+1}/3: {e}")
            time.sleep(5)
    return None


def get_total_pages(soup):
    if not soup:
        return 1
    last = soup.select_one('a[title="마지막 페이지"]')
    if last:
        href = last.get("href", "")
        m = re.search(r"pageIndex=(\d+)", href)
        if m:
            return int(m.group(1))
    pages = soup.select(".paginate a")
    nums = []
    for p in pages:
        m = re.search(r"pageIndex=(\d+)", p.get("href", ""))
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 1


def parse_list_page(soup):
    if not soup:
        return []
    rows = soup.select("table tbody tr")
    items = []
    for row in rows:
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
        href = link_tag.get("href", "")
        m = re.search(r"seq_id=(\d+)", href)
        if not m:
            continue
        seq_id = m.group(1)

        date = ""
        attach_count = 0
        if len(cols) >= 6:
            date_text = cols[4].get_text(strip=True)
            if re.match(r"\d{4}\.\d{2}\.\d{2}", date_text):
                date = date_text
            m2 = re.search(r"\d+", cols[5].get_text(strip=True))
            if m2:
                attach_count = int(m2.group())
        elif len(cols) == 5:
            date_text = cols[4].get_text(strip=True)
            if re.match(r"\d{4}\.\d{2}\.\d{2}", date_text):
                date = date_text

        items.append({
            "no": no,
            "seq_id": seq_id,
            "court": court,
            "agency": agency,
            "title": title,
            "date": date,
            "attach_count": attach_count,
        })
    return items


def parse_detail_page(seq_id, page_index=1):
    params = {
        "pageIndex": page_index,
        "seq_id": seq_id,
        "bub_cd": "",
        "searchWord": "",
        "searchOption": "",
    }
    soup = fetch(VIEW_URL, params=params)
    if not soup:
        return {}

    result = {"phone": "", "end_date": "", "date": "", "files": []}

    # 상세 페이지 테이블에서 정보 파싱
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

    # 첨부파일 파싱 - javascript:download('파일ID', '파일명') 패턴
    for tag in soup.find_all(onclick=True):
        onclick = tag.get("onclick", "")
        _parse_download(onclick, result["files"])

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if href.startswith("javascript:download"):
            _parse_download(href, result["files"])

    # a 태그 텍스트에서 파일명 추출 (onclick 방식)
    for a in soup.find_all("a"):
        onclick = a.get("onclick", "")
        if "download" in onclick.lower():
            _parse_download(onclick, result["files"])

    # 중복 제거
    seen = set()
    unique_files = []
    for f in result["files"]:
        key = f["url"]
        if key not in seen:
            seen.add(key)
            unique_files.append(f)
    result["files"] = unique_files

    return result


def _parse_download(text, files_list):
    """javascript:download('파일ID', '파일명') 패턴 파싱"""
    m = re.search(r"download\('([^']+)',\s*'([^']+)'\)", text)
    if m:
        file_id = m.group(1)
        file_name = m.group(2)
        # 실제 파일 URL 구성
        file_url = FILE_BASE_URL + file_id
        ext = ""
        name_lower = file_name.lower()
        if name_lower.endswith(".pdf"):
            ext = "pdf"
        elif name_lower.endswith(".hwp") or name_lower.endswith(".hwpx"):
            ext = "hwp"
        elif name_lower.endswith(".docx") or name_lower.endswith(".doc"):
            ext = "docx"
        files_list.append({
            "name": file_name,
            "url": file_url,
            "ext": ext,
        })


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
    print(f"프록시: {PROXY_URL}")
    print("=" * 50)

    print("\n[1단계] 전체 페이지 수 확인...")
    soup1 = fetch(LIST_URL, params={"pageIndex": 1})
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
        if page == 1:
            soup = soup1
        else:
            soup = fetch(LIST_URL, params={"pageIndex": page})
            time.sleep(1)

        items = parse_list_page(soup)
        for item in items:
            if item["seq_id"] not in seen_seq:
                seen_seq.add(item["seq_id"])
                all_items.append(item)

    print(f"  총 {len(all_items)}건 수집 완료")

    print("\n[3단계] 상세 정보 수집 중...")
    for i, item in enumerate(all_items):
        print(f"  상세 {i+1}/{len(all_items)} (no={item['no']})...")
        detail = parse_detail_page(item["seq_id"])
        # 상세 페이지 날짜가 있으면 덮어쓰기
        if detail.get("date"):
            item["date"] = detail["date"]
        item["end_date"] = detail.get("end_date", "")
        item["phone"] = detail.get("phone", "")
        item["files"] = detail.get("files", [])
        item["cat"] = categorize(item["title"], item["agency"])
        item["detail_url"] = f"{VIEW_URL}?pageIndex=1&seq_id={item['seq_id']}&bub_cd=&searchWord=&searchOption="
        time.sleep(0.8)

    now_kst = datetime.now(KST)
    output = {
        "updated_at": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_display": now_kst.strftime("%Y-%m-%d %H:%M"),
        "total": len(all_items),
        "notices": all_items,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "notices.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n완료! {len(all_items)}건 저장됨")


if __name__ == "__main__":
    scrape_all()
