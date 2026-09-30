# discord-stream-bot

Mały bot, który wchodzi na jeden kanał głosowy Discorda i gra tam strumień audio (np. stację Icecast z DeadAir) — 24/7, bez komend i bez uprawnień uprzywilejowanych. Cała konfiguracja to zmienne środowiskowe.

Obraz buduje się automatycznie (GitHub Actions) i ląduje w GHCR: `ghcr.io/radzupl/discord-stream-bot:latest`.

## Zmienne środowiskowe

| Zmienna | Wymagana | Opis |
|---|---|---|
| `DISCORD_TOKEN` | tak | Token bota z Discord Developer Portal (strona Bot) |
| `VOICE_CHANNEL_ID` | tak | ID kanału głosowego (tryb dewelopera → prawy klik na kanale → Copy Channel ID) |
| `STREAM_URL` | tak | Adres strumienia, np. `http://192.168.10.15:8084/live.mp3` |
| `VOLUME` | nie | Mnożnik głośności, domyślnie `1.0` |
| `LOG_LEVEL` | nie | `DEBUG` / `INFO` / `WARNING` / `ERROR`, domyślnie `INFO` |
| `FFMPEG_BEFORE_OPTIONS` | nie | Nadpisuje opcje ffmpeg przed wejściem (domyślnie: auto-reconnect strumienia) |
| `HEALTH_FILE` | nie | Plik znacznika dla healthchecka, domyślnie `/tmp/streambot.healthy` |

Kontener nie wystawia żadnych portów. Musi tylko widzieć `STREAM_URL` (ta sama sieć Dockera albo adres hosta z portem).

## Uprawnienia bota w Discordzie

Scope w OAuth2: `bot` (+ `applications.commands`, jeśli ten sam token używa wtyczka DeadAir).

Uprawnienia do samego streamingu: **View Channels**, **Connect**, **Speak** — suma `3146752`. Jeśli ten sam token obsługuje też tekstową wtyczkę DeadAir, dodaj **Send Messages** i **Read Message History** — suma `3214336`.

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot%20applications.commands&permissions=3214336
```

Bot głosowy nie potrzebuje żadnych Privileged Gateway Intents. Message Content Intent jest potrzebny tylko wtyczce tekstowej, jeśli z niej korzystasz.

## Uruchomienie

```yaml
services:
  discord-stream-bot:
    image: ghcr.io/radzupl/discord-stream-bot:latest
    container_name: discord-stream-bot
    restart: unless-stopped
    environment:
      DISCORD_TOKEN: ${DISCORD_TOKEN:?}
      VOICE_CHANNEL_ID: "123456789012345678"
      STREAM_URL: http://deadair/live.mp3
```

Na Unraidzie: w `unraid/discord-stream-bot.xml` jest szablon (Docker → Add Container → wybierz szablon albo wklej `TemplateURL`), dzięki czemu zmienne są w formularzu, a aktualizacje obrazu widać w UI.

## Jak to działa

- Co 5 s pętla nadzorcza sprawdza: czy bot jest na właściwym kanale i czy gra. Jeśli nie — wraca / łączy się ponownie / uruchamia strumień od nowa.
- Padnięty strumień jest ponawiany z rosnącym opóźnieniem (2, 4, 8 … do 60 s), żeby nie zaśmiecać logów ani nie męczyć serwera.
- Ręczne rozłączenie lub przeniesienie bota na inny kanał kończy się powrotem na kanał docelowy.
- Healthcheck jest zielony tylko wtedy, gdy bot jest połączony z głosem i faktycznie odtwarza dźwięk. Nieosiągalny strumień = `unhealthy`.
- Discord od 1 marca 2026 wymaga szyfrowania E2EE (DAVE) na kanałach głosowych. Obsługuje je discord.py ≥ 2.7 z pakietem `davey`; oba są w `requirements.txt`. Jeśli w logu startowym widzisz `davey MISSING`, głos nie zadziała.
- Kanały typu Stage nie są obsługiwane, tylko zwykłe kanały głosowe.

## Rozwiązywanie problemów

| Objaw | Co sprawdzić |
|---|---|
| `Missing required environment variable` | Brakuje `DISCORD_TOKEN`, `VOICE_CHANNEL_ID` albo `STREAM_URL` |
| `Forbidden` / `Missing Access` przy starcie | Bot nie ma View Channels / Connect na tym kanale |
| Wchodzi na kanał, ale cisza | Uprawnienie Speak; log `davey`; czy `STREAM_URL` jest osiągalny z kontenera |
| `Stream ended after 0s` w kółko | Zły adres lub strumień niedostępny; ustaw `LOG_LEVEL=DEBUG`, żeby zobaczyć ffmpeg |

## Rozwój

Push do `main` buduje i publikuje `latest` oraz tag `sha-<krótki hash>`; tag `v1.2.3` publikuje też `1.2.3`. Raz w tygodniu (poniedziałek) obraz przebudowuje się sam, żeby łapać świeże zależności.

Po pierwszym udanym buildzie: GitHub → Packages → `discord-stream-bot` → Package settings → Change visibility → Public. Bez tego Unraid będzie potrzebował logowania do GHCR przy pobieraniu obrazu.
