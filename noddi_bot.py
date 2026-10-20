#!/usr/bin/env python3
"""
noddi_bot — 디시인사이드 '워뇨띠'(고정닉 uid: rlawo200) 새 글 → 텔레그램 채널 전송

- 고정닉 uid로 판별 → 같은 닉네임을 쓰는 유동닉/사칭 글은 걸러짐
- 새 글: 제목 + 본문 원문 + 작성시각 + 원문 링크 + 첨부 이미지(직접 업로드)
- 원문을 archive/에 마크다운으로 보관 → 나중에 글이 삭제돼도 기록이 남음
- 수집이 연속으로 실패하면(디시 차단 등) 채널로 경고 → 조용히 죽지 않음
- 실행 방식
    1) GitHub Actions: python noddi_bot.py           (1회 점검 후 종료)
    2) PC/서버 상시 실행: python noddi_bot.py --loop 60  (60초마다 점검)
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

KST = dt.timezone(dt.timedelta(hours=9))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TARGET_UID = os.getenv("TARGET_UID", "rlawo200")
TARGET_NICK = os.getenv("TARGET_NICK", "워뇨띠")
# 형식: 갤러리ID:종류:표시이름  (종류 g=정식갤, m=마이너갤, mini=미니갤)
GALLERIES = os.getenv("GALLERIES", "kospi:m:코스피갤,bitcoins_new1:g:비트코인갤")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", "archive"))
FAIL_ALERT_AFTER = int(os.getenv("FAIL_ALERT_AFTER", "3"))
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "10"))
SEEN_KEEP = 300

PATHS = {"g": "board", "m": "mgallery/board", "mini": "mini/board"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

S = requests.Session()
S.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://gall.dcinside.com/",
})


class BlockedError(Exception):
    """디시 페이지가 정상 구조가 아님 (차단/점검/구조변경 의심)."""


def log(*a):
    print(dt.datetime.now(KST).strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


def parse_galleries(spec):
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        gid = parts[0]
        kind = parts[1] if len(parts) > 1 else "m"
        name = parts[2] if len(parts) > 2 else gid
        out.append({"id": gid, "kind": kind, "name": name, "base": f"https://gall.dcinside.com/{PATHS[kind]}"})
    return out


# ───────────────────────── 디시 수집 ─────────────────────────
def parse_list(html_text):
    """목록 HTML → 대상 uid가 쓴 글 [{no,title,date}] (번호 오름차순)."""
    soup = BeautifulSoup(html_text, "html.parser")
    if not soup.select("table.gall_list, tbody.listwrap2, tr.ub-content"):
        raise BlockedError("목록 테이블 없음")
    posts = []
    for tr in soup.select("tr.ub-content"):
        no = (tr.get("data-no") or "").strip()
        if not no.isdigit():
            continue  # 공지/광고
        w = tr.select_one("td.gall_writer")
        if not w or (w.get("data-uid") or "").strip() != TARGET_UID:
            continue
        a = None
        for cand in tr.select("td.gall_tit a"):
            if "reply_numbox" not in (cand.get("class") or []):
                a = cand
                break
        title = a.get_text(" ", strip=True) if a else "(제목 없음)"
        d = tr.select_one("td.gall_date")
        date = (d.get("title") or d.get_text(strip=True)) if d else ""
        posts.append({"no": int(no), "title": title, "date": date})
    posts.sort(key=lambda p: p["no"])
    return posts


def fetch_list(gal):
    r = S.get(f"{gal['base']}/lists/",
              params={"id": gal["id"], "s_type": "search_name", "s_keyword": TARGET_NICK},
              timeout=20)
    if r.status_code != 200:
        raise BlockedError(f"HTTP {r.status_code}")
    return parse_list(r.text)


def view_url(gal, no):
    return f"{gal['base']}/view/?id={gal['id']}&no={no}"


def parse_view(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    title = soup.select_one(".title_subject")
    head = soup.select_one(".gallview_head")
    date = head.select_one(".gall_date") if head else None
    writer = head.select_one(".gall_writer") if head else None
    body = soup.select_one(".write_div")
    if body is None:
        raise BlockedError("본문 영역 없음")

    for br in body.find_all("br"):
        br.replace_with("\n")
    for blk in body.find_all(["p", "div"]):
        blk.insert_after("\n")
    text = body.get_text()
    text = re.sub(r"[ \t ]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    images = []
    for img in body.find_all("img"):
        src = img.get("data-original") or img.get("src") or ""
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("http") and src not in images:
            images.append(src)
    has_video = bool(body.find(["video", "iframe", "embed"]))

    return {
        "title": title.get_text(" ", strip=True) if title else "",
        "date": (date.get("title") or date.get_text(strip=True)) if date else "",
        "uid": (writer.get("data-uid") or "") if writer else "",
        "text": text,
        "images": images,
        "has_video": has_video,
    }


def fetch_view(gal, no):
    r = S.get(view_url(gal, no), timeout=20)
    if r.status_code != 200:
        raise BlockedError(f"본문 HTTP {r.status_code}")
    return parse_view(r.text)


def download_image(url, referer):
    try:
        r = S.get(url, headers={"Referer": referer}, timeout=30)
        if r.status_code == 200 and len(r.content) > 500:
            ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
            return r.content, ctype
    except requests.RequestException as e:
        log("이미지 다운로드 실패", url, e)
    return None


# ───────────────────────── 텔레그램 ─────────────────────────
def tg(method, data=None, files=None):
    if not BOT_TOKEN or not CHAT_ID:
        log("[DRY-RUN]", method, (data or {}).get("text", "")[:200])
        return {"ok": True}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    payload = {"chat_id": CHAT_ID, **(data or {})}
    for attempt in range(4):
        try:
            r = S.post(url, data=payload, files=files, timeout=60)
            j = r.json()
            if j.get("ok"):
                return j
            if r.status_code == 429:  # 속도 제한
                time.sleep(j.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            log("텔레그램 오류", method, j)
            return j
        except (requests.RequestException, ValueError) as e:
            log("텔레그램 전송 재시도", attempt + 1, e)
            time.sleep(3 * (attempt + 1))
    return {"ok": False}


def send_text(text):
    return tg("sendMessage", {"text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})


def chunk(text, size):
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def build_messages(gal, post, view, url):
    e = html.escape
    title = view.get("title") or post["title"]
    when = view.get("date") or post["date"]
    head = (f"🚨 <b>{e(TARGET_NICK)} 새 글</b> · {e(gal['name'])}\n"
            f"<b>{e(title)}</b>\n🕒 {e(when)} KST\n━━━━━━━━━━━━")
    tail = f"━━━━━━━━━━━━\n🔗 <a href=\"{e(url)}\">원문 보기</a>"
    notes = []
    if view.get("images"):
        notes.append(f"🖼 이미지 {len(view['images'])}장 (아래 첨부)")
    if view.get("has_video"):
        notes.append("🎬 동영상/임베드 포함 → 원문에서 확인")
    if view.get("error"):
        notes.append(f"⚠️ 본문 수집 실패({e(view['error'])}) — 삭제됐을 수 있음")
    note = ("\n" + "\n".join(notes)) if notes else ""

    body = e(view.get("text") or "(본문 텍스트 없음)")
    pieces = chunk(body, 3500)
    msgs = []
    for i, p in enumerate(pieces):
        first = i == 0
        last = i == len(pieces) - 1
        m = (head + "\n" if first else f"(이어서 {i + 1}/{len(pieces)})\n") + p
        if last:
            m += "\n" + tail + note
        msgs.append(m)
    return msgs


def send_images(view, referer):
    imgs = []
    for u in view.get("images", [])[:MAX_IMAGES]:
        got = download_image(u, referer)
        if got:
            imgs.append(got)
    if not imgs:
        return
    if len(imgs) == 1:
        content, ctype = imgs[0]
        method, field = ("sendAnimation", "animation") if "gif" in ctype else ("sendPhoto", "photo")
        j = tg(method, files={field: ("img", content, ctype)})
        if not j.get("ok"):
            tg("sendDocument", files={"document": ("image", content, ctype)})
        return
    media, files = [], {}
    for i, (content, ctype) in enumerate(imgs):
        key = f"f{i}"
        media.append({"type": "photo", "media": f"attach://{key}"})
        files[key] = (key, content, ctype)
    j = tg("sendMediaGroup", {"media": json.dumps(media)}, files=files)
    if not j.get("ok"):  # GIF·대용량 섞이면 실패 가능 → 한 장씩 문서로
        for i, (content, ctype) in enumerate(imgs):
            tg("sendDocument", files={"document": (f"image{i}", content, ctype)})


def archive(gal, post, view, url):
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        p = ARCHIVE_DIR / f"{gal['id']}_{post['no']}.md"
        lines = [f"# {view.get('title') or post['title']}", "",
                 f"- 갤러리: {gal['name']} ({gal['id']})",
                 f"- 작성: {view.get('date') or post['date']} KST",
                 f"- 원문: {url}",
                 f"- 수집: {dt.datetime.now(KST):%Y-%m-%d %H:%M:%S} KST", "",
                 view.get("text") or "(본문 없음)", ""]
        lines += [f"![img]({u})" for u in view.get("images", [])]
        p.write_text("\n".join(lines), encoding="utf-8")
    except OSError as ex:
        log("아카이브 저장 실패", ex)


# ───────────────────────── 상태 ─────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            log("state.json 손상 → 초기화")
    return {"seen": {}, "fails": 0, "alerted": False, "started": False}


def save_state(st):
    st["last_ok_date"] = st.get("last_ok_date", "")
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────── 메인 ─────────────────────────
def check_once(st):
    gals = parse_galleries(GALLERIES)
    any_ok = False
    errors = []
    start_lines = []

    for gal in gals:
        try:
            posts = fetch_list(gal)
        except (BlockedError, requests.RequestException) as ex:
            errors.append(f"{gal['name']}: {ex}")
            log("목록 수집 실패", gal["id"], ex)
            continue
        any_ok = True
        key = gal["id"]
        seen = set(st["seen"].get(key, []))
        first_time = key not in st["seen"]

        if first_time:  # 처음 보는 갤러리 → 기존 글은 기록만 하고 전송 안 함
            st["seen"][key] = [p["no"] for p in posts][-SEEN_KEEP:]
            latest = posts[-1] if posts else None
            start_lines.append(
                f"• {html.escape(gal['name'])}: "
                + (f"최근 글 <a href=\"{html.escape(view_url(gal, latest['no']))}\">{html.escape(latest['title'])}</a>"
                   if latest else "최근 글 없음"))
            log(gal["id"], "초기화 — 기존 글", len(posts), "건 기록")
            continue

        new_posts = [p for p in posts if p["no"] not in seen]
        for post in new_posts:
            url = view_url(gal, post["no"])
            try:
                view = fetch_view(gal, post["no"])
                if view.get("uid") and view["uid"] != TARGET_UID:
                    log("작성자 불일치, 건너뜀", url)
                    seen.add(post["no"])
                    continue
            except (BlockedError, requests.RequestException) as ex:
                view = {"title": post["title"], "date": post["date"], "text": "", "images": [], "error": str(ex)}
            log("새 글 전송", gal["id"], post["no"], post["title"])
            for m in build_messages(gal, post, view, url):
                send_text(m)
            send_images(view, url)
            archive(gal, post, view, url)
            seen.add(post["no"])
            st["seen"][key] = sorted(seen)[-SEEN_KEEP:]
            save_state(st)  # 글마다 저장 → 중간에 죽어도 중복 전송 최소화
        st["seen"][key] = sorted(seen)[-SEEN_KEEP:]

    if start_lines and not st.get("started"):
        send_text(f"✅ <b>{html.escape(TARGET_NICK)} 추적 시작</b>\n"
                  f"새 글이 올라오면 원문·이미지·링크를 이 채널로 보냅니다.\n" + "\n".join(start_lines))
        st["started"] = True

    if any_ok and not errors:
        if st.get("alerted"):
            send_text(f"✅ {html.escape(TARGET_NICK)} 추적 복구됨 — 정상 수집 재개")
        st["fails"], st["alerted"] = 0, False
        st["last_ok_date"] = dt.datetime.now(KST).strftime("%Y-%m-%d")
    elif errors:
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] >= FAIL_ALERT_AFTER and not st.get("alerted"):
            send_text(f"⚠️ <b>{html.escape(TARGET_NICK)} 추적 수집 실패 {st['fails']}회 연속</b>\n"
                      + html.escape("\n".join(errors))
                      + "\n디시 접속 차단·점검 가능성. 이 상태에선 새 글을 놓칠 수 있습니다.")
            st["alerted"] = True
    save_state(st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="N초마다 반복 (PC/서버 상시 실행용)")
    args = ap.parse_args()
    st = load_state()
    if args.loop <= 0:
        check_once(st)
        return
    log(f"상시 감시 시작 — {args.loop}초 간격")
    while True:
        try:
            check_once(st)
        except Exception as ex:  # 상시 모드는 어떤 오류에도 계속 돈다
            log("예외", repr(ex))
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
