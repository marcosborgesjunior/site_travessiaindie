# HLTV — Jogos do Brasil (Travessia Indie)

Documentação das **regras**, da **estrutura do JSON** e da **integração front**.

- **Fonte:** [hltv.org/matches](https://www.hltv.org/matches) + `/events` + páginas dos matches
- **JSON público:** `https://www.travessiaindie.com.br/brazil-matches.json`
- **Fuso de tudo:** `America/Sao_Paulo` (epoch `data-unix` convertido)
- **Atualização:** cron diário às **00:10 BRT** (GitHub Actions → Pages)

---

## 1. Regras de coleta (anti-estresse)

| Regra | Valor | Onde |
| --- | --- | --- |
| Cache | **nenhum** — cada execução busca fresco | — |
| Limite de chamadas | **20 por execução** (corta o restante) | `--max-calls` |
| Ritmo mínimo | **1 request a cada 2 minutos** | `--interval` (segundos) |
| Contador exposto | campo `http_calls` no JSON | — |
| Falha em página de match | não derruba o run (só perde stats daquele match) | — |
| Falha no índice `/events` | não derruba o run (só perde `event_info`) | — |

```
run típico:1 (/matches) + 1 (/events) + ~10 páginas de match = ~12 chamadas
duração: ~12 × 2 min ≈ 25 min  →  JSON publicado ~00:35 BRT
```

O Cloudflare do HLTV bloqueia rajadas — por isso o limite é **inegociável**.
Se aparecer `403`/`Just a moment`, a execução aborta com aviso (não retenta).

---

## 2. Regras de detecção de time brasileiro

Ordem de verificação por time (fonte primária → fallback):

1. **Bandeira no markup** — `img[src$="BR.gif"]` ou `title="Brazil"` (`country-flag-small`)
2. **Fallback por nome** — o HLTV nem sempre renderiza bandeira (comum em matches
   mais distantes). Nome (case-insensitive) em `BR_TEAM_NAMES`:
   `furia, mibr, imperial, pain, legacy, fluxo, red canids, godsent, sharks,
   intz, w7m, case, havan libre, kabum, los héroes, br crowd` + qualquer nome
   terminado em `" br"` (ex.: `Low Cortisol BR`)
3. **Exceção manual** — `BR_EXCLUDED_NAMES = {"bestia"}`: sobrepõe até a
   bandeira exibida pelo HLTV (o site mostra BR errado; Bestia é argentina)

Um match entra no JSON se **qualquer** um dos dois times for BR.

### O que conta como match "com horário"

- Wrapper com `.match-time[data-unix]` → entra em `days[]` com hora em SP
- Wrapper **live** (sem horário) → entra no array `live[]` com `score`
- Sem horário e sem live → fica de fora

---

## 3. Estrutura do JSON

```jsonc
{
  "source": "https://www.hltv.org/matches",
  "scraped_at": "2026-10-30T17:10:31+00:00",   // UTC, quando rodou
  "timezone": "America/Sao_Paulo",
  "total_matches": 10,
  "http_calls": 12,          // chamadas HTTP desta execução (teto20)

  "live": [ /* matches ao vivo AGORA — ver3.2 */ ],

  "days": [                  // agrupado por dia (SP), ordenado
    {
      "date": "2026-10-30",
      "weekday": "sexta",
      "matches": [ /* ver3.1 */ ]
    }
  ]
}
```

###3.1 Objeto de match (em `days[].matches[]`)

```jsonc
{
  "series": {                // presente quando a página do match foi lida
    "format": "md3",         // md3 (bo3) | md5 (bo5) — inferido
    "current_map": "Mirage", // mapa em curso  (null se nenhum)
    "next_map": "Cache",     // próximo mapa    (null se não há)
    "finished_maps": ["Dust2"],
    "maps": [
      {
        "name": "Dust2", "status": "done",
        "score": {"MOUZ":6,"FURIA":13}, "half": "( 6 : 6 ; 0 : 7 )",
        "stats_url": "https://www.hltv.org/stats/matches/mapstatsid/243227/...",
        // só em mapas FINALIZADOS (bloco embutido na página, zero requests):
        "players": [                     // lista PLANA —10 jogadores
          { "nick":"molodoy", "team":"FURIA", "kills":24, "deaths":9,
            "kd_ratio":2.67, "adr":98.9, "kast":89.5, "rating":1.86 },
          ...
        ],
        "mvp": { "nick":"molodoy", "team":"FURIA", "rating":1.86, ... }  // maior rating DO MAPA
      },
      { "name": "Mirage", "status": "live", "score": {"MOUZ":4,"FURIA":8}, "half": "( 4 : 8 ; -:- )" },
      { "name": "Cache",  "status": "upcoming" }
    ]
  },

  "time": "12:00",           // SP; null só em live[] (sem horário no markup)
  "event": "PGL Masters Bucharest 2026",   // headline do wrapper
  "event_id": "8050",        // id em hltv.org/events/<id>/ — cruza com índice
  "format": "bo3",           // formato cru do HLTV (bo1/bo3/bo5)
  "live": false,             // true = ao vivo
  "stars": 5,                // destaque HLTV0–5
  "match_id": "2399757",
  "url": "https://www.hltv.org/matches/2399757/...",
  "teams": [
    { "name": "MOUZ",   "country": null,      "is_brazil": false },
    { "name": "FURIA",  "country": "Brazil",  "is_brazil": true  }
  ],

  "stats": [                 // por time — presente se a página do match carregou
    { "team": "FURIA", "players": [
      { "nick": "molodoy", "kills": 24, "deaths": 9,
        "kd_ratio": 2.67, "adr": 98.9, "kast": 89.5, "rating": 1.86 },
      ...
    ]}
  ],

  "mvp": {                   // MAIOR rating do match (empate → maior kd_ratio)
    "nick": "molodoy", "team": "FURIA",
    "kills": 24, "deaths": 9, "kd_ratio": 2.67,
    "adr": 98.9, "kast": 89.5, "rating": 1.86
  },

  "watch": [ "https://www.twitch.tv/pgl", "..." ],  // streams do jogo

  "event_info": {            // cruzado com /events via event_id
    "name": "PGL Masters Bucharest 2026",
    "url": "https://www.hltv.org/events/8050/...",
    "start": "2026-10-24",   // datas completas do campeonato (SP)
    "end": "2026-10-31",
    "location": "Bucharest, Romania (Romania)",   // quando há
    "prize": "$1,000,000",                        // quando há
    "prize_distribution": { "player_share": "$350,000", "club_share": "$650,000" },
    "teams_count": 8,
    "lan": true
  }
}
```

###3.2 Array `live[]` (ao vivo agora)

Mesmos campos do match, com diferenças:

| Campo | Valor em live |
| --- | --- |
| `time` | `null` (o markup live não traz horário) |
| `live` | `true` |
| `score` | `{ "rounds": {"Nemiga":13,"Vitality":8}, "maps": {"Nemiga":1,"Vitality":0} }` |
| `series`/`stats`/`mvp` | presentes se a página do match foi lida |

`score.rounds` = placar do **mapa atual**; `score.maps` = **série** (maps vencidos).
Vem do SSR do `/matches` — atualiza a cada execução, **não** em tempo real.

---

## 4. Status dos mapas (série)

| `status` | Significado | Marcador na tabela |
| --- | --- | --- |
| `done` | mapa finalizado (placar e ambos os tempos preenchidos) | ✓ |
| `live` | em curso — `half` contém `"-:-"` ou é o último mapa com placar com o match ao vivo | ● |
| `upcoming` | ainda não começou (sem placar) | ○ |

`series.format`: `md3` = melhor de3, `md5` = melhor de5 (inferido de `bo3`/`bo5`
ou da contagem de mapas no veto).

---

## 5. Integração front

### Acesso

```
GET https://www.travessiaindie.com.br/brazil-matches.json
Content-Type: application/json; charset=utf-8
Access-Control-Allow-Origin: *          ← CORS liberado (fetch de qualquer origem)
Cache-Control: max-age=600             ← CDN guarda10 min
```

> **Atenção ao cache do CDN (10 min):** para forçar versão nova na mão,
> use `?cb=1` no final da URL. No código de produção, não é preciso —
> basta buscar de novo em cada atualização do seu front.

### Exemplo mínimo (vanilla JS)

```js
const URL = "https://www.travessiaindie.com.br/brazil-matches.json";

async function carregar() {
  const r = await fetch(URL, { cache: "no-store" });
  const data = await r.json();

  //1) Jogos de hoje
  const hoje = data.days.find(d => d.date === new Date().toISOString().slice(0, 10))
            ?? data.days[0];

  //2) Ao vivo agora
  for (const m of data.live) {
    const placar = Object.entries(m.score?.rounds ?? {})
      .map(([t, v]) => `${t} ${v}`).join(" x ");
    console.log(`AO VIVO: ${placar}`, m.teams.map(t => t.name).join(" x "));
  }

  //3) Agenda do dia com séries e MVP
  for (const m of hoje.matches) {
    const br = m.teams.filter(t => t.is_brazil).map(t => t.name);
    const s = m.series;
    const mapa = s?.current_map ? ` [${s.current_map}]` : "";
    const mvp = m.mvp ? ` MVP: ${m.mvp.nick} (${m.mvp.rating})` : "";
    console.log(`${m.time} ${br.join(" x ")} vs ${m.teams.filter(t => !t.is_brazil).map(t => t.name).join(" x ")}${mapa} — ${m.event}${mvp}`);
  }

  return data;
}
```

### React (mesma lógica)

```jsx
function useJogosBR() {
  const [data, setData] = useState(null);
  useEffect(() => {
    fetch("https://www.travessiaindie.com.br/brazil-matches.json")
      .then(r => r.json())
      .then(setData);
  }, []);
  return data;   // { live, days, total_matches, http_calls, ... }
}
```

### Campos mais úteis para UI

| UI | Campo |
| --- | --- |
| Badge "AO VIVO" | `live === true` ou presença em `data.live` |
| Placar do mapa atual | `score.rounds` / série `score.maps` |
| Progresso da série | `series.maps[]` com `status` (✓ ● ○) |
| Selo "MVP" | `mvp.nick` + `mvp.rating` |
| Tag do campeonato + prêmio | `event` + `event_info.prize` |
| Bandeirinha BR | `teams[].is_brazil` |
| Contador "atualizado há X" | `scraped_at` (UTC ISO) |

---

## 6. Operação (GitHub Actions)

| Workflow | Gatilho | O que faz |
| --- | --- | --- |
| **Jogos BR (HLTV)** | cron `10 3 * * *` (00:10 BRT) + manual | roda `hltv/scraper.py` → commita `brazil-matches.json` → Pages publica |
| **Snapshot HLTV (events)** | manual (botão) | baixa `/events`, página de evento, `/highlights`, match live → `hltv/snapshots/` (diagnóstico de markup) |

- Código do scraper: `hltv/scraper.py` (repositório `site_travessiaindie`)
- Dependências: `hltv/requirements.txt` (`curl_cffi`, `beautifulsoup4`, `tzdata`)
- Logs de cada chamada: `[n/20] GET ...` no stderr do step do workflow
- Diagnóstico de URLs: `hltv/snapshots/STATUS.txt`

### Rodar localmente

```bash
pip install -r hltv/requirements.txt
python hltv/scraper.py --out brazil-matches.json
python hltv/scraper.py --max-calls 10 --interval 60   # mais conservador
```

---

## 7. Limitações conhecidas (honestidade)

1. **Highlights de vídeo** — a página `/highlights` retorna **404** (removida
   no redesign) e a página de match não tem seção de highlights. **Não há
   fonte HTML** para links de melhores momentos.
2. **Tempo real** — o scorebot do HLTV usa websocket; nosso score é um
   snapshot do momento da execução (atualiza a cada run do cron).
3. **Stats por mapa** — os stats de jogador **por mapa finalizado** vêm do
   bloco embutido `div#<mapstatsid>-content` na página do match (zero requests).
   O mapa **ao vivo ainda não tem** esse bloco — aparece quando o mapa termina.
   A página dedicada `/stats/matches/mapstatsid/...` tem WAF próprio (403).
4. **Jogo ao vivo sem horário** — entra só em `live[]`; não recebe `time`
   porque o markup não expõe o início.
5. **Cache do CDN** — o JSON publicado pode estar até10 min atrás do
   `scraped_at` se o consumidor pegar o cache do edge.

---

*Gerado para o projeto Travessia Indie — scraper HLTV.*
