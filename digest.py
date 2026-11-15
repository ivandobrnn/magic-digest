import html
import json
import os
import re
import subprocess
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STATE = Path(__file__).with_name("state.json")
SOURCES = {
    "TCC Magic": "https://tccmagic.com/collections/new-releases",
    "Ellusionist": "https://ellusionist.com/collections/new-releases",
    "Alakazam Magic": "https://alakazam.co.uk/collections/new-in",
    "Tannen's": "https://www.tannens.com/collections/new-arrivals",
    "Merchant of Magic": "https://magicshop.co.uk/collections/whats-new",
    "Magic Dream": "https://magicdream.fr/collections/nouveautes",
    "Wizard Magic Shop": "https://wizardmagicshop.com/collections/new-arrivals",
    "J&B Magic": "https://brickandmortarmagic.com/collections/special-order-items",
}
RULES = (
    ("Магические приложения", r"app|apps|ios|android|app store"),
    ("Обучающие материалы", r"book|ebook|lecture|tutorial|masterclass|instructional|video download"),
    ("Электроника и гаджеты", r"electronic|electronics|bluetooth|remote control|rechargeable|wireless"),
    ("Сценические иллюзии", r"stage|parlor|parlour|levitation"),
    ("Ментализм", r"mentalism|mind reading|prediction|mindreading|memorized"),
    ("Карточные фокусы", r"card|cards|deck|aces|cardistry"),
)
FALLBACK = "Close-up и реквизит"
AGENT = "MagicDigest"
WEB = requests.Session()
WEB.headers["User-Agent"] = AGENT + "/1.0"
WEB.mount("https://", HTTPAdapter(max_retries=Retry(
    total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504),
    respect_retry_after_header=False,
)))
ROBOTS = {}


def fetch(url):
    root = urlsplit(url)
    origin = f"{root.scheme}://{root.netloc}"
    if origin not in ROBOTS:
        response = WEB.get(origin + "/robots.txt", timeout=(5, 10))
        policy = RobotFileParser()
        if response.status_code == 404:
            policy.parse([])
        else:
            response.raise_for_status()
            policy.parse(response.text.splitlines())
        ROBOTS[origin] = policy
    if not ROBOTS[origin].can_fetch(AGENT, url):
        raise ValueError("robots.txt запрещает чтение страницы")
    response = WEB.get(url, timeout=(5, 12))
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def listings(source, url):
    links = {}
    for anchor in fetch(url).select("a[href]"):
        target = urlsplit(urljoin(url, anchor["href"]))
        if target.hostname != urlsplit(url).hostname:
            continue
        title = " ".join(anchor.stripped_strings)
        product = re.fullmatch(r"(?:/collections/[^/]+)?(/products/[^/]+)/?", target.path)
        if not product:
            continue
        clean = f"https://{target.netloc}{product[1]}"
        if clean not in links or (title and title != "More Info"):
            links[clean] = title
    if not links:
        raise ValueError("в каталоге не найдены карточки товаров")
    return list(links.items())[:24]


def fingerprint(title):
    name = re.split(r"\s+by\s+", title, maxsplit=1, flags=re.I)[0]
    name = re.sub(r"\s*\((?:instant download|red|blue)\)\s*$", "", name, flags=re.I)
    normalized = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized))


def category(title, description, url):
    for text in (title, urlsplit(url).path.replace("-", " "), description):
        for label, pattern in RULES:
            if re.search(r"\b(?:" + pattern + r")\b", text, re.I):
                return label
    return FALLBACK


def video_link(raw, base):
    url = urlsplit(urljoin(base, raw))
    host = url.hostname
    parts = url.path.strip("/").split("/")
    code = ""
    if host in {"youtu.be", "www.youtu.be"}:
        code = parts[0]
    if host in {"youtube.com", "www.youtube.com", "www.youtube-nocookie.com",
                "youtube-nocookie.com", "m.youtube.com"}:
        if parts[0] == "watch":
            code = parse_qs(url.query).get("v", [""])[0]
        if len(parts) == 2 and parts[0] in {"embed", "shorts"}:
            code = parts[1]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", code):
        return "https://www.youtube.com/watch?v=" + code
    if host in {"vimeo.com", "www.vimeo.com", "player.vimeo.com"} and parts[-1].isdigit():
        return "https://vimeo.com/" + parts[-1]
    return ""


def details(source, url, fallback):
    page = fetch(url)

    def meta(*names):
        for name in names:
            tag = page.find("meta", attrs={"property": name}) or page.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    heading = page.find("h1")
    title = (heading.get_text(" ", strip=True) if heading else "") or meta("og:title") or fallback
    title = re.sub(r"\s*[|]\s*(Vanishing Inc\.?.*|Penguin Magic.*)$", "", title).strip()
    if not title or title == "More Info":
        raise ValueError("нет названия товара")
    description = meta("og:description", "description")
    raw_image = meta("og:image:secure_url", "og:image", "twitter:image")
    image = urljoin(url, raw_image) if raw_image else ""
    if urlsplit(image).scheme != "https":
        image = ""
    candidates = [meta("og:video:url", "og:video", "twitter:player")]
    for tag in page.select("iframe, [data-video-url], [data-youtube-id], a[href]"):
        candidates.extend(tag.get(key, "") for key in ("src", "data-src", "data-video-url", "href"))
        if tag.get("data-youtube-id"):
            candidates.append("https://youtu.be/" + tag["data-youtube-id"])
    video = next((link for raw in candidates if raw and (link := video_link(raw, url))), "")
    return dict(title=title, category=category(title, description, url), source=source,
                url=url, image=image, video=video)


def collect(state, today):
    items = state.setdefault("items", {})
    successes = 0
    new_cards = 0
    requested_details = False
    for source, url in SOURCES.items():
        try:
            links = listings(source, url)
            successes += 1
            print(f"{source}: найдено {len(links)} карточек", flush=True)
        except (requests.RequestException, ValueError) as error:
            print(f"{source}: {error}", flush=True)
            continue
        known = state.setdefault("urls", {})
        detail_errors = 0
        source_cards = 0
        deadline = time.monotonic() + 50
        for target, title in links:
            if target in known:
                continue
            if time.monotonic() >= deadline:
                print(f"{source}: remaining cards deferred until next run", flush=True)
                break
            requested_details = True
            try:
                item = details(source, target, title)
            except (requests.RequestException, ValueError) as error:
                print(f"{target}: {error}", flush=True)
                detail_errors += 1
                if detail_errors >= 4:
                    break
                continue
            key = fingerprint(item["title"])
            if not key:
                continue
            known[target] = key
            source_cards += 1
            new_cards += 1
            if key in items:
                old = items[key]
                for field in ("image", "video"):
                    old[field] = old[field] or item[field]
                continue
            items[key] = dict(item, first_seen=today.isoformat())
        print(f"{source}: processed {source_cards} previously unseen product URLs", flush=True)
    if not successes or (requested_details and not new_cards and not items):
        raise SystemExit("Сбор не удался. Подробности в журнале.")


def save(state, push):
    value = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    if STATE.read_text(encoding="utf-8") == value:
        return
    STATE.write_text(value, encoding="utf-8")
    if push:
        commands = [
            ["git", "-c", "user.name=github-actions[bot]", "-c",
             "user.email=41898282+github-actions[bot]@users.noreply.github.com",
             "commit", "-m", "Update digest state", "--", "state.json"],
            ["git", "push", "origin", "HEAD"],
        ]
        for command in commands:
            subprocess.run(command, check=True, cwd=STATE.parent)


def telegram(method, payload):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Добавьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в Secrets.")
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=dict(payload, chat_id=chat), timeout=(5, 25),
        )
        body = response.json()
    except (requests.RequestException, ValueError):
        raise SystemExit("Ответ Telegram не получен. Проверьте канал перед повтором.") from None
    if body.get("ok") is True:
        return body["result"], 0
    code = body.get("error_code", response.status_code)
    if code == 429:
        delay = body.get("parameters", {}).get("retry_after", 0)
        if 0 < delay <= 30:
            time.sleep(delay)
    return None, code


def publish_item(item, today):
    escape = html.escape
    title = escape(item["title"][:180])
    text = f"🎩 <b>Новинки фокусов · {today:%d.%m.%Y}</b>\n\n"
    text += f"{escape(item['category'])}\n<b>{title}</b>\n"
    text += f'<a href="{escape(item["url"])}">{escape(item["source"])}</a>'
    if item["video"]:
        text += f' · <a href="{escape(item["video"])}">Видеодемонстрация</a>'
    payload = {"parse_mode": "HTML"}
    if item["image"]:
        result, code = telegram("sendPhoto", dict(payload, photo=item["image"], caption=text))
        if result or code != 400:
            return result, code
    return telegram("sendMessage", dict(payload, text=text, link_preview_options={"is_disabled": True}))


def main():
    mode = os.environ.get("MODE", "preview")
    if mode not in {"preview", "test", "publish", "schedule"}:
        raise SystemExit("Неизвестный режим запуска")
    if mode == "test":
        result, code = telegram("sendMessage", {"text": "🎩 Бот подключён. Это тестовое сообщение."})
        if not result:
            raise SystemExit(f"Telegram отклонил проверку: код {code}")
        return
    push = os.environ.get("GITHUB_ACTIONS") == "true"
    if mode != "preview" and not push:
        raise SystemExit("Режим публикации запускайте через GitHub Actions.")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    today = datetime.now(timezone(timedelta(hours=3))).date()
    if mode != "preview" and state.get("pending"):
        raise SystemExit("Предыдущая отправка требует сверки. См. README и pending в state.json.")
    collect(state, today)
    items = state["items"]
    candidates = [
        key for key, item in items.items()
        if not item.get("published") and date.fromisoformat(item["first_seen"]) >= today - timedelta(days=7)
    ]
    # Prefer newer discovery days, then alternate categories within the issue.
    candidates.sort(key=lambda key: items[key]["first_seen"], reverse=True)
    selection = []
    while candidates and len(selection) < 7:
        used = {items[key]["category"] for key in selection}
        covered_sources = {items[key]["source"] for key in selection}
        index = min(range(len(candidates)), key=lambda i: (
            items[candidates[i]]["category"] in used,
            items[candidates[i]]["first_seen"] != items[candidates[0]]["first_seen"],
            items[candidates[i]]["source"] in covered_sources,
        ))
        selection.append(candidates.pop(index))
    if mode == "preview":
        for key in selection:
            print(json.dumps(items[key], ensure_ascii=False))
        print(f"Карточек в выпуске: {len(selection)}")
        return
    save(state, push)
    last = state.get("last_issue")
    if mode == "schedule" and last and (today - date.fromisoformat(last)).days < 3:
        return
    for key in selection:
        if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
            raise SystemExit("Добавьте секреты Telegram в настройках GitHub.")
        state["pending"] = {"key": key, "date": today.isoformat()}
        save(state, push)
        result, code = publish_item(items[key], today)
        if result:
            items[key]["published"] = result["message_id"]
            state["last_issue"] = today.isoformat()
        elif code not in {400, 401, 403, 404, 429}:
            raise SystemExit(f"Неопределённый результат Telegram: код {code}. Проверьте канал.")
        del state["pending"]
        save(state, push)
        if not result:
            raise SystemExit(f"Telegram отклонил публикацию: код {code}")
        time.sleep(1.2)


if __name__ == "__main__":
    main()
