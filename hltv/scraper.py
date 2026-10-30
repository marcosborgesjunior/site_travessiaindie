"""Scrapper HLTV — jogos com times do Brasil em TODAY'S MATCHES.

Busca https://www.hltv.org/matches (HTML server-side rendered), filtra os
matches cujo qualquer time tenha a bandeira do Brasil (BR.gif/BR.png) e
organiza os horários em JSON, convertendo o epoch canônico (data-unix) para
America/Sao_Paulo.

Cache (evita bater no site toda execução):
- Página /matches: cache/matches-AAAA-MM-DD.html (data em São Paulo).
  Na primeira execução do dia baixa e salva; execuções seguintes do mesmo
  dia reusam o arquivo. --fresh força novo download.

Casos especiais do markup HLTV:
- Upcoming: flag do país dentro de .match-team (img.country-flag-small).
- Ao vivo: wrapper sem .match-time e sem flag — fica de fora (sem fetch de
  páginas de match; somente os horários da página principal interessam).
- Um mesmo match aparece em várias colunas (pinned, calendário, live) com
  completude diferente (uma tem evento, outra tem flag); as aparições são
  merged campo-a-campo por match_id.

Uso:
    python scraper.py            # usa cache do dia se existir
    python scraper.py --fresh    # baixa de novo
    python scraper.py --out x.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from curl_cffi import requests

URL = "https://www.hltv.org/matches"
BASE = "https://www.hltv.org"
SP = ZoneInfo("America/Sao_Paulo")
CACHE_DIR = Path(__file__).resolve().parent / "cache"
WEEKDAYS_PT = {
    "Monday": "segunda",
    "Tuesday": "terça",
    "Wednesday": "quarta",
    "Thursday": "quinta",
    "Friday": "sexta",
    "Saturday": "sábado",
    "Sunday": "domingo",
}
HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(url: str) -> str:
    r = requests.get(url, impersonate="chrome", headers=HEADERS, timeout=30)
    r.raise_for_status()
    if "Just a moment" in r.text[:2000]:
        raise SystemExit(f"Bloqueado pelo Cloudflare em {url}. Tente de novo em instantes.")
    return r.text


def fetch_matches_page(fresh: bool = False, url: str = URL) -> tuple[str, str]:
    """Retorna (html, origem). Cache diário em São Paulo."""
    today = datetime.now(SP).strftime("%Y-%m-%d")
    cache_file = CACHE_DIR / f"matches-{today}.html"
    if cache_file.exists() and not fresh:
        return cache_file.read_text(encoding="utf-8"), f"cache:{cache_file.name}"
    print(f"Baixando {url} (cache do dia ainda não existe) ...", file=sys.stderr)
    html = fetch(url)
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(html, encoding="utf-8")
    return html, f"rede:{cache_file.name}"


def is_brazil_flag(img) -> bool:
    if img is None:
        return False
    src = img.get("src", "")
    title = img.get("title", "") or img.get("alt", "")
    return src.endswith(("BR.gif", "BR.png")) or title == "Brazil"


def parse_team(wrapper_team) -> dict:
    name_el = wrapper_team.select_one(".match-teamname")
    name = name_el.get_text(strip=True) if name_el else "(TBD)"
    flag = wrapper_team.select_one("img.country-flag-small, img.flag")
    country = (flag.get("title") or flag.get("alt")) if flag else None
    return {"name": name, "country": country, "is_brazil": is_brazil_flag(flag)}


def parse_match(wrapper, section_date: str | None) -> dict:
    match_id = wrapper.get("data-match-id")
    link = wrapper.select_one('a[href^="/matches/"]')
    href = link.get("href") if link else f"/matches/{match_id}"

    event_el = wrapper.select_one(".match-event")
    event = None
    if event_el:
        event = event_el.get("data-event-headline")
        if not event:
            name_el = event_el.select_one("div.text-ellipsis")
            event = (name_el.get_text(strip=True) if name_el else event_el.get_text(" ", strip=True)) or None
    if not event:
        # Starred/pinned sem .match-event: nome vive no header do grupo
        group = wrapper.find_parent(class_="event-headline-wrapper")
        if group:
            event = group.get("data-event-headline")

    live = wrapper.get("live") == "true"
    meta = [m.get_text(strip=True) for m in wrapper.select(".match-meta")]
    fmt = " ".join(m for m in meta if m != "Live") or None

    time_el = wrapper.select_one(".match-time")
    unix_ms = int(time_el["data-unix"]) if time_el and time_el.get("data-unix") else None

    # Times reais vivem em a.match-teams > .match-team; descarta blocos sem nome
    teams = [
        parse_team(t)
        for t in wrapper.select("a.match-teams > .match-team")
        if t.select_one(".match-teamname")
    ]

    return {
        "unix_ms": unix_ms,
        "hltv_section_date": section_date,
        "event": event,
        "format": fmt,
        "live": live,
        "stars": int(wrapper.get("data-stars") or 0),
        "match_id": match_id,
        "url": f"{BASE}{href}" if href.startswith("/") else href,
        "teams": teams,
        "has_flag": any(t["country"] for t in teams),
    }


def _completeness(mt: dict) -> tuple:
    return (
        mt["unix_ms"] is not None,
        mt["event"] is not None,
        mt["has_flag"],
        len(mt["teams"]),
        mt["live"] is False,
    )


def dedup_by_id(matches: list[dict]) -> list[dict]:
    """Um registro por match_id; merge campo-a-campo entre aparições.

    O mesmo match aparece em várias colunas (pinned, calendário, live) com
    completude diferente — uma tem evento, outra tem flag, outra tem horário.
    A mais completa vira base; lacunas são preenchidas pelas demais.
    """
    by_id: dict[str, list[dict]] = {}
    for mt in matches:
        by_id.setdefault(mt["match_id"], []).append(mt)

    out: list[dict] = []
    for mts in by_id.values():
        base = max(mts, key=_completeness)
        for other in mts:
            if other is base:
                continue
            if base["unix_ms"] is None and other["unix_ms"] is not None:
                base["unix_ms"] = other["unix_ms"]
            if base["event"] is None and other["event"] is not None:
                base["event"] = other["event"]
            if base["format"] is None and other["format"] is not None:
                base["format"] = other["format"]
            if base["stars"] == 0 and other["stars"] > 0:
                base["stars"] = other["stars"]
            if not base["teams"]:
                base["teams"] = other["teams"]
                base["has_flag"] = other["has_flag"]
                continue
            other_by_name = {t["name"].lower(): t for t in other["teams"]}
            for t in base["teams"]:
                ot = other_by_name.get(t["name"].lower())
                if ot and t["country"] is None and ot["country"] is not None:
                    t["country"] = ot["country"]
                    t["is_brazil"] = ot["is_brazil"]
            base["has_flag"] = any(t["country"] for t in base["teams"])
        out.append(base)
    return out


def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    matches: list[dict] = []
    for section in soup.select("div.matches-list-section"):
        headline = section.select_one("span.matches-list-headline")
        if not headline:
            continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})", headline.get_text())
        section_date = m.group(1) if m else None
        for wrapper in section.select("div.match-wrapper"):
            matches.append(parse_match(wrapper, section_date))

    # Fora das seções: coluna live + starred/destaques do topo
    section_ids = {
        id(w)
        for s in soup.select("div.matches-list-section")
        for w in s.select("div.match-wrapper")
    }
    for wrapper in soup.select("div.match-wrapper"):
        if id(wrapper) not in section_ids:
            matches.append(parse_match(wrapper, None))

    matches = dedup_by_id(matches)

    # Filtra Brasil com horário definido e ordena por horário (São Paulo).
    # Ao vivo (wrapper sem .match-time) fica de fora por não ter horário.
    br = [
        mt
        for mt in matches
        if mt["unix_ms"] is not None and any(t["is_brazil"] for t in mt["teams"])
    ]
    return sorted(br, key=lambda x: x["unix_ms"] or 0)


def build_output(matches: list[dict]) -> dict:
    days: dict[str, list[dict]] = {}
    for mt in matches:
        dt_sp = datetime.fromtimestamp(mt["unix_ms"] / 1000, tz=SP)
        item = {
            "time": dt_sp.strftime("%H:%M"),
            "event": mt["event"],
            "format": mt["format"],
            "live": mt["live"],
            "stars": mt["stars"],
            "match_id": mt["match_id"],
            "url": mt["url"],
            "teams": [
                {"name": t["name"], "country": t["country"], "is_brazil": t["is_brazil"]}
                for t in mt["teams"]
            ],
        }
        days.setdefault(dt_sp.strftime("%Y-%m-%d"), []).append(item)

    out_days = []
    for date in sorted(days):
        en_wd = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        out_days.append(
            {"date": date, "weekday": WEEKDAYS_PT.get(en_wd, en_wd), "matches": days[date]}
        )

    return {
        "source": URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": "America/Sao_Paulo",
        "total_matches": len(matches),
        "days": out_days,
    }


def print_table(data: dict) -> None:
    for day in data["days"]:
        print(f"\n== {day['date']} ({day['weekday']}) ==")
        for mt in day["matches"]:
            vs = " x ".join(t["name"] for t in mt["teams"])
            flags = "".join(" [BR]" if t["is_brazil"] else "" for t in mt["teams"])
            live = " (AO VIVO)" if mt["live"] else ""
            ev = mt["event"] or "-"
            print(f"  {mt['time']}  {vs}{flags}  — {ev} ({mt['format'] or '?'}){live}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Scrapper HLTV: jogos do Brasil")
    ap.add_argument("--out", default="brazil-matches.json", help="arquivo JSON de saída")
    ap.add_argument("--fresh", action="store_true", help="ignora cache e baixa de novo")
    ap.add_argument(
        "--url",
        default=URL,
        help=(
            "URL da página de matches (ex.: um Worker proxy) — "
            "default: direto do HLTV"
        ),
    )
    args = ap.parse_args()

    html, origin = fetch_matches_page(fresh=args.fresh, url=args.url)
    print(f"HTML: {origin}", file=sys.stderr)
    matches = parse_page(html)
    if not matches:
        raise SystemExit("Nenhum match com bandeira do Brasil encontrado (markup mudou?).")

    data = build_output(matches)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print_table(data)
    print(f"\nTotal: {data['total_matches']} matches -> {args.out}")


if __name__ == "__main__":
    main()
