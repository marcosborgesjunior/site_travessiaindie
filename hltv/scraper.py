"""Scrapper HLTV — jogos com times do Brasil em TODAY'S MATCHES.

Busca https://www.hltv.org/matches (HTML server-side rendered), filtra os
matches cujo qualquer time tenha a bandeira do Brasil (BR.gif/BR.png) e
organiza os horários em JSON, convertendo o epoch canônico (data-unix) para
America/Sao_Paulo.

Sem cache: cada execução busca dados frescos, protegida por contador de
chamadas (default20/execução) e ritmo mínimo de1 request a cada2 min
(--max-calls / --interval) para não estressar o site.

Por match o JSON traz: horário, campeonato (event_info com prêmio/datas),
score de rounds/maps (lives), stats por jogador (K-D, kd_ratio, ADR, KAST,
rating), MVP (maior rating), watch links e a série de mapas (md3/md5) com
status done/live/upcoming.

Casos especiais do markup HLTV:
- Upcoming: flag do país dentro de .match-team (img.country-flag-small).
- Ao vivo: wrapper sem .match-time e sem flag — entra com live:true; o
  score de rounds vem do SSR do /matches; mapas/stats da página do match.
- Um mesmo match aparece em várias colunas (pinned, calendário, live) com
  completude diferente; as aparições são merged campo-a-campo por match_id.

Uso:
    python scraper.py                 # padrão:20 chamadas,120s de intervalo
    python scraper.py --max-calls 10 --interval 60
    python scraper.py --out x.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from curl_cffi import requests

URL = "https://www.hltv.org/matches"
BASE = "https://www.hltv.org"
SP = ZoneInfo("America/Sao_Paulo")
# Sem cache: cada execução busca fresco. Proteção contra estresse do site:
# contador de chamadas por execução + ritmo de1 request a cada2 min.
MAX_CALLS_DEFAULT = 20
MIN_INTERVAL_DEFAULT = 120  # segundos entre requests
_http_calls = 0
_max_calls = MAX_CALLS_DEFAULT
_min_interval = MIN_INTERVAL_DEFAULT
_last_call_ts = 0.0


def http_calls() -> int:
    return _http_calls
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
    """GET com contador de chamadas e ritmo máximo de1 request / _min_interval.

    - Limite duro de _max_calls por execução (anti-estresse).
    - Espaçamento mínimo entre requests (default120s =1 a cada2 min).
    """
    global _http_calls, _last_call_ts
    if _http_calls >= _max_calls:
        raise RuntimeError(f"limite de {_max_calls} chamadas por execução atingido")
    wait = _min_interval - (time.monotonic() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()
    _http_calls += 1
    print(f"[{_http_calls}/{_max_calls}] GET {url}", file=sys.stderr)
    r = requests.get(url, impersonate="chrome", headers=HEADERS, timeout=30)
    r.raise_for_status()
    if "Just a moment" in r.text[:2000]:
        raise SystemExit(f"Bloqueado pelo Cloudflare em {url}. Tente de novo em instantes.")
    return r.text


def fetch_matches_page(url: str = URL) -> tuple[str, str]:
    """HTML do /matches (sem cache — sempre fresco, sujeito ao rate limit)."""
    return fetch(url), "rede"


EVENTS_URL = f"{BASE}/events"


def fetch_events_index() -> tuple[dict, str]:
    """Índice de campeonatos (/events): id -> nome, datas, local, prêmio.

    O cruzamento com os matches é todo offline (data-event-id dos wrappers).
    """
    return parse_events_index(fetch(EVENTS_URL)), "rede"


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


def parse_match_stats(html: str) -> list[dict]:
    """Stats por jogador da página de um match (div.matchstats).

    Tabelas vêm por time × lado (Both/T/CT); a primeira de cada time é a
    agregada. Colunas: nome, K-D, eK-eD, Swing, ADR, eADR, KAST, eKAST, Rating.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen_teams: set[str] = set()
    for table in soup.select("table"):
        if not table.select_one("td.adr"):
            continue
        head = table.select_one("tr")
        first_cell = head.select_one("td") if head else None
        team = first_cell.get_text(strip=True) if first_cell else None
        if not team or team in seen_teams:
            continue
        seen_teams.add(team)

        players: list[dict] = []
        for tr in table.select("tr")[1:]:
            cells = tr.select("td")
            if len(cells) < 9:
                continue
            nick_el = tr.select_one('a[href^="/player/"]')
            raw = nick_el.get_text(" ", strip=True) if nick_el else cells[0].get_text(strip=True)
            # célula vem como "Dorian 'xertioN' Berman xertioN" → apelido entre aspas
            m_nick = re.search(r"'([^']+)'", raw)
            nick = m_nick.group(1).strip() if m_nick else raw.strip()
            kd = cells[1].get_text(strip=True)
            kills = deaths = None
            if "-" in kd:
                k, d = kd.split("-", 1)
                kills, deaths = int(k), int(d)

            def num(idx: int, pct: bool = False) -> float | None:
                txt = cells[idx].get_text(strip=True).replace("%", "")
                try:
                    return float(txt)
                except ValueError:
                    return None

            adr = num(4)
            kast = num(6, pct=True)
            rating = num(8)
            players.append(
                {
                    "nick": nick,
                    "kills": kills,
                    "deaths": deaths,
                    "kd_ratio": round(kills / deaths, 2)
                    if kills is not None and deaths
                    else None,
                    "adr": adr,
                    "kast": kast,
                    "rating": rating,
                }
            )
        if players:
            out.append({"team": team, "players": players})
    return out


def parse_match_maps(html: str, match_live: bool = False) -> list[dict]:
    """Mapa a mapa da página do match (div.mapholder).

    Status: done (placar completo), live ("-:-" =2º tempo não iniciado, ou
    último mapa com placar quando o match está ao vivo) ou upcoming.
    Inclui placar por time e half-score quando existem.
    """
    soup = BeautifulSoup(html, "html.parser")
    raw: list[dict] = []
    for mh in soup.select("div.mapholder"):
        name_el = mh.select_one(".mapname")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)

        left = mh.select_one(".results-left .results-teamname")
        right = mh.select_one(".results-right .results-teamname")
        ls = mh.select_one(".results-left .results-team-score")
        rs = mh.select_one(".results-right .results-team-score")
        half = mh.select_one(".results-center-half-score")
        half_txt = half.get_text(" ", strip=True) if half else None

        score: dict[str, int] = {}
        if left and right and ls and rs:
            lsv, rsv = ls.get_text(strip=True), rs.get_text(strip=True)
            if lsv.isdigit() and rsv.isdigit():
                score = {
                    left.get_text(strip=True): int(lsv),
                    right.get_text(strip=True): int(rsv),
                }
        raw.append({"name": name, "score": score, "half": half_txt})

    # último mapa com placar = candidato a "em curso" quando o match é live
    last_scored = max(
        (i for i, m in enumerate(raw) if m["score"]), default=None
    )

    maps: list[dict] = []
    for i, m in enumerate(raw):
        if not m["score"]:
            status = "upcoming"
        elif m["half"] and "-:-" in m["half"]:
            status = "live"
        elif match_live and i == last_scored:
            status = "live"
        else:
            status = "done"
        entry: dict = {"name": m["name"], "status": status}
        if m["score"]:
            entry["score"] = m["score"]
        if m["half"]:
            entry["half"] = m["half"]
        maps.append(entry)
    return maps


def pick_mvp(stats: list[dict]) -> dict | None:
    """Melhor jogador da partida: maior rating (empate → maior kd_ratio)."""
    best: tuple | None = None
    best_p: dict | None = None
    for st in stats:
        for p in st["players"]:
            rating = p.get("rating")
            if rating is None:
                continue
            key = (rating, p.get("kd_ratio") or 0.0)
            if best is None or key > best:
                best, best_p = key, {**p, "team": st["team"]}
    return best_p


def parse_watch_links(html: str) -> list[str]:
    """Links externos de transmissão (stream-box) da página do match."""
    soup = BeautifulSoup(html, "html.parser")
    seen: list[str] = []
    for a in soup.select("div.external-stream a[href]"):
        href = str(a.get("href") or "")
        if href.startswith("http") and href not in seen:
            seen.append(href)
    return seen


def fetch_match_page(match_id: str, url: str) -> str | None:
    """Página de um match BR (sem cache, sujeito ao rate limit). None se falhar."""
    try:
        return fetch(url)
    except Exception as e:  # noqa: BLE001 - não derruba o run
        print(f"  ! match page {match_id}: {e}", file=sys.stderr)
        return None


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

    # Lives: rounds do mapa atual e maps vencidos (renderizados no SSR do /matches)
    score = None
    if live:
        sc = wrapper.select("[data-livescore-current-map-score]")
        mp = wrapper.select("[data-livescore-maps-won-for]")
        if sc or mp:
            score = {
                "rounds": [int(x.get_text(strip=True) or 0) for x in sc],
                "maps": [int(x.get_text(strip=True) or 0) for x in mp],
            }

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
        "score": score,
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
            if base.get("score") is None and other.get("score"):
                base["score"] = other["score"]
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

    # Filtra Brasil: datados (com horário) ou ao vivo (sem horário, com flag live)
    br = [
        mt
        for mt in matches
        if any(t["is_brazil"] for t in mt["teams"])
        and (mt["unix_ms"] is not None or mt["live"])
    ]
    return sorted(br, key=lambda x: (x["unix_ms"] is None, x["unix_ms"] or 0))


def build_output(matches: list[dict], events_index: dict[str, dict] | None = None) -> dict:
    events_index = events_index or {}
    days: dict[str, list[dict]] = {}
    live_items: list[dict] = []

    def score_named(mt: dict) -> dict | None:
        """Converte score [por team1/team2] em dict nomeado por time."""
        if not mt.get("score"):
            return None
        names = [t["name"] for t in mt["teams"]]
        out: dict[str, dict] = {}
        for key in ("rounds", "maps"):
            vals = mt["score"].get(key) or []
            out[key] = {
                names[i]: vals[i] for i in range(min(len(names), len(vals)))
            }
        return out or None

    for mt in matches:
        item: dict = {}

        # Cabeçalho: série de mapas com status (md3/md5)
        maps = mt.get("maps") or []
        if maps:
            fmt = (mt.get("format") or "").lower()
            md = {"bo3": "md3", "bo5": "md5"}.get(fmt) or {
                3: "md3",
                5: "md5",
            }.get(len(maps))
            item["series"] = {
                "format": md,
                "current_map": next(
                    (m["name"] for m in maps if m["status"] == "live"), None
                ),
                "next_map": next(
                    (m["name"] for m in maps if m["status"] == "upcoming"), None
                ),
                "finished_maps": [m["name"] for m in maps if m["status"] == "done"],
                "maps": maps,
            }

        item.update(
            {
                "time": None,
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
        )
        sc = score_named(mt)
        if sc:
            item["score"] = sc
        if mt.get("stats"):
            item["stats"] = mt["stats"]
        if mt.get("mvp"):
            item["mvp"] = mt["mvp"]
        if mt.get("watch"):
            item["watch"] = mt["watch"]

        if mt["unix_ms"] is None:
            # ao vivo sem horário de início no markup
            live_items.append(item)
            continue

        dt_sp = datetime.fromtimestamp(mt["unix_ms"] / 1000, tz=SP)
        item["time"] = dt_sp.strftime("%H:%M")

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
        "live": live_items,
        "days": out_days,
    }


def print_table(data: dict) -> None:
    if data.get("live"):
        print("\n== AO VIVO ==")
        for mt in data["live"]:
            vs = " x ".join(t["name"] for t in mt["teams"])
            flags = "".join(" [BR]" if t["is_brazil"] else "" for t in mt["teams"])
            ev = mt["event"] or "-"
            sc = mt.get("score", {})
            rounds = sc.get("rounds", {})
            maps = sc.get("maps", {})
            score_txt = ""
            if rounds:
                r = "-".join(str(v) for v in rounds.values())
                m = " / ".join(f"{k}:{v}" for k, v in maps.items())
                score_txt = f"  [{r} rounds" + (f" | {m}]" if m else "]")
            print(f"  ● {vs}{flags}  — {ev}{score_txt}")
    for day in data["days"]:
        print(f"\n== {day['date']} ({day['weekday']}) ==")
        for mt in day["matches"]:
            vs = " x ".join(t["name"] for t in mt["teams"])
            flags = "".join(" [BR]" if t["is_brazil"] else "" for t in mt["teams"])
            live = " (AO VIVO)" if mt["live"] else ""
            ev = mt["event"] or "-"
            line = f"  {mt['time']}  {vs}{flags}  — {ev} ({mt['format'] or '?'}){live}"
            s = mt.get("series")
            if s:
                parts = []
                for m in s["maps"]:
                    mark = {"done": "✓", "live": "●", "upcoming": "○"}.get(
                        m["status"], "?"
                    )
                    sc = m.get("score")
                    sc_txt = ""
                    if sc:
                        sc_txt = " " + "-".join(str(v) for v in sc.values())
                    parts.append(f"{m['name']}{mark}{sc_txt}")
                line += f"\n      mapas ({s['format'] or '?'}): " + " → ".join(parts)
            if mt.get("mvp"):
                p = mt["mvp"]
                line += f"\n      MVP: {p['nick']} ({p.get('team')}) — rating {p.get('rating')}"
            print(line)


def main() -> None:
    global _max_calls, _min_interval
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Scrapper HLTV: jogos do Brasil")
    ap.add_argument("--out", default="brazil-matches.json", help="arquivo JSON de saída")
    ap.add_argument(
        "--max-calls",
        type=int,
        default=MAX_CALLS_DEFAULT,
        help=f"limite de requests HTTP por execução (default {MAX_CALLS_DEFAULT})",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=MIN_INTERVAL_DEFAULT,
        help=f"segundos mínimos entre requests (default {MIN_INTERVAL_DEFAULT} =1 a cada2 min)",
    )
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
        help="arquivo HTML de /events já baixado (validação offline) — default: baixa via rede",
    )
    args = ap.parse_args()
    _max_calls = max(1, args.max_calls)
    _min_interval = max(0, args.interval)

    html, origin = fetch_matches_page(url=args.url)
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
            events_index, ev_origin = fetch_events_index()
            print(f"Events: {ev_origin}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - matches continuam publicáveis
        print(f"! indice de eventos indisponivel ({e}); seguindo sem event_info", file=sys.stderr)
    print(f"Eventos no indice: {len(events_index)}", file=sys.stderr)

    # Stats, mapas, MVP e links de transmissão (páginas dos matches BR;
    # falha ou limite de chamadas não derrubam o run)
    print(f"Paginas de match: {len(matches)} ...", file=sys.stderr)
    for mt in matches:
        if http_calls() >= _max_calls:
            print(
                f"  ! limite de {_max_calls} chamadas atingido; "
                "pulando paginas restantes",
                file=sys.stderr,
            )
            break
        page = fetch_match_page(mt["match_id"], mt["url"])
        if not page:
            continue
        stats = parse_match_stats(page)
        if stats:
            names = {t["name"].casefold() for t in mt["teams"]}
            picked = [s for s in stats if s["team"].casefold() in names]
            mt["stats"] = picked or stats
            mt["mvp"] = pick_mvp(mt["stats"])
        maps = parse_match_maps(page, match_live=mt["live"])
        if maps:
            mt["maps"] = maps
        watch = parse_watch_links(page)
        if watch:
            mt["watch"] = watch

    data = build_output(matches, events_index)
    data["http_calls"] = http_calls()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print_table(data)
    print(
        f"\nTotal: {data['total_matches']} matches -> {args.out} "
        f"| chamadas HTTP: {http_calls()}/{_max_calls}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
