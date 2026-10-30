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


EVENTS_URL = f"{BASE}/events"


def fetch_events_index(fresh: bool = False) -> tuple[dict, str]:
    """Índice de campeonatos (/events): id -> nome, datas, local.

    1 request/dia com cache local — daí o cruzamento com os matches é
    todo offline (data-event-id dos wrappers).
    """
    today = datetime.now(SP).strftime("%Y-%m-%d")
    cache_file = CACHE_DIR / f"events-{today}.html"
    if cache_file.exists() and not fresh:
        html, origin = cache_file.read_text(encoding="utf-8"), f"cache:{cache_file.name}"
    else:
        print(f"Baixando {EVENTS_URL} ...", file=sys.stderr)
        html = fetch(EVENTS_URL)
        CACHE_DIR.mkdir(exist_ok=True)
        cache_file.write_text(html, encoding="utf-8")
        origin = f"rede:{cache_file.name}"
    return parse_events_index(html), origin


def parse_events_index(html: str) -> dict[str, dict]:
    """Extrai de /events: id -> {name, start_ms, end_ms, location, top_teams}.

    Duas fontes de card: a.big-event (upcoming, agrupados por mês) e
    a.ongoing-event (campeonatos em andamento) — os matches BR vivem
    principalmente nas ongoing.
    """
    soup = BeautifulSoup(html, "html.parser")
    index: dict[str, dict] = {}

    def merge(event_id: str, data: dict) -> None:
        cur = index.get(event_id, {})
        for k, v in data.items():
            if v is not None and not cur.get(k):
                cur[k] = v
        index[event_id] = cur

    for card in soup.select("a.big-event, a.ongoing-event"):
        href = card.get("href") or ""
        m = re.search(r"/events/(\d+)/", str(href))
        if not m:
            continue
        event_id = m.group(1)

        name = None
        for sel in (".big-event-name", ".event-name-small .text-ellipsis", "img.logo"):
            el = card.select_one(sel)
            if el:
                name = (
                    el.get("title")
                    if el.name == "img"
                    else el.get_text(strip=True)
                )
                if name:
                    break

        # datas:1º span data-unix = início, 2º = fim (padrão igual nos dois cards)
        unics = [
            int(s["data-unix"]) for s in card.select("[data-unix]") if s.get("data-unix")
        ]
        start_ms = unics[0] if unics else None
        end_ms = unics[1] if len(unics) > 1 else None

        loc_el = card.select_one(".big-event-location")
        loc_flag = card.select_one(".location-top-teams img.flag")
        location = None
        if loc_el:
            location = loc_el.get_text(strip=True)
            if loc_flag:
                country = loc_flag.get("title") or loc_flag.get("alt")
                location = f"{location} ({country})" if country else location

        top_teams = [
            img.get("title") or img.get("alt")
            for img in card.select("img.small-team-logo")
        ]

        # Premiação / formato (cards upcoming trazem a tabela additional-info)
        prize = None
        teams_count = None
        for td in card.select(".additional-info td.col-value"):
            if "col-date" in (td.get("class") or []):
                continue
            txt = td.get_text(strip=True)
            title = str(td.get("title") or "")
            if "$" in txt or title.startswith("$"):
                prize = txt or title
            elif txt.isdigit():
                teams_count = int(txt)

        # Distribuição da premiação (tooltip escondido no card)
        distribution = {}
        for row in card.select(".moneyShare-popup .moneyShare-row"):
            k = row.select_one(".moneyShare-row-left")
            v = row.select_one(".moneyShare-row-right")
            if k and v:
                key = k.get_text(strip=True).rstrip(":").lower().replace(" ", "_")
                distribution[key] = v.get_text(strip=True)
        lan = bool(card.select_one(".lan-marker"))

        merge(
            event_id,
            {
                "name": name,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "location": location,
                "top_teams": [t for t in top_teams if t],
                "prize": prize,
                "prize_distribution": distribution or None,
                "teams_count": teams_count,
                "lan": lan,
                "url": f"{BASE}{href}",
            },
        )
    return index


def is_brazil_flag(img) -> bool:
    if img is None:
        return False
    src = img.get("src", "")
    title = img.get("title", "") or img.get("alt", "")
    return src.endswith(("BR.gif", "BR.png")) or title == "Brazil"


# Fallback quando o wrapper não renderiza a bandeira (comum em matches de
# datas mais distantes): nomes de times brasileiros do cenário principal.
BR_TEAM_NAMES = {
    "furia",
    "mibr",
    "imperial",
    "pain",
    "legacy",
    "fluxo",
    "red canids",
    "godsent",
    "sharks",
    "intz",
    "w7m",
    "case",
    "havan libre",
    "kabum",
    "los héroes",
    "br crowd",
}

# Correção manual: o HLTV às vezes renderiza flag BR errada; o usuário
# confirmou que Bestia NÃO é brasileiro (é argentina).
BR_EXCLUDED_NAMES = {"bestia"}


def is_brazil_team_name(name: str | None) -> bool:
    if not name:
        return False
    n = name.strip().casefold()
    if n in BR_EXCLUDED_NAMES:
        return False
    return n in BR_TEAM_NAMES or n.endswith(" br")


def parse_team(wrapper_team) -> dict:
    name_el = wrapper_team.select_one(".match-teamname")
    name = name_el.get_text(strip=True) if name_el else "(TBD)"
    flag = wrapper_team.select_one("img.country-flag-small, img.flag")
    country = (flag.get("title") or flag.get("alt")) if flag else None
    brazil = is_brazil_flag(flag)
    if name.strip().casefold() in BR_EXCLUDED_NAMES:
        # exceção conhecida sobrepõe até a bandeira exibida pelo HLTV
        brazil = False
        country = None if country == "Brazil" else country
    elif not brazil and is_brazil_team_name(name):
        # wrapper sem tag de bandeira: reconhece pelo nome do time
        brazil = True
        country = country or "Brazil"
    return {"name": name, "country": country, "is_brazil": brazil}


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
        "event_id": wrapper.get("data-event-id"),
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
            if base.get("event_id") is None and other.get("event_id"):
                base["event_id"] = other["event_id"]
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


def build_output(matches: list[dict], events_index: dict[str, dict] | None = None) -> dict:
    events_index = events_index or {}
    days: dict[str, list[dict]] = {}
    for mt in matches:
        dt_sp = datetime.fromtimestamp(mt["unix_ms"] / 1000, tz=SP)
        item = {
            "time": dt_sp.strftime("%H:%M"),
            "event": mt["event"],
            "event_id": mt.get("event_id"),
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
        # Cruza com /events: datas completas do campeonato, local, URL
        info = events_index.get(mt.get("event_id") or "")
        if info:
            if not item["event"]:
                item["event"] = info["name"]
            ev = {"name": info["name"], "url": info["url"]}
            if info.get("start_ms"):
                ev["start"] = datetime.fromtimestamp(
                    info["start_ms"] / 1000, tz=SP
                ).strftime("%Y-%m-%d")
            if info.get("end_ms"):
                ev["end"] = datetime.fromtimestamp(info["end_ms"] / 1000, tz=SP).strftime(
                    "%Y-%m-%d"
                )
            if info.get("location"):
                ev["location"] = info["location"]
            if info.get("prize"):
                ev["prize"] = info["prize"]
            if info.get("prize_distribution"):
                ev["prize_distribution"] = info["prize_distribution"]
            if info.get("teams_count") is not None:
                ev["teams_count"] = info["teams_count"]
            if info.get("lan"):
                ev["lan"] = True
            item["event_info"] = ev
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
    ap.add_argument(
        "--events-list",
        default=None,
        help="arquivo HTML de /events já baixado (validação offline) — default: baixa com cache diário",
    )
    args = ap.parse_args()

    html, origin = fetch_matches_page(fresh=args.fresh, url=args.url)
    print(f"HTML: {origin}", file=sys.stderr)
    matches = parse_page(html)
    if not matches:
        raise SystemExit("Nenhum match com bandeira do Brasil encontrado (markup mudou?).")

    # Índice de campeonatos: falha não derruba o run (só perde event_info)
    events_index: dict[str, dict] = {}
    try:
        if args.events_list:
            events_index = parse_events_index(
                Path(args.events_list).read_text(encoding="utf-8")
            )
            print(f"Events: arquivo:{args.events_list}", file=sys.stderr)
        else:
            events_index, ev_origin = fetch_events_index(fresh=args.fresh)
            print(f"Events: {ev_origin}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - matches continuam publicáveis
        print(f"! indice de eventos indisponivel ({e}); seguindo sem event_info", file=sys.stderr)
    print(f"Eventos no indice: {len(events_index)}", file=sys.stderr)

    data = build_output(matches, events_index)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print_table(data)
    print(f"\nTotal: {data['total_matches']} matches -> {args.out}")


if __name__ == "__main__":
    main()
