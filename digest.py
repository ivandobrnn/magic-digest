import html
import json
import os
import re
import subprocess
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from threading import local
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
    "52Magic": "https://www.52magic.com/New-Arrivals-rc19075.html",
    "IARVEL MAGIC": "https://www.iarvelmagic.com/collections/new-products",
    "PITATA Magic": "https://pitata.cn/collections/frontpage",
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
DISCOVERY = local()
NEW_COLLECTION = re.compile(
    r"new[-_ ](?:in|items|products|arrivals|releases)|what.?s[-_ ]new|"
    r"latest[-_ ]arrivals|nouveaut|novedad|novit|neuheiten|nieuw|novidades|"
    r"новинки|新着|新商品|新品", re.I,
)


def fetch(url):
    root = urlsplit(url)
    origin = f"{root.scheme}://{root.netloc}"
    session = getattr(DISCOVERY, "session", WEB)
    robots = getattr(DISCOVERY, "robots", ROBOTS)
    timeout = (4, 8) if session is not WEB else (5, 12)
    if origin not in robots:
        response = session.get(origin + "/robots.txt", timeout=timeout)
        policy = RobotFileParser()
        if response.status_code == 404:
            policy.parse([])
        else:
            response.raise_for_status()
            policy.parse(response.text.splitlines())
        robots[origin] = policy
    if not robots[origin].can_fetch(AGENT, url):
        raise ValueError("robots.txt запрещает чтение страницы")
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def listings(source, url):
    links = {}
    for anchor in fetch(url).select("a[href]"):
        target = urlsplit(urljoin(url, anchor["href"]))
        if target.hostname != urlsplit(url).hostname:
            continue
        title = " ".join(anchor.stripped_strings)
        pattern = (r"(/[^/]+-p[0-9]+[.]html)" if source == "52Magic"
                   else r"(?:/collections/[^/]+)?(/products/[^/]+)/?")
        product = re.fullmatch(pattern, target.path)
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
    if source == "52Magic":
        title = (meta("og:title") or fallback or title).lstrip("* ").strip()
    if not title or title == "More Info":
        raise ValueError("нет названия товара")
    description = (meta("keywords", "description") if source == "52Magic"
                   else meta("og:description", "description"))
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
    if source == "52Magic" and not video:
        for script in page.find_all("script"):
            match = re.search(
                r'var videoSrc = "(https://ssl[.]images-ssl-mars[.]com/[^"\s]+[.]mp4)"',
                script.string or "",
            )
            if match:
                video = match[1]
                break
    return dict(title=title, category=category(title, description, url), source=source,
                url=url, image=image, video=video)


def catalog_shops(today):
    def host(url):
        return urlsplit(url).hostname.removeprefix("www.")

    active = {host(url) for url in SOURCES.values()}
    shops = []
    catalog = STATE.with_name("MAGIC_SHOPS.md").read_text(encoding="utf-8")
    for line in catalog.splitlines():
        columns = line[2:].split(" | ") if line.startswith("| ") else []
        if len(columns) < 4:
            continue
        store = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)]+)\)", columns[1])
        if not store or host(store[2]) in active:
            continue
        recent = re.fullmatch(r"\[Открыть\]\((https?://[^)]+)\)", columns[2])
        shops.append((store[1], store[2], recent[1] if recent else ""))
    return shops[today.toordinal() % 7::7]


def discover_store(shop, known_urls):
    source, site, recent = shop
    DISCOVERY.robots = {}
    with requests.Session() as session:
        DISCOVERY.session = session
        session.headers["User-Agent"] = AGENT + "/1.0"
        try:
            if not recent:
                links = []
                for anchor in fetch(site).select("a[href]"):
                    candidate = urlsplit(urljoin(site, anchor["href"]))
                    if (candidate.hostname or "").removeprefix("www.") != urlsplit(site).hostname.removeprefix("www."):
                        continue
                    if not re.fullmatch(r"/collections/[^/]+/?", candidate.path):
                        continue
                    label = anchor.get_text(" ", strip=True)
                    if NEW_COLLECTION.search(candidate.path + " " + label):
                        links.append(candidate._replace(query="", fragment="").geturl())
                recent = next(iter(links), "")
            if not recent:
                return source, "витрина новинок не найдена", []
            products = listings(source, recent)
            cards = []
            for url, title in products[:5]:
                if url in known_urls:
                    continue
                try:
                    item = details(source, url, title)
                except (ValueError, requests.RequestException):
                    continue
                if item["image"] and fingerprint(item["title"]):
                    cards.append(item)
            return source, f"{len(products)} ссылок в витрине, {len(cards)} новых карточек", cards
        except (ValueError, requests.RequestException) as error:
            return source, str(error), []
        finally:
            del DISCOVERY.session, DISCOVERY.robots


def collect_catalog(state, today):
    known = state.setdefault("urls", {})
    items = state.setdefault("items", {})
    shops = catalog_shops(today)
    print(f"Мировой каталог: проверяю {len(shops)} магазинов в сегодняшней группе", flush=True)
    known_urls = frozenset(known)
    with ThreadPoolExecutor(max_workers=10) as pool:
        for source, status, cards in pool.map(lambda shop: discover_store(shop, known_urls), shops):
            print(f"Мировой каталог · {source}: {status}", flush=True)
            for item in cards:
                key = fingerprint(item["title"])
                known[item["url"]] = key
                if key not in items:
                    items[key] = dict(item, first_seen=today.isoformat())
                else:
                    for field in ("image", "video"):
                        items[key][field] = items[key][field] or item[field]


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
        source_cards = source_photos = source_videos = 0
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
            source_photos += bool(item["image"])
            source_videos += bool(item["video"])
            new_cards += 1
            if key in items:
                old = items[key]
                for field in ("image", "video"):
                    old[field] = old[field] or item[field]
                continue
            items[key] = dict(item, first_seen=today.isoformat())
        print(f"{source}: processed {source_cards} previously unseen product URLs "
              f"({source_photos} with photo, {source_videos} with video)", flush=True)
    previous_count = len(items)
    collect_catalog(state, today)
    if (not successes and len(items) == previous_count) or (requested_details and not new_cards and not items):
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


CHECK_CALLBACK = "magic_digest_check"
CHECK_KEYBOARD = {"inline_keyboard": [[{
    "text": "Проверить сейчас", "callback_data": CHECK_CALLBACK,
}]]}
CHECK_TEXT = "🎩 Проверка новинок для администраторов канала"


def requested_check():
    params = {"allowed_updates": ["callback_query"], "limit": 100, "timeout": 0}
    updates, code = telegram("getUpdates", params)
    if code:
        raise SystemExit(f"Telegram отклонил получение нажатий: код {code}")
    if not updates:
        return None

    requested = None
    channel_id = os.environ["TELEGRAM_CHAT_ID"]
    membership = {}
    for update in updates:
        callback = update.get("callback_query", {})
        message = callback.get("message", {})
        chat = message.get("chat", {})
        if callback.get("data") != CHECK_CALLBACK or not (
            str(chat.get("id")) == channel_id
            or chat.get("username") and "@" + chat["username"] == channel_id
        ):
            continue
        user_id = callback["from"]["id"]
        if user_id not in membership:
            member, code = telegram("getChatMember", {"user_id": user_id})
            membership[user_id] = bool(member and member["status"] in {"creator", "administrator"})
        if membership[user_id]:
            requested = message["message_id"]
    _, code = telegram("getUpdates", dict(params, offset=updates[-1]["update_id"] + 1, limit=1))
    if code:
        raise SystemExit(f"Telegram отклонил подтверждение нажатий: код {code}")
    return requested


def update_button(message_id, status):
    _, code = telegram("editMessageText", {
        "message_id": message_id, "text": CHECK_TEXT + "\n" + status,
        "reply_markup": CHECK_KEYBOARD,
    })
    if code:
        print(f"Telegram не обновил статус кнопки: код {code}")


def main():
    mode = os.environ.get("MODE", "preview")
    if mode not in {"preview", "test", "publish", "schedule", "button", "poll", "webhook"}:
        raise SystemExit("Неизвестный режим запуска")
    if mode == "test":
        result, code = telegram("sendMessage", {"text": "🎩 Бот подключён. Это тестовое сообщение."})
        if not result:
            raise SystemExit(f"Telegram отклонил проверку: код {code}")
        return
    if mode == "webhook":
        url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
        secret = os.environ.get("WEBHOOK_SECRET", "")
        target = urlsplit(url)
        if target.scheme != "https" or not target.netloc or target.path != "/telegram":
            raise SystemExit("Укажите HTTPS-адрес Worker с путём /telegram.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise SystemExit("Создайте TELEGRAM_WEBHOOK_SECRET длиной от 32 символов.")
        result, code = telegram("setWebhook", {
            "url": url, "secret_token": secret, "allowed_updates": ["callback_query"],
        })
        if not result:
            raise SystemExit(f"Telegram отклонил настройку webhook: код {code}")
        print("Webhook Telegram подключён.")
        return
    if mode == "button":
        result, code = telegram("sendMessage", {
            "text": CHECK_TEXT,
            "reply_markup": CHECK_KEYBOARD,
        })
        if not result:
            raise SystemExit(f"Telegram отклонил создание кнопки: код {code}")
        return
    button_message = None
    raw_button_message = os.environ.get("BUTTON_MESSAGE_ID", "")
    if mode == "publish" and raw_button_message:
        if not re.fullmatch(r"[1-9][0-9]{0,15}", raw_button_message):
            raise SystemExit("Некорректный ID сообщения с кнопкой.")
        button_message = int(raw_button_message)
    if mode == "poll":
        if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
            return
        button_message = requested_check()
        if button_message is None:
            return
        mode = "publish"
    push = os.environ.get("GITHUB_ACTIONS") == "true"
    if mode != "preview" and not push:
        raise SystemExit("Режим публикации запускайте через GitHub Actions.")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    today = datetime.now(timezone(timedelta(hours=3))).date()
    if mode != "preview" and state.get("pending"):
        raise SystemExit("Предыдущая отправка требует сверки. См. README и pending в state.json.")
    if button_message:
        update_button(button_message, "Проверяю источники…")
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
    if selection:
        _, code = telegram("sendMessage", {"text": f"📅 {today:%d.%m.%Y}\nНа этом пока всё."})
        if code:
            print(f"Telegram отклонил завершающее сообщение: код {code}", flush=True)
    if button_message:
        status = f"Опубликовано карточек: {len(selection)}." if selection else "Новых карточек нет."
        now = datetime.now(timezone(timedelta(hours=3)))
        update_button(button_message, f"{now:%d.%m %H:%M} МСК · {status}")


if __name__ == "__main__":
    main()
