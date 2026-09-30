# discord-stream-bot

Mały bot, który wchodzi na jeden kanał głosowy Discorda i gra tam strumień audio (np. stację Icecast z DeadAir) — 24/7, bez komend i bez uprawnień uprzywilejowanych. Cała konfiguracja to zmienne środowiskowe.

Obraz buduje się automatycznie (GitHub Actions) i ląduje w GHCR: `ghcr.io/radzupl/discord-stream-bot:latest`. Repo jest publiczne, więc obraz ciąga się bez logowania.

## Zmienne środowiskowe

| Zmienna | Wymagana | Opis |
|---|---|---|
| `DISCORD_TOKEN` | tak | Token bota z Discord Developer Portal (strona Bot) |
| `VOICE_CHANNEL_ID` | tak | ID kanału głosowego (tryb dewelopera → prawy klik na kanale → Copy Channel ID) |
| `STREAM_URL` | tak | Adres strumienia, np. `http://deadair:80/live.mp3` albo `http://192.168.10.15:8084/live.mp3` |
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

## Uruchomienie: Docker Compose

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

## Uruchomienie: Unraid

W `unraid/discord-stream-bot.xml` jest szablon kontenera. Dzięki niemu zmienne są w formularzu z opisami, token jest maskowany, a kontener ma ikonę. Szablon ma domyślnie sieć `docker_network` i ikonę radia — dostosuj do siebie w formularzu albo w pliku.

Unraid trzyma szablony użytkownika jako pliki `my-<nazwa>.xml` w `/boot/config/plugins/dockerMan/templates-user/`. Wystarczy pobrać tam plik z repo.

### Nowa instalacja

1. Na Unraidzie (Terminal albo SSH) pobierz szablon:

   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-discord-stream-bot.xml \
     https://raw.githubusercontent.com/RadzuPL/discord-stream-bot/main/unraid/discord-stream-bot.xml
   ```

2. Zakładka **Docker** → **Add Container** → z listy **Template** wybierz `my-discord-stream-bot` (sekcja *User templates*).
3. Wpisz trzy pola: token, ID kanału głosowego, adres strumienia. W razie potrzeby zmień sieć (i ewentualnie ustaw stały IP).
4. **Apply**. W logu kontenera po kilku sekundach pojawi się `Joining #<kanał>` i `Starting stream from ...`.

### Podmiana szablonu przy już działającym kontenerze

Jeśli kontener już stoi (np. zrobiony ręcznie), nie musisz go usuwać. Unraid dopasowuje kontener do szablonu po nazwie, a ręcznie zrobiony kontener `discord-stream-bot` ma własny plik `my-discord-stream-bot.xml`:

1. Zrób kopię starego szablonu:

   ```bash
   cp /boot/config/plugins/dockerMan/templates-user/my-discord-stream-bot.xml /boot/config/my-discord-stream-bot.xml.bak
   ```

2. Nadpisz go wersją z repo (ta sama komenda `wget -O ...` co wyżej).
3. **Docker** → kliknij kontener → **Edit**. Sprawdź pola: token, ID kanału, adres strumienia, sieć i IP. Jeśli formularz wczytał puste wartości albo domyślne, uzupełnij je z kopii zapasowej (`.bak`) lub z poprzedniego `docker run`.
4. **Apply** przebuduje kontener z nowymi wartościami.

Gdyby formularz zachowywał się dziwnie: usuń sam kontener (obraz zostaje), przywróć `.bak` albo dodaj kontener od nowa według sekcji *Nowa instalacja*.

### Aktualizacja szablonu

Gdy szablon w repo się zmieni, powtórz `wget -O ...`. Aktualizacje samego obrazu Unraid pokazuje normalnie w zakładce Docker (*update ready*).

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
